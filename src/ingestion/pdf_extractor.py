"""PDF extraction utilities."""

import hashlib
import json
import re
import statistics
from pathlib import Path
from typing import Any

import pymupdf


CAPTION_PATTERN = re.compile(
    r"^\s*(?:figure|fig\.?|table)\s+\d+(?:[.-]\d+)*\b",
    re.IGNORECASE,
)
NUMBERED_SECTION_PATTERN = re.compile(r"^\s*\d+(?:\.\d+)+\s+\S")


def classify_text_block(
    text: str,
    spans: list[dict[str, Any]],
    typical_font_size: float,
) -> str:
    """Classify a text block as a caption, heading, or ordinary paragraph.

    This is layout metadata, not a semantic guarantee. Explicit figure/table
    labels take priority; font size and short all-caps text are weaker heading
    signals that later relationship validation can review.
    """
    if CAPTION_PATTERN.match(text):
        return "caption"

    sizes = [span["size"] for span in spans if span.get("size")]
    is_larger = bool(
        sizes
        and typical_font_size
        and statistics.median(sizes) >= typical_font_size * 1.18
    )
    # A numbered section label is strong structural evidence. Uppercase alone
    # is intentionally not enough because table headers and part descriptions
    # are commonly uppercase in technical manuals.
    is_numbered_section = bool(NUMBERED_SECTION_PATTERN.match(text))
    if is_larger or is_numbered_section:
        return "section_heading"
    return "paragraph"


def calculate_file_hash(pdf_path: Path) -> str:
    """Calculate a SHA-256 hash to identify the exact PDF contents."""
    sha256 = hashlib.sha256()

    with pdf_path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            sha256.update(block)

    return sha256.hexdigest()


def extract_pdf(
        pdf_path: Path,
        output_dir: Path,
        machine_name: str,
        relative_source_path: str,
        document_output_dir: Path | None = None,
        metadata_filename: str = "extraction.json",
    ) -> dict:
    """Extract page text, blocks, links, and images with source provenance.

    ``document_output_dir`` and ``metadata_filename`` let the coordinated
    pipeline use source-name-based artifacts while preserving the original
    hashed-directory behavior for existing direct callers.
    """

    if not pdf_path.exists():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError("Input file must be a PDF.")

    document_hash = calculate_file_hash(pdf_path)
    document_id = document_hash[:16]

    document_output = (
        Path(document_output_dir)
        if document_output_dir is not None
        else output_dir / machine_name / document_id
    )
    images_dir = document_output / "images"

    images_dir.mkdir(parents=True, exist_ok=True)

    pages = []
    extracted_images = []
    extraction_warnings = []

    with pymupdf.open(pdf_path) as pdf:
        page_count = len(pdf)

        for page_index, page in enumerate(pdf):
            # Internal index is zero-based; displayed page number is one-based.
            page_number = page_index + 1

            page_text = page.get_text("text", sort=True)

            # Keep blocks separate and retain span-level geometry/style so
            # headings, captions, and reading order can be reviewed later.
            raw_blocks = page.get_text("dict", sort=True)["blocks"]
            font_sizes = [
                span.get("size", 0.0)
                for block in raw_blocks
                if block.get("type") == 0
                for line in block.get("lines", [])
                for span in line.get("spans", [])
                if span.get("size", 0.0) > 0
            ]
            typical_font_size = (
                statistics.median(font_sizes) if font_sizes else 0.0
            )
            text_blocks = []

            for block_index, block in enumerate(raw_blocks):
                if block.get("type") != 0:
                    continue

                lines = []
                spans_for_block = []
                for line in block.get("lines", []):
                    spans = line.get("spans", [])
                    lines.append("".join(span.get("text", "") for span in spans))
                    for span in spans:
                        spans_for_block.append({
                            "text": span.get("text", ""),
                            "bbox": list(span["bbox"]) if span.get("bbox") else None,
                            "font": span.get("font"),
                            "size": span.get("size"),
                            "flags": span.get("flags"),
                        })

                block_text = "\n".join(lines).strip()

                if not block_text:
                    continue

                # sort=True provides PyMuPDF's best available reading order;
                # preserve that order explicitly rather than merging blocks.
                text_blocks.append({
                    "block_id": f"{document_id}-p{page_number}-b{block_index}",
                    "text": block_text,
                    "bbox": list(block["bbox"]) if block.get("bbox") else None,
                    "reading_order": len(text_blocks),
                    "kind": classify_text_block(
                        block_text,
                        spans_for_block,
                        typical_font_size,
                    ),
                    "spans": spans_for_block,
                })

            # Extract links with their source-page positions.
            links = []
            for link_index, link in enumerate(page.get_links()):
                links.append({
                    "link_id": (
                        f"{document_id}-p{page_number}-l{link_index}"
                    ),
                    "source_page": page_number,
                    "source_bbox": list(link["from"]),
                    "target_page": link.get("page"),
                    "target_page_number": (
                        link["page"] + 1
                        if isinstance(link.get("page"), int) and link["page"] >= 0
                        else None
                    ),
                    "target_uri": link.get("uri"),
                    "target_named": link.get("nameddest"),
                })

            # Extract embedded images and record their provenance.
            for image_index, image_info in enumerate(page.get_images(full=True)):
                xref = image_info[0]

                try:
                    image_data = pdf.extract_image(xref)
                except Exception as error:
                    extraction_warnings.append({
                        "page_number": page_number,
                        "image_xref": xref,
                        "warning": f"Could not extract image: {error}",
                    })
                    continue

                image_bytes = image_data["image"]
                image_hash = hashlib.sha256(image_bytes).hexdigest()
                extension = image_data.get("ext", "bin")
                try:
                    image_rects = [
                        list(rect) for rect in page.get_image_rects(xref)
                    ]
                except Exception as error:
                    image_rects = []
                    extraction_warnings.append({
                        "page_number": page_number,
                        "image_xref": xref,
                        "warning": (
                            "Image bytes were extracted, but placement "
                            f"coordinates could not be read: {error}"
                        ),
                    })

                image_path = images_dir / (
                    f"page_{page_number}_image_{image_index}_{image_hash[:8]}."
                    f"{extension}"
                )

                if not image_path.exists():
                    image_path.write_bytes(image_bytes)

                extracted_images.append({
                    "image_id": f"{document_id}-p{page_number}-i{image_index}",
                    "page_number": page_number,
                    "xref": xref,
                    "sha256": image_hash,
                    "path": image_path.relative_to(
                        document_output
                    ).as_posix(),
                    "bboxes": image_rects,
                    "bbox": image_rects[0] if len(image_rects) == 1 else None,
                })

            page_warnings = []
            if not page_text.strip():
                page_warnings.append("no_extractable_text")
                if page.get_images(full=True):
                    page_warnings.append("possible_scanned_page_ocr_needed")
                    extraction_warnings.append({
                        "page_number": page_number,
                        "warning": (
                            "Page contains images but no extractable text; "
                            "OCR was not run."
                        ),
                    })

            pages.append({
                "page_number": page_number,
                "text": page_text,
                "text_blocks": text_blocks,
                "links": links,
                "image_ids": [
                    image["image_id"] for image in extracted_images
                    if image["page_number"] == page_number
                ],
                "warnings": page_warnings,
            })

    result = {
        "schema_version": 2,
        "document_id": document_id,
        "machine_name": machine_name,
        "relative_source_path": relative_source_path,
        "source_filename": pdf_path.name,
        "source_sha256": document_hash,
        "page_count": page_count,
        "pages": pages,
        "images": extracted_images,
        "warnings": extraction_warnings,
    }

    document_output.mkdir(parents=True, exist_ok=True)
    metadata_path = document_output / metadata_filename
    metadata_path.write_text(
        json.dumps(result, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return result
