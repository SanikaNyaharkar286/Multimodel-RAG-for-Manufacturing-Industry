"""Read-only audits for processed document knowledge units and provenance."""

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

from .config import PROCESSED_DATA_DIR


AUDIT_SCHEMA_VERSION = 1
_REPLACEMENT_CHAR = "\ufffd"
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
_SUSPICIOUS_WORD_ENDING = re.compile(
    r"\b(?:[A-Za-z]{5,}e(?:e|i)ng|[A-Za-z]{2,}gign)\b",
    re.IGNORECASE,
)
_INCOMPLETE_STEP_ENDING = re.compile(
    r"(?:[:,;]|\b(?:and|or|with|to))\s*$",
    re.IGNORECASE,
)


def _read_json(path: Path) -> dict[str, Any]:
    """Load one JSON object and reject other top-level shapes."""
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def _main_document_path(path: Path) -> Path:
    """Resolve a combined document JSON path from a file or output folder."""
    path = Path(path)
    if path.is_file():
        return path
    if not path.is_dir():
        raise FileNotFoundError(f"Document path does not exist: {path}")

    candidates = []
    for candidate in sorted(path.glob("*.json")):
        if candidate.name.endswith(
            (".extraction.json", ".tables.json", ".audit.json")
        ):
            continue
        try:
            data = _read_json(candidate)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if isinstance(data.get("elements"), list) and isinstance(
            data.get("rag_units"), list
        ):
            candidates.append(candidate)
    if len(candidates) != 1:
        raise ValueError(
            f"Expected one combined document JSON in {path}; "
            f"found {len(candidates)}."
        )
    return candidates[0]


def _valid_bbox(value: Any) -> bool:
    """Accept absent boxes; validate any supplied PDF rectangle."""
    if value is None:
        return True
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return False
    try:
        x0, y0, x1, y1 = (float(item) for item in value)
    except (TypeError, ValueError):
        return False
    return (
        all(math.isfinite(item) for item in (x0, y0, x1, y1))
        and x0 <= x1
        and y0 <= y1
    )


def _safe_relative_file(directory: Path, value: Any) -> Path | None:
    """Resolve a document-relative file without allowing directory escape."""
    if not isinstance(value, str) or not value.strip():
        return None
    resolved = (directory / value).resolve()
    try:
        resolved.relative_to(directory.resolve())
    except ValueError:
        return None
    return resolved


def _tokens(value: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", value.casefold())


def _blank_cell(value: Any) -> bool:
    return value is None or (
        isinstance(value, str) and not value.strip()
    )


def _normalized_cell_text(value: Any) -> str | None:
    if value is None:
        return None
    normalized = re.sub(r"\s+", " ", str(value)).strip()
    return normalized or None


def _severity_status(findings: list[dict[str, Any]]) -> str:
    if any(item["severity"] == "error" for item in findings):
        return "invalid"
    if any(item["severity"] == "warning" for item in findings):
        return "needs_review"
    return "valid"


def audit_document(
    document_path: Path,
    report_directory: Path | None = None,
) -> dict[str, Any]:
    """Audit one processed document without modifying its source artifacts."""
    main_path = _main_document_path(Path(document_path))
    document_directory = main_path.parent
    document = _read_json(main_path)
    document_id = document.get("document_id")
    source_filename = document.get("source_filename")
    findings: list[dict[str, Any]] = []

    def add(
        code: str,
        severity: str,
        category: str,
        message: str,
        recommendation: str,
        **location: Any,
    ) -> None:
        finding = {
            "code": code,
            "severity": severity,
            "category": category,
            "message": message,
            "recommendation": recommendation,
            "document_id": document_id,
            "source_filename": source_filename,
        }
        finding.update({
            key: value for key, value in location.items()
            if value is not None
        })
        findings.append(finding)

    def load_artifact(field: str) -> tuple[dict[str, Any] | None, Path | None]:
        artifact_name = document.get(field)
        artifact_path = _safe_relative_file(document_directory, artifact_name)
        if artifact_path is None:
            add(
                "artifact_reference_invalid",
                "error",
                "provenance",
                f"Document field {field!r} is missing or escapes its output folder.",
                "Restore the correct document-relative artifact filename.",
                artifact=artifact_name,
            )
            return None, None
        if not artifact_path.is_file():
            add(
                "artifact_missing",
                "error",
                "provenance",
                f"Referenced artifact is missing: {artifact_name}",
                "Reprocess the source PDF or restore the referenced artifact.",
                artifact=artifact_name,
            )
            return None, artifact_path
        try:
            return _read_json(artifact_path), artifact_path
        except (OSError, json.JSONDecodeError, ValueError) as error:
            add(
                "artifact_unreadable",
                "error",
                "provenance",
                f"Could not read {artifact_name}: {error}",
                "Recreate the artifact from the source PDF.",
                artifact=artifact_name,
            )
            return None, artifact_path

    if not isinstance(document_id, str) or not document_id.strip():
        add(
            "document_id_missing",
            "error",
            "provenance",
            "The combined document has no document ID.",
            "Restore the source PDF hash-derived document ID.",
        )
    if not isinstance(source_filename, str) or not source_filename.strip():
        add(
            "source_filename_missing",
            "error",
            "provenance",
            "The combined document has no source filename.",
            "Restore the original PDF filename in the document metadata.",
        )

    page_count = document.get("page_count")
    if not isinstance(page_count, int) or page_count < 1:
        add(
            "page_count_invalid",
            "error",
            "provenance",
            "The document page count must be a positive integer.",
            "Reprocess the source PDF and retain its page count.",
        )

    extraction, _ = load_artifact("extraction_file")
    table_artifact, _ = load_artifact("tables_file")
    elements = document.get("elements", [])
    tables = document.get("tables", [])
    logical_tables = document.get("logical_tables", [])
    relationships = document.get("relationships", [])
    units = document.get("rag_units", [])
    elements = elements if isinstance(elements, list) else []
    tables = tables if isinstance(tables, list) else []
    logical_tables = logical_tables if isinstance(logical_tables, list) else []
    relationships = relationships if isinstance(relationships, list) else []
    units = units if isinstance(units, list) else []
    extraction = extraction or {}
    pages = extraction.get("pages", [])
    pages = pages if isinstance(pages, list) else []
    extraction_images = extraction.get("images", [])
    extraction_images = (
        extraction_images if isinstance(extraction_images, list) else []
    )
    image_assets = extraction.get("image_assets", [])
    image_assets = image_assets if isinstance(image_assets, list) else []

    for field, value in (
        ("elements", document.get("elements")),
        ("tables", document.get("tables")),
        ("relationships", document.get("relationships")),
        ("rag_units", document.get("rag_units")),
    ):
        if not isinstance(value, list):
            add(
                "document_collection_invalid",
                "error",
                "provenance",
                f"The document field {field!r} must be a list.",
                "Regenerate the combined document JSON.",
                field=field,
            )

    metadata_pairs = (
        ("document_id", document_id),
        ("source_filename", source_filename),
        ("source_sha256", document.get("source_sha256")),
    )
    for artifact_name, artifact in (
        ("extraction", extraction),
        ("tables", table_artifact or {}),
    ):
        for key, expected in metadata_pairs:
            if expected is not None and artifact.get(key) != expected:
                add(
                    "artifact_metadata_mismatch",
                    "error",
                    "provenance",
                    f"{artifact_name} artifact {key} does not match "
                    "the combined document.",
                    "Regenerate all document artifacts from the same PDF.",
                    artifact=artifact_name,
                    field=key,
                )
    if extraction and extraction.get("page_count") != page_count:
        add(
            "artifact_page_count_mismatch",
            "error",
            "provenance",
            "Extraction page count differs from the combined document.",
            "Regenerate both artifacts from the same source PDF.",
        )

    element_ids: dict[str, dict[str, Any]] = {}
    element_counts = Counter(
        item.get("element_id")
        for item in elements
        if isinstance(item, dict)
        and isinstance(item.get("element_id"), str)
        and item.get("element_id")
    )
    for element_id, count in element_counts.items():
        if count > 1:
            add(
                "duplicate_element_id",
                "error",
                "provenance",
                f"Element ID {element_id!r} occurs {count} times.",
                "Regenerate stable, unique element identifiers.",
                element_id=element_id,
            )
    for element in elements:
        if not isinstance(element, dict):
            continue
        element_id = element.get("element_id")
        if isinstance(element_id, str) and element_id:
            element_ids.setdefault(element_id, element)
        if not element_id:
            add(
                "element_id_missing",
                "error",
                "provenance",
                "An indexed element has no element ID.",
                "Regenerate the shared element index.",
            )
            continue
        if (
            element.get("document_id") != document_id
            or element.get("source_filename") != source_filename
        ):
            add(
                "element_provenance_missing",
                "error",
                "provenance",
                "Element document ID or source filename is missing or inconsistent.",
                "Restore source document metadata on the element.",
                element_id=element_id,
                page_index=element.get("page_index"),
            )
        page_index = element.get("page_index")
        if (
            not isinstance(page_index, int)
            or isinstance(page_index, bool)
            or page_index < 0
            or (isinstance(page_count, int) and page_index >= page_count)
        ):
            add(
                "element_page_index_invalid",
                "error",
                "provenance",
                "Element page_index is not a valid zero-based PDF page index.",
                "Retain zero-based page_index and one-based page_number separately.",
                element_id=element_id,
                page_index=page_index,
            )
        elif (
            isinstance(element.get("page_number"), int)
            and element["page_number"] != page_index + 1
        ):
            add(
                "element_page_number_mismatch",
                "error",
                "provenance",
                "One-based page_number does not match page_index + 1.",
                "Correct page provenance while preserving both conventions.",
                element_id=element_id,
                page_index=page_index,
                page_number=element.get("page_number"),
            )
        if not _valid_bbox(element.get("bbox")):
            add(
                "element_bbox_invalid",
                "error",
                "provenance",
                "Element bounding box is malformed or has reversed coordinates.",
                "Restore the original finite, ordered PDF bounding box.",
                element_id=element_id,
                page_index=page_index,
            )
        elif element.get("bbox") is None and element.get("element_type") in {
            "heading",
            "paragraph",
            "list",
            "caption",
            "table",
            "image",
            "link",
        }:
            add(
                "element_bbox_missing",
                "warning",
                "provenance",
                "Element has no source bounding box for page-level localization.",
                "Review its layout provenance against the source PDF.",
                element_id=element_id,
                page_index=page_index,
                page_number=element.get("page_number"),
            )

    pages_by_index: dict[int, dict[str, Any]] = {}
    for page in pages:
        if not isinstance(page, dict):
            continue
        page_index = page.get("page_index")
        page_number = page.get("page_number")
        if (
            not isinstance(page_index, int)
            or isinstance(page_index, bool)
            or page_index < 0
            or (isinstance(page_count, int) and page_index >= page_count)
            or page_number != page_index + 1
        ):
            add(
                "extraction_page_provenance_invalid",
                "error",
                "provenance",
                "Extraction page index/number is invalid or inconsistent.",
                "Keep page_index zero-based and page_number one-based.",
                page_index=page_index,
                page_number=page_number,
            )
        else:
            if page_index in pages_by_index:
                add(
                    "duplicate_page_index",
                    "error",
                    "provenance",
                    f"Extraction contains duplicate page_index {page_index}.",
                    "Regenerate the extraction with unique page records.",
                    page_index=page_index,
                    page_number=page_number,
                )
            pages_by_index[page_index] = page

    text_blocks: dict[str, dict[str, Any]] = {}
    links: dict[str, dict[str, Any]] = {}
    for page in pages:
        if not isinstance(page, dict):
            continue
        page_index = page.get("page_index")
        page_number = page.get("page_number")
        for image_id in page.get("image_ids", []):
            image = next(
                (
                    item for item in extraction_images
                    if isinstance(item, dict)
                    and item.get("image_id") == image_id
                ),
                None,
            )
            if image is None or image.get("page_index") != page_index:
                add(
                    "page_image_reference_invalid",
                    "error",
                    "provenance",
                    "Page image_ids contains an unknown occurrence or one "
                    "assigned to a different page.",
                    "Correct the page-to-image occurrence reference.",
                    element_id=image_id,
                    page_index=page_index,
                    page_number=page_number,
                )
        for block in page.get("text_blocks", []):
            if not isinstance(block, dict):
                continue
            block_id = block.get("block_id")
            if isinstance(block_id, str):
                if block_id in text_blocks:
                    add(
                        "duplicate_text_block_id",
                        "error",
                        "provenance",
                        f"Text block ID {block_id!r} is duplicated.",
                        "Regenerate unique text-block IDs.",
                        element_id=block_id,
                        page_index=page_index,
                    )
                text_blocks[block_id] = block
            if block.get("page_index", page_index) != page_index:
                add(
                    "text_block_page_mismatch",
                    "error",
                    "provenance",
                    "Text block page index does not match its containing page.",
                    "Restore matching page provenance.",
                    element_id=block_id,
                    page_index=page_index,
                    page_number=page_number,
                )
        for link in page.get("links", []):
            if not isinstance(link, dict):
                continue
            link_id = link.get("link_id")
            if not isinstance(link_id, str) or not link_id:
                add(
                    "link_id_missing",
                    "error",
                    "links",
                    "An extracted hyperlink has no stable link ID.",
                    "Regenerate a unique ID for the link occurrence.",
                    page_index=page_index,
                )
                continue
            if link_id in links:
                add(
                    "duplicate_link_id",
                    "error",
                    "links",
                    f"Link ID {link_id!r} occurs more than once.",
                    "Regenerate unique IDs for link occurrences.",
                    link_id=link_id,
                    page_index=page_index,
                )
            links[link_id] = link
            if link.get("source_page") != page_number:
                add(
                    "link_source_page_mismatch",
                    "error",
                    "links",
                    "Link source page does not match its containing extraction page.",
                    "Restore the link's one-based source page.",
                    link_id=link_id,
                    page_index=page_index,
                    page_number=page_number,
                )
            if (
                isinstance(page_index, int)
                and isinstance(link.get("source_page"), int)
                and link["source_page"] != page_index + 1
            ):
                add(
                    "link_page_index_mismatch",
                    "error",
                    "provenance",
                    "Link source_page does not match its containing zero-based "
                    "page_index.",
                    "Preserve one-based source_page and zero-based page_index.",
                    link_id=link_id,
                    page_index=page_index,
                    page_number=link.get("source_page"),
                )
            if link.get("source_bbox") is None:
                add(
                    "link_bbox_missing",
                    "warning",
                    "provenance",
                    "Link has no source bounding box for section association.",
                    "Review the link's source location in the PDF.",
                    link_id=link_id,
                    page_index=page_index,
                    page_number=page_number,
                )
            elif not _valid_bbox(link.get("source_bbox")):
                add(
                    "link_bbox_invalid",
                    "error",
                    "links",
                    "Link source bounding box is invalid.",
                    "Retain a finite, ordered source rectangle.",
                    link_id=link_id,
                    page_index=page_index,
                )
            target_uri = link.get("target_uri")
            if target_uri is not None and (
                not isinstance(target_uri, str) or not target_uri.strip()
            ):
                add(
                    "link_target_invalid",
                    "warning",
                    "links",
                    "Link target URI is empty or not text.",
                    "Review the source PDF link annotation.",
                    link_id=link_id,
                    page_index=page_index,
                )
            if target_uri and "youtube.com" in target_uri.casefold():
                add(
                    "youtube_link_preserved",
                    "info",
                    "links",
                    "YouTube URL is retained as an extracted linked resource; "
                    "no live request was made.",
                    "Confirm its section association from the link location and text.",
                    link_id=link_id,
                    page_index=page_index,
                    page_number=page_number,
                    target_uri=target_uri,
                )
            if link_id not in element_ids:
                add(
                    "link_not_indexed",
                    "warning",
                    "links",
                    "Extracted link has no corresponding indexed link element.",
                    "Preserve the URL, page, bounding box, and uncertainty in "
                    "the combined document representation.",
                    link_id=link_id,
                    page_index=page_index,
                    page_number=page_number,
                    target_uri=target_uri,
                )

    table_by_id: dict[str, dict[str, Any]] = {}
    table_id_counts = Counter(
        table.get("table_id")
        for table in tables
        if isinstance(table, dict) and table.get("table_id")
    )
    for table_id, count in table_id_counts.items():
        if count > 1:
            add(
                "duplicate_table_id",
                "error",
                "tables",
                f"Table ID {table_id!r} occurs {count} times.",
                "Regenerate unique table identifiers.",
                table_id=table_id,
            )
    for table in tables:
        if not isinstance(table, dict):
            continue
        table_id = table.get("table_id")
        if isinstance(table_id, str):
            table_by_id[table_id] = table
        if not table_id:
            add(
                "table_id_missing",
                "error",
                "tables",
                "A table has no table ID.",
                "Regenerate the table with a stable identifier.",
            )
            continue
        if table_id not in element_ids:
            add(
                "table_element_missing",
                "error",
                "provenance",
                "Table ID does not resolve to a shared document element.",
                "Rebuild the shared element index from the table artifact.",
                table_id=table_id,
                page_index=table.get("page_index"),
                page_number=table.get("page_number"),
            )
        elif element_ids[table_id].get("page_index") != table.get("page_index"):
            add(
                "table_element_page_mismatch",
                "error",
                "provenance",
                "Table and shared element index disagree on page_index.",
                "Regenerate the element locator from the page-level table.",
                table_id=table_id,
                page_index=table.get("page_index"),
                page_number=table.get("page_number"),
            )
        headers = table.get("headers")
        rows = table.get("rows")
        raw_rows = table.get("raw_rows")
        normalized_raw_rows = table.get("normalized_raw_rows", raw_rows)
        if not isinstance(headers, list) or not headers:
            add(
                "table_headers_missing",
                "warning",
                "tables",
                "No normalized headers are available; raw table rows remain "
                "the source evidence.",
                "Review the table structure; do not assume its first data row "
                "is a header.",
                table_id=table_id,
                page_index=table.get("page_index"),
                page_number=table.get("page_number"),
            )
        if not isinstance(rows, list) or not isinstance(raw_rows, list):
            add(
                "table_rows_invalid",
                "error",
                "tables",
                "Table normalized rows and raw_rows must be lists.",
                "Re-extract the table while preserving original rows.",
                table_id=table_id,
                page_index=table.get("page_index"),
            )
            continue
        for raw_row_index, raw_row in enumerate(raw_rows):
            if not isinstance(raw_row, list):
                add(
                    "table_raw_row_invalid",
                    "error",
                    "tables",
                    "A raw table row is not a list of source cells.",
                    "Restore the original cell-array structure.",
                    table_id=table_id,
                    row_index=raw_row_index,
                    page_index=table.get("page_index"),
                )
        if (
            table.get("document_id") != document_id
            or table.get("source_filename") != source_filename
        ):
            add(
                "table_provenance_mismatch",
                "error",
                "provenance",
                "Table document ID or source filename differs from the document.",
                "Restore the original PDF provenance on the table.",
                table_id=table_id,
                page_index=table.get("page_index"),
                page_number=table.get("page_number"),
            )
        table_page_index = table.get("page_index")
        if (
            not isinstance(table_page_index, int)
            or isinstance(table_page_index, bool)
            or table_page_index < 0
            or (
                isinstance(page_count, int)
                and table_page_index >= page_count
            )
        ):
            add(
                "table_page_index_invalid",
                "error",
                "provenance",
                "Table page_index is not a valid zero-based PDF page index.",
                "Restore the table's zero-based page index.",
                table_id=table_id,
                page_index=table_page_index,
                page_number=table.get("page_number"),
            )
        elif table.get("page_number") != table_page_index + 1:
            add(
                "table_page_number_mismatch",
                "error",
                "provenance",
                "Table page_number does not match page_index + 1.",
                "Correct the one-based page number without changing page_index.",
                table_id=table_id,
                page_index=table_page_index,
                page_number=table.get("page_number"),
            )
        if not _valid_bbox(table.get("bbox")):
            add(
                "table_bbox_invalid",
                "error",
                "provenance",
                "Table bounding box is malformed or has reversed coordinates.",
                "Restore the table's original PDF rectangle.",
                table_id=table_id,
                page_index=table_page_index,
                page_number=table.get("page_number"),
            )
        elif table.get("bbox") is None:
            add(
                "table_bbox_missing",
                "warning",
                "provenance",
                "Table has no source bounding box for layout review.",
                "Compare its page-level location with the source PDF.",
                table_id=table_id,
                page_index=table_page_index,
                page_number=table.get("page_number"),
            )
        if isinstance(headers, list) and len(headers) != table.get(
            "column_count",
            len(headers),
        ):
            add(
                "table_header_column_count_mismatch",
                "warning",
                "tables",
                "Header count differs from the detected column count.",
                "Review column alignment against the PDF geometry.",
                table_id=table_id,
                page_index=table.get("page_index"),
            )
        row_lengths = [len(row) for row in raw_rows if isinstance(row, list)]
        if len(set(row_lengths)) > 1:
            add(
                "table_raw_row_length_mismatch",
                "warning",
                "tables",
                "Raw rows have inconsistent cell counts.",
                "Review malformed or partial rows without dropping their raw values.",
                table_id=table_id,
                page_index=table.get("page_index"),
            )
        if not isinstance(normalized_raw_rows, list) or (
            len(normalized_raw_rows) != len(raw_rows)
        ):
            add(
                "table_raw_normalized_row_count_mismatch",
                "error",
                "tables",
                "Normalized raw rows do not preserve one row per raw source row.",
                "Regenerate normalization while retaining every raw row.",
                table_id=table_id,
                page_index=table.get("page_index"),
            )
            normalized_raw_rows = raw_rows
        adjustment_keys: set[tuple[int, int]] = set()
        for adjustment in table.get("layout_adjustments", []):
            if not isinstance(adjustment, dict):
                continue
            source_row = adjustment.get("source_row_index")
            target_row = adjustment.get("target_row_index")
            column = adjustment.get("column_index")
            if all(isinstance(item, int) and item >= 0
                   for item in (source_row, target_row, column)):
                indexes_valid = (
                    source_row < len(raw_rows)
                    and target_row < len(raw_rows)
                    and target_row < len(normalized_raw_rows)
                    and isinstance(raw_rows[source_row], list)
                    and isinstance(raw_rows[target_row], list)
                    and isinstance(normalized_raw_rows[target_row], list)
                    and column < len(raw_rows[source_row])
                    and column < len(raw_rows[target_row])
                    and column < len(normalized_raw_rows[target_row])
                )
                source_value_matches = (
                    indexes_valid
                    and _normalized_cell_text(
                        raw_rows[source_row][column]
                    ) == _normalized_cell_text(adjustment.get("value"))
                )
                valid_adjustment_box = (
                    adjustment.get("bbox") is not None
                    and _valid_bbox(adjustment.get("bbox"))
                )
                if indexes_valid and source_value_matches and valid_adjustment_box:
                    adjustment_keys.add((target_row, column))
                if (
                    source_value_matches
                    and valid_adjustment_box
                    and _normalized_cell_text(
                        normalized_raw_rows[target_row][column]
                    ) == _normalized_cell_text(adjustment.get("value"))
                    and _blank_cell(raw_rows[target_row][column])
                ):
                    add(
                        "confirmed_merged_cell_expansion",
                        "info",
                        "tables",
                        "A blank raw cell was normalized using recorded "
                        "vertical-merge geometry evidence.",
                        "Retain both raw and normalized values and the geometry record.",
                        table_id=table_id,
                        row_index=target_row,
                        page_index=table.get("page_index"),
                    )
                elif (
                    not indexes_valid
                    or not source_value_matches
                    or not valid_adjustment_box
                ):
                    add(
                        "table_merge_evidence_invalid",
                        "warning",
                        "tables",
                        "Merged-cell adjustment lacks a matching source value "
                        "or a valid geometry rectangle.",
                        "Review the original table geometry and keep the raw "
                        "cell unchanged until confirmed.",
                        table_id=table_id,
                        row_index=target_row,
                        page_index=table.get("page_index"),
                    )
        for row_index, raw_row in enumerate(raw_rows):
            if not isinstance(raw_row, list):
                continue
            normalized_row = (
                normalized_raw_rows[row_index]
                if row_index < len(normalized_raw_rows)
                and isinstance(normalized_raw_rows[row_index], list)
                else raw_row
            )
            for column_index, raw_value in enumerate(raw_row):
                if column_index >= len(normalized_row):
                    continue
                normalized_value = normalized_row[column_index]
                if (
                    _blank_cell(raw_value)
                    and not _blank_cell(normalized_value)
                    and (row_index, column_index) not in adjustment_keys
                ):
                    add(
                        "table_normalization_without_geometry_evidence",
                        "warning",
                        "tables",
                        "A blank raw cell gained a normalized value without "
                        "a matching recorded layout adjustment.",
                        "Review the cell against source geometry; do not treat "
                        "an unrecorded inferred value as confirmed.",
                        table_id=table_id,
                        row_index=row_index,
                        page_index=table.get("page_index"),
                    )
                elif (
                    not _blank_cell(raw_value)
                    and not _blank_cell(normalized_value)
                    and _normalized_cell_text(raw_value)
                    != _normalized_cell_text(normalized_value)
                    and (row_index, column_index) not in adjustment_keys
                ):
                    add(
                        "table_raw_value_changed",
                        "warning",
                        "tables",
                        "A non-empty raw cell differs from its normalized value "
                        "beyond whitespace normalization.",
                        "Review the source cell; do not replace the raw value.",
                        table_id=table_id,
                        row_index=row_index,
                        page_index=table.get("page_index"),
                    )
            if row_index < len(normalized_raw_rows) and isinstance(
                normalized_raw_rows[row_index], list
            ):
                for column_index, value in enumerate(
                    normalized_raw_rows[row_index]
                ):
                    if value is None:
                        add(
                            "table_empty_cell_preserved",
                            "info",
                            "tables",
                            "An empty normalized table cell is preserved; "
                            "the audit does not fill it.",
                            "Keep it empty unless source geometry confirms a merge.",
                            table_id=table_id,
                            row_index=row_index,
                            page_index=table.get("page_index"),
                        )
        expected_headers = set(headers) if isinstance(headers, list) else set()
        for row_index, row in enumerate(rows):
            if not isinstance(row, dict):
                add(
                    "table_normalized_row_invalid",
                    "error",
                    "tables",
                    "A normalized row is not a header/value mapping.",
                    "Rebuild the normalized row without discarding raw cells.",
                    table_id=table_id,
                    row_index=row_index,
                    page_index=table.get("page_index"),
                )
            elif any(_blank_cell(value) for value in row.values()):
                add(
                    "table_row_contains_empty_cells",
                    "info",
                    "tables",
                    "Normalized table row contains an empty value; it was not "
                    "automatically filled.",
                    "Review against the source layout to distinguish an empty "
                    "cell from a confirmed merged cell.",
                    table_id=table_id,
                    row_index=row_index,
                    page_index=table.get("page_index"),
                )
            elif expected_headers and set(row) != expected_headers:
                add(
                    "table_normalized_row_misaligned",
                    "error",
                    "tables",
                    "Normalized row keys do not exactly match table headers.",
                    "Correct the header/value association and retain the raw row.",
                    table_id=table_id,
                    row_index=row_index,
                    page_index=table.get("page_index"),
                )
        row_signatures: dict[str, list[int]] = defaultdict(list)
        for row_index, row in enumerate(rows):
            if isinstance(row, dict):
                signature = json.dumps(row, sort_keys=True, ensure_ascii=False)
                row_signatures[signature].append(row_index)
        for duplicate_indexes in row_signatures.values():
            if len(duplicate_indexes) > 1:
                add(
                    "duplicate_normalized_table_rows",
                    "info",
                    "duplicates",
                    "Table contains repeated normalized row values.",
                    "Confirm whether rows are intentional repeats in the source.",
                    table_id=table_id,
                    row_indexes=duplicate_indexes,
                    page_index=table.get("page_index"),
                )
        for warning in table.get("validation", {}).get("warnings", []):
            if isinstance(warning, dict):
                add(
                    f"extraction_{warning.get('code', 'warning')}",
                    "warning",
                    "tables",
                    warning.get("message", "Table extraction recorded a warning."),
                    "Review the table warning against the original PDF.",
                    table_id=table_id,
                    page_index=table.get("page_index"),
                    page_number=table.get("page_number"),
                )

    if isinstance(table_artifact, dict):
        artifact_tables = table_artifact.get("tables", [])
        if isinstance(artifact_tables, list):
            artifact_by_id = {
                item.get("table_id"): item for item in artifact_tables
                if isinstance(item, dict) and item.get("table_id")
            }
            for table_id, table in table_by_id.items():
                artifact_table = artifact_by_id.get(table_id)
                if artifact_table is None:
                    add(
                        "table_missing_from_table_artifact",
                        "error",
                        "provenance",
                        "Combined document table is missing from tables JSON.",
                        "Regenerate the combined and tables artifacts together.",
                        table_id=table_id,
                        page_index=table.get("page_index"),
                    )
                elif (
                    artifact_table.get("raw_rows") != table.get("raw_rows")
                    or artifact_table.get("headers") != table.get("headers")
                    or artifact_table.get("rows") != table.get("rows")
                ):
                    add(
                        "table_artifact_content_mismatch",
                        "error",
                        "provenance",
                        "Table data differs between combined and tables JSON.",
                        "Regenerate both artifacts from the same extraction.",
                        table_id=table_id,
                        page_index=table.get("page_index"),
                    )

    for logical_table in logical_tables:
        if not isinstance(logical_table, dict):
            continue
        logical_id = logical_table.get("logical_table_id")
        for fragment in logical_table.get("source_fragments", []):
            fragment_id = (
                fragment.get("table_id") if isinstance(fragment, dict) else None
            )
            if fragment_id not in table_by_id:
                add(
                    "logical_table_fragment_missing",
                    "error",
                    "tables",
                    "Logical table source fragment does not resolve to a table.",
                    "Restore the page-level source fragment record.",
                    logical_table_id=logical_id,
                    table_id=fragment_id,
                )
        for row in logical_table.get("rows", []):
            if not isinstance(row, dict):
                continue
            table_id = row.get("source_fragment_id")
            table = table_by_id.get(table_id, {})
            raw_row_index = row.get("source_raw_row_index")
            source_row = row.get("source_row_index")
            source_page_index = row.get("source_page_index")
            if (
                not isinstance(raw_row_index, int)
                or raw_row_index < 0
                or raw_row_index >= len(table.get("raw_rows", []))
            ):
                add(
                    "logical_table_row_source_invalid",
                    "error",
                    "tables",
                    "Logical row source_raw_row_index does not resolve to a "
                    "raw row in its source fragment.",
                    "Restore the original fragment row reference.",
                    logical_table_id=logical_id,
                    table_id=table_id,
                    row_index=source_row,
                    page_index=source_page_index,
                )

    relationship_id_counts = Counter(
        item.get("relationship_id")
        for item in relationships
        if isinstance(item, dict)
        and isinstance(item.get("relationship_id"), str)
        and item.get("relationship_id")
    )
    for relationship_id, count in relationship_id_counts.items():
        if count > 1:
            add(
                "duplicate_relationship_id",
                "error",
                "relationships",
                f"Relationship ID {relationship_id!r} occurs {count} times.",
                "Regenerate unique relationship identifiers.",
                relationship_id=relationship_id,
            )

    for relationship in relationships:
        if not isinstance(relationship, dict):
            continue
        source_id = relationship.get("source_id")
        target_id = relationship.get("target_id")
        status = relationship.get("relation_status")
        if source_id not in element_ids:
            add(
                "relationship_source_missing",
                "error",
                "relationships",
                "Relationship source does not resolve to an indexed element.",
                "Restore the source element or remove only the invalid relation.",
                element_id=source_id,
                relationship_id=relationship.get("relationship_id"),
            )
        if target_id is not None and target_id not in element_ids:
            add(
                "relationship_target_missing",
                "error",
                "relationships",
                "Relationship target does not resolve to an indexed element.",
                "Restore the target element or preserve the relation as unresolved.",
                element_id=source_id,
                target_id=target_id,
                relationship_id=relationship.get("relationship_id"),
            )
        if status not in {"confirmed", "candidate", "unresolved"}:
            add(
                "relationship_status_invalid",
                "error",
                "relationships",
                "Relationship status must be confirmed, candidate, or unresolved.",
                "Use the existing certainty vocabulary and keep its evidence.",
                element_id=source_id,
                relationship_id=relationship.get("relationship_id"),
            )
        evidence = relationship.get("evidence")
        if not isinstance(evidence, dict) or not evidence:
            add(
                "relationship_evidence_missing",
                "warning",
                "relationships",
                "Relationship has no supporting evidence record.",
                "Record the extraction evidence and keep uncertain links as candidates.",
                element_id=source_id,
                relationship_id=relationship.get("relationship_id"),
            )
        if (
            relationship.get("relationship_id")
            and not isinstance(relationship.get("relationship_id"), str)
        ):
            add(
                "relationship_id_invalid",
                "error",
                "relationships",
                "Relationship ID must be text.",
                "Regenerate stable relationship identifiers.",
                element_id=source_id,
            )
        source = element_ids.get(source_id, {})
        target = element_ids.get(target_id, {}) if target_id is not None else {}
        if (
            evidence
            and evidence.get("same_page") is True
            and source
            and target
            and source.get("page_index") != target.get("page_index")
        ):
            add(
                "relationship_page_conflict",
                "error",
                "relationships",
                "Relationship evidence claims same-page content but its "
                "endpoints are on different pages.",
                "Correct the endpoint or its page evidence.",
                element_id=source_id,
                target_id=target_id,
                page_index=source.get("page_index"),
                page_number=source.get("page_number"),
                relationship_id=relationship.get("relationship_id"),
            )

    assets_by_id: dict[str, dict[str, Any]] = {}
    for asset in image_assets:
        if not isinstance(asset, dict):
            continue
        asset_id = asset.get("asset_id")
        if not asset_id:
            add(
                "image_asset_id_missing",
                "error",
                "images",
                "An extracted image asset has no asset ID.",
                "Regenerate image asset identifiers.",
            )
            continue
        if asset_id in assets_by_id:
            add(
                "duplicate_image_asset_id",
                "error",
                "images",
                f"Image asset ID {asset_id!r} is duplicated.",
                "Regenerate unique asset identifiers.",
                asset_id=asset_id,
            )
        assets_by_id[asset_id] = asset
        asset_path = _safe_relative_file(document_directory, asset.get("path"))
        if asset_path is None:
            add(
                "image_asset_path_invalid",
                "error",
                "images",
                "Image asset path is missing or escapes the document folder.",
                "Restore a safe document-relative asset path.",
                asset_id=asset_id,
            )
        elif not asset_path.is_file():
            add(
                "image_asset_missing",
                "error",
                "images",
                f"Image asset file is missing: {asset.get('path')}",
                "Restore or re-extract the image asset.",
                asset_id=asset_id,
            )
    image_by_id: dict[str, dict[str, Any]] = {}
    for image in extraction_images:
        if not isinstance(image, dict):
            continue
        image_id = image.get("image_id")
        if not image_id:
            add(
                "image_occurrence_id_missing",
                "error",
                "images",
                "An image occurrence has no image ID.",
                "Regenerate a stable ID for each image placement.",
            )
            continue
        if image_id in image_by_id:
            add(
                "duplicate_image_occurrence_id",
                "error",
                "images",
                f"Image occurrence ID {image_id!r} is duplicated.",
                "Regenerate unique IDs for image placements.",
                element_id=image_id,
                page_index=image.get("page_index"),
            )
        image_by_id[image_id] = image
        if image.get("asset_id") not in assets_by_id:
            add(
                "image_asset_reference_missing",
                "error",
                "images",
                "Image occurrence points to an unknown image asset.",
                "Restore the referenced shared image asset.",
                element_id=image_id,
                asset_id=image.get("asset_id"),
                page_index=image.get("page_index"),
                page_number=image.get("page_number"),
            )
        elif image.get("bbox") is None and not image.get("bboxes"):
            add(
                "image_occurrence_bbox_missing",
                "warning",
                "provenance",
                "Image occurrence has no placement rectangle.",
                "Review image placement against the source PDF.",
                element_id=image_id,
                page_index=image.get("page_index"),
                page_number=image.get("page_number"),
            )
        else:
            asset = assets_by_id[image["asset_id"]]
            if image.get("path") != asset.get("path"):
                add(
                    "image_occurrence_path_mismatch",
                    "error",
                    "images",
                    "Image occurrence path differs from its shared asset path.",
                    "Regenerate the occurrence reference from the asset record.",
                    element_id=image_id,
                    asset_id=image.get("asset_id"),
                    page_index=image.get("page_index"),
                )
            if (
                asset.get("sha256")
                and image.get("sha256") != asset.get("sha256")
            ):
                add(
                    "image_occurrence_hash_mismatch",
                    "error",
                    "images",
                    "Image occurrence hash differs from its shared asset hash.",
                    "Restore the matching asset hash provenance.",
                    element_id=image_id,
                    asset_id=image.get("asset_id"),
                    page_index=image.get("page_index"),
                )
        image_page_index = image.get("page_index")
        if (
            not isinstance(image_page_index, int)
            or isinstance(image_page_index, bool)
            or image_page_index < 0
            or (
                isinstance(page_count, int)
                and image_page_index >= page_count
            )
            or image.get("page_number") != image_page_index + 1
        ):
            add(
                "image_occurrence_page_invalid",
                "error",
                "provenance",
                "Image occurrence page index/number is invalid or inconsistent.",
                "Restore its zero-based index and one-based page number.",
                element_id=image_id,
                page_index=image_page_index,
                page_number=image.get("page_number"),
            )
        if not _valid_bbox(image.get("bbox")) or any(
            not _valid_bbox(box) for box in image.get("bboxes", [])
        ):
            add(
                "image_occurrence_bbox_invalid",
                "error",
                "images",
                "Image occurrence has an invalid placement bounding box.",
                "Retain valid source placement geometry.",
                element_id=image_id,
                page_index=image.get("page_index"),
                page_number=image.get("page_number"),
            )
        if not any(
            relation.get("target_id") == image_id
            and relation.get("relationship") in {"caption_for", "nearby_context"}
            for relation in relationships
            if isinstance(relation, dict)
        ):
            add(
                "image_without_context",
                "info",
                "relationships",
                "Image has no caption or contextual text relationship.",
                "Review the figure manually; do not assign nearby text without evidence.",
                element_id=image_id,
                page_index=image.get("page_index"),
                page_number=image.get("page_number"),
                asset_id=image.get("asset_id"),
            )

    for element_id, element in element_ids.items():
        content_ref = element.get("content_ref")
        if not isinstance(content_ref, dict):
            add(
                "element_content_reference_missing",
                "error",
                "provenance",
                "Indexed element has no reference to its source artifact.",
                "Restore the source artifact and record locator.",
                element_id=element_id,
                page_index=element.get("page_index"),
                page_number=element.get("page_number"),
            )
            continue
        if element.get("element_type") == "table":
            if content_ref.get("artifact") != document.get("tables_file"):
                add(
                    "element_table_artifact_mismatch",
                    "error",
                    "provenance",
                    "Table element points to the wrong source artifact.",
                    "Set its content_ref artifact to the document tables JSON.",
                    element_id=element_id,
                    table_id=content_ref.get("table_id"),
                    page_index=element.get("page_index"),
                )
            table_id = content_ref.get("table_id")
            if table_id not in table_by_id:
                add(
                    "element_table_reference_missing",
                    "error",
                    "provenance",
                    "Element table reference does not resolve to a table.",
                    "Restore the referenced table record.",
                    element_id=element_id,
                    table_id=table_id,
                    page_index=element.get("page_index"),
                )
        elif element.get("element_type") == "image":
            if content_ref.get("artifact") != document.get("extraction_file"):
                add(
                    "element_image_artifact_mismatch",
                    "error",
                    "provenance",
                    "Image element points to the wrong source artifact.",
                    "Set its content_ref artifact to the extraction JSON.",
                    element_id=element_id,
                    page_index=element.get("page_index"),
                )
            image_id = content_ref.get("image_id")
            if image_id not in image_by_id:
                add(
                    "element_image_reference_missing",
                    "error",
                    "provenance",
                    "Element image reference does not resolve to an occurrence.",
                    "Restore the referenced image occurrence.",
                    element_id=element_id,
                    page_index=element.get("page_index"),
                )
        elif element.get("element_type") == "link":
            if content_ref.get("artifact") != document.get("extraction_file"):
                add(
                    "element_link_artifact_mismatch",
                    "error",
                    "provenance",
                    "Link element points to the wrong source artifact.",
                    "Set its content_ref artifact to the extraction JSON.",
                    element_id=element_id,
                    page_index=element.get("page_index"),
                )
            link_id = content_ref.get("link_id")
            if link_id not in links:
                add(
                    "element_link_reference_missing",
                    "error",
                    "provenance",
                    "Link element reference does not resolve to an extracted link.",
                    "Restore the source link occurrence.",
                    element_id=element_id,
                    link_id=link_id,
                    page_index=element.get("page_index"),
                )
            elif (
                element.get("target_uri") != links[link_id].get("target_uri")
                or element.get("bbox") != links[link_id].get("source_bbox")
            ):
                add(
                    "element_link_provenance_mismatch",
                    "error",
                    "provenance",
                    "Link element target or bounding box differs from its "
                    "extracted link source.",
                    "Regenerate the indexed link from the extraction record.",
                    element_id=element_id,
                    link_id=link_id,
                    page_index=element.get("page_index"),
                )
        else:
            if content_ref.get("artifact") != document.get("extraction_file"):
                add(
                    "element_text_artifact_mismatch",
                    "error",
                    "provenance",
                    "Text element points to the wrong source artifact.",
                    "Set its content_ref artifact to the extraction JSON.",
                    element_id=element_id,
                    page_index=element.get("page_index"),
                )
            block_id = content_ref.get("element_id")
            if block_id not in text_blocks:
                add(
                    "element_text_reference_missing",
                    "error",
                    "provenance",
                    "Text element reference does not resolve to an extracted block.",
                    "Restore the source text block or its locator.",
                    element_id=element_id,
                    page_index=element.get("page_index"),
                )
            elif text_blocks[block_id].get(
                "page_index",
                element.get("page_index"),
            ) != element.get("page_index"):
                add(
                    "element_text_page_mismatch",
                    "error",
                    "provenance",
                    "Text element page differs from its source text block.",
                    "Regenerate the element page locator from extraction.",
                    element_id=element_id,
                    page_index=element.get("page_index"),
                    page_number=element.get("page_number"),
                )
            else:
                block_ref = content_ref
                ref_page = block_ref.get("page_index")
                ref_block = block_ref.get("block_index")
                if isinstance(ref_page, int) and ref_page not in pages_by_index:
                    add(
                        "element_content_page_missing",
                        "error",
                        "provenance",
                        "Element content reference page does not resolve.",
                        "Correct the source page locator.",
                        element_id=element_id,
                        page_index=ref_page,
                    )
                elif isinstance(ref_block, int):
                    blocks = pages_by_index.get(ref_page, {}).get(
                        "text_blocks", []
                    )
                    if (
                        ref_block < 0
                        or ref_block >= len(blocks)
                        or blocks[ref_block].get("block_id") != block_id
                    ):
                        add(
                            "element_content_block_locator_invalid",
                            "error",
                            "provenance",
                            "Element content_ref block index does not resolve "
                            "to the referenced text block.",
                            "Correct the source block locator.",
                            element_id=element_id,
                            page_index=ref_page,
                        )

    unit_counts = Counter(
        unit.get("unit_id")
        for unit in units
        if isinstance(unit, dict) and unit.get("unit_id")
    )
    for unit_id, count in unit_counts.items():
        if count > 1:
            add(
                "duplicate_rag_unit_id",
                "error",
                "provenance",
                f"Retrieval unit ID {unit_id!r} occurs {count} times.",
                "Regenerate unique retrieval-unit identifiers.",
                unit_id=unit_id,
            )
    units_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for unit in units:
        if not isinstance(unit, dict):
            continue
        unit_id = unit.get("unit_id")
        source_id = unit.get("source_element_id")
        if isinstance(source_id, str):
            units_by_source[source_id].append(unit)
        if not unit_id:
            add(
                "rag_unit_id_missing",
                "error",
                "provenance",
                "Retrieval unit has no stable unit ID.",
                "Regenerate the unit identifier from its source and row.",
                element_id=source_id,
            )
        if source_id not in element_ids:
            add(
                "rag_unit_source_missing",
                "error",
                "provenance",
                "Retrieval unit source element does not resolve.",
                "Restore its source element or correct the unit reference.",
                unit_id=unit_id,
                element_id=source_id,
                page_index=unit.get("page_index"),
                page_number=unit.get("page_number"),
            )
        elif element_ids[source_id].get("page_index") != page_index:
            add(
                "rag_unit_source_page_mismatch",
                "error",
                "provenance",
                "Retrieval unit page_index differs from its source element.",
                "Restore page provenance from the referenced source element.",
                unit_id=unit_id,
                element_id=source_id,
                page_index=page_index,
                page_number=unit.get("page_number"),
            )
        if (
            unit.get("document_id") != document_id
            or unit.get("source_filename") != source_filename
        ):
            add(
                "rag_unit_provenance_missing",
                "error",
                "provenance",
                "Retrieval unit document ID or source filename is invalid.",
                "Restore exact source document provenance.",
                unit_id=unit_id,
                element_id=source_id,
                page_index=unit.get("page_index"),
            )
        page_index = unit.get("page_index")
        if (
            not isinstance(page_index, int)
            or isinstance(page_index, bool)
            or page_index < 0
            or (isinstance(page_count, int) and page_index >= page_count)
        ):
            add(
                "rag_unit_page_index_invalid",
                "error",
                "provenance",
                "Retrieval unit does not have a valid zero-based page index.",
                "Restore page index and one-based page number independently.",
                unit_id=unit_id,
                element_id=source_id,
                page_index=page_index,
            )
        elif (
            isinstance(unit.get("page_number"), int)
            and unit["page_number"] != page_index + 1
        ):
            add(
                "rag_unit_page_number_mismatch",
                "error",
                "provenance",
                "Retrieval unit page_number does not match page_index + 1.",
                "Correct the page citation without changing the index convention.",
                unit_id=unit_id,
                element_id=source_id,
                page_index=page_index,
                page_number=unit.get("page_number"),
            )
        if not _valid_bbox(unit.get("bbox")):
            add(
                "rag_unit_bbox_invalid",
                "error",
                "provenance",
                "Retrieval unit bounding box is invalid.",
                "Restore the source element's valid bounding box.",
                unit_id=unit_id,
                element_id=source_id,
                page_index=page_index,
            )
        text = unit.get("text")
        if unit.get("kind") != "image" and (
            not isinstance(text, str) or not text.strip()
        ):
            add(
                "rag_unit_text_empty",
                "warning",
                "content_quality",
                "Retrieval unit has empty or whitespace-only text.",
                "Review extraction quality before indexing this unit.",
                unit_id=unit_id,
                element_id=source_id,
                page_index=page_index,
                page_number=unit.get("page_number"),
            )
        elif isinstance(text, str):
            unusual_word_forms = sorted(set(
                _SUSPICIOUS_WORD_ENDING.findall(text)
            ))
            if unusual_word_forms:
                add(
                    "possible_wordform_extraction_anomaly",
                    "info",
                    "content_quality",
                    "Text contains an unusual word ending that may be a "
                    "source typo or extraction artifact: "
                    + ", ".join(f"`{word}`" for word in unusual_word_forms),
                    "Compare each word with the original PDF; this heuristic "
                    "does not establish that the source wording is incorrect.",
                    unit_id=unit_id,
                    element_id=source_id,
                    page_index=page_index,
                    page_number=unit.get("page_number"),
                )
            if _REPLACEMENT_CHAR in text or _CONTROL_CHARACTERS.search(text):
                add(
                    "text_encoding_artifact",
                    "warning",
                    "content_quality",
                    "Text contains a replacement or non-printing control character.",
                    "Compare this text with the source PDF; do not auto-correct it.",
                    unit_id=unit_id,
                    element_id=source_id,
                    page_index=page_index,
                    page_number=unit.get("page_number"),
                )
            if unit.get("kind") == "text" and 0 < len(text.strip()) < 12:
                add(
                    "text_unit_suspiciously_short",
                    "info",
                    "content_quality",
                    "Text retrieval unit is unusually short and may lack context.",
                    "Confirm whether it is a valid heading, label, or fragment.",
                    unit_id=unit_id,
                    element_id=source_id,
                    page_index=page_index,
                    page_number=unit.get("page_number"),
                )
            if len(text) > 6000:
                add(
                    "text_unit_suspiciously_long",
                    "warning",
                    "content_quality",
                    "Text retrieval unit is over 6,000 characters and may "
                    "combine unrelated content.",
                    "Review its source block boundaries before indexing.",
                    unit_id=unit_id,
                    element_id=source_id,
                    page_index=page_index,
                    page_number=unit.get("page_number"),
                )
            if (
                unit.get("kind") == "text"
                and unit.get("source_element_type") in {"paragraph", "list"}
                and len(text.strip()) > 120
                and not unit.get("section_context")
            ):
                add(
                    "text_section_context_missing",
                    "info",
                    "content_quality",
                    "Long text unit has no extracted section context.",
                    "Review whether a nearby heading is explicitly supported; "
                    "do not assign context based on proximity alone.",
                    unit_id=unit_id,
                    element_id=source_id,
                    page_index=page_index,
                    page_number=unit.get("page_number"),
                )
            if (
                unit.get("source_element_type") == "list"
                and _INCOMPLETE_STEP_ENDING.search(text.strip())
            ):
                add(
                    "possible_incomplete_procedure_step",
                    "info",
                    "content_quality",
                    "List text ends with punctuation or a connector that may "
                    "indicate an incomplete procedure step.",
                    "Compare the step with the original PDF; no completion was inferred.",
                    unit_id=unit_id,
                    element_id=source_id,
                    page_index=page_index,
                    page_number=unit.get("page_number"),
                )
        if unit.get("kind") == "table_row":
            table_id = unit.get("table_id", source_id)
            table = table_by_id.get(table_id)
            if table is None:
                add(
                    "rag_table_reference_missing",
                    "error",
                    "tables",
                    "Table-row unit table_id does not resolve to a table.",
                    "Restore its page-level or logical-table source fragment.",
                    unit_id=unit_id,
                    table_id=table_id,
                    row_index=unit.get("row_index"),
                    page_index=page_index,
                )
            elif unit.get("logical_table_id"):
                logical = next(
                    (
                        item for item in logical_tables
                        if isinstance(item, dict)
                        and item.get("logical_table_id")
                        == unit.get("logical_table_id")
                    ),
                    None,
                )
                raw_index = unit.get("source_row_index")
                if logical is None or not isinstance(raw_index, int):
                    add(
                        "rag_logical_table_reference_invalid",
                        "error",
                        "tables",
                        "Logical table-row unit has no matching logical table "
                        "or source row index.",
                        "Restore its logical-table row provenance.",
                        unit_id=unit_id,
                        table_id=table_id,
                        row_index=unit.get("row_index"),
                        page_index=page_index,
                    )
                elif raw_index < 0 or raw_index >= len(table.get("raw_rows", [])):
                    add(
                        "rag_table_raw_row_reference_invalid",
                        "error",
                        "tables",
                        "Logical row source_row_index does not resolve to a raw "
                        "row in its page fragment.",
                        "Correct the page-fragment row locator.",
                        unit_id=unit_id,
                        table_id=table_id,
                        row_index=raw_index,
                        page_index=page_index,
                    )
            else:
                row_index = unit.get("row_index")
                if (
                    not isinstance(row_index, int)
                    or row_index < 0
                    or row_index >= len(table.get("rows", []))
                ):
                    add(
                        "rag_table_row_reference_invalid",
                        "error",
                        "tables",
                        "Table-row unit row_index does not resolve to a normalized row.",
                        "Correct the normalized row locator.",
                        unit_id=unit_id,
                        table_id=table_id,
                        row_index=row_index,
                        page_index=page_index,
                    )
                elif unit.get("row") != table["rows"][row_index]:
                    add(
                        "rag_table_row_content_mismatch",
                        "error",
                        "tables",
                        "Table-row unit content does not match its referenced "
                        "normalized source row.",
                        "Regenerate the unit from its source table row.",
                        unit_id=unit_id,
                        table_id=table_id,
                        row_index=row_index,
                        page_index=page_index,
                    )
        if unit.get("kind") == "image":
            image = image_by_id.get(source_id)
            if not image or image.get("asset_id") != unit.get("asset_id"):
                add(
                    "rag_image_reference_invalid",
                    "error",
                    "images",
                    "Image unit does not resolve to its source occurrence asset.",
                    "Restore the matching image occurrence and asset ID.",
                    unit_id=unit_id,
                    element_id=source_id,
                    asset_id=unit.get("asset_id"),
                    page_index=page_index,
                )
        if unit.get("kind") == "link":
            link = links.get(source_id)
            if not link or unit.get("target_uri") != link.get("target_uri"):
                add(
                    "rag_link_reference_invalid",
                    "error",
                    "links",
                    "Link unit does not resolve to the original link target.",
                    "Preserve the URI directly from its extracted source link.",
                    unit_id=unit_id,
                    element_id=source_id,
                    page_index=page_index,
                )

    for source_id, block in text_blocks.items():
        source_units = units_by_source.get(source_id, [])
        if block.get("included_in_rag_units") is False and source_units:
            add(
                "excluded_text_present_as_rag_unit",
                "error",
                "duplicates",
                "Text block marked excluded from retrieval still has a RAG unit.",
                "Resolve the duplicate policy consistently without deleting raw evidence.",
                element_id=source_id,
                page_index=block.get("page_index"),
                page_number=block.get("page_number"),
            )
        if block.get("included_in_rag_units") is not False and not source_units:
            # Captions may be joined to their image unit, and some old schemas
            # intentionally omit blocks; only report when no relation explains it.
            caption_joined = any(
                relation.get("source_id") == source_id
                and relation.get("relationship") == "caption_for"
                for relation in relationships
                if isinstance(relation, dict)
            )
            if not caption_joined and block.get("kind") != "caption":
                continue
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            if _REPLACEMENT_CHAR in text or _CONTROL_CHARACTERS.search(text):
                add(
                    "extracted_text_encoding_artifact",
                    "warning",
                    "content_quality",
                    "Source text block contains a replacement or control character.",
                    "Compare the unmodified source text against the PDF.",
                    element_id=source_id,
                    page_index=block.get("page_index"),
                    page_number=block.get("page_number"),
                )

    exact_text_sources: dict[str, list[str]] = defaultdict(list)
    for source_id, block in text_blocks.items():
        text = block.get("text")
        if isinstance(text, str) and text.strip():
            exact_text_sources[text.strip()].append(source_id)
    for text, source_ids in exact_text_sources.items():
        if len(source_ids) > 1:
            source_units = [
                unit
                for source_id in source_ids
                for unit in units_by_source.get(source_id, [])
            ]
            add(
                "repeated_extracted_text",
                "info",
                "duplicates",
                f"Identical extracted text occurs in {len(source_ids)} blocks; "
                f"{len(source_units)} corresponding retrieval units remain.",
                "Confirm repeated headings or instructions are intentional; "
                "preserve unique context.",
                element_ids=source_ids,
                page_indexes=sorted({
                    block.get("page_index")
                    for source_id, block in text_blocks.items()
                    if source_id in source_ids
                    and isinstance(block.get("page_index"), int)
                }),
                text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            )

    relationship_pairs = {
        (item.get("source_id"), item.get("target_id"), item.get("relationship"))
        for item in relationships
        if isinstance(item, dict)
    }
    table_text_candidates = 0
    for table in tables:
        table_id = table.get("table_id")
        values = [
            str(value)
            for row in table.get("raw_rows", [])
            if isinstance(row, list)
            for value in row
            if value is not None
        ]
        table_tokens = set(_tokens(" ".join(values)))
        duplicate_metadata = table.get("duplicate_detection", {})
        matched = {
            item.get("text_block_id"): item
            for item in duplicate_metadata.get("matched_text_blocks", [])
            if isinstance(item, dict)
        }
        for source_id, block in text_blocks.items():
            if block.get("page_number") != table.get("page_number"):
                continue
            block_tokens = _tokens(block.get("text", ""))
            if len(block_tokens) < 4 or not table_tokens:
                continue
            overlap = sum(token in table_tokens for token in block_tokens)
            ratio = overlap / len(block_tokens)
            if ratio < 0.8:
                continue
            table_text_candidates += 1
            linked = (
                source_id,
                table_id,
                "possible_duplicate_of_table",
            ) in relationship_pairs
            if not linked and source_id not in matched:
                add(
                    "table_text_overlap_unrecorded",
                    "warning",
                    "duplicates",
                    f"Text block overlaps {ratio:.0%} of its tokens with table "
                    "cells but no duplicate relationship records the evidence.",
                    "Review whether it is an exact duplicate, useful context, "
                    "caption, or uncertain overlap; do not discard it automatically.",
                    element_id=source_id,
                    table_id=table_id,
                    page_index=block.get("page_index"),
                    page_number=block.get("page_number"),
                    overlap_ratio=round(ratio, 3),
                )
            elif source_id in matched:
                included = bool(units_by_source.get(source_id))
                add(
                    "table_text_overlap_review",
                    "info",
                    "duplicates",
                    f"Text/table token overlap is {ratio:.0%}; matching block "
                    f"is {'retained' if included else 'excluded'} from RAG units.",
                    "Keep captions and unique surrounding context; exclude only "
                    "near-exact duplicate text while retaining extraction evidence.",
                    element_id=source_id,
                    table_id=table_id,
                    page_index=block.get("page_index"),
                    page_number=block.get("page_number"),
                    overlap_ratio=round(ratio, 3),
                    retained_in_rag=included,
                    content_removed=duplicate_metadata.get("content_removed"),
                )

    if not links:
        add(
            "hyperlink_association_unavailable",
            "info",
            "links",
            "No extracted page-level hyperlink records were found.",
            "If the PDF contains links, preserve their URLs and source rectangles "
            "during extraction.",
        )
    for link_id, link in links.items():
        target_uri = link.get("target_uri")
        if not target_uri:
            continue
        link_element = element_ids.get(link_id)
        relations = [
            item for item in relationships
            if isinstance(item, dict)
            and item.get("relationship") == "hyperlink_in_text"
            and item.get("target_id") == link_id
        ]
        if not relations:
            add(
                "hyperlink_section_association_uncertain",
                "warning",
                "links",
                "URL is preserved, but no text/section relationship is recorded.",
                "Associate it only when the link annotation overlaps relevant "
                "text or other explicit evidence; otherwise retain as unresolved.",
                link_id=link_id,
                element_id=link_id if link_element else None,
                page_index=link.get("source_page", 1) - 1,
                page_number=link.get("source_page"),
                target_uri=target_uri,
            )
        elif all(item.get("relation_status") != "confirmed" for item in relations):
            add(
                "hyperlink_section_association_candidate",
                "info",
                "links",
                "URL has only candidate text associations; its section meaning "
                "is not treated as certain.",
                "Confirm the link's semantic association against surrounding text.",
                link_id=link_id,
                element_id=link_id if link_element else None,
                page_index=link.get("source_page", 1) - 1,
                page_number=link.get("source_page"),
                target_uri=target_uri,
            )

    raw_upstream_warnings = (
        document.get("validation", {}).get("extraction_warnings", [])
        if isinstance(document.get("validation"), dict)
        else []
    )
    for warning in raw_upstream_warnings:
        if isinstance(warning, dict):
            add(
                f"upstream_{warning.get('code', 'extraction_warning')}",
                "warning",
                "content_quality",
                warning.get("message", warning.get(
                    "warning", "Ingestion recorded an extraction warning."
                )),
                "Review the upstream extraction warning; audit findings do not "
                "rewrite extracted technical content.",
                element_id=warning.get("element_id"),
                page_index=warning.get("page_index"),
                page_number=warning.get("page_number"),
            )

    severity_counts = Counter(item["severity"] for item in findings)
    category_counts = Counter(item["category"] for item in findings)
    report = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "audit_timestamp": datetime.now(timezone.utc).isoformat(),
        "document_id": document_id,
        "source_filename": source_filename,
        "document_json": main_path.name,
        "overall_status": _severity_status(findings),
        "summary": {
            "finding_count": len(findings),
            "by_severity": {
                severity: severity_counts.get(severity, 0)
                for severity in ("error", "warning", "info")
            },
            "by_category": dict(sorted(category_counts.items())),
            "tables_checked": len(tables),
            "elements_checked": len(elements),
            "relationships_checked": len(relationships),
            "rag_units_checked": len(units),
            "hyperlinks_checked": len(links),
            "table_text_overlaps_checked": table_text_candidates,
        },
        "findings": findings,
    }
    output_directory = (
        Path(report_directory) if report_directory is not None
        else document_directory
    )
    output_directory.mkdir(parents=True, exist_ok=True)
    report_json = output_directory / "knowledge-audit.json"
    report_markdown = output_directory / "knowledge-audit.md"
    report_json.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    report_markdown.write_text(
        render_markdown_report(report),
        encoding="utf-8",
    )
    report["report_json"] = str(report_json)
    report["report_markdown"] = str(report_markdown)
    return report


def render_markdown_report(report: dict[str, Any]) -> str:
    """Format an audit report for human review."""
    summary = report.get("summary", {})
    lines = [
        f"# Knowledge-unit audit: {report.get('source_filename') or 'Unknown document'}",
        "",
        f"- **Document ID:** `{report.get('document_id') or 'missing'}`",
        f"- **Status:** `{report.get('overall_status', 'unknown')}`",
        f"- **Audit timestamp:** {report.get('audit_timestamp', 'unknown')}",
        f"- **Findings:** {summary.get('finding_count', 0)}",
        "",
        "## Counts",
        "",
        "| Severity | Count |",
        "|---|---:|",
    ]
    for severity in ("error", "warning", "info"):
        lines.append(
            f"| {severity} | {summary.get('by_severity', {}).get(severity, 0)} |"
        )
    lines.extend(["", "## Findings", ""])
    if not report.get("findings"):
        lines.append("No findings.")
    for index, finding in enumerate(report.get("findings", []), start=1):
        location_parts = []
        for label, key in (
            ("page", "page_number"),
            ("page index", "page_index"),
            ("element", "element_id"),
            ("table", "table_id"),
            ("row", "row_index"),
            ("link", "link_id"),
            ("asset", "asset_id"),
            ("unit", "unit_id"),
        ):
            if finding.get(key) is not None:
                location_parts.append(f"{label} `{finding[key]}`")
        if finding.get("target_uri"):
            location_parts.append(f"URL `{finding['target_uri']}`")
        location = ", ".join(location_parts) or "document-level"
        lines.extend([
            f"### {index}. [{finding['severity'].upper()}] "
            f"{finding['code']}",
            "",
            f"- **Category:** {finding['category']}",
            f"- **Location:** {location}",
            f"- **Finding:** {finding['message']}",
            f"- **Recommended review:** {finding['recommendation']}",
            "",
        ])
    lines.extend([
        "## Scope and limitations",
        "",
        "This report validates extracted structure, references, provenance, "
        "and deterministic content-quality signals. It does not verify the "
        "technical correctness of the source PDF, compare every word against "
        "the PDF visually, make live URL requests, infer missing cell values, "
        "or establish semantic image/link meaning from proximity alone.",
        "",
    ])
    return "\n".join(lines)


def _write_execution_failure_report(
    document_file: Path,
    error: Exception,
) -> dict[str, Any]:
    """Persist a report when the document cannot be parsed or audited."""
    document_id = None
    source_filename = document_file.name
    report = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "audit_timestamp": datetime.now(timezone.utc).isoformat(),
        "document_id": document_id,
        "source_filename": source_filename,
        "document_json": document_file.name,
        "overall_status": "failed",
        "summary": {
            "finding_count": 1,
            "by_severity": {"error": 1, "warning": 0, "info": 0},
            "by_category": {"execution": 1},
        },
        "findings": [{
            "code": "audit_execution_failed",
            "severity": "error",
            "category": "execution",
            "message": f"{type(error).__name__}: {error}",
            "recommendation": (
                "Repair or restore the referenced JSON artifacts, then rerun "
                "the audit."
            ),
            "document_id": document_id,
            "source_filename": source_filename,
        }],
    }
    report["report_json"] = str(
        document_file.parent / "knowledge-audit.json"
    )
    report["report_markdown"] = str(
        document_file.parent / "knowledge-audit.md"
    )
    Path(report["report_json"]).write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    Path(report["report_markdown"]).write_text(
        render_markdown_report(report),
        encoding="utf-8",
    )
    return report


def _combined_document_files(root: Path) -> list[Path]:
    candidates = []
    for path in sorted(root.rglob("*.json")):
        if path.name.startswith("knowledge-audit"):
            continue
        if path.name.endswith((".extraction.json", ".tables.json")):
            continue
        try:
            value = _read_json(path)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if isinstance(value.get("elements"), list) and isinstance(
            value.get("rag_units"), list
        ):
            candidates.append(path)
    return candidates


def _has_combined_document(root: Path) -> bool:
    for candidate in root.glob("*.json"):
        if candidate.name.startswith("knowledge-audit"):
            continue
        if candidate.name.endswith((".extraction.json", ".tables.json")):
            continue
        try:
            value = _read_json(candidate)
        except (OSError, json.JSONDecodeError, ValueError):
            continue
        if isinstance(value.get("elements"), list) and isinstance(
            value.get("rag_units"), list
        ):
            return True
    return False


def audit_processed_documents(
    processed_root: Path = PROCESSED_DATA_DIR,
) -> dict[str, Any]:
    """Audit all combined documents under a processed-data directory."""
    processed_root = Path(processed_root)
    if not processed_root.is_dir():
        raise FileNotFoundError(
            f"Processed-data directory does not exist: {processed_root}"
        )
    document_files = _combined_document_files(processed_root)
    results = []
    for document_file in document_files:
        try:
            report = audit_document(document_file)
            results.append({
                "document_json": str(document_file),
                "document_id": report.get("document_id"),
                "source_filename": report.get("source_filename"),
                "status": report["overall_status"],
                "finding_count": report["summary"]["finding_count"],
                "report_json": report["report_json"],
                "report_markdown": report["report_markdown"],
            })
        except Exception as error:
            report = _write_execution_failure_report(document_file, error)
            results.append({
                "document_json": str(document_file),
                "status": "failed",
                "error": f"{type(error).__name__}: {error}",
                "report_json": report["report_json"],
                "report_markdown": report["report_markdown"],
            })
    counts = Counter(item["status"] for item in results)
    invalid_count = counts.get("invalid", 0)
    summary = {
        "schema_version": AUDIT_SCHEMA_VERSION,
        "audit_timestamp": datetime.now(timezone.utc).isoformat(),
        "processed_root": str(processed_root),
        "document_count": len(document_files),
        "completed_count": sum(item["status"] != "failed" for item in results),
        "failed_count": counts.get("failed", 0),
        "invalid_count": invalid_count,
        "validation_failure_count": invalid_count,
        "execution_failure_count": counts.get("failed", 0),
        "status_counts": dict(sorted(counts.items())),
        "documents": results,
    }
    (processed_root / "knowledge-audit-summary.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    markdown = [
        "# Processed-document knowledge audit",
        "",
        f"- **Documents discovered:** {summary['document_count']}",
        f"- **Audit execution failures:** {summary['failed_count']}",
        "",
        "| Document | Status | Findings | Report |",
        "|---|---|---:|---|",
    ]
    for result in results:
        markdown.append(
            f"| {result.get('source_filename', result['document_json'])} "
            f"| {result['status']} "
            f"| {result.get('finding_count', '—')} "
            f"| {Path(result.get('report_markdown', '')).name or 'not created'} |"
        )
    (processed_root / "knowledge-audit-summary.md").write_text(
        "\n".join(markdown) + "\n",
        encoding="utf-8",
    )
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit processed PDF knowledge units and provenance."
    )
    parser.add_argument(
        "path",
        nargs="?",
        type=Path,
        default=PROCESSED_DATA_DIR,
        help="A combined document JSON, document output folder, or processed root.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Audit every combined document below path.",
    )
    args = parser.parse_args(argv)
    if args.all or args.path.is_dir() and not _has_combined_document(args.path):
        result = audit_processed_documents(args.path)
        print(
            f"Audited {result['completed_count']}/{result['document_count']} "
            f"documents; invalid={result['invalid_count']}, "
            f"execution failures={result['failed_count']}."
        )
        return int(result["failed_count"] > 0 or result["invalid_count"] > 0)
    result = audit_document(args.path)
    print(
        f"{result['source_filename']}: {result['overall_status']} "
        f"({result['summary']['finding_count']} findings)"
    )
    print(result["report_markdown"])
    return int(result["overall_status"] == "invalid")


if __name__ == "__main__":
    raise SystemExit(main())
