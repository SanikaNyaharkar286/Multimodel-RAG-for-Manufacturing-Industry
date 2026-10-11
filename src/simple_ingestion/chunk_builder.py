import json
from pathlib import Path
def clean_text(text):
    """Normalize whitespace without removing meaningful line breaks."""
    import re

    text = text.replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)

    return text.strip()

def build_table_chunks(
    table,
    machine_name,
    document_name,
    page_number,
):
    """Convert a table into chunks that preserve row context."""

    rows = table.get("rows", [])

    if len(rows) < 2:
        return []

    headers = [
        clean_text(str(value)) if value is not None else ""
        for value in rows[0]
    ]

    chunks = []

    for row_number, row in enumerate(rows[1:], start=1):
        values = [
            str(value).strip() if value is not None else ""
            for value in row
        ]

        # Skip completely empty rows.
        if not any(values):
            continue

        fields = []

        for index, value in enumerate(values):
            if not value:
                continue

            header = (
                headers[index]
                if index < len(headers) and headers[index]
                else f"Column {index + 1}"
            )

            fields.append(f"{header}: {value}")

        text = "\n".join(fields)

        chunks.append({
            "chunk_id": (
                f"{document_name}_p{page_number}_"
                f"{table.get('id', 'table')}_r{row_number}"
            ),
            "content": text,
            "metadata": {
                "machine_name": machine_name,
                "document_name": document_name,
                "page_number": page_number,
                "content_type": "table_row",
                "table_id": table.get("id"),
                "row_number": row_number,
            },
        })

    return chunks


def build_chunks_from_extraction(
    extracted_data,
    machine_name,
    document_name,
):
    """Build table-row chunks from one extracted PDF."""

    chunks = []

    for page in extracted_data.get("pages", []):
        page_number = page.get("page_number")

        for table in page.get("tables", []):
            table_chunks = build_table_chunks(
                table=table,
                machine_name=machine_name,
                document_name=document_name,
                page_number=page_number,
            )

            chunks.extend(table_chunks)

    return chunks


def build_paragraph_chunks(
    extracted_data,
    machine_name,
    document_name,
):
    """Convert extracted paragraphs into chunks."""

    chunks = []

    for page in extracted_data.get("pages", []):
        page_number = page.get("page_number")

        for paragraph in page.get("paragraphs", []):
            content = clean_text(paragraph.get("text", ""))

            if not content or content in {".", "-", "•"}:
                continue

            chunks.append({
                "chunk_id": (
                    f"{document_name}_p{page_number}_"
                    f"{paragraph.get('id', 'paragraph')}"
                ),
                "content": content,
                "metadata": {
                    "machine_name": machine_name,
                    "document_name": document_name,
                    "page_number": page_number,
                    "content_type": "paragraph",
                    "source_id": paragraph.get("id"),
                },
            })

    return chunks

def build_image_chunks(
    extracted_data,
    machine_name,
    document_name,
):
    """Create chunks containing image references and nearby text."""

    chunks = []

    for page in extracted_data.get("pages", []):
        page_number = page.get("page_number")

        # Collect text blocks from this page for context.
        paragraphs = page.get("paragraphs", [])

        for image_data in page.get("images", []):
            image_id = image_data.get("id", "unknown_image")
            image_path = image_data.get("asset", "")

            # Use the nearest paragraph as context when possible.
            image_bbox = image_data.get("bbox", [])
            nearby_text = ""

            if len(image_bbox) == 4 and paragraphs:
                image_x = (image_bbox[0] + image_bbox[2]) / 2
                image_y = (image_bbox[1] + image_bbox[3]) / 2

                nearest_paragraph = min(
                    paragraphs,
                    key=lambda paragraph: (
                        (
                            (paragraph.get("bbox", [0, 0, 0, 0])[0]
                             + paragraph.get("bbox", [0, 0, 0, 0])[2]) / 2
                            - image_x
                        ) ** 2
                        +
                        (
                            (paragraph.get("bbox", [0, 0, 0, 0])[1]
                             + paragraph.get("bbox", [0, 0, 0, 0])[3]) / 2
                            - image_y
                        ) ** 2
                    ),
                )

                nearby_text = clean_text(
                    nearest_paragraph.get("text", "")
                )

            content = (
                f"Machine: {machine_name}\n"
                f"Document: {document_name}\n"
                f"Page: {page_number}\n"
                f"Image ID: {image_id}\n"
                f"Nearby text: {nearby_text or 'No nearby text available'}"
            )

            chunks.append({
                "chunk_id": (
                    f"{document_name}_p{page_number}_{image_id}"
                ),
                "content": content,
                "metadata": {
                    "machine_name": machine_name,
                    "document_name": document_name,
                    "page_number": page_number,
                    "content_type": "image",
                    "image_id": image_id,
                    "image_path": image_path,
                    "bbox": image_bbox,
                },
            })

    return chunks
def build_all_chunks(extracted_data, machine_name, document_name):
    """Build paragraph and table chunks for one extracted PDF."""

    chunks = []

    chunks.extend(
        build_paragraph_chunks(
            extracted_data,
            machine_name,
            document_name,
        )
    )

    chunks.extend(
        build_chunks_from_extraction(
            extracted_data,
            machine_name,
            document_name,
        )
    )
    chunks.extend(
        build_image_chunks(
            extracted_data,
            machine_name,
            document_name,
        )
    )

    return chunks



def save_chunks(chunks, output_path):
    """Save chunks as UTF-8 JSON."""

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Normalize asset paths for portability.
    for chunk in chunks:
        metadata = chunk.get("metadata", {})
        image_path = metadata.get("image_path")

        if image_path:
            metadata["image_path"] = image_path.replace("\\", "/")

    output_path.write_text(
        json.dumps(chunks, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"Saved {len(chunks)} chunks to: {output_path}")
