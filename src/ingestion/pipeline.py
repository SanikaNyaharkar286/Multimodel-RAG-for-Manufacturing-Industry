"""Coordinate PDF ingestion stages and preserve their provenance."""

import json
import logging
import re
from pathlib import Path
from typing import Any

from .file_naming import document_output_directory, safe_filename_stem
from .pdf_extractor import extract_pdf
from .table_extractor import extract_tables


REFERENCE_PATTERN = re.compile(
    r"\b(?:figure|fig\.?|table)\s+(\d+(?:[.-]\d+)*)\b",
    re.IGNORECASE,
)


def _tokens(text: str) -> list[str]:
    """Normalize text into comparable words for conservative overlap checks."""
    return re.findall(r"[a-z0-9]+", text.casefold())


def _bbox(element: dict[str, Any]) -> list[float] | None:
    """Read an element bounding box without assuming it was extracted."""
    value = element.get("bbox")
    if not value or len(value) < 4:
        return None
    return [float(coordinate) for coordinate in value[:4]]


def _vertical_gap(first: list[float], second: list[float]) -> float:
    """Return vertical separation; overlapping boxes have a zero gap."""
    if first[3] < second[1]:
        return second[1] - first[3]
    if second[3] < first[1]:
        return first[1] - second[3]
    return 0.0


def _horizontal_overlap(first: list[float], second: list[float]) -> float:
    """Measure horizontal alignment from zero (none) to one (fuller overlap)."""
    intersection = max(0.0, min(first[2], second[2]) - max(first[0], second[0]))
    narrower_width = min(first[2] - first[0], second[2] - second[0])
    return intersection / narrower_width if narrower_width > 0 else 0.0


def _caption_reference(text: str) -> tuple[str, str] | None:
    """Return a normalized (figure/table, number) key for labeled captions."""
    match = REFERENCE_PATTERN.match(text)
    if not match:
        return None
    kind_match = re.match(r"\s*(figure|fig\.?|table)", text, re.IGNORECASE)
    if not kind_match:
        return None
    kind = "figure" if kind_match.group(1).casefold().startswith("fig") else "table"
    return kind, match.group(1).replace("-", ".")


def match_table_text_blocks(
    pages: list[dict[str, Any]],
    tables: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Flag possible duplicated page text while preserving both source forms.

    A high fraction of a text block's words must also occur in the table before
    it is considered a duplicate candidate. This is evidence for later RAG
    deduplication, not permission to delete either extracted representation.
    """
    relationships = []
    blocks_by_page = {
        page["page_number"]: page.get("text_blocks", [])
        for page in pages
    }

    for table in tables:
        table_tokens = set(
            _tokens(" ".join(
                str(cell or "")
                for row in table.get("raw_rows", [])
                for cell in row
            ))
        )
        matches = []
        for block in blocks_by_page.get(table["page_number"], []):
            block_tokens = _tokens(block.get("text", ""))
            if len(block_tokens) < 4 or not table_tokens:
                continue
            overlap = sum(token in table_tokens for token in block_tokens)
            ratio = overlap / len(block_tokens)
            if ratio >= 0.8:
                matches.append({
                    "text_block_id": block["block_id"],
                    "overlap_ratio": round(ratio, 3),
                })
                relationships.append({
                    "relationship_id": (
                        f"{table['table_id']}-duplicate-{block['block_id']}"
                    ),
                    "source_id": block["block_id"],
                    "target_id": table["table_id"],
                    "relationship": "possible_duplicate_of_table",
                    "confidence": round(ratio, 3),
                    "evidence": {
                        "method": "normalized_token_overlap",
                        "block_token_coverage": round(ratio, 3),
                    },
                })

        table["duplicate_detection"] = {
            "method": "normalized_token_overlap",
            "possible_duplicate_in_text": bool(matches),
            "matched_text_blocks": matches,
            "content_removed": False,
        }

    return relationships


def build_relationships(
    pages: list[dict[str, Any]],
    tables: list[dict[str, Any]],
    images: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Link sections, captions, tables, figures, and explicit references.

    Geometric proximity is recorded as low-confidence evidence; only an
    explicit textual figure/table reference receives high confidence.
    """
    relationships = match_table_text_blocks(pages, tables)
    tables_by_page: dict[int, list[dict[str, Any]]] = {}
    images_by_page: dict[int, list[dict[str, Any]]] = {}
    for table in tables:
        tables_by_page.setdefault(table["page_number"], []).append(table)
    for image in images:
        images_by_page.setdefault(image["page_number"], []).append(image)

    caption_targets: dict[tuple[str, str], list[str]] = {}
    active_section: dict[str, Any] | None = None

    for page in pages:
        page_number = page["page_number"]
        blocks = page.get("text_blocks", [])
        section_headings = sorted(
            (
                block for block in blocks
                if block.get("kind") == "section_heading"
            ),
            key=lambda block: (block.get("bbox") or [0, 0])[1],
        )

        # Reading order and page position establish section context without
        # claiming that every nearby paragraph discusses the heading's topic.
        elements = [
            ("text_block", block) for block in blocks
        ] + [
            ("table", table)
            for table in tables_by_page.get(page_number, [])
        ]
        for element_type, element in elements:
            element_box = _bbox(element)
            if not element_box:
                continue
            prior_headings = [
                heading for heading in section_headings
                if _bbox(heading) and _bbox(heading)[1] <= element_box[1]
            ]
            if prior_headings:
                heading = max(prior_headings, key=lambda item: _bbox(item)[1])
                element["section_context"] = heading.get("text")
                if heading["block_id"] != element.get("block_id"):
                    target_id = (
                        element.get("table_id")
                        if element_type == "table"
                        else element.get("block_id")
                    )
                    relationships.append({
                        "relationship_id": (
                            f"{heading['block_id']}-section-{target_id}"
                        ),
                        "source_id": heading["block_id"],
                        "target_id": target_id,
                        "relationship": "follows_section_heading",
                        "confidence": 0.7,
                        "evidence": {
                            "same_page": True,
                            "preceding_heading_position": heading["bbox"],
                            "target_bbox": element.get("bbox"),
                        },
                    })
            elif active_section:
                element["section_context"] = active_section["text"]
                target_id = (
                    element.get("table_id")
                    if element_type == "table"
                    else element.get("block_id")
                )
                relationships.append({
                    "relationship_id": (
                        f"{active_section['block_id']}-continued-section-"
                        f"{target_id}"
                    ),
                    "source_id": active_section["block_id"],
                    "target_id": target_id,
                    "relationship": "continues_section",
                    "confidence": 0.6,
                    "evidence": {
                        "heading_page": active_section["page_number"],
                        "target_page": page_number,
                        "no_new_heading_before_target": True,
                    },
                })

        # A labeled caption is associated with its nearest image/table only
        # when geometry gives a plausible candidate on the same page.
        for block in blocks:
            if block.get("kind") != "caption":
                continue
            reference = _caption_reference(block.get("text", ""))
            if not reference:
                continue
            kind, number = reference
            candidate_targets = (
                images_by_page.get(page_number, [])
                if kind == "figure"
                else tables_by_page.get(page_number, [])
            )
            block_box = _bbox(block)
            candidates = []
            if block_box:
                for target in candidate_targets:
                    boxes = (
                        target.get("bboxes", [])
                        if kind == "figure"
                        else [target.get("bbox")]
                    )
                    for target_box in boxes:
                        if not target_box:
                            continue
                        normalized_box = [
                            float(value) for value in target_box[:4]
                        ]
                        gap = _vertical_gap(block_box, normalized_box)
                        alignment = _horizontal_overlap(block_box, normalized_box)
                        if gap <= 120 and alignment > 0:
                            candidates.append((
                                gap,
                                -alignment,
                                target,
                                normalized_box,
                            ))
            if candidates:
                gap, negative_alignment, target, target_box = min(
                    candidates,
                    key=lambda item: (item[0], item[1]),
                )
                target_id = (
                    target["image_id"] if kind == "figure"
                    else target["table_id"]
                )
                relationships.append({
                    "relationship_id": (
                        f"{block['block_id']}-caption-{target_id}"
                    ),
                    "source_id": block["block_id"],
                    "target_id": target_id,
                    "relationship": "caption_for",
                    "confidence": 0.75,
                    "evidence": {
                        "caption_reference": f"{kind} {number}",
                        "same_page": True,
                        "vertical_gap_points": round(gap, 2),
                        "horizontal_alignment": round(-negative_alignment, 3),
                    },
                })
                caption_targets.setdefault(reference, []).append(target_id)

        # Nearby text is only weak contextual evidence. Keep the link distinct
        # from a caption or explicit cross-reference so retrieval can down-rank it.
        for image in images_by_page.get(page_number, []):
            image_boxes = image.get("bboxes", [])
            candidate_blocks = []
            for block in blocks:
                if block.get("kind") == "caption":
                    continue
                block_box = _bbox(block)
                if not block_box:
                    continue
                for image_box in image_boxes:
                    image_box = [float(value) for value in image_box[:4]]
                    gap = _vertical_gap(block_box, image_box)
                    alignment = _horizontal_overlap(block_box, image_box)
                    if gap <= 80 and alignment > 0:
                        candidate_blocks.append(
                            (gap, -alignment, block, image_box)
                        )
            if candidate_blocks:
                gap, negative_alignment, block, image_box = min(
                    candidate_blocks,
                    key=lambda item: (item[0], item[1]),
                )
                relationships.append({
                    "relationship_id": (
                        f"{block['block_id']}-nearby-{image['image_id']}"
                    ),
                    "source_id": block["block_id"],
                    "target_id": image["image_id"],
                    "relationship": "nearby_context",
                    "confidence": 0.55,
                    "evidence": {
                        "same_page": True,
                        "vertical_gap_points": round(gap, 2),
                        "horizontal_alignment": round(-negative_alignment, 3),
                        "image_bbox": image_box,
                    },
                })

        if section_headings:
            last_heading = section_headings[-1]
            active_section = {
                "block_id": last_heading["block_id"],
                "text": last_heading["text"],
                "page_number": page_number,
            }

    # Resolve references after captions from every page have been collected;
    # manuals often refer to a figure on a different page.
    for page in pages:
        for block in page.get("text_blocks", []):
            if block.get("kind") == "caption":
                continue
            for match in REFERENCE_PATTERN.finditer(block.get("text", "")):
                reference_text = match.group(0)
                kind_match = re.match(
                    r"(figure|fig\.?|table)",
                    reference_text,
                    re.IGNORECASE,
                )
                if not kind_match:
                    continue
                kind = (
                    "figure"
                    if kind_match.group(1).casefold().startswith("fig")
                    else "table"
                )
                number = match.group(1).replace("-", ".")
                targets = caption_targets.get((kind, number), [])
                if targets:
                    for target_id in targets:
                        relationships.append({
                            "relationship_id": (
                                f"{block['block_id']}-reference-{target_id}"
                            ),
                            "source_id": block["block_id"],
                            "target_id": target_id,
                            "relationship": "explicit_reference",
                            "confidence": 0.98,
                            "evidence": {
                                "matched_text": reference_text,
                                "same_document": True,
                            },
                        })
                else:
                    relationships.append({
                        "relationship_id": (
                            f"{block['block_id']}-unresolved-{kind}-{number}"
                        ),
                        "source_id": block["block_id"],
                        "target_id": None,
                        "relationship": "unresolved_explicit_reference",
                        "confidence": 0.45,
                        "evidence": {
                            "matched_text": reference_text,
                            "reference_key": f"{kind} {number}",
                            "reason": (
                                "No matching labeled caption with a nearby "
                                "image or table was found in this document."
                            ),
                        },
                    })

    return relationships


def build_rag_units(
    pages: list[dict[str, Any]],
    tables: list[dict[str, Any]],
    images: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    document_id: str,
) -> list[dict[str, Any]]:
    """Prepare page-addressable text/table/image units without losing lineage."""
    units = []
    duplicate_blocks: dict[str, list[str]] = {}
    image_captions: dict[str, list[str]] = {}
    image_context: dict[str, list[str]] = {}

    for relationship in relationships:
        if relationship["relationship"] == "possible_duplicate_of_table":
            duplicate_blocks.setdefault(
                relationship["source_id"], []
            ).append(relationship["target_id"])
        elif relationship["relationship"] == "caption_for":
            image_captions.setdefault(
                relationship["target_id"], []
            ).append(relationship["source_id"])

    block_by_id = {
        block["block_id"]: block
        for page in pages
        for block in page.get("text_blocks", [])
    }
    for relationship in relationships:
        if relationship["relationship"] == "caption_for":
            continue
        if relationship["target_id"] not in {
            image["image_id"] for image in images
        }:
            continue
        if relationship["source_id"] in block_by_id:
            image_context.setdefault(
                relationship["target_id"], []
            ).append(relationship["source_id"])

    for page in pages:
        for block in page.get("text_blocks", []):
            block["possible_duplicate_table_ids"] = duplicate_blocks.get(
                block["block_id"], []
            )
            block["included_in_rag_units"] = True
            duplicate_table_ids = duplicate_blocks.get(block["block_id"], [])
            if duplicate_table_ids:
                best_overlap = max(
                    relationship["confidence"]
                    for relationship in relationships
                    if relationship["source_id"] == block["block_id"]
                    and relationship["relationship"]
                    == "possible_duplicate_of_table"
                )
                # Only suppress near-exact text repeats. Partial overlaps are
                # retained because the block may also contain useful context.
                if best_overlap >= 0.95 and len(_tokens(block["text"])) >= 8:
                    block["included_in_rag_units"] = False
                    block["rag_exclusion_reason"] = (
                        "near_exact_table_text_duplicate"
                    )
                    continue
            if block.get("kind") == "caption" and any(
                block["block_id"] in caption_ids
                for caption_ids in image_captions.values()
            ):
                # Caption remains a separate extraction element; its retrieval
                # text is joined to the image unit to avoid indexing twice.
                continue

            units.append({
                "unit_id": f"{block['block_id']}-unit",
                "kind": "text",
                "text": block["text"],
                "source_element_id": block["block_id"],
                "document_id": document_id,
                "page_number": page["page_number"],
                "bbox": block.get("bbox"),
                "section_context": block.get("section_context"),
                "related_table_ids": duplicate_table_ids,
            })

    for table in tables:
        for row_index, row in enumerate(table.get("rows", [])):
            if table.get("header_source") == "unknown_header":
                row_text = " | ".join(str(value) for value in row.values()
                                      if value is not None)
            else:
                row_text = "; ".join(
                    f"{header}: {value}"
                    for header, value in row.items()
                    if value is not None
                )
            context = [
                value for value in (
                    table.get("section_context"),
                    table.get("title"),
                )
                if value
            ]
            text = "\n".join(context + [row_text]) if context else row_text
            units.append({
                "unit_id": f"{table['table_id']}-row-{row_index + 1}",
                "kind": "table_row",
                "text": text,
                "source_element_id": table["table_id"],
                "document_id": table["document_id"],
                "page_number": table["page_number"],
                "bbox": table.get("bbox"),
                "row_index": row_index,
                "headers": table.get("headers"),
                "title": table.get("title"),
                "section_context": table.get("section_context"),
                "validation_status": table.get("validation", {}).get("status"),
            })

    for image in images:
        related_text = []
        for block_id in (
            image_captions.get(image["image_id"], [])
            + image_context.get(image["image_id"], [])
        ):
            text = block_by_id[block_id]["text"]
            if text not in related_text:
                related_text.append(text)
        units.append({
            "unit_id": f"{image['image_id']}-unit",
            "kind": "image",
            "text": "\n".join(related_text) if related_text else None,
            "source_element_id": image["image_id"],
            "document_id": document_id,
            "page_number": image["page_number"],
            "bbox": image.get("bbox"),
            "image_path": image["path"],
            "image_sha256": image["sha256"],
            "context_evidence": (
                "caption_or_nearby_text"
                if related_text else "no_linked_text_found"
            ),
        })

    return units


def run_document_pipeline(
    pdf_path: Path,
    raw_root: Path,
    output_root: Path,
    machine_name: str,
    logger: logging.Logger | None = None,
) -> dict[str, Any]:
    """Run text, table, image, relationship, and validation stages for a PDF."""
    pdf_path = Path(pdf_path)
    raw_root = Path(raw_root)
    output_root = Path(output_root)
    relative_source_path = pdf_path.relative_to(raw_root).as_posix()
    output_stem = safe_filename_stem(pdf_path.name)
    document_output = document_output_directory(
        output_root,
        machine_name,
        relative_source_path,
    )
    document_output.mkdir(parents=True, exist_ok=True)

    extraction = extract_pdf(
        pdf_path=pdf_path,
        output_dir=output_root,
        machine_name=machine_name,
        relative_source_path=relative_source_path,
        document_output_dir=document_output,
        metadata_filename=f"{output_stem}.extraction.json",
    )
    table_result = extract_tables(
        pdf_path=pdf_path,
        output_dir=document_output,
        output_filename=f"{output_stem}.tables.json",
    )
    tables = table_result["tables"]

    relationships = build_relationships(
        pages=extraction["pages"],
        tables=tables,
        images=extraction["images"],
    )
    for table in tables:
        nearby_section = table.get("section_context")
        if nearby_section and not table.get("section_heading"):
            table["section_context_source"] = "preceding_layout_heading"
        elif table.get("section_heading"):
            table["section_context_source"] = table.get(
                "section_heading_source",
                "nearest_text_block_above_table",
            )
        else:
            table["section_context_source"] = "unknown"

    # Keep the standalone table artifact in sync with relationships and
    # section context added by the coordinated document-level pipeline.
    table_result["tables"] = tables
    table_result["table_count"] = len(tables)
    (document_output / f"{output_stem}.tables.json").write_text(
        json.dumps(table_result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    rag_units = build_rag_units(
        pages=extraction["pages"],
        tables=tables,
        images=extraction["images"],
        relationships=relationships,
        document_id=extraction["document_id"],
    )
    image_only_pages = [
        page["page_number"]
        for page in extraction["pages"]
        if "possible_scanned_page_ocr_needed" in page.get("warnings", [])
    ]
    validation = {
        "page_count": extraction["page_count"],
        "pages_with_text": sum(
            bool(page.get("text_blocks")) for page in extraction["pages"]
        ),
        "image_only_pages": image_only_pages,
        "table_count": len(tables),
        "suspicious_tables": sum(
            table.get("validation", {}).get("status") == "suspicious"
            for table in tables
        ),
        "uncertain_tables": sum(
            table.get("validation", {}).get("status") == "uncertain"
            for table in tables
        ),
        "possible_duplicate_text_blocks": sum(
            relationship["relationship"] == "possible_duplicate_of_table"
            for relationship in relationships
        ),
        "images_without_text_context": sum(
            not any(
                relationship["target_id"] == image["image_id"]
                and relationship["relationship"] in {
                    "caption_for",
                    "nearby_context",
                }
                for relationship in relationships
            )
            for image in extraction["images"]
        ),
        "extraction_warnings": extraction.get("warnings", []),
    }
    result = {
        "schema_version": 1,
        "document_id": extraction["document_id"],
        "machine_name": machine_name,
        "relative_source_path": relative_source_path,
        "source_filename": extraction["source_filename"],
        "source_sha256": extraction["source_sha256"],
        "page_count": extraction["page_count"],
        "extraction_file": f"{output_stem}.extraction.json",
        "tables_file": f"{output_stem}.tables.json",
        "image_count": len(extraction["images"]),
        "tables": tables,
        "relationships": relationships,
        "validation": validation,
        "rag_units": rag_units,
    }
    document_output.mkdir(parents=True, exist_ok=True)
    result["output_directory"] = document_output.relative_to(
        output_root
    ).as_posix()
    result["output_files"] = {
        "document_json": f"{output_stem}.json",
        "extraction_json": f"{output_stem}.extraction.json",
        "tables_json": f"{output_stem}.tables.json",
        "log": f"{output_stem}.log",
    }
    document_json_path = document_output / f"{output_stem}.json"

    if logger:
        logger.info(
            "Completed %s: pages=%d tables=%d images=%d relationships=%d "
            "rag_units=%d",
            pdf_path.name,
            result["page_count"],
            len(tables),
            result["image_count"],
            len(relationships),
            len(rag_units),
        )
        for warning in validation["extraction_warnings"]:
            logger.warning(
                "Extraction warning page=%s: %s",
                warning.get("page_number"),
                warning.get("warning"),
            )
        for table in tables:
            for warning in table.get("validation", {}).get("warnings", []):
                logger.warning(
                    "Table warning page=%s table=%s code=%s: %s",
                    table.get("page_number"),
                    table.get("table_index_on_page"),
                    warning.get("code"),
                    warning.get("message"),
                )

    document_json_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return result
