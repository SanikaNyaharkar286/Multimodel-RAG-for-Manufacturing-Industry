"""PDF table extraction with provenance, structural header inference, and validation.

PyMuPDF remains the first-pass parser. The JSON output keeps the original table
rows and geometry, while a separate validation record describes uncertain or
suspicious results without silently dropping detected content.
"""

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Sequence

import pymupdf


def clean_cell(value: Any) -> str | None:
    """Collapse whitespace in one cell while keeping blank cells as ``None``."""
    if value is None:
        return None

    value = re.sub(r"\s+", " ", str(value)).strip()
    return value if value else None


def make_unique_headers(headers: Sequence[str | None]) -> list[str]:
    """Give missing or repeated header labels stable, non-colliding keys."""
    result = []
    counts: dict[str, int] = {}

    for index, header in enumerate(headers):
        name = clean_cell(header) or f"column_{index + 1}"
        counts[name] = counts.get(name, 0) + 1

        if counts[name] > 1:
            name = f"{name}_{counts[name]}"

        result.append(name)

    return result


def _filled_cells(row: Sequence[str | None]) -> list[str]:
    """Return non-empty cell text without changing its column positions."""
    return [cell for cell in row if cell]


def _row_coverage(row: Sequence[str | None], column_count: int) -> float:
    """Measure how many table columns contain text in a row."""
    if not column_count:
        return 0.0
    return len(_filled_cells(row)) / column_count


def _is_numeric_cell(value: str) -> bool:
    """Recognize common numeric identifiers without treating them as headers."""
    return bool(re.fullmatch(r"[\d.,/%+-]+", value.strip()))


def _header_candidate(
    rows: Sequence[Sequence[str | None]],
    row_index: int,
    column_count: int,
) -> tuple[float, list[int]]:
    """Score a possible header using row shape and contrast with later rows.

    Unlike the previous fixed-word list, this works with unfamiliar labels.
    A candidate is stronger when it fills distinct columns, looks textual, and
    is followed by consistently populated rows with a different data pattern.
    The score is only a confidence estimate; low-scoring rows remain in output.
    """
    row = rows[row_index]
    values = _filled_cells(row)
    if not values or not column_count:
        return 0.0, []

    # A short, sparse row between a potential header and the body may be a
    # second header level (for example, a parent label over child columns).
    following_indexes = list(
        range(row_index + 1, min(row_index + 3, len(rows)))
    )
    if len(following_indexes) == 2:
        middle = rows[following_indexes[0]]
        body = rows[following_indexes[1]]
        middle_values = _filled_cells(middle)
        if (
            len(middle_values) >= 2
            and _row_coverage(middle, column_count)
            < _row_coverage(row, column_count)
            and all(not _is_numeric_cell(value) for value in middle_values)
            and _row_coverage(body, column_count) >= 0.6
        ):
            following_indexes = list(
                range(row_index + 2, min(row_index + 4, len(rows)))
            )

    # One following row is not enough evidence: it could itself be the first
    # body row of a headerless table. Requiring two rows avoids dropping it.
    if len(following_indexes) < 2:
        return 0.0, []

    # High cell coverage and unique text labels are common to headers, while
    # body rows tend to contain longer or numeric values.
    coverage = _row_coverage(row, column_count)
    unique_ratio = len({value.casefold() for value in values}) / len(values)
    text_ratio = sum(not _is_numeric_cell(value) for value in values) / len(values)
    next_values = [
        value
        for index in following_indexes
        for value in _filled_cells(rows[index])
    ]
    next_coverage = sum(
        _row_coverage(rows[index], column_count)
        for index in following_indexes
    ) / len(following_indexes)

    header_length = sum(len(value) for value in values) / len(values)
    body_length = (
        sum(len(value) for value in next_values) / len(next_values)
        if next_values
        else header_length
    )
    length_contrast = max(
        0.0,
        min(1.0, (body_length - header_length) / max(body_length, 12)),
    )

    score = (
        coverage * 0.28
        + unique_ratio * 0.20
        + text_ratio * 0.18
        + next_coverage * 0.24
        + length_contrast * 0.10
    )
    return score, following_indexes


def looks_like_header(row: Sequence[str | None]) -> bool:
    """Check whether a row has basic header-like structure, without vocabulary.

    This compatibility helper can only assess one row; full extraction uses
    ``_header_candidate`` so it can also compare the row with subsequent data.
    """
    values = _filled_cells(row)
    return (
        len(values) >= 2
        and len({value.casefold() for value in values}) == len(values)
        and all(not _is_numeric_cell(value) for value in values)
    )


def _is_multi_level_header(
    rows: Sequence[Sequence[str | None]],
    parent_index: int,
    column_count: int,
) -> bool:
    """Determine whether a sparse second row is a child-header row."""
    if parent_index + 2 >= len(rows):
        return False

    parent = rows[parent_index]
    child = rows[parent_index + 1]
    child_values = _filled_cells(child)
    body = rows[parent_index + 2]

    return (
        len(child_values) >= 2
        and _row_coverage(child, column_count)
        < _row_coverage(parent, column_count)
        and all(not _is_numeric_cell(value) for value in child_values)
        and _row_coverage(body, column_count) >= 0.6
    )


def _combine_header_levels(
    parent_row: Sequence[str | None],
    child_row: Sequence[str | None],
    column_count: int,
) -> tuple[list[str | None], bool]:
    """Combine a merged parent label with child labels when its span is clear.

    A parent is extended only across adjacent child columns before the next
    explicit parent label. If a parent span cannot be established, the raw
    rows are retained and callers can mark the header as uncertain.
    """
    headers: list[str | None] = [None] * column_count
    parent_positions = [
        index for index, value in enumerate(parent_row[:column_count]) if value
    ]
    child_positions = [
        index for index, value in enumerate(child_row[:column_count]) if value
    ]
    combined_any = False

    for column_index in range(column_count):
        parent_value = (
            parent_row[column_index]
            if column_index < len(parent_row)
            else None
        )
        child_value = (
            child_row[column_index]
            if column_index < len(child_row)
            else None
        )

        previous_parents = [
            index for index in parent_positions if index <= column_index
        ]
        if child_value and previous_parents:
            parent_index = previous_parents[-1]
            next_parents = [
                index for index in parent_positions if index > parent_index
            ]
            span_end = next_parents[0] if next_parents else column_count
            children_in_span = [
                index for index in child_positions
                if parent_index <= index < span_end
            ]

            # Repeating a parent is justified only when it visibly covers two
            # or more child cells, not merely because a neighboring cell is blank.
            if (
                len(children_in_span) >= 2
                and parent_index <= column_index < span_end
            ):
                span_parent = parent_row[parent_index]
                headers[column_index] = f"{span_parent} - {child_value}"
                combined_any = True
                continue

        headers[column_index] = child_value or parent_value

    return headers, combined_any


def extract_merged_title(raw_rows: Sequence[Sequence[str | None]]) -> str | None:
    """Return a first-row merged title only when a later header is supported."""
    if len(raw_rows) < 2:
        return None

    title_cells = _filled_cells(raw_rows[0])
    if len(raw_rows[0]) < 2 or len(title_cells) != 1:
        return None

    width = max((len(row) for row in raw_rows), default=0)
    header_index = next(
        (
            index
            for index in range(1, min(len(raw_rows) - 1, 4))
            if _header_candidate(raw_rows, index, width)[0] >= 0.64
        ),
        None,
    )
    return title_cells[0] if header_index == 1 else None


def _infer_table_structure(
    raw_rows: Sequence[Sequence[str | None]],
) -> tuple[
    str | None,
    list[str],
    list[list[str | None]],
    str,
    list[dict[str, str]],
]:
    """Separate a supported title/header from body rows without losing content.

    Header detection uses the position, fill pattern, uniqueness, and cell
    text contrast of the first few rows. Rows that do not meet the threshold
    are preserved as data and receive an explicit uncertain-header warning.
    """
    rows = [list(row) for row in raw_rows]
    column_count = max((len(row) for row in rows), default=0)
    title = extract_merged_title(rows)
    start_index = 1 if title else 0
    search_end = min(len(rows) - 1, start_index + 4)
    candidate_index = None

    for index in range(start_index, search_end):
        score, _ = _header_candidate(rows, index, column_count)
        if score >= 0.64:
            candidate_index = index
            break

    warnings: list[dict[str, str]] = []
    if candidate_index is None:
        headers = make_unique_headers([None] * column_count)
        if rows:
            warnings.append({
                "code": "unknown_header_structure",
                "message": (
                    "No header row met the structural confidence threshold; "
                    "all extracted rows were preserved as data."
                ),
            })
        return title, headers, rows[start_index:], "unknown_header", warnings

    parent_row = rows[candidate_index]
    if _is_multi_level_header(rows, candidate_index, column_count):
        child_row = rows[candidate_index + 1]
        combined, combined_any = _combine_header_levels(
            parent_row,
            child_row,
            column_count,
        )
        header_source = (
            "detected_multilevel_header" if combined_any
            else "uncertain_multilevel_header"
        )
        header_row_count = 2
        if not combined_any:
            warnings.append({
                "code": "uncertain_multilevel_header",
                "message": (
                    "A possible second header row was found, but parent-to-child "
                    "column relationships could not be reconstructed reliably."
                ),
            })
    else:
        combined = list(parent_row)
        header_source = "detected_header_row"
        header_row_count = 1

    # If malformed rows are wider than the detected header, create explicit
    # placeholders instead of silently truncating the excess cell values.
    combined.extend([None] * max(0, column_count - len(combined)))
    normalized_header_cells = [clean_cell(cell) for cell in combined]
    populated_labels = [
        label.casefold() for label in normalized_header_cells if label
    ]
    if len(populated_labels) != len(normalized_header_cells):
        warnings.append({
            "code": "empty_headers",
            "message": (
                "One or more detected header cells are empty; placeholder "
                "column names were used without changing the raw rows."
            ),
        })
    if len(populated_labels) != len(set(populated_labels)):
        warnings.append({
            "code": "duplicate_headers",
            "message": (
                "Detected header labels repeat; unique output keys were "
                "generated while preserving the original labels in raw_rows."
            ),
        })
    headers = make_unique_headers(combined)
    data_start = candidate_index + header_row_count
    return (
        title,
        headers,
        rows[data_start:],
        header_source,
        warnings,
    )


def is_probably_false_table(raw_rows: Sequence[Sequence[str | None]]) -> bool:
    """Flag a degenerate extraction shape; callers must not discard its rows."""
    if not raw_rows:
        return True

    non_empty_cells = [
        cell for row in raw_rows for cell in row if cell
    ]
    return (
        len(raw_rows) == 1
        and len(non_empty_cells) <= 1
        and bool(non_empty_cells)
        and len(non_empty_cells[0]) > 120
    )


def validate_table(
    raw_rows: Sequence[Sequence[str | None]],
    headers: Sequence[str],
    bbox: Sequence[float] | None,
    page_rect: Sequence[float] | None,
    header_source: str,
) -> dict[str, Any]:
    """Validate extraction shape and return warnings without removing content.

    The result distinguishes an unknown header from a probable page-wide false
    positive. The page rectangle is used only for geometric suspicion, not to
    reject a table; all thresholds are deliberately broad for varied manuals.
    """
    warnings: list[dict[str, str]] = []
    row_lengths = [len(row) for row in raw_rows]
    column_count = max([len(headers), *row_lengths], default=0)
    non_empty_cells = [
        cell for row in raw_rows for cell in row if cell
    ]

    def add_warning(code: str, message: str) -> None:
        warnings.append({"code": code, "message": message})

    if not raw_rows or not non_empty_cells:
        add_warning("empty_table", "PyMuPDF returned no non-empty table cells.")

    if len(set(row_lengths)) > 1:
        add_warning(
            "inconsistent_row_lengths",
            "Rows have different cell counts; shorter rows were padded only "
            "in normalized records, while raw rows remain unchanged.",
        )

    if not headers:
        add_warning("empty_headers", "No column labels or placeholders were produced.")
    elif len({header.casefold() for header in headers}) != len(headers):
        add_warning("duplicate_headers", "Column labels are not unique.")

    if header_source == "unknown_header":
        add_warning(
            "unknown_header_structure",
            "Column labels are placeholders because no header was confidently "
            "identified; extracted rows remain available for review.",
        )
        if len(raw_rows) > 1 and looks_like_header(raw_rows[0]):
            add_warning(
                "potential_misplaced_header",
                "The first row resembles a header, but there was not enough "
                "following-row evidence to classify it confidently.",
            )

    if is_probably_false_table(raw_rows):
        add_warning(
            "single_large_text_cell",
            "The detection contains one unusually large text cell.",
        )

    page_wide = False
    if bbox and page_rect and len(bbox) >= 4 and len(page_rect) >= 4:
        table_width = max(0.0, float(bbox[2]) - float(bbox[0]))
        table_height = max(0.0, float(bbox[3]) - float(bbox[1]))
        page_width = max(0.0, float(page_rect[2]) - float(page_rect[0]))
        page_height = max(0.0, float(page_rect[3]) - float(page_rect[1]))
        page_wide = (
            page_width > 0
            and page_height > 0
            and table_width / page_width >= 0.85
            and table_height / page_height >= 0.85
        )

    if page_wide and (len(raw_rows) <= 2 or len(non_empty_cells) <= 1):
        add_warning(
            "page_wide_text_detection",
            "A nearly full-page bounding box contains too little row structure "
            "to confirm that this is a real table.",
        )

    if column_count > 50:
        add_warning(
            "excessive_column_count",
            f"The extraction contains {column_count} columns.",
        )
    if len(raw_rows) > 200:
        add_warning(
            "excessive_row_count",
            f"The extraction contains {len(raw_rows)} rows.",
        )

    if header_source == "uncertain_multilevel_header":
        add_warning(
            "uncertain_multilevel_header",
            "Multi-level header rows were detected but their relationships "
            "could not be normalized reliably.",
        )

    warning_codes = {warning["code"] for warning in warnings}
    probable_false_positive = (
        page_wide
        and (
            len(raw_rows) <= 2
            or len(non_empty_cells) <= 1
            or "single_large_text_cell" in warning_codes
        )
    )
    if probable_false_positive:
        detection_class = "probable_false_positive"
    elif header_source == "unknown_header":
        detection_class = "unknown_header_structure"
    else:
        detection_class = "table_candidate"

    if probable_false_positive or warning_codes & {
        "empty_table",
        "empty_headers",
        "inconsistent_row_lengths",
        "duplicate_headers",
        "single_large_text_cell",
        "page_wide_text_detection",
        "excessive_column_count",
        "excessive_row_count",
    }:
        status = "suspicious"
    elif warning_codes:
        status = "uncertain"
    else:
        status = "valid"

    return {
        "status": status,
        "detection_class": detection_class,
        "warnings": warnings,
        "fallback_needed": bool(warning_codes),
    }


def extract_table_title(page: Any, bbox: Sequence[float] | None) -> str | None:
    """Return the nearest text block above a table as provisional section text.

    The proximity heuristic does not prove whether the text is a section
    heading or a separate table title; its JSON field and source marker retain
    that limitation rather than presenting the classification as certain.
    """
    if not bbox or len(bbox) < 2:
        return None

    # A page-sized detection has no reliable "above-table" area; page headers
    # and running metadata must not be mistaken for a section heading.
    page_rect = page.rect
    page_width = float(page_rect[2]) - float(page_rect[0])
    page_height = float(page_rect[3]) - float(page_rect[1])
    table_width = float(bbox[2]) - float(bbox[0])
    table_height = float(bbox[3]) - float(bbox[1])
    if (
        page_width > 0
        and page_height > 0
        and table_width / page_width >= 0.85
        and table_height / page_height >= 0.85
    ):
        return None

    table_top = bbox[1]
    candidates = []

    for block in page.get_text("blocks"):
        if len(block) < 5:
            continue
        y1 = block[3]
        text = clean_cell(block[4])

        if text and y1 <= table_top and table_top - y1 <= 100:
            candidates.append({"text": text, "y1": y1})

    if not candidates:
        return None

    # The nearest preceding block is useful context, but cannot reliably tell
    # a section heading from a standalone table title by proximity alone.
    candidates.sort(key=lambda item: item["y1"], reverse=True)
    return candidates[0]["text"]


def _records_from_rows(
    headers: Sequence[str],
    data_rows: Sequence[Sequence[str | None]],
) -> list[dict[str, str | None]]:
    """Map body cells to stable header keys and pad short rows without truncating."""
    records = []
    for row in data_rows:
        aligned = list(row) + [None] * max(0, len(headers) - len(row))
        records.append({
            header: clean_cell(value)
            for header, value in zip(headers, aligned)
        })
    return records


def _fallback_metadata(validation: dict[str, Any]) -> dict[str, Any]:
    """Describe when a local layout-aware fallback should be attempted.

    Coordinate-based reconstruction is exposed as a pending strategy rather
    than applied speculatively: the sample manuals contain false page-wide
    regions but no validated case where span clustering safely improves them.
    """
    if not validation["fallback_needed"]:
        return {
            "needed": False,
            "method": None,
            "status": "not_needed",
            "reason": None,
        }

    reason = "; ".join(
        warning["message"] for warning in validation["warnings"]
    )
    return {
        "needed": True,
        "method": "text_spans_and_coordinates",
        "status": "pending",
        "reason": reason,
    }


def extract_tables(
    pdf_path: Path,
    output_dir: Path,
    output_filename: str = "tables.json",
) -> dict[str, Any]:
    """Extract and validate tables, then write provenance-rich table JSON.

    PyMuPDF's table finder and ``table.extract()`` remain the first pass. Raw
    cell text and table bounds are preserved even when validation marks a
    result suspicious, so downstream review can distinguish uncertain
    extraction from content that was never captured. The default filename is
    retained for existing callers; the document pipeline can give it a
    source-name-based suffix.
    """
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError("Input file must be a PDF.")

    # Hashing the source bytes gives the same stable document identity used by
    # the current ingestion output and lets every table retain source lineage.
    source_hash = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    document_id = source_hash[:16]
    extracted_tables: list[dict[str, Any]] = []
    extraction_warnings: list[dict[str, Any]] = []
    output_dir.mkdir(parents=True, exist_ok=True)

    with pymupdf.open(pdf_path) as doc:
        for page_index, page in enumerate(doc):
            page_number = page_index + 1

            try:
                finder = page.find_tables()
            except Exception as exc:
                extraction_warnings.append({
                    "page_number": page_number,
                    "warning": f"Table detection failed: {exc}",
                })
                continue

            # Keep a table record when extraction raises or returns malformed
            # rows; validation will make the failure visible to downstream users.
            for table_index, table in enumerate(finder.tables, start=1):
                table_warnings: list[dict[str, str]] = []
                try:
                    extracted = table.extract() or []
                except Exception as exc:
                    extracted = []
                    message = f"Table extraction failed: {exc}"
                    table_warnings.append({
                        "code": "table_extraction_error",
                        "message": message,
                    })
                    extraction_warnings.append({
                        "page_number": page_number,
                        "table_index": table_index,
                        "warning": message,
                    })

                # Keep exact cell strings in raw_rows; whitespace cleanup is
                # applied only to the normalized title, headers, and records.
                raw_rows = [
                    [
                        None if cell is None else str(cell)
                        for cell in (row or [])
                    ]
                    for row in extracted
                ]
                normalized_rows = [
                    [clean_cell(cell) for cell in row]
                    for row in raw_rows
                ]
                (
                    table_title,
                    headers,
                    data_rows,
                    header_source,
                    header_warnings,
                ) = _infer_table_structure(normalized_rows)

                bbox_value = getattr(table, "bbox", None)
                bbox = list(bbox_value) if bbox_value else None
                validation = validate_table(
                    raw_rows=normalized_rows,
                    headers=headers,
                    bbox=bbox,
                    page_rect=page.rect,
                    header_source=header_source,
                )
                # Inference and validation can describe the same uncertain
                # header; keep one warning per code so uncertainty is not
                # incorrectly escalated to a failed extraction.
                unique_warnings = []
                warning_codes = set()
                for warning in (
                    table_warnings
                    + header_warnings
                    + validation["warnings"]
                ):
                    if warning["code"] not in warning_codes:
                        unique_warnings.append(warning)
                        warning_codes.add(warning["code"])
                validation["warnings"] = unique_warnings

                # A parser exception means the first-pass result is incomplete,
                # unlike an unknown-but-preserved header, so it is suspicious.
                if table_warnings:
                    validation["status"] = "suspicious"
                    validation["fallback_needed"] = True
                elif warning_codes & {"empty_headers", "duplicate_headers"}:
                    validation["status"] = "suspicious"
                    validation["fallback_needed"] = True
                elif warning_codes and validation["status"] == "valid":
                    validation["status"] = "uncertain"
                    validation["fallback_needed"] = True

                # Keep the old ``section_heading`` field for compatibility,
                # but label its source as provisional because nearby text may
                # be a title rather than a section heading.
                section_heading = extract_table_title(page, bbox)
                records = _records_from_rows(headers, data_rows)

                extracted_tables.append({
                    "table_id": (
                        f"{document_id}_p{page_number:03d}"
                        f"_t{table_index:02d}"
                    ),
                    "document_id": document_id,
                    "source_filename": pdf_path.name,
                    "source_sha256": source_hash,
                    "page_number": page_number,
                    "table_index_on_page": table_index,
                    "bbox": bbox,
                    "section_heading": section_heading,
                    "section_heading_source": (
                        "nearest_text_block_above_table"
                        if section_heading else "unknown"
                    ),
                    "title": table_title,
                    "title_source": (
                        "merged_first_row" if table_title else "unknown"
                    ),
                    "headers": headers,
                    "rows": records,
                    "raw_rows": raw_rows,
                    "header_source": header_source,
                    "extraction_method": "pymupdf",
                    "row_count": len(records),
                    "column_count": len(headers),
                    "validation": validation,
                    "fallback": _fallback_metadata(validation),
                })

    result = {
        "schema_version": 2,
        "document_id": document_id,
        "source_filename": pdf_path.name,
        "source_sha256": source_hash,
        "table_count": len(extracted_tables),
        "tables": extracted_tables,
        "warnings": extraction_warnings,
    }

    output_path = output_dir / output_filename
    with output_path.open("w", encoding="utf-8") as file:
        json.dump(result, file, indent=2, ensure_ascii=False)

    print(f"PDF: {pdf_path.name}")
    print(f"Tables extracted: {len(extracted_tables)}")
    print(f"Warnings: {len(extraction_warnings)}")
    print(f"Saved to: {output_path}")
    return result
