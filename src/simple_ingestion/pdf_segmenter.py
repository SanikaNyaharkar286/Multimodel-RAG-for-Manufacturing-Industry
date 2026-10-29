#!/usr/bin/env python3
"""
Conservative PDF segmenter for RAG preparation.

- Does NOT rebuild PDF pages from extracted text.
- Detects candidate tables with pdfplumber.
- Groups nearby headings/captions and vertically overlapping images with table regions.
- Creates cropped PDF pages from the original PDF using pypdf.
- Preserves original page number and source bounding boxes in a manifest.
- Writes previews and a review report.
- Uses no fixed table-header names.

Important: automatic layout detection is heuristic. Inspect the report and preview
before using output in production. Ambiguous regions are flagged rather than silently
claimed as correct.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

try:
    import pdfplumber
    from pypdf import PdfReader, PdfWriter
    from pypdf.generic import RectangleObject
except ImportError as exc:
    print(
        "Missing dependencies. Install with:\n"
        "  python -m pip install pdfplumber pypdf pillow\n"
        f"Details: {exc}",
        file=sys.stderr,
    )
    raise SystemExit(2)

VERSION = "pdf-related-segments-0.3.0"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def overlap(a: tuple[float, float, float, float],
            b: tuple[float, float, float, float]) -> float:
    x0 = max(a[0], b[0])
    top = max(a[1], b[1])
    x1 = min(a[2], b[2])
    bottom = min(a[3], b[3])
    if x1 <= x0 or bottom <= top:
        return 0.0
    return (x1 - x0) * (bottom - top)


def as_bbox(obj: dict[str, Any]) -> tuple[float, float, float, float]:
    return (float(obj["x0"]), float(obj["top"]),
            float(obj["x1"]), float(obj["bottom"]))


def line_candidates(page) -> list[dict[str, Any]]:
    """Group words into lines and infer heading-like lines from typography/shape."""
    words = page.extract_words(
        x_tolerance=2, y_tolerance=3, keep_blank_chars=False,
        extra_attrs=["size", "fontname"]
    ) or []
    if not words:
        return []
    # Cluster by vertical center; tolerates slight font baseline variation.
    words = sorted(words, key=lambda w: ((w["top"] + w["bottom"]) / 2, w["x0"]))
    clusters: list[list[dict[str, Any]]] = []
    for word in words:
        cy = (float(word["top"]) + float(word["bottom"])) / 2
        if not clusters:
            clusters.append([word])
            continue
        prev_cy = sum((float(w["top"]) + float(w["bottom"])) / 2 for w in clusters[-1]) / len(clusters[-1])
        if abs(cy - prev_cy) <= 3.5:
            clusters[-1].append(word)
        else:
            clusters.append([word])

    sizes = [float(w.get("size", 0) or 0) for w in words if float(w.get("size", 0) or 0) > 0]
    median_size = sorted(sizes)[len(sizes)//2] if sizes else 0
    lines = []
    for cluster in clusters:
        cluster.sort(key=lambda w: float(w["x0"]))
        text = " ".join(str(w["text"]) for w in cluster).strip()
        if not text:
            continue
        bbox = (
            min(float(w["x0"]) for w in cluster),
            min(float(w["top"]) for w in cluster),
            max(float(w["x1"]) for w in cluster),
            max(float(w["bottom"]) for w in cluster),
        )
        line_size = max(float(w.get("size", 0) or 0) for w in cluster)
        # Generic signals only; no known heading vocabulary.
        short_line = len(text) <= 100
        numbered = bool(re.match(r"^\s*(?:\d+(?:\.\d+)*[.)]?|[A-Z][.)])\s+\S", text))
        all_caps = any(ch.isalpha() for ch in text) and text.upper() == text
        larger = median_size > 0 and line_size >= median_size * 1.18
        heading_like = short_line and (numbered or all_caps or larger)
        lines.append({
            "text": text, "bbox": bbox, "font_size_max": line_size,
            "heading_like": heading_like, "word_count": len(cluster)
        })
    return lines


def detect_tables(page) -> list[dict[str, Any]]:
    """
    Detect table candidates using several strategies, then reject page-wide,
    structurally sparse candidates. A large candidate is not considered a table
    merely because pdfplumber can construct many mostly-empty rows/cells.
    """
    configs = [
        {},
        {"vertical_strategy": "text", "horizontal_strategy": "text",
         "intersection_tolerance": 5, "text_tolerance": 3},
        {"vertical_strategy": "lines", "horizontal_strategy": "text",
         "intersection_tolerance": 5, "text_tolerance": 3},
    ]
    candidates = []
    page_area = max(1.0, float(page.width) * float(page.height))

    for config in configs:
        try:
            found_tables = page.find_tables(table_settings=config)
        except Exception:
            continue

        for table in found_tables:
            bbox = tuple(float(v) for v in table.bbox)
            x0, top, x1, bottom = bbox
            box_width = max(0.0, x1 - x0)
            box_height = max(0.0, bottom - top)
            area_ratio = (box_width * box_height) / page_area
            height_ratio = box_height / max(1.0, float(page.height))
            width_ratio = box_width / max(1.0, float(page.width))

            try:
                rows = table.extract() or []
            except Exception:
                rows = []

            row_count = len(rows)
            max_cols = max((len(row) for row in rows if row), default=0)
            nonempty = sum(
                1 for row in rows for cell in (row or [])
                if cell is not None and str(cell).strip()
            )
            total_slots = sum(len(row or []) for row in rows)
            occupancy = nonempty / max(1, total_slots)
            word_count = sum(
                len(str(cell).split())
                for row in rows for cell in (row or [])
                if cell is not None and str(cell).strip()
            )

            # Structural score. Header names are deliberately not used.
            score = 0
            score += 2 if row_count >= 3 else 0
            score += 2 if max_cols >= 2 else 0
            score += 1 if nonempty >= 6 else 0
            score += 1 if word_count >= 12 else 0
            score += 1 if occupancy >= 0.30 else 0

            # pdfplumber can interpret a long, multi-column page layout as one
            # giant table. Reject it when it spans most of the page and its
            # extracted grid is mostly empty. Keep tall, genuinely dense tables.
            page_wide_tall = height_ratio >= 0.70 and width_ratio >= 0.45
            sparse_grid = occupancy < 0.18 or (total_slots >= 100 and occupancy < 0.25)
            global_false_positive = page_wide_tall and sparse_grid

            # Very large low-structure candidates are suspicious regardless of
            # their row count. Do not let a fabricated grid win due to many rows.
            if area_ratio >= 0.50 and (occupancy < 0.25 or max_cols >= 8):
                global_false_positive = True

            # A single-column block is usually text/video content, not a table.
            if max_cols <= 1:
                score -= 3

            if global_false_positive:
                # Discard rather than use as a crop region. It remains visible
                # in diagnostics so the developer can inspect false positives.
                continue

            review_required = score < 4 or occupancy < 0.25
            candidate = {
                "bbox": bbox,
                "rows": rows,
                "row_count": row_count,
                "max_columns": max_cols,
                "nonempty_cells": nonempty,
                "total_cells": total_slots,
                "cell_occupancy": round(occupancy, 4),
                "word_count": word_count,
                "area_ratio": round(area_ratio, 4),
                "height_ratio": round(height_ratio, 4),
                "width_ratio": round(width_ratio, 4),
                "score": score,
                "method": "pdfplumber.find_tables",
                "review_required": review_required,
            }

            # Deduplicate candidates with nearly identical geometry.
            duplicate = None
            for old in candidates:
                inter = overlap(bbox, old["bbox"])
                area_a = max(1.0, (bbox[2] - bbox[0]) * (bbox[3] - bbox[1]))
                area_b = max(1.0, (old["bbox"][2] - old["bbox"][0]) *
                             (old["bbox"][3] - old["bbox"][1]))
                if inter / min(area_a, area_b) > 0.85:
                    duplicate = old
                    break

            if duplicate is None:
                candidates.append(candidate)
            elif (candidate["score"], candidate["cell_occupancy"]) > (
                    duplicate["score"], duplicate["cell_occupancy"]):
                candidates.remove(duplicate)
                candidates.append(candidate)

    candidates.sort(key=lambda c: (c["bbox"][1], c["bbox"][0]))
    return candidates


def detect_images(page) -> list[dict[str, Any]]:
    # pdfplumber image coordinates are useful for grouping, though some vector artwork
    # and embedded media previews may not appear as image objects.
    result = []
    for img in getattr(page, "images", []) or []:
        try:
            bbox = as_bbox(img)
            if bbox[2] > bbox[0] and bbox[3] > bbox[1]:
                result.append({"bbox": bbox, "source": "pdfplumber.image"})
        except Exception:
            pass
    return result


def merge_related_content(page, tables, lines, images):
    """
    Expand candidate table vertically to include nearby heading-like lines and
    captions, and naturally include horizontally adjacent content via full-width crops.
    Does not infer semantic relation from a fixed list of words.
    """
    page_w, page_h = float(page.width), float(page.height)
    regions = []
    for idx, table in enumerate(tables):
        x0, top, x1, bottom = table["bbox"]
        included_lines = []
        # Attach the closest heading-like line above the table within a conservative gap.
        above = [
            line for line in lines
            if line["heading_like"] and line["bbox"][3] <= top
            and top - line["bbox"][3] <= max(90, page_h * 0.06)
        ]
        if above:
            closest = max(above, key=lambda line: line["bbox"][3])
            # Avoid assigning one heading to multiple tables.
            included_lines.append(closest)
            top = min(top, closest["bbox"][1] - 2)

        # Include nearby caption/text lines below the table only when very close.
        below = [
            line for line in lines
            if line["bbox"][1] >= bottom and line["bbox"][1] - bottom <= 24
            and len(line["text"]) <= 180
        ]
        if below:
            closest_below = min(below, key=lambda line: line["bbox"][1])
            included_lines.append(closest_below)
            bottom = max(bottom, closest_below["bbox"][3] + 2)

        # Keep adjacent images in their own preceding/following content segment.
        # An image is associated with a table only when its bounding box actually
        # overlaps the table vertically and horizontally. Do not expand a table
        # crop to include an image that merely sits immediately above it: doing
        # so prevents tables directly after images from becoming separate pages.
        related_images = []
        for image in images:
            ib = image["bbox"]
            horizontal_overlap = min(x1, ib[2]) > max(x0, ib[0])
            vertical_overlap = min(bottom, ib[3]) > max(top, ib[1])
            if horizontal_overlap and vertical_overlap:
                related_images.append(image)
                # Expand only if the image protrudes across the table boundary.
                top = min(top, ib[1])
                bottom = max(bottom, ib[3])

        regions.append({
            "kind": "table_group",
            "bbox": [0.0, max(0.0, top), page_w, min(page_h, bottom)],
            "table_bbox": list(table["bbox"]),
            "table_index": idx + 1,
            "table_detection": {k: v for k, v in table.items() if k != "rows"},
            "attached_heading_lines": [
                {"text": l["text"], "bbox": list(l["bbox"])} for l in included_lines
            ],
            "related_images": [
                {"bbox": list(i["bbox"]), "source": i["source"]} for i in related_images
            ],
        })
    return regions


def create_segments(page, table_regions):
    """Create ordered full-width crops; gaps become text/other-content segments."""
    page_w, page_h = float(page.width), float(page.height)
    regions = sorted(table_regions, key=lambda r: r["bbox"][1])
    # Merge overlapping table groups to avoid duplicate/overlapping crops.
    merged = []
    for region in regions:
        if merged and region["bbox"][1] <= merged[-1]["bbox"][3] + 2:
            old = merged[-1]
            old["bbox"][1] = min(old["bbox"][1], region["bbox"][1])
            old["bbox"][3] = max(old["bbox"][3], region["bbox"][3])
            old.setdefault("merged_table_indices", [old.get("table_index")])
            old["merged_table_indices"].append(region.get("table_index"))
            old["review_required"] = True
        else:
            merged.append(region.copy())

    segments = []
    cursor = 0.0
    for region in merged:
        top, bottom = region["bbox"][1], region["bbox"][3]
        if top - cursor > 8:
            segments.append({
                "segment_type": "content",
                "bbox": [0.0, cursor, page_w, top],
                "associated_table": False,
                "review_required": False,
            })
        table_det = region.get("table_detection", {})
        segments.append({
            "segment_type": "table_group",
            "bbox": [0.0, top, page_w, bottom],
            "table_bbox": region.get("table_bbox"),
            "associated_table": True,
            "attached_heading_lines": region.get("attached_heading_lines", []),
            "related_images": region.get("related_images", []),
            "table_detection": table_det,
            "review_required": bool(table_det.get("review_required", False)) or bool(region.get("review_required", False)),
        })
        cursor = max(cursor, bottom)
    if page_h - cursor > 8:
        segments.append({
            "segment_type": "content",
            "bbox": [0.0, cursor, page_w, page_h],
            "associated_table": False,
            "review_required": False,
        })
    if not segments:
        segments = [{
            "segment_type": "content",
            "bbox": [0.0, 0.0, page_w, page_h],
            "associated_table": False,
            "review_required": True,
        }]
    return segments


def add_cropped_page(writer, source_page, bbox, page_height):
    """Set crop/trim boxes on a cloned original page; no text/image reconstruction."""
    from copy import copy
    page = copy(source_page)
    x0, top, x1, bottom = bbox
    media = source_page.mediabox
    # pdfplumber uses top-origin; PDF page boxes use bottom-origin.
    crop = RectangleObject([
        float(media.left) + x0,
        float(media.bottom) + page_height - bottom,
        float(media.left) + x1,
        float(media.bottom) + page_height - top,
    ])
    page.cropbox = crop
    page.trimbox = crop
    writer.add_page(page)


def render_preview(pdf_path: Path, output_dir: Path):
    try:
        import pypdfium2 as pdfium
        pdf = pdfium.PdfDocument(str(pdf_path))
        for i in range(len(pdf)):
            page = pdf[i]
            bitmap = page.render(scale=0.8)
            image = bitmap.to_pil()
            image.save(output_dir / f"segment_{i+1:03d}.png")
    except Exception as exc:
        (output_dir / "preview_error.txt").write_text(str(exc), encoding="utf-8")


def process_pdf(input_path: Path, output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    source_hash = sha256_file(input_path)
    reader = PdfReader(str(input_path))
    writer = PdfWriter()
    manifest = {
        "source_file": input_path.name,
        "sha256": source_hash,
        "pipeline_version": VERSION,
        "source_pdf_modified": False,
        "coordinate_note": "pdfplumber bbox: [x0, top, x1, bottom], points, top-left origin",
        "segments": [],
        "page_reports": [],
        "limitations": [
            "Table detection is heuristic and may miss borderless/irregular tables or detect non-table regions.",
            "PDF crop boxes preserve original page artwork in the crop, but annotation/link behavior depends on the source PDF and viewer.",
            "Full-width vertical crops are used to retain adjacent visual content; review crops where multiple independent sections share the same vertical band.",
            "Scanned pages may need OCR/layout analysis; this script does not perform OCR."
        ],
    }
    with pdfplumber.open(str(input_path)) as pdf:
        for page_idx, layout_page in enumerate(pdf.pages):
            lines = line_candidates(layout_page)
            tables = detect_tables(layout_page)
            images = detect_images(layout_page)
            table_regions = merge_related_content(layout_page, tables, lines, images)
            segments = create_segments(layout_page, table_regions)
            physical_page = reader.pages[page_idx]
            for virtual_idx, seg in enumerate(segments, 1):
                seg_id = f"{source_hash[:12]}-p{page_idx+1}-v{virtual_idx}"
                bbox = [round(float(v), 2) for v in seg["bbox"]]
                add_cropped_page(writer, physical_page, bbox, float(layout_page.height))
                manifest["segments"].append({
                    "segment_id": seg_id,
                    "virtual_page": len(manifest["segments"]) + 1,
                    "segment_type": seg["segment_type"],
                    "associated_table": seg.get("associated_table", False),
                    "source": {
                        "document_id": source_hash,
                        "original_filename": input_path.name,
                        "original_page_number": page_idx + 1,
                        "bbox": bbox,
                        "coordinate_system": "pdfplumber top-left coordinates, points",
                    },
                    "detected_table_bbox": seg.get("table_bbox"),
                    "attached_heading_lines": seg.get("attached_heading_lines", []),
                    "related_images": seg.get("related_images", []),
                    "table_detection": seg.get("table_detection"),
                    "review_required": seg.get("review_required", False),
                })
            manifest["page_reports"].append({
                "original_page": page_idx + 1,
                "width": round(float(layout_page.width), 2),
                "height": round(float(layout_page.height), 2),
                "detected_table_candidates": len(tables),
                "segments_created": len(segments),
                "low_confidence_tables": sum(1 for t in tables if t["review_required"]),
                "images_detected": len(images),
                "headings_detected": sum(1 for line in lines if line["heading_like"]),
            })

    output_pdf = output_dir / f"{input_path.stem}_segmented.pdf"
    with output_pdf.open("wb") as f:
        writer.write(f)
    manifest_path = output_dir / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    report_lines = [
        f"Source: {input_path.name}",
        f"SHA-256: {source_hash}",
        f"Pipeline: {VERSION}",
        f"Segments: {len(manifest['segments'])}",
        "",
    ]
    for p in manifest["page_reports"]:
        report_lines.append(
            f"Original page {p['original_page']}: {p['detected_table_candidates']} table candidates; "
            f"{p['segments_created']} segments; {p['images_detected']} images detected; "
            f"{p['low_confidence_tables']} low-confidence table candidates."
        )
    report_lines.extend(["", "Segments requiring review:"])
    flagged = [s for s in manifest["segments"] if s["review_required"]]
    if not flagged:
        report_lines.append("None flagged by heuristics. Visual review is still recommended.")
    else:
        for s in flagged:
            report_lines.append(
                f"- {s['segment_id']} ({s['segment_type']}), original page "
                f"{s['source']['original_page_number']}, bbox={s['source']['bbox']}"
            )
    report_lines.extend(["", "Limitations:"] + [f"- {x}" for x in manifest["limitations"]])
    (output_dir / "report.txt").write_text("\n".join(report_lines), encoding="utf-8")
    render_preview(output_pdf, output_dir)
    return output_pdf, manifest_path, output_dir / "report.txt"


def main():
    parser = argparse.ArgumentParser(description="Create original-content PDF crops with related-content grouping.")
    parser.add_argument("pdf", help="Path to input PDF")
    parser.add_argument("--output-dir", default=None, help="Output directory (default: <PDF directory>/segmentation_output)")
    args = parser.parse_args()
    input_path = Path(args.pdf).expanduser().resolve()
    if not input_path.exists():
        parser.error(f"Input file not found: {input_path}")
    if input_path.suffix.lower() != ".pdf":
        parser.error("Input must be a .pdf file")
    out_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else input_path.parent / "segmentation_output"
    pdf_path, manifest_path, report_path = process_pdf(input_path, out_dir)
    print(f"Created segmented PDF: {pdf_path}")
    print(f"Manifest: {manifest_path}")
    print(f"Report: {report_path}")
    print(f"Preview PNGs: {out_dir / 'segment_001.png'} etc.")
    print("Review the PDF and report before integrating into production.")


if __name__ == "__main__":
    main()
