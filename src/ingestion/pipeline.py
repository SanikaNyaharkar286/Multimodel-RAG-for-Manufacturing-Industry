"""Coordinate PDF ingestion stages and preserve their provenance."""

import json
import logging
import math
import re
from pathlib import Path
from typing import Any

from .file_naming import document_output_directory, safe_filename_stem
from .pdf_extractor import element_type_for_text_kind, extract_pdf
from .table_extractor import extract_tables, reconstruct_logical_tables


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
    if not _valid_bbox(value) or value is None:
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


def _valid_bbox(value: Any) -> bool:
    """Accept only finite, ordered PDF rectangles; absent boxes are allowed."""
    if value is None:
        return True
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return False
    try:
        x0, y0, x1, y1 = (float(coordinate) for coordinate in value)
    except (TypeError, ValueError):
        return False
    return (
        all(math.isfinite(coordinate) for coordinate in (x0, y0, x1, y1))
        and x0 <= x1
        and y0 <= y1
    )


def build_elements(
    extraction: dict[str, Any],
    tables: list[dict[str, Any]],
    extraction_filename: str,
    tables_filename: str,
) -> list[dict[str, Any]]:
    """Build a shared element index that points to, rather than copies, content."""
    elements = []
    document_id = extraction.get("document_id")
    source_filename = extraction.get("source_filename")

    for page in extraction.get("pages", []):
        page_index = page.get(
            "page_index",
            page.get("page_number", 1) - 1,
        )
        for block_index, block in enumerate(page.get("text_blocks", [])):
            element_id = block["block_id"]
            elements.append({
                "element_id": element_id,
                "element_type": block.get(
                    "element_type",
                    element_type_for_text_kind(block.get("kind", "paragraph")),
                ),
                "document_id": block.get("document_id", document_id),
                "source_filename": block.get("source_filename", source_filename),
                "page_index": block.get("page_index", page_index),
                "page_number": page.get("page_number"),
                "printed_page_number": block.get("printed_page_number"),
                "bbox": block.get("bbox"),
                "content_ref": {
                    "artifact": extraction_filename,
                    "page_index": page_index,
                    "block_index": block_index,
                    "element_id": element_id,
                },
                "extraction_warnings": block.get("extraction_warnings", []),
            })

        for link in page.get("links", []):
            link_id = link["link_id"]
            elements.append({
                "element_id": link_id,
                "element_type": "link",
                "document_id": page.get("document_id", document_id),
                "source_filename": page.get("source_filename", source_filename),
                "page_index": page_index,
                "page_number": page.get("page_number"),
                "printed_page_number": page.get("printed_page_number"),
                "bbox": link.get("source_bbox"),
                "target_uri": link.get("target_uri"),
                "target_page_number": link.get("target_page_number"),
                "target_named": link.get("target_named"),
                "content_ref": {
                    "artifact": extraction_filename,
                    "page_index": page_index,
                    "link_id": link_id,
                    "target_uri": link.get("target_uri"),
                },
                "extraction_warnings": [],
            })

    for table in tables:
        elements.append({
            "element_id": table["table_id"],
            "element_type": "table",
            "document_id": table.get("document_id", document_id),
            "source_filename": table.get("source_filename", source_filename),
            "page_index": table.get(
                "page_index",
                table.get("page_number", 1) - 1,
            ),
            "page_number": table.get("page_number"),
            "printed_page_number": table.get("printed_page_number"),
            "bbox": table.get("bbox"),
            "content_ref": {
                "artifact": tables_filename,
                "table_id": table["table_id"],
            },
            "extraction_warnings": table.get("extraction_warnings", []),
        })

    for image in extraction.get("images", []):
        elements.append({
            "element_id": image["image_id"],
            "element_type": "image",
            "document_id": image.get("document_id", document_id),
            "source_filename": image.get("source_filename", source_filename),
            "page_index": image.get(
                "page_index",
                image.get("page_number", 1) - 1,
            ),
            "page_number": image.get("page_number"),
            "printed_page_number": image.get("printed_page_number"),
            "bbox": image.get("bbox"),
            "asset_id": image.get("asset_id"),
            "content_ref": {
                "artifact": extraction_filename,
                "image_id": image["image_id"],
                "asset_id": image.get("asset_id"),
                "path": image.get("path"),
            },
            "extraction_warnings": image.get("extraction_warnings", []),
        })

    return elements


def validate_document(
    extraction: dict[str, Any],
    tables: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    rag_units: list[dict[str, Any]],
    elements: list[dict[str, Any]],
    document_output: Path,
    extraction_filename: str,
    tables_filename: str,
) -> dict[str, Any]:
    """Check cross-file provenance and references without discarding content."""
    issues: list[dict[str, Any]] = []
    issue_keys: set[tuple[str, str, str]] = set()

    def add_issue(code: str, message: str, element_id: str | None = None) -> None:
        issue_key = (code, message, element_id or "")
        if issue_key in issue_keys:
            return
        issue_keys.add(issue_key)
        issue = {
            "code": code,
            "severity": "warning",
            "message": message,
        }
        if element_id:
            issue["element_id"] = element_id
        issues.append(issue)

    document_id = extraction.get("document_id")
    page_count = extraction.get("page_count")
    element_ids = {element["element_id"] for element in elements}
    images_by_id = {
        image["image_id"]: image
        for image in extraction.get("images", [])
    }
    assets = {
        asset.get("asset_id"): asset
        for asset in extraction.get("image_assets", [])
        if asset.get("asset_id")
    }
    # Older extraction files did not separate image assets and occurrences.
    for image in extraction.get("images", []):
        if image.get("asset_id") and image["asset_id"] not in assets:
            assets[image["asset_id"]] = image

    for element in elements:
        if not element.get("document_id") or not element.get("source_filename"):
            add_issue(
                "missing_element_provenance",
                "Element is missing its document ID or source filename.",
                element["element_id"],
            )
        page_index = element.get("page_index")
        if not isinstance(page_index, int) or page_index < 0:
            add_issue(
                "missing_page_provenance",
                "Element has no valid zero-based PDF page index.",
                element["element_id"],
            )
        elif isinstance(page_count, int) and page_index >= page_count:
            add_issue(
                "page_index_out_of_range",
                "Element page index falls outside the source PDF page count.",
                element["element_id"],
            )
        page_number = element.get("page_number")
        if (
            isinstance(page_index, int)
            and isinstance(page_number, int)
            and page_number != page_index + 1
        ):
            add_issue(
                "page_number_index_mismatch",
                "One-based page number does not match the zero-based page index.",
                element["element_id"],
            )
        if not _valid_bbox(element.get("bbox")):
            add_issue(
                "invalid_bbox",
                "Element bounding box is malformed or has reversed coordinates.",
                element["element_id"],
            )

    for table in tables:
        headers = table.get("headers")
        if not isinstance(headers, list) or not headers:
            add_issue(
                "missing_table_headers",
                "Table has no normalized header list; raw rows are retained.",
                table.get("table_id"),
            )
        elif table.get("header_source") == "unknown_header":
            add_issue(
                "uncertain_table_headers",
                "Table headers are placeholders because the structure was uncertain.",
                table.get("table_id"),
            )
        raw_rows = table.get("raw_rows", [])
        row_lengths = {len(row) for row in raw_rows}
        if len(row_lengths) > 1:
            add_issue(
                "inconsistent_table_row_lengths",
                "Raw table rows have inconsistent cell counts.",
                table.get("table_id"),
            )
        if isinstance(headers, list):
            expected_headers = set(headers)
            for row in table.get("rows", []):
                if set(row) != expected_headers:
                    add_issue(
                        "normalized_table_row_misaligned",
                        "A normalized row does not contain exactly the table's "
                        "normalized header keys.",
                        table.get("table_id"),
                    )
                    break
        for warning in table.get("validation", {}).get("warnings", []):
            add_issue(
                f"table_{warning.get('code', 'warning')}",
                warning.get("message", "Table validation reported a warning."),
                table.get("table_id"),
            )

    for relationship in relationships:
        source_id = relationship.get("source_id")
        target_id = relationship.get("target_id")
        evidence = relationship.get("evidence")
        if source_id not in element_ids:
            add_issue(
                "relationship_source_missing",
                "Relationship source does not resolve to a document element.",
                source_id,
            )
        if target_id is not None and target_id not in element_ids:
            add_issue(
                "relationship_target_missing",
                "Relationship target does not resolve to a document element.",
                source_id,
            )
        if not isinstance(evidence, dict) or not evidence:
            add_issue(
                "relationship_evidence_missing",
                "Relationship has no supporting evidence record.",
                source_id,
            )
        if relationship.get("relation_status") not in {
            "confirmed",
            "candidate",
            "unresolved",
        }:
            add_issue(
                "relationship_status_invalid",
                "Relationship certainty must be confirmed, candidate, or unresolved.",
                source_id,
            )

    for image_id, image in images_by_id.items():
        asset_id = image.get("asset_id")
        asset = assets.get(asset_id)
        if not asset:
            add_issue(
                "image_asset_reference_missing",
                "Image occurrence does not point to a known image asset.",
                image_id,
            )
            continue
        relative_path = asset.get("path") or image.get("path")
        if not relative_path:
            add_issue(
                "image_asset_path_missing",
                "Image asset has no output path.",
                image_id,
            )
            continue
        asset_path = (document_output / relative_path).resolve()
        try:
            asset_path.relative_to(document_output.resolve())
        except ValueError:
            add_issue(
                "image_asset_path_invalid",
                "Image asset path resolves outside its document output folder.",
                image_id,
            )
            continue
        if not asset_path.is_file():
            add_issue(
                "image_asset_file_missing",
                f"Image asset file was not found: {relative_path}",
                image_id,
            )
        asset_hash = asset.get("sha256")
        if asset_hash and image.get("sha256") != asset_hash:
            add_issue(
                "image_asset_hash_mismatch",
                "Image occurrence and image asset hashes do not match.",
                image_id,
            )
        for bbox in image.get("bboxes", []):
            if not _valid_bbox(bbox):
                add_issue(
                    "invalid_image_occurrence_bbox",
                    "Image occurrence contains an invalid placement bounding box.",
                    image_id,
                )

    for artifact_filename in (extraction_filename, tables_filename):
        if not (document_output / artifact_filename).is_file():
            add_issue(
                "output_file_missing",
                f"Expected output file was not written: {artifact_filename}",
            )

    for unit in rag_units:
        source_id = unit.get("source_element_id")
        if source_id not in element_ids:
            add_issue(
                "rag_unit_source_missing",
                "Retrieval unit does not resolve to a source element.",
                source_id,
            )
        if unit.get("document_id") != document_id:
            add_issue(
                "rag_unit_document_provenance_missing",
                "Retrieval unit has missing or mismatched document provenance.",
                source_id,
            )
        if not unit.get("source_filename"):
            add_issue(
                "rag_unit_source_filename_missing",
                "Retrieval unit has no source filename.",
                source_id,
            )
        page_index = unit.get("page_index")
        if not isinstance(page_index, int) or page_index < 0:
            add_issue(
                "rag_unit_page_provenance_missing",
                "Retrieval unit has no valid zero-based PDF page index.",
                source_id,
            )
        elif isinstance(page_count, int) and page_index >= page_count:
            add_issue(
                "rag_unit_page_index_out_of_range",
                "Retrieval unit page index falls outside the source PDF.",
                source_id,
            )
        page_number = unit.get("page_number")
        if (
            isinstance(page_index, int)
            and isinstance(page_number, int)
            and page_number != page_index + 1
        ):
            add_issue(
                "rag_unit_page_number_index_mismatch",
                "Retrieval unit page number does not match its page index.",
                source_id,
            )
        if unit.get("kind") == "image":
            image = images_by_id.get(source_id)
            if not image or unit.get("asset_id") != image.get("asset_id"):
                add_issue(
                    "rag_unit_image_reference_invalid",
                    "Image retrieval unit does not resolve to its occurrence asset.",
                    source_id,
                )
            elif unit.get("image_path") != image.get("path"):
                add_issue(
                    "rag_unit_image_path_mismatch",
                    "Image retrieval unit path differs from its source occurrence.",
                    source_id,
                )

    return {
        "status": "valid" if not issues else "needs_review",
        "issue_count": len(issues),
        "issues": issues,
        "counts": {
            "elements": len(elements),
            "relationships": len(relationships),
            "rag_units": len(rag_units),
            "image_assets": len(assets),
            "image_occurrences": len(images_by_id),
            "possible_duplicate_relationships": sum(
                relationship.get("relationship")
                == "possible_duplicate_of_table"
                for relationship in relationships
            ),
            "candidate_relationships": sum(
                relationship.get("relation_status") == "candidate"
                for relationship in relationships
            ),
            "missing_or_invalid_references": sum(
                issue["code"].startswith(("image_", "rag_unit_", "relationship_"))
                for issue in issues
            ),
        },
    }


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
                    "relation_status": "candidate",
                    "confidence": round(ratio, 3),
                    "evidence": {
                        "method": "normalized_token_overlap",
                        "block_token_coverage": round(ratio, 3),
                        "reason": (
                            "Most words in this page text block also occur "
                            "in the extracted table cells."
                        ),
                    },
                })

        table["duplicate_detection"] = {
            "method": "normalized_token_overlap",
            "possible_duplicate_in_text": bool(matches),
            "matched_text_blocks": matches,
            "content_removed": False,
        }

    return relationships


def _build_link_relationships(
    pages: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Link an annotation to overlapping text without inferring its meaning."""
    relationships = []
    for page in pages:
        blocks = page.get("text_blocks", [])
        for link in page.get("links", []):
            link_box = link.get("source_bbox")
            overlapping_blocks = []
            for block in blocks:
                block_box = _bbox(block)
                if not link_box or not block_box:
                    continue
                overlap = _rect_overlap_ratio(link_box, block_box)
                if overlap > 0:
                    overlapping_blocks.append((overlap, block))
            if overlapping_blocks:
                overlap, block = max(
                    overlapping_blocks,
                    key=lambda item: item[0],
                )
                relationships.append({
                    "relationship_id": (
                        f"{block['block_id']}-hyperlink-{link['link_id']}"
                    ),
                    "source_id": block["block_id"],
                    "target_id": link["link_id"],
                    "relationship": "hyperlink_in_text",
                    "relation_status": "candidate",
                    "confidence": round(min(overlap, 0.95), 3),
                    "evidence": {
                        "method": "link_annotation_bbox_overlap",
                        "overlap_ratio": round(overlap, 3),
                        "same_page": True,
                        "target_uri": link.get("target_uri"),
                        "reason": (
                            "The link annotation overlaps this text block. "
                            "This identifies its source location, not the "
                            "semantic meaning of the URL."
                        ),
                    },
                })
            else:
                relationships.append({
                    "relationship_id": (
                        f"{link['link_id']}-hyperlink-unresolved"
                    ),
                    "source_id": link["link_id"],
                    "target_id": None,
                    "relationship": "hyperlink_context_unresolved",
                    "relation_status": "unresolved",
                    "confidence": 0.0,
                    "evidence": {
                        "method": "link_annotation_bbox_overlap",
                        "same_page": True,
                        "target_uri": link.get("target_uri"),
                        "reason": (
                            "No extracted text block overlaps the link "
                            "annotation; its section association is unresolved."
                        ),
                    },
                })
    return relationships


def _rect_overlap_ratio(first: Any, second: Any) -> float:
    """Measure the intersecting fraction of the smaller rectangle."""
    if not _valid_bbox(first) or not _valid_bbox(second):
        return 0.0
    if first is None or second is None:
        return 0.0
    ax0, ay0, ax1, ay1 = (float(value) for value in first)
    bx0, by0, bx1, by1 = (float(value) for value in second)
    intersection = max(0.0, min(ax1, bx1) - max(ax0, bx0)) * max(
        0.0, min(ay1, by1) - max(ay0, by0)
    )
    smaller_area = min(
        max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0),
        max(0.0, bx1 - bx0) * max(0.0, by1 - by0),
    )
    return intersection / smaller_area if smaller_area else 0.0


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
    relationships.extend(_build_link_relationships(pages))
    tables_by_page: dict[int, list[dict[str, Any]]] = {}
    images_by_page: dict[int, list[dict[str, Any]]] = {}
    for table in tables:
        tables_by_page.setdefault(table["page_number"], []).append(table)
    for image in images:
        images_by_page.setdefault(image["page_number"], []).append(image)

    caption_targets: dict[tuple[str, str], list[dict[str, Any]]] = {}
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
                        "relation_status": "candidate",
                        "confidence": 0.7,
                        "evidence": {
                            "same_page": True,
                            "preceding_heading_position": heading["bbox"],
                            "target_bbox": element.get("bbox"),
                            "reason": (
                                "The heading is the nearest section heading "
                                "above this element on the same page."
                            ),
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
                    "relation_status": "candidate",
                    "confidence": 0.6,
                    "evidence": {
                        "heading_page": active_section["page_number"],
                        "target_page": page_number,
                        "no_new_heading_before_target": True,
                        "reason": (
                            "No later section heading was found before this "
                            "element on its page."
                        ),
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
            caption_targets.setdefault(reference, []).append({
                "caption_id": block["block_id"],
                "target_id": None,
            })
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
                    "relation_status": "candidate",
                    "confidence": 0.75,
                    "evidence": {
                        "caption_reference": f"{kind} {number}",
                        "same_page": True,
                        "vertical_gap_points": round(gap, 2),
                        "horizontal_alignment": round(-negative_alignment, 3),
                        "reason": (
                            "The labeled caption is geometrically aligned "
                            "with the nearest image or table on the same page."
                        ),
                    },
                })
                caption_target = next(
                    item for item in caption_targets[reference]
                    if item["caption_id"] == block["block_id"]
                )
                caption_target["target_id"] = target_id

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
                    "relation_status": "candidate",
                    "confidence": 0.55,
                    "evidence": {
                        "same_page": True,
                        "vertical_gap_points": round(gap, 2),
                        "horizontal_alignment": round(-negative_alignment, 3),
                        "image_bbox": image_box,
                        "reason": (
                            "This is the closest aligned non-caption text "
                            "block; proximity alone does not confirm meaning."
                        ),
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
                captions = caption_targets.get((kind, number), [])
                if captions:
                    for caption in captions:
                        relationships.append({
                            "relationship_id": (
                                f"{block['block_id']}-reference-"
                                f"{caption['caption_id']}"
                            ),
                            "source_id": block["block_id"],
                            # The explicit evidence identifies a caption, not
                            # necessarily the image geometrically near it.
                            "target_id": caption["caption_id"],
                            "relationship": "explicit_reference",
                            "relation_status": (
                                "confirmed" if len(captions) == 1
                                else "candidate"
                            ),
                            "confidence": (
                                0.98 if len(captions) == 1 else 0.7
                            ),
                            "evidence": {
                                "matched_text": reference_text,
                                "same_document": True,
                                "numbered_caption_match": True,
                                "caption_target_id": caption["target_id"],
                                "reason": (
                                    "The text explicitly names the same "
                                    "numbered figure/table caption."
                                ),
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
                        "relation_status": "unresolved",
                        "confidence": 0.45,
                        "evidence": {
                            "matched_text": reference_text,
                            "reference_key": f"{kind} {number}",
                            "reason": (
                                "No matching numbered caption was found in "
                                "this document."
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
    logical_tables: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Prepare page-addressable text/table/image units without losing lineage."""
    units = []
    duplicate_blocks: dict[str, list[str]] = {}
    image_context: dict[str, list[dict[str, Any]]] = {}

    for relationship in relationships:
        if relationship["relationship"] == "possible_duplicate_of_table":
            duplicate_blocks.setdefault(
                relationship["source_id"], []
            ).append(relationship["target_id"])

    block_by_id = {
        block["block_id"]: block
        for page in pages
        for block in page.get("text_blocks", [])
    }
    for relationship in relationships:
        if relationship["target_id"] not in {
            image["image_id"] for image in images
        }:
            continue
        if (
            relationship["relationship"] in {"caption_for", "nearby_context"}
            and relationship["source_id"] in block_by_id
        ):
            image_context.setdefault(relationship["target_id"], []).append(
                relationship
            )

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
            caption_relationship = next(
                (
                    relationship
                    for related in image_context.values()
                    for relationship in related
                    if relationship["source_id"] == block["block_id"]
                    and relationship["relationship"] == "caption_for"
                ),
                None,
            )
            if caption_relationship:
                # Caption remains a separate extraction element; its retrieval
                # text is joined to the image unit to avoid indexing twice.
                continue

            page_index = page.get(
                "page_index",
                page.get("page_number", 1) - 1,
            )
            units.append({
                "unit_id": f"{block['block_id']}-unit",
                "kind": "text",
                "text": block["text"],
                "source_element_id": block["block_id"],
                "source_element_type": block.get(
                    "element_type",
                    element_type_for_text_kind(block.get("kind", "paragraph")),
                ),
                "document_id": document_id,
                "source_filename": block.get(
                    "source_filename",
                    page.get("source_filename"),
                ),
                "page_index": block.get("page_index", page_index),
                "page_number": page["page_number"],
                "printed_page_number": block.get("printed_page_number"),
                "bbox": block.get("bbox"),
                "section_context": block.get("section_context"),
                "related_table_ids": duplicate_table_ids,
            })

    logical_tables = logical_tables or []
    confirmed_fragments = {
        fragment["table_id"]
        for logical_table in logical_tables
        if logical_table.get("merge_status") == "confirmed"
        for fragment in logical_table.get("source_fragments", [])
    }
    uncertain_fragments = {
        relationship["target_id"]
        for relationship in relationships
        if relationship.get("relationship") == "continues_table"
        and relationship.get("relation_status") == "candidate"
    }

    for table in tables:
        # Keep suspicious detections in the source table JSON for review, but
        # do not turn page-wide text detections into misleading table chunks.
        if (
            table.get("validation", {}).get("detection_class")
            == "probable_false_positive"
        ):
            table["included_in_rag_units"] = False
            table["rag_exclusion_reason"] = "probable_false_positive"
            continue
        if table["table_id"] in confirmed_fragments:
            table["included_in_rag_units"] = False
            table["rag_exclusion_reason"] = "reconstructed_logical_table"
            continue
        if table["table_id"] in uncertain_fragments:
            table["included_in_rag_units"] = False
            table["rag_exclusion_reason"] = "uncertain_table_continuation"
            continue
        table["included_in_rag_units"] = True
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
                "source_element_type": "table",
                "table_id": table["table_id"],
                "source_row_index": row_index,
                "document_id": table["document_id"],
                "source_filename": table.get("source_filename"),
                "page_index": table.get(
                    "page_index",
                    table.get("page_number", 1) - 1,
                ),
                "page_number": table["page_number"],
                "printed_page_number": table.get("printed_page_number"),
                "bbox": table.get("bbox"),
                "row_index": row_index,
                "headers": table.get("headers"),
                "row": row,
                "title": table.get("title"),
                "section_context": table.get("section_context"),
                "validation_status": table.get("validation", {}).get("status"),
            })

    # Emit each confirmed logical row once. Its source element remains the
    # page-level fragment containing that row, so citations stay page-accurate.
    for logical_table in logical_tables:
        if logical_table.get("merge_status") != "confirmed":
            continue
        for logical_row in logical_table.get("rows", []):
            row = logical_row["row"]
            row_text = "; ".join(
                f"{header}: {value}"
                for header, value in row.items()
                if value is not None
            )
            context = [
                value for value in (
                    logical_table.get("section_context"),
                    logical_table.get("title"),
                )
                if value
            ]
            text = "\n".join(context + [row_text]) if context else row_text
            units.append({
                "unit_id": (
                    f"{logical_table['logical_table_id']}-row-"
                    f"{logical_row['row_index'] + 1}"
                ),
                "kind": "table_row",
                "text": text,
                "source_element_id": logical_row["source_fragment_id"],
                "source_element_type": "table",
                "table_id": logical_row["source_fragment_id"],
                "logical_table_id": logical_table["logical_table_id"],
                "source_row_index": logical_row["source_raw_row_index"],
                "document_id": logical_table["document_id"],
                "source_filename": logical_table.get("source_filename"),
                "page_index": logical_row["source_page_index"],
                "page_number": logical_row["source_page_number"],
                "printed_page_number": None,
                "bbox": logical_row["source_bbox"],
                "row_index": logical_row["row_index"],
                "headers": logical_table["headers"],
                "row": row,
                "title": logical_table.get("title"),
                "section_context": logical_table.get("section_context"),
                "validation_status": "valid",
                "merge_status": logical_table["merge_status"],
            })

    for image in images:
        related_text = []
        context_links = []
        for relationship in image_context.get(image["image_id"], []):
            block_id = relationship["source_id"]
            text = block_by_id[block_id]["text"]
            if text not in related_text:
                related_text.append(text)
            context_links.append({
                "source_element_id": block_id,
                "relationship_id": relationship["relationship_id"],
                "relationship": relationship["relationship"],
                "relation_status": relationship.get(
                    "relation_status",
                    "candidate",
                ),
                "confidence": relationship.get("confidence"),
                "evidence": relationship.get("evidence", {}),
            })
        page_index = image.get(
            "page_index",
            image.get("page_number", 1) - 1,
        )
        units.append({
            "unit_id": f"{image['image_id']}-unit",
            "kind": "image",
            "source_element_type": "image",
            "text": "\n".join(related_text) if related_text else None,
            "source_element_id": image["image_id"],
            "document_id": document_id,
            "source_filename": image.get("source_filename"),
            "page_index": page_index,
            "page_number": image.get("page_number"),
            "printed_page_number": image.get("printed_page_number"),
            "bbox": image.get("bbox"),
            "asset_id": image.get("asset_id"),
            "image_path": image.get("path"),
            "image_sha256": image.get("sha256"),
            "context_relationships": context_links,
            "context_status": "candidate" if context_links else "unlinked",
        })

    for page in pages:
        page_index = page.get(
            "page_index",
            page.get("page_number", 1) - 1,
        )
        for link in page.get("links", []):
            link_id = link["link_id"]
            link_relationships = [
                relationship
                for relationship in relationships
                if relationship.get("relationship") == "hyperlink_in_text"
                and relationship.get("target_id") == link_id
            ]
            context_texts = [
                block_by_id[relationship["source_id"]]["text"]
                for relationship in link_relationships
                if relationship.get("source_id") in block_by_id
            ]
            section_contexts = list(dict.fromkeys(
                block_by_id[relationship["source_id"]].get("section_context")
                for relationship in link_relationships
                if relationship.get("source_id") in block_by_id
                and block_by_id[relationship["source_id"]].get(
                    "section_context"
                )
            ))
            units.append({
                "unit_id": f"{link_id}-unit",
                "kind": "link",
                "text": (
                    link.get("target_uri")
                    or link.get("target_named")
                    or (
                        f"Internal PDF page {link['target_page_number']}"
                        if link.get("target_page_number") is not None
                        else "Internal PDF link"
                    )
                ),
                "source_element_id": link_id,
                "source_element_type": "link",
                "document_id": document_id,
                "source_filename": page.get("source_filename"),
                "page_index": page_index,
                "page_number": page.get("page_number"),
                "printed_page_number": page.get("printed_page_number"),
                "bbox": link.get("source_bbox"),
                "target_uri": link.get("target_uri"),
                "target_page": link.get("target_page"),
                "target_page_number": link.get("target_page_number"),
                "target_named": link.get("target_named"),
                "context_text": list(dict.fromkeys(context_texts)),
                "section_context": (
                    section_contexts[0]
                    if len(section_contexts) == 1
                    else None
                ),
                "section_context_candidates": section_contexts,
                "section_context_status": (
                    "candidate" if section_contexts else "unresolved"
                ),
                "context_relationships": [
                    {
                        "relationship_id": relationship["relationship_id"],
                        "relation_status": relationship["relation_status"],
                        "confidence": relationship["confidence"],
                        "evidence": relationship["evidence"],
                    }
                    for relationship in link_relationships
                ],
                "context_status": (
                    "candidate" if link_relationships else "unresolved"
                ),
            })

    return units


def _apply_confirmed_logical_table_schema(
    tables: list[dict[str, Any]],
    logical_tables: list[dict[str, Any]],
) -> None:
    """Expose confirmed continuation rows under their logical table headers.

    A page fragment may have no header row of its own. Once a continuation is
    confirmed, use the logical table's headers for its normalized rows, while
    retaining the page-local interpretation and raw cells for auditing.
    """
    tables_by_id = {table["table_id"]: table for table in tables}
    for logical_table in logical_tables:
        if logical_table.get("merge_status") != "confirmed":
            continue

        rows_by_fragment: dict[str, list[dict[str, Any]]] = {}
        for logical_row in logical_table.get("rows", []):
            rows_by_fragment.setdefault(
                logical_row["source_fragment_id"],
                [],
            ).append(logical_row)

        source_fragments = logical_table.get("source_fragments", [])
        first_fragment_id = (
            source_fragments[0].get("table_id")
            if source_fragments
            else None
        )
        for fragment in source_fragments:
            table = tables_by_id.get(fragment.get("table_id"))
            if table is None or table["table_id"] == first_fragment_id:
                continue

            table["logical_table_id"] = logical_table["logical_table_id"]
            # The first row of a headerless continuation can look like a
            # header to the local heuristic. Preserve that interpretation for
            # audit, but expose the confirmed logical rows as the fragment's
            # normalized view so the first real continuation row is not lost.
            detected_header_source = table.get("header_source")
            table["detected_headers"] = table.get("headers")
            table["detected_header_source"] = detected_header_source
            table["detected_rows"] = table.get("rows")
            table["headers"] = logical_table["headers"]
            table["header_source"] = (
                "inherited_from_confirmed_logical_table"
            )
            table["rows"] = [
                dict(logical_row["row"])
                for logical_row in rows_by_fragment.get(
                    table["table_id"],
                    [],
                )
            ]
            table["row_count"] = len(table["rows"])
            validation = table.get("validation", {})
            original_warnings = validation.get("warnings", [])
            resolved_codes = {
                "unknown_header_structure",
                "generic_headers",
                "empty_headers",
            }
            resolved_warnings = [
                {
                    **warning,
                    "resolved_by": logical_table["logical_table_id"],
                }
                for warning in original_warnings
                if warning.get("code") in resolved_codes
            ]
            if detected_header_source in {
                "detected_header_row",
                "detected_multilevel_header",
            }:
                resolved_warnings.append({
                    "code": "continuation_first_row_reclassified_as_data",
                    "message": (
                        "The page-local first row was treated as a header, "
                        "but confirmed continuation evidence identifies it "
                        "as data under the preceding table's headers."
                    ),
                    "resolved_by": logical_table["logical_table_id"],
                })
            if resolved_warnings:
                validation["resolved_header_warnings"] = resolved_warnings
                validation["warnings"] = [
                    warning
                    for warning in original_warnings
                    if warning.get("code") not in resolved_codes
                ]
                validation["fallback_needed"] = bool(validation["warnings"])
                if not validation["warnings"]:
                    validation["status"] = "valid"


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
        source_sha256=extraction["source_sha256"],
    )
    tables = table_result["tables"]

    relationships = build_relationships(
        pages=extraction["pages"],
        tables=tables,
        images=extraction["images"],
    )
    # Page-level records remain intact; this separate pass groups only pairs
    # supported by adjacent-page geometry and compatible row/header evidence.
    logical_tables, continuation_relationships = reconstruct_logical_tables(
        tables,
    )
    relationships.extend(continuation_relationships)
    for relationship in continuation_relationships:
        if relationship["relation_status"] == "candidate":
            issue = {
                "code": "uncertain_table_continuation",
                "message": relationship["evidence"]["reason"],
                "element_id": relationship["target_id"],
            }
            # Candidate links stay visible, but their malformed page-level
            # rows are not promoted into RAG as though the merge were certain.
            table = next(
                item for item in tables
                if item["table_id"] == relationship["target_id"]
            )
            table["validation"]["warnings"].append({
                "code": issue["code"],
                "message": issue["message"],
            })
            table["validation"]["status"] = "uncertain"
            table["validation"]["fallback_needed"] = True
            table["rag_exclusion_reason"] = "uncertain_table_continuation"
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

    _apply_confirmed_logical_table_schema(tables, logical_tables)
    rag_units = build_rag_units(
        pages=extraction["pages"],
        tables=tables,
        images=extraction["images"],
        relationships=relationships,
        document_id=extraction["document_id"],
        logical_tables=logical_tables,
    )
    table_result["tables"] = tables
    table_result["table_count"] = len(tables)
    table_result["logical_tables"] = logical_tables
    (document_output / f"{output_stem}.tables.json").write_text(
        json.dumps(table_result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    # Relationship context and duplicate flags are attached to extracted
    # blocks during RAG preparation, so persist those additions in-place.
    extraction_path = document_output / f"{output_stem}.extraction.json"
    extraction_path.write_text(
        json.dumps(extraction, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    elements = build_elements(
        extraction=extraction,
        tables=tables,
        extraction_filename=f"{output_stem}.extraction.json",
        tables_filename=f"{output_stem}.tables.json",
    )
    image_only_pages = [
        page["page_number"]
        for page in extraction["pages"]
        if "possible_scanned_page_ocr_needed" in page.get("warnings", [])
    ]
    document_checks = validate_document(
        extraction=extraction,
        tables=tables,
        relationships=relationships,
        rag_units=rag_units,
        elements=elements,
        document_output=document_output,
        extraction_filename=f"{output_stem}.extraction.json",
        tables_filename=f"{output_stem}.tables.json",
    )
    table_extraction_warnings = table_result.get("warnings", [])
    combined_extraction_warnings = (
        extraction.get("warnings", []) + table_extraction_warnings
    )
    validation = {
        "status": document_checks["status"],
        "issue_count": document_checks["issue_count"],
        "issues": document_checks["issues"],
        "checks": document_checks["counts"],
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
        "images_with_candidate_context": sum(
            any(
                relationship["target_id"] == image["image_id"]
                and relationship.get("relation_status") == "candidate"
                for relationship in relationships
            )
            for image in extraction["images"]
        ),
        "tables_excluded_from_rag": sum(
            table.get("included_in_rag_units") is False
            for table in tables
        ),
        "table_warning_count": sum(
            len(table.get("validation", {}).get("warnings", []))
            for table in tables
        ),
        "extraction_warnings": combined_extraction_warnings,
    }
    result = {
        "schema_version": 2,
        "document_id": extraction["document_id"],
        "machine_name": machine_name,
        "relative_source_path": relative_source_path,
        "source_filename": extraction["source_filename"],
        "source_sha256": extraction["source_sha256"],
        "page_count": extraction["page_count"],
        "extraction_file": f"{output_stem}.extraction.json",
        "tables_file": f"{output_stem}.tables.json",
        "image_count": len(extraction["images"]),
        "image_asset_count": len(extraction.get("image_assets", [])),
        "elements": elements,
        "tables": tables,
        "logical_tables": logical_tables,
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
        for issue in document_checks["issues"]:
            logger.warning(
                "Validation warning code=%s element=%s: %s",
                issue["code"],
                issue.get("element_id"),
                issue["message"],
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
