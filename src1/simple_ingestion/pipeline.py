
"""Local pipeline: segment, extract, chunk, upload, and audit manufacturing PDFs."""

import json
import shutil
from pathlib import Path

from config import RAW_DATA_DIR, PROCESSED_DATA_DIR
from chunk_builder import build_all_chunks, save_chunks
from embedding_generator import (
    DEFAULT_DIMENSIONS,
    DEFAULT_MODEL,
    enrich_chunks,
    generate_embeddings as create_embeddings,
    normalize_record,
)
from processed_uploader import upload_directory
from gcs_audit_to_bigquery import transfer_audits
from pdf_extractor import extract_pdf
from pdf_segmenter import process_pdf as split_pdf
from relationship_builder import build_relationships


def _generate_and_save_embeddings(chunks_path, relationships_path, output_path):
    with Path(chunks_path).open("r", encoding="utf-8") as f:
        raw = json.load(f)

    chunks = (
        raw
        if isinstance(raw, list)
        else raw.get("chunks", raw.get("items", raw.get("records")))
    )

    if not isinstance(chunks, list):
        raise ValueError(f"Unsupported chunks JSON structure: {chunks_path}")

    with Path(relationships_path).open("r", encoding="utf-8") as f:
        relationships = json.load(f)

    chunks = enrich_chunks(chunks, relationships)

    records = [
        normalize_record(chunk, DEFAULT_MODEL, DEFAULT_DIMENSIONS)
        for chunk in chunks
    ]

    records = create_embeddings(
        records,
        model=DEFAULT_MODEL,
        dimensions=DEFAULT_DIMENSIONS,
        batch_size=16,
    )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8") as f:
        json.dump(
            {
                "model": DEFAULT_MODEL,
                "dimensions": DEFAULT_DIMENSIONS,
                "count": len(records),
                "embeddings_generated": True,
                "chunks": records,
            },
            f,
            ensure_ascii=False,
            indent=2,
        )

    return records


def _apply_original_source_mapping(extracted, manifest_path):
    """Restore original page numbers and coordinates after virtual-page extraction."""

    manifest = json.loads(
        Path(manifest_path).read_text(encoding="utf-8")
    )
    segments = manifest.get("segments", [])

    for index, page in enumerate(extracted.get("pages", [])):
        if index >= len(segments):
            page["source_mapping_warning"] = (
                "No matching segmentation manifest entry"
            )
            continue

        segment = segments[index]
        source = segment.get("source", {})
        crop_bbox = source.get("bbox", [0, 0, 0, 0])

        x_offset = float(crop_bbox[0])
        y_offset = float(crop_bbox[1])

        virtual_page_number = page.get("page_number", index + 1)

        page["virtual_page_number"] = virtual_page_number
        page["page_number"] = source.get(
            "original_page_number",
            virtual_page_number,
        )
        page["segment_id"] = segment.get("segment_id")
        page["segment_type"] = segment.get("segment_type")
        page["segment_bbox"] = crop_bbox
        page["source_document_id"] = source.get("document_id")
        page["source_filename"] = source.get("original_filename")
        page["source_coordinate_system"] = source.get(
            "coordinate_system"
        )

        for collection in ("paragraphs", "tables", "images", "links"):
            for element in page.get(collection, []) or []:
                bbox = element.get("bbox")

                if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
                    element["virtual_bbox"] = list(bbox)
                    element["bbox"] = [
                        float(bbox[0]) + x_offset,
                        float(bbox[1]) + y_offset,
                        float(bbox[2]) + x_offset,
                        float(bbox[3]) + y_offset,
                    ]

                element["original_page_number"] = page["page_number"]
                element["virtual_page_number"] = virtual_page_number
                element["segment_id"] = page["segment_id"]

    extracted["segmentation"] = {
        "manifest_file": str(manifest_path),
        "source_file": manifest.get("source_file"),
        "pipeline_version": manifest.get("pipeline_version"),
        "segment_count": len(segments),
        "source_pdf_modified": manifest.get(
            "source_pdf_modified", False
        ),
    }

    return extracted


def process_pdf(
    pdf_path,
    machine_name,
    *,
    generate_embeddings=False,
    upload_to_gcs=False,
):
    """Process a PDF locally and optionally upload artifacts and transfer audit records."""

    pdf_path = Path(pdf_path).expanduser().resolve()

    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError("Input file must be a PDF")

    if not machine_name or Path(machine_name).name != machine_name:
        raise ValueError("Provide a valid machine folder name")

    document_name = pdf_path.stem
    document_dir = PROCESSED_DATA_DIR / machine_name / document_name
    document_dir.mkdir(parents=True, exist_ok=True)

    # Preserve the original PDF.
    original_copy = document_dir / pdf_path.name

    if pdf_path.resolve() != original_copy.resolve():
        shutil.copy2(pdf_path, original_copy)

    # Step 1: Segment the original PDF.
    segmentation_dir = document_dir / "segmentation"

    print(f"\nStep 1/4: Segmenting PDF: {pdf_path.name}")

    segmented_pdf, manifest_path, report_path = split_pdf(
        pdf_path,
        segmentation_dir,
    )

    print(f"Saved segmented PDF: {segmented_pdf}")
    print(f"Saved segmentation manifest: {manifest_path}")
    print(f"Saved segmentation report: {report_path}")

    # Step 2: Extract content from the segmented PDF.
    assets_dir = document_dir / "assets"
    (assets_dir / "images").mkdir(parents=True, exist_ok=True)

    print("Step 2/4: Extracting text, tables, images and links")

    extracted = extract_pdf(segmented_pdf, assets_dir)

    if not isinstance(extracted, dict):
        raise TypeError("extract_pdf() must return a dictionary")

    extracted = _apply_original_source_mapping(
        extracted,
        manifest_path,
    )
    extracted["document"] = pdf_path.name

    extraction_path = document_dir / "extraction.json"
    extraction_path.write_text(
        json.dumps(extracted, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    # Step 3: Build relationships and chunks.
    print("Step 3/4: Building relationships and chunks")

    relationships = build_relationships(extracted)

    relationship_path = document_dir / "relationships.json"
    relationship_path.write_text(
        json.dumps(relationships, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    chunks = build_all_chunks(
        extracted,
        machine_name,
        pdf_path.name,
        relationships_data=relationships,
    )

    chunks_path = document_dir / "chunks.json"
    save_chunks(chunks, chunks_path)

    print(f"Saved {len(chunks)} chunks: {chunks_path}")

    # Embeddings remain optional.
    if generate_embeddings:
        embedded_path = document_dir / "embedded_chunks.json"

        records = _generate_and_save_embeddings(
            chunks_path,
            relationship_path,
            embedded_path,
        )

        print(f"Saved {len(records)} embedded chunks: {embedded_path}")

    # Step 4: Upload processed artifacts and transfer GCS audit events.
    if upload_to_gcs:
        print("\nStep 4/4: Uploading processed artifacts to GCS")

        upload_failures = 0
        upload_exception = None

        try:
            upload_failures = upload_directory(
                source_dir=document_dir,
                machine_name=machine_name,
            )
        except Exception as exc:
            upload_exception = exc
            print(f"[ERROR] Processed upload raised an exception: {exc}")

        # Attempt audit transfer even if some artifact uploads failed.
        print("\nTransferring GCS audit events to BigQuery...")

        try:
            audit_result = transfer_audits()

            if audit_result != 0:
                raise RuntimeError(
                    "The GCS audit transfer reported failures. "
                    "Check the transfer output."
                )

            print("GCS audit transfer to BigQuery completed successfully.")

        except Exception as exc:
            raise RuntimeError(
                "The processed upload attempt finished, but the audit "
                "transfer to BigQuery failed. Check the GCS audit JSON "
                "files and BigQuery transfer error."
            ) from exc

        if upload_exception is not None:
            raise RuntimeError(
                "The processed artifact uploader raised an exception. "
                "The audit transfer was attempted; check the preceding "
                "output for details."
            ) from upload_exception

        if upload_failures:
            raise RuntimeError(
                f"Processed artifact upload failed for "
                f"{upload_failures} file(s). Audit transfer was attempted."
            )

        print("All processed artifacts uploaded successfully.")

    print(f"\nLocal processing complete: {document_dir}")
    return document_dir


def process_all_pdfs(
    *,
    generate_embeddings=False,
    upload_to_gcs=False,
):
    """Process every PDF under RAW_DATA_DIR."""

    pdf_files = sorted(RAW_DATA_DIR.rglob("*.pdf"))

    if not pdf_files:
        print(f"No PDFs found under: {RAW_DATA_DIR}")
        return

    failures = []

    for pdf_path in pdf_files:
        relative_path = pdf_path.relative_to(RAW_DATA_DIR)

        if len(relative_path.parts) < 2:
            failures.append((pdf_path, "No machine folder found"))
            continue

        try:
            process_pdf(
                pdf_path,
                relative_path.parts[0],
                generate_embeddings=generate_embeddings,
                upload_to_gcs=upload_to_gcs,
            )

        except Exception as exc:
            failures.append((pdf_path, str(exc)))
            print(f"FAILED: {pdf_path}\nReason: {exc}")

    print(f"\nPDFs found: {len(pdf_files)}; failed: {len(failures)}")

    for path, reason in failures:
        print(f"- {path}: {reason}")

    if failures:
        raise RuntimeError(
            f"{len(failures)} PDF(s) failed. Review the errors above."
        )
