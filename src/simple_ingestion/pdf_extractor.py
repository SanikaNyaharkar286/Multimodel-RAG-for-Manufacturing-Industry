
import hashlib
from pathlib import Path

import pymupdf

from table_extractor import extract_tables


def overlaps_table(block_bbox, table_bbox):
    """Return True when most of a text block lies inside a table."""
    x0, y0, x1, y1 = block_bbox
    a0, b0, a1, b1 = table_bbox

    width = max(0, min(x1, a1) - max(x0, a0))
    height = max(0, min(y1, b1) - max(y0, b0))
    intersection = width * height
    block_area = max(1, (x1 - x0) * (y1 - y0))

    return intersection / block_area >= 0.5


def extract_pdf(pdf_path, output_dir):
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    assets_dir = output_dir / "images"
    assets_dir.mkdir(parents=True, exist_ok=True)

    document = pymupdf.open(pdf_path)

    result = {
        "document": pdf_path.name,
        "pages": [],
    }

    for page_index, page in enumerate(document):
        page_number = page_index + 1

        tables = extract_tables(page)
        table_boxes = [table["bbox"] for table in tables]

        paragraphs = []

        for block in page.get_text("blocks"):
            x0, y0, x1, y1, text = block[:5]

            if not str(text).strip():
                continue

            bbox = (x0, y0, x1, y1)

            if any(overlaps_table(bbox, box) for box in table_boxes):
                continue

            paragraphs.append({
                "id": f"p{page_number}_text_{len(paragraphs) + 1}",
                "bbox": list(bbox),
                "text": str(text).strip(),
            })

        images = []
        saved_assets = {}

        for image_info in page.get_images(full=True):
            xref = image_info[0]

            extracted = document.extract_image(xref)
            image_bytes = extracted["image"]
            digest = hashlib.sha256(image_bytes).hexdigest()

            if digest not in saved_assets:
                filename = f"{digest[:16]}.{extracted['ext']}"
                asset_path = assets_dir / filename
                asset_path.write_bytes(image_bytes)
                saved_assets[digest] = str(Path("assets") / "images" / filename)

            for rect in page.get_image_rects(xref):
                images.append({
                    "id": f"p{page_number}_image_{len(images) + 1}",
                    "bbox": list(rect),
                    "asset": saved_assets[digest],
                })

        links = []

        for link in page.get_links():
            uri = link.get("uri")

            if not uri:
                continue

            links.append({
                "id": f"p{page_number}_link_{len(links) + 1}",
                "bbox": list(link["from"]),
                "url": uri,
            })

        result["pages"].append({
            "page_number": page_number,
            "paragraphs": paragraphs,
            "tables": tables,
            "images": images,
            "links": links,
        })

    document.close()
    return result
