"""PDF table extraction with provenance, structural header inference, and validation.

PyMuPDF remains the first-pass parser. The JSON output keeps the original table
rows and geometry, while a separate validation record describes uncertain or
suspicious results without silently dropping detected content.
"""

import json
import re
from pathlib import Path
from typing import Any, Sequence

import pymupdf

from .pdf_extractor import calculate_file_hash


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

    # A numbered first column followed by consecutive numbers is evidence that
    # the row is data, even when its remaining cells contain descriptive text.
    # This avoids promoting entries such as "7 | Buttons | Power up" to headers.
    first_number = _row_number(row[0] if row else None)
    following_numbers = [
        _row_number(rows[index][0])
        for index in range(row_index + 1, min(row_index + 3, len(rows)))
        if rows[index]
    ]
    if (
        first_number is not None
        and len(following_numbers) >= 2
        and all(number is not None for number in following_numbers)
        and following_numbers == [
            first_number + offset
            for offset in range(1, len(following_numbers) + 1)
        ]
    ):
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


def _expand_vertical_merged_cells(
    raw_rows: Sequence[Sequence[str | None]],
    layout_rows: Sequence[Any],
) -> tuple[list[list[str | None]], list[dict[str, Any]]]:
    """Repeat text only where PyMuPDF geometry proves a vertical cell span.

    PyMuPDF returns ``None`` in continuation slots for vertically merged
    cells. A blank is expanded only when the cell rectangle above it covers
    the full row interval measured from neighboring cells; ordinary empty
    cells therefore remain empty.
    """
    expanded = [list(row) for row in raw_rows]
    adjustments: list[dict[str, Any]] = []
    if len(layout_rows) != len(expanded):
        return expanded, adjustments

    for row_index, layout_row in enumerate(layout_rows):
        cells = getattr(layout_row, "cells", ())
        if not cells:
            continue

        for column_index, value in enumerate(expanded[row_index]):
            if value is not None or column_index >= len(cells):
                continue
            if cells[column_index] is not None:
                continue

            peer_rectangles = [
                cell
                for peer_index, cell in enumerate(cells)
                if peer_index != column_index and cell is not None
            ]
            if not peer_rectangles:
                continue
            row_top = sorted(rect[1] for rect in peer_rectangles)[
                len(peer_rectangles) // 2
            ]
            row_bottom = sorted(rect[3] for rect in peer_rectangles)[
                len(peer_rectangles) // 2
            ]
            if row_bottom <= row_top:
                continue

            for source_row_index in range(row_index - 1, -1, -1):
                source_cells = getattr(
                    layout_rows[source_row_index],
                    "cells",
                    (),
                )
                if column_index >= len(source_cells):
                    continue
                source_rect = source_cells[column_index]
                source_value = (
                    expanded[source_row_index][column_index]
                    if column_index < len(expanded[source_row_index])
                    else None
                )
                if (
                    source_rect is not None
                    and source_value is not None
                    and source_rect[1] <= row_top + 1.0
                    and source_rect[3] >= row_bottom - 1.0
                ):
                    expanded[row_index][column_index] = source_value
                    adjustments.append({
                        "code": "vertical_merged_cell_expanded",
                        "source_row_index": source_row_index,
                        "target_row_index": row_index,
                        "column_index": column_index,
                        "value": source_value,
                        "bbox": list(source_rect),
                    })
                    break

    return expanded, adjustments


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


def _generic_header_count(headers: Sequence[str]) -> int:
    """Count placeholders that reveal PyMuPDF could not identify real labels."""
    return sum(
        bool(re.fullmatch(r"column_\d+(?:_\d+)?", header.strip(), re.I))
        for header in headers
    )


def _row_number(value: Any) -> int | None:
    """Read a simple leading row number without guessing from prose."""
    match = re.fullmatch(r"\s*(\d{1,4})[.)]?\s*", str(value or ""))
    return int(match.group(1)) if match else None


def _normalized_row(row: Sequence[Any]) -> list[str | None]:
    """Normalize raw cells only for comparisons; source rows remain untouched."""
    return [clean_cell(value) for value in row]


def _looks_like_path_artifact(row: Sequence[Any]) -> bool:
    """Recognize a page footer accidentally captured as a table row."""
    text = " ".join(str(value or "") for value in row)
    path_separators = len(re.findall(r"[\\/]", text))
    has_file_suffix = bool(re.search(r"\.[a-z0-9]{2,8}\b", text, re.I))
    has_uri_scheme = bool(re.search(r"\b[a-z][a-z0-9+.-]*://", text, re.I))
    return has_uri_scheme or (path_separators >= 3 and has_file_suffix)


def _column_profiles_compatible(
    previous_rows: Sequence[dict[str, Any]],
    current_rows: Sequence[Sequence[str | None]],
    headers: Sequence[str],
) -> bool:
    """Compare broad value shapes without changing or inventing cell values.

    This is only a continuation clue after geometry and section context have
    matched. It compares cell lengths and numeric share in corresponding
    columns; it does not split flattened text or decide what a cell means.
    """
    if not previous_rows or not current_rows or len(headers) < 2:
        return False

    matches = 0
    compared_columns = 0
    for column_index, header in enumerate(headers):
        previous_values = [
            clean_cell(row.get(header))
            for row in previous_rows
            if clean_cell(row.get(header))
        ]
        current_values = [
            clean_cell(row[column_index])
            for row in current_rows
            if column_index < len(row) and clean_cell(row[column_index])
        ]
        if not previous_values or not current_values:
            continue

        previous_median = sorted(
            len(value) for value in previous_values
        )[len(previous_values) // 2]
        current_median = sorted(
            len(value) for value in current_values
        )[len(current_values) // 2]
        previous_numeric_share = sum(
            _is_numeric_cell(value) for value in previous_values
        ) / len(previous_values)
        current_numeric_share = sum(
            _is_numeric_cell(value) for value in current_values
        ) / len(current_values)
        compared_columns += 1
        if (
            abs(previous_median - current_median)
            <= max(12, max(previous_median, current_median) * 0.7)
            and abs(previous_numeric_share - current_numeric_share) <= 0.35
        ):
            matches += 1

    return compared_columns >= 2 and matches / compared_columns >= 0.75


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
    page_area_ratio = 0.0
    width_ratio = 0.0
    height_ratio = 0.0
    if bbox and page_rect and len(bbox) >= 4 and len(page_rect) >= 4:
        table_width = max(0.0, float(bbox[2]) - float(bbox[0]))
        table_height = max(0.0, float(bbox[3]) - float(bbox[1]))
        page_width = max(0.0, float(page_rect[2]) - float(page_rect[0]))
        page_height = max(0.0, float(page_rect[3]) - float(page_rect[1]))
        width_ratio = table_width / page_width if page_width else 0.0
        height_ratio = table_height / page_height if page_height else 0.0
        page_area_ratio = width_ratio * height_ratio
        page_wide = (
            page_width > 0
            and page_height > 0
            and width_ratio >= 0.85
            and height_ratio >= 0.85
        )

    generic_header_count = _generic_header_count(headers)
    if generic_header_count:
        add_warning(
            "generic_headers",
            f"{generic_header_count} normalized column labels are placeholders "
            "rather than extracted header text.",
        )

    cell_text = " ".join(non_empty_cells)
    long_single_row = len(raw_rows) == 1 and len(cell_text) >= 120
    if long_single_row:
        add_warning(
            "one_row_large_text",
            "A one-row detection contains a large amount of text and may be a "
            "flattened paragraph, list, or page region.",
        )

    numbered_entries = len(re.findall(
        r"(?<!\w)\d{1,3}\s+(?=[A-Za-z])",
        cell_text,
    ))
    one_dominant_cell = (
        bool(non_empty_cells)
        and len(non_empty_cells) == 1
        and len(non_empty_cells[0]) >= 100
    )
    if numbered_entries >= 4:
        add_warning(
            "numbered_entries_in_cell",
            f"One extracted cell contains at least {numbered_entries} "
            "numbered-list-like entries.",
        )

    if page_wide:
        add_warning(
            "page_wide_text_detection",
            "The table bounding box covers nearly the full page; this is "
            "suspicious only when combined with weak row and cell structure.",
        )

    if (
        len(raw_rows) == 1
        and column_count >= 3
        and one_dominant_cell
    ):
        add_warning(
            "column_structure_mismatch",
            "Several columns were reported, but nearly all content occupies "
            "one cell in a single row.",
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
    # No single heuristic rejects a table. A broad page rectangle must also
    # have collapsed row structure, or a one-row candidate must combine
    # placeholder headers with paragraph/list-like content.
    broad_unstructured_detection = page_wide and (
        one_dominant_cell
        or len(non_empty_cells) <= 1
        or (
            len(raw_rows) == 1
            and long_single_row
            and generic_header_count > 0
        )
    )
    collapsed_list_detection = (
        len(raw_rows) == 1
        and generic_header_count >= 2
        and numbered_entries >= 4
        and one_dominant_cell
    )
    probable_false_positive = (
        broad_unstructured_detection
        or collapsed_list_detection
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
        "signals": {
            "bbox_page_area_ratio": round(page_area_ratio, 4),
            "bbox_width_ratio": round(width_ratio, 4),
            "bbox_height_ratio": round(height_ratio, 4),
            "generic_header_count": generic_header_count,
            "one_row_large_text": long_single_row,
            "numbered_entries_in_cell": numbered_entries,
            "single_dominant_cell": one_dominant_cell,
            "probable_false_positive": probable_false_positive,
        },
    }


def reconstruct_logical_tables(
    tables: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Join only strongly supported table fragments from adjacent pages.

    A page-edge match and compatible geometry are necessary but not enough:
    repeated headers, a continuous numeric first column, or matching column
    value patterns plus section context must also support the join. Ambiguous
    pairs are returned as candidate relationships and retain their original
    page-level rows without producing logical rows.
    """
    ordered = sorted(
        tables,
        key=lambda table: (
            table.get("page_index", -1),
            (table.get("bbox") or [0, 0, 0, 0])[1],
            table.get("table_index_on_page", 0),
        ),
    )
    tables_by_page: dict[int, list[dict[str, Any]]] = {}
    for table in ordered:
        page_index = table.get("page_index")
        if isinstance(page_index, int):
            tables_by_page.setdefault(page_index, []).append(table)
    adjacent_pairs = [
        (previous, current)
        for page_index, page_tables in tables_by_page.items()
        for next_page_table in [tables_by_page.get(page_index + 1, [])]
        for previous in page_tables
        for current in next_page_table
    ]
    logical_by_first_id: dict[str, dict[str, Any]] = {}
    fragment_to_logical: dict[str, str] = {}
    continuation_relationships: list[dict[str, Any]] = []

    for previous, current in adjacent_pairs:
        previous_page = previous.get("page_index")
        current_page = current.get("page_index")
        if (
            not isinstance(previous_page, int)
            or current_page != previous_page + 1
            or previous.get("document_id") != current.get("document_id")
            or previous.get("validation", {}).get("detection_class")
            == "probable_false_positive"
            or current.get("validation", {}).get("detection_class")
            == "probable_false_positive"
        ):
            continue

        previous_rect = previous.get("page_rect")
        current_rect = current.get("page_rect")
        previous_bbox = previous.get("bbox")
        current_bbox = current.get("bbox")
        if not all((
            previous_rect, current_rect, previous_bbox, current_bbox,
        )):
            continue

        previous_height = previous_rect[3] - previous_rect[1]
        current_height = current_rect[3] - current_rect[1]
        previous_width = previous_rect[2] - previous_rect[0]
        current_width = current_rect[2] - current_rect[0]
        if min(previous_height, current_height, previous_width, current_width) <= 0:
            continue

        bottom_ratio = (
            previous_bbox[3] - previous_rect[1]
        ) / previous_height
        top_ratio = (
            current_bbox[1] - current_rect[1]
        ) / current_height
        overlap_width = max(
            0.0,
            min(previous_bbox[2], current_bbox[2])
            - max(previous_bbox[0], current_bbox[0]),
        )
        narrower_width = min(
            previous_bbox[2] - previous_bbox[0],
            current_bbox[2] - current_bbox[0],
        )
        horizontal_overlap = (
            overlap_width / narrower_width if narrower_width > 0 else 0.0
        )
        previous_headers = previous.get("headers") or []
        expected_columns = len(previous_headers)
        current_raw = current.get(
            "normalized_raw_rows",
            current.get("raw_rows", []),
        )
        current_widths = {len(row) for row in current_raw}
        compatible_columns = (
            expected_columns >= 2
            and current_widths == {expected_columns}
            and _generic_header_count(previous_headers) == 0
            and previous.get("header_source") in {
                "detected_header_row",
                "detected_multilevel_header",
            }
        )
        current_rows = [_normalized_row(row) for row in current_raw]
        repeated_header = bool(
            current_rows
            and [value or "" for value in current_rows[0]]
            == [clean_cell(value) or "" for value in previous_headers]
        )

        group_id = fragment_to_logical.get(previous["table_id"])
        if group_id:
            group = logical_by_first_id[group_id]
            previous_logical_rows = group["rows"]
        else:
            group = None
            previous_logical_rows = []
        previous_rows = (
            previous_logical_rows
            if group
            else previous.get("rows", [])
        )
        previous_profile_rows = (
            [row["row"] for row in previous_rows]
            if group else previous_rows
        )
        last_row = (
            previous_rows[-1]["row"] if group and previous_rows
            else previous_rows[-1] if previous_rows else {}
        )
        last_number = _row_number(
            last_row.get(previous_headers[0]) if previous_headers else None
        )
        data_start = 1 if repeated_header else 0
        first_number = (
            _row_number(current_rows[data_start][0])
            if len(current_rows) > data_start and current_rows[data_start]
            else None
        )
        continuation_data_rows = current_rows[data_start:]
        column_profile_compatible = _column_profiles_compatible(
            previous_profile_rows,
            continuation_data_rows,
            previous_headers,
        )
        numbering_continues = (
            last_number is not None
            and first_number is not None
            and first_number == last_number + 1
        )
        numbering_conflicts = (
            last_number is not None
            and first_number is not None
            and not numbering_continues
        )
        previous_context = (
            previous.get("section_context")
            or previous.get("section_heading")
        )
        current_context = (
            current.get("section_context")
            or current.get("section_heading")
        )
        same_context = bool(
            previous_context
            and current_context
            and _tokens_for_table_context(previous_context)
            == _tokens_for_table_context(current_context)
        )
        near_page_edges = bottom_ratio >= 0.82 and top_ratio <= 0.20
        geometry_matches = horizontal_overlap >= 0.75
        evidence = {
            "previous_table_ends_near_page_bottom": round(bottom_ratio, 3),
            "next_table_starts_near_page_top": round(top_ratio, 3),
            "horizontal_bbox_overlap": round(horizontal_overlap, 3),
            "compatible_column_count": compatible_columns,
            "repeated_header": repeated_header,
            "numbering_continues": numbering_continues,
            "same_section_context": same_context,
            "column_profile_compatible": column_profile_compatible,
        }
        can_confirm = (
            near_page_edges
            and geometry_matches
            and compatible_columns
            and (
                numbering_continues
                or (
                    repeated_header
                    and same_context
                    and not numbering_conflicts
                )
                or (
                    column_profile_compatible
                    and same_context
                    and len(continuation_data_rows) >= 2
                    and not numbering_conflicts
                )
            )
        )
        can_be_candidate = (
            near_page_edges
            and geometry_matches
            and compatible_columns
            and (same_context or bool(current_context) or numbering_continues)
        )
        if not can_confirm and not can_be_candidate:
            continue

        relation_id = (
            f"{previous['table_id']}-continues-{current['table_id']}"
        )
        continuation_relationships.append({
            "relationship_id": relation_id,
            "source_id": previous["table_id"],
            "target_id": current["table_id"],
            "relationship": "continues_table",
            "relation_status": "confirmed" if can_confirm else "candidate",
            "confidence": (
                0.96 if numbering_continues and can_confirm
                else 0.92 if can_confirm
                else 0.6
            ),
            "evidence": {
                "method": "page_edge_geometry_and_row_structure",
                "signals": evidence,
                "reason": (
                    "The fragments share page-edge geometry and compatible "
                    "columns, with row numbering, repeated-header, or "
                    "column-profile evidence."
                    if can_confirm else
                    "The fragments are geometrically compatible, but the "
                    "row/header evidence is insufficient to merge them."
                ),
            },
        })
        if not can_confirm:
            continue

        if group is None:
            logical_id = f"{previous['table_id']}_logical"
            group = {
                "logical_table_id": logical_id,
                "document_id": previous.get("document_id"),
                "source_filename": previous.get("source_filename"),
                "source_sha256": previous.get("source_sha256"),
                "headers": list(previous_headers),
                "title": previous.get("title"),
                "section_context": previous_context,
                "merge_status": "confirmed",
                "confidence": continuation_relationships[-1]["confidence"],
                "evidence": [],
                "source_fragments": [],
                "rows": [],
                "warnings": [],
            }
            logical_by_first_id[logical_id] = group
            fragment_to_logical[previous["table_id"]] = logical_id
            raw_offset = (
                len(previous.get("raw_rows", [])) - len(previous_rows)
            )
            for row_index, row in enumerate(previous_rows):
                source_raw_row_index = raw_offset + row_index
                source_raw_rows = previous.get("raw_rows", [])
                source_raw_row = (
                    source_raw_rows[source_raw_row_index]
                    if 0 <= source_raw_row_index < len(source_raw_rows)
                    else []
                )
                if _looks_like_path_artifact(source_raw_row):
                    group["warnings"].append({
                        "code": "page_footer_row_not_reconstructed",
                        "message": (
                            "A path-like footer row remains in the source "
                            "fragment but was not promoted as logical table data."
                        ),
                        "source_fragment_id": previous["table_id"],
                        "source_raw_row_index": source_raw_row_index,
                    })
                    continue
                group["rows"].append({
                    "row": dict(row),
                    "row_index": len(group["rows"]),
                    "source_fragment_id": previous["table_id"],
                    "source_page_index": previous_page,
                    "source_page_number": previous.get("page_number"),
                    "source_bbox": previous_bbox,
                    "source_raw_row_index": source_raw_row_index,
                })
            _record_table_fragment(group, previous)

        for raw_row_index in range(data_start, len(current_rows)):
            raw_row = current_rows[raw_row_index]
            source_raw_rows = current.get("raw_rows", [])
            source_raw_row = (
                source_raw_rows[raw_row_index]
                if raw_row_index < len(source_raw_rows)
                else []
            )
            if _looks_like_path_artifact(source_raw_row):
                group["warnings"].append({
                    "code": "page_footer_row_not_reconstructed",
                    "message": (
                        "A path-like footer row remains in the source "
                        "fragment but was not promoted as logical table data."
                    ),
                    "source_fragment_id": current["table_id"],
                    "source_raw_row_index": raw_row_index,
                })
                continue
            if len(raw_row) != expected_columns:
                group["warnings"].append({
                    "code": "continuation_row_width_mismatch",
                    "message": (
                        "A continuation row did not match the established "
                        "column count and was not normalized."
                    ),
                    "source_fragment_id": current["table_id"],
                    "source_raw_row_index": raw_row_index,
                })
                continue
            group["rows"].append({
                "row": {
                    header: value
                    for header, value in zip(previous_headers, raw_row)
                },
                "row_index": len(group["rows"]),
                "source_fragment_id": current["table_id"],
                "source_page_index": current_page,
                "source_page_number": current.get("page_number"),
                "source_bbox": current_bbox,
                "source_raw_row_index": raw_row_index,
            })
        if current["table_id"] not in fragment_to_logical:
            fragment_to_logical[current["table_id"]] = group["logical_table_id"]
            _record_table_fragment(group, current)
        group["evidence"].append({
            "relationship_id": relation_id,
            **evidence,
        })

    return list(logical_by_first_id.values()), continuation_relationships


def _tokens_for_table_context(value: Any) -> tuple[str, ...]:
    """Compare section labels without treating punctuation as meaningful."""
    return tuple(re.findall(r"[a-z0-9]+", str(value).casefold()))


def _record_table_fragment(
    logical_table: dict[str, Any],
    table: dict[str, Any],
) -> None:
    """Keep complete source evidence alongside every logical table fragment."""
    logical_table["source_fragments"].append({
        "table_id": table["table_id"],
        "page_index": table.get("page_index"),
        "page_number": table.get("page_number"),
        "bbox": table.get("bbox"),
        "headers": table.get("headers"),
        "raw_rows": table.get("raw_rows", []),
        "normalized_raw_rows": table.get(
            "normalized_raw_rows",
            table.get("raw_rows", []),
        ),
        "layout_adjustments": table.get("layout_adjustments", []),
        "header_source": table.get("header_source"),
    })


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
    source_sha256: str | None = None,
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

    # The coordinated pipeline reuses extract_pdf's hash to avoid reading a
    # large source PDF a second time; direct callers still get a source hash.
    source_hash = source_sha256 or calculate_file_hash(pdf_path)
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
                layout_rows = getattr(table, "rows", ())
                normalized_raw_rows, layout_adjustments = (
                    _expand_vertical_merged_cells(
                        normalized_rows,
                        layout_rows,
                    )
                )
                (
                    table_title,
                    headers,
                    data_rows,
                    header_source,
                    header_warnings,
                ) = _infer_table_structure(normalized_rows)
                # Header inference intentionally uses untouched cell text.
                # Apply geometry-backed merged-cell expansion only to the
                # data suffix, leaving headers and raw_rows unchanged.
                data_start = len(normalized_rows) - len(data_rows)
                data_rows = normalized_raw_rows[data_start:]

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
                    "element_type": "table",
                    "document_id": document_id,
                    "source_filename": pdf_path.name,
                    "source_sha256": source_hash,
                    "page_number": page_number,
                    "page_index": page_number - 1,
                    "page_rect": list(page.rect),
                    "printed_page_number": None,
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
                    "normalized_raw_rows": normalized_raw_rows,
                    "layout_adjustments": layout_adjustments,
                    "header_source": header_source,
                    "extraction_method": "pymupdf",
                    "row_count": len(records),
                    "column_count": len(headers),
                    "validation": validation,
                    "extraction_warnings": unique_warnings,
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
