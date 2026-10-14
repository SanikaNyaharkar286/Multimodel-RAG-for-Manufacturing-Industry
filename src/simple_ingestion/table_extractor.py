def _grid_edges(table):
    cells = [cell for row in table.rows for cell in row.cells if cell]
    x_edges = sorted({round(edge, 3) for cell in cells for edge in (cell[0], cell[2])})
    y_edges = sorted({round(edge, 3) for cell in cells for edge in (cell[1], cell[3])})
    return x_edges, y_edges


def _expand_merged_cells(table, rows):
    x_edges, y_edges = _grid_edges(table)
    columns = [(left + right) / 2 for left, right in zip(x_edges, x_edges[1:])]
    row_centers = [(top + bottom) / 2 for top, bottom in zip(y_edges, y_edges[1:])]
    if len(columns) != table.col_count or len(row_centers) != len(rows):
        return [list(row) for row in rows]

    expanded = [["" for _ in columns] for _ in rows]
    for row_index, row in enumerate(table.rows):
        for column_index, (cell, value) in enumerate(zip(row.cells, rows[row_index])):
            if not cell or value is None or not str(value).strip():
                continue
            for target_row, center_y in enumerate(row_centers):
                if cell[1] <= center_y < cell[3]:
                    for target_column, center_x in enumerate(columns):
                        if cell[0] <= center_x < cell[2]:
                            expanded[target_row][target_column] = str(value).strip()
    return expanded


def _normalize_headers(table, rows):
    title = None
    if rows:
        first_row = table.rows[0]
        values = list(dict.fromkeys(
            str(value).strip() for value in rows[0]
            if value is not None and str(value).strip()
        ))
        full_width = any(
            cell
            and abs(cell[0] - table.bbox[0]) < 1
            and abs(cell[2] - table.bbox[2]) < 1
            for cell in first_row.cells
        )
        if full_width and len(values) == 1:
            title = values[0]
            rows = rows[1:]

    header_count = 1
    if rows:
        first_cells = table.rows[1] if title else table.rows[0]
        _, y_edges = _grid_edges(table)
        y_centers = [(top + bottom) / 2 for top, bottom in zip(y_edges, y_edges[1:])]
        for cell in first_cells.cells:
            if cell:
                covered = [index for index, center in enumerate(y_centers) if cell[1] <= center < cell[3]]
                if covered:
                    header_count = max(header_count, max(covered) + 1 - int(bool(title)))

    headers = []
    for column in range(len(rows[0])) if rows else ():
        parts = []
        for row in rows[:header_count]:
            value = row[column]
            if value and value not in parts:
                parts.append(value)
        headers.append(" / ".join(parts))
    return title, [headers, *rows[header_count:]] if rows else rows


def extract_tables(page):
    """Extract tables, expanding merged cells and combining multi-row headers."""
    result = []
    for number, table in enumerate(page.find_tables().tables, start=1):
        rows = _expand_merged_cells(table, table.extract())
        title, rows = _normalize_headers(table, rows)
        if len(rows) < 2 or table.col_count < 2:
            continue
        if not any(str(cell).strip() for row in rows for cell in row if cell is not None):
            continue

        extracted = {"id": f"table_{number}", "bbox": list(table.bbox), "rows": rows}
        if title:
            extracted["title"] = title
        result.append(extracted)
    return result
