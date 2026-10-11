
def fill_merged_cells(rows):
    """Fill down vertically merged cells in the first column."""
    if not rows or len(rows) < 2:
        return rows

    normalized_rows = [list(row) for row in rows]

    # Process data rows, skipping the header.
    previous_value = None

    for row in normalized_rows[1:]:
        value = row[0] if row else None

        if value is not None and str(value).strip():
            previous_value = value
        elif previous_value is not None:
            row[0] = previous_value

    return normalized_rows

def extract_tables(page):
    """Extract structurally plausible tables from one PDF page."""
    result = []

    finder = page.find_tables()

    for number, table in enumerate(finder.tables, start=1):
        rows = table.extract()
        rows = fill_merged_cells(rows)

        print("\n--- TABLE DEBUG ---")
        print("Rows:", len(rows))
        print("Columns:", table.col_count)
        print("Table bbox:", table.bbox)

        for row_number, row in enumerate(rows, start=1):
            print(f"Row {row_number}: {row}")

        for row_number, row in enumerate(table.rows, start=1):
            print(f"Row {row_number} cell boxes:", row.cells)

        if not rows:
            continue

        # Ignore detections without a basic row/column structure.
        if len(rows) < 2 or table.col_count < 2:
            continue

        # Ignore completely empty detections.
        if not any(
            cell and str(cell).strip()
            for row in rows
            for cell in row
        ):
            continue

        result.append({
            "id": f"table_{number}",
            "bbox": list(table.bbox),
            "rows": rows,
        })

    return result
