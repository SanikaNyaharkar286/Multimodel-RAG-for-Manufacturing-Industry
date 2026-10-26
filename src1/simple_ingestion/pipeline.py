"""Manufacturing PDF pipeline plus reprocessing of existing extracted artifacts.

The existing-PDF path remains unchanged. The processed-artifact path lets us
rebuild chunks from extraction.json + relationships.json without segmenting or
extracting PDFs again, then optionally upload to GCS and invoke the BQ loader.
"""
import json
import shutil
import uuid
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

    chunks = raw if isinstance(raw, list) else raw.get(
        "chunks", raw.get("items", raw.get("records"))
    )
    if not isinstance(chunks, list):
        raise ValueError(f"Unsupported chunks JSON structure: {chunks_path}")

    with Path(relationships_path).open("r", encoding="utf-8") as f:
        relationships = json.load(f)

    chunks = enrich_chunks(chunks, relationships)
    chunk_ids = [chunk.get("chunk_id") for chunk in chunks]
    if any(not chunk_id for chunk_id in chunk_ids):
        raise ValueError("A chunk has a missing chunk_id.")
    if len(chunk_ids) != len(set(chunk_ids)):
        raise ValueError("Duplicate chunk IDs found. Fix chunks before embedding.")

    records = [
        normalize_record(chunk, DEFAULT_MODEL, DEFAULT_DIMENSIONS)
        for chunk in chunks
    ]
    records = create_embeddings(
        records, model=DEFAULT_MODEL, dimensions=DEFAULT_DIMENSIONS, batch_size=16
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
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    segments = manifest.get("segments", [])

    for index, page in enumerate(extracted.get("pages", [])):
        if index >= len(segments):
            page["source_mapping_warning"] = "No matching segmentation manifest entry"
            continue

        segment = segments[index]
        source = segment.get("source", {})
        crop_bbox = source.get("bbox", [0, 0, 0, 0])
        x_offset, y_offset = float(crop_bbox[0]), float(crop_bbox[1])
        virtual_page_number = page.get("page_number", index + 1)

        page["virtual_page_number"] = virtual_page_number
        page["page_number"] = source.get("original_page_number", virtual_page_number)
        page["segment_id"] = segment.get("segment_id")
        page["segment_type"] = segment.get("segment_type")
        page["segment_bbox"] = crop_bbox
        page["source_document_id"] = source.get("document_id")
        page["source_filename"] = source.get("original_filename")
        page["source_coordinate_system"] = source.get("coordinate_system")

        for collection in ("paragraphs", "tables", "images", "links"):
            for element in page.get(collection, []) or []:
                bbox = element.get("bbox")
                if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
                    element["virtual_bbox"] = list(bbox)
                    element["bbox"] = [
                        float(bbox[0]) + x_offset, float(bbox[1]) + y_offset,
                        float(bbox[2]) + x_offset, float(bbox[3]) + y_offset,
                    ]
                if collection == "tables":
                    table_id = element.get("id")
                    if table_id:
                        element["id"] = f"v{virtual_page_number}_{table_id}"
                element["original_page_number"] = page["page_number"]
                element["virtual_page_number"] = virtual_page_number
                element["segment_id"] = page["segment_id"]

    extracted["segmentation"] = {
        "manifest_file": str(manifest_path),
        "source_file": manifest.get("source_file"),
        "pipeline_version": manifest.get("pipeline_version"),
        "segment_count": len(segments),
        "source_pdf_modified": manifest.get("source_pdf_modified", False),
    }
    return extracted


def _validate_chunk_uuids(chunks, document_dir):
    if not isinstance(chunks, list):
        raise ValueError(f"Expected a list of chunks in {document_dir}")
    seen = set()
    for index, chunk in enumerate(chunks):
        chunk_id = chunk.get("chunk_id", f"index={index}")
        value = chunk.get("chunk_uuid")
        if not value:
            raise ValueError(f"{document_dir}: chunk {chunk_id} has no chunk_uuid")
        try:
            parsed = uuid.UUID(str(value))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ValueError(
                f"{document_dir}: chunk {chunk_id} has invalid UUID {value!r}"
            ) from exc
        normalized = str(parsed)
        if normalized in seen:
            raise ValueError(f"{document_dir}: duplicate chunk_uuid {normalized}")
        seen.add(normalized)
    return len(seen)


def _upload_and_transfer_audit(document_dir, machine_name):
    print("\nUploading processed artifacts to GCS...")
    failures = upload_directory(source_dir=document_dir, machine_name=machine_name)
    if failures:
        raise RuntimeError(f"GCS upload failed for {failures} file(s).")

    print("Transferring GCS upload audit records to BigQuery...")
    audit_result = transfer_audits()
    if audit_result != 0:
        raise RuntimeError("GCS audit transfer reported failures.")
    print("GCS upload and audit transfer completed.")


def process_existing_document(
    machine_name,
    document_name,
    *,
    rebuild_chunks=True,
    upload_to_gcs=False,
    load_to_bigquery=False,
    commit_bigquery=False,
    tables="all",
    generate_embeddings=False,
):
    """Rebuild chunks from existing JSON, optionally upload and load one document.

    BigQuery dry-run is the default when load_to_bigquery=True. Embeddings are
    generated locally before upload when requested, then the loader reuses the
    precomputed embedded_chunks.json artifact.
    """
    if not machine_name or Path(machine_name).name != machine_name:
        raise ValueError("Invalid machine name.")
    if not document_name or Path(document_name).name != document_name:
        raise ValueError("Invalid document folder name.")
    if commit_bigquery and not load_to_bigquery:
        raise ValueError("--commit-bigquery requires --load-to-bigquery.")

    document_dir = PROCESSED_DATA_DIR / machine_name / document_name
    extraction_path = document_dir / "extraction.json"
    relationships_path = document_dir / "relationships.json"
    chunks_path = document_dir / "chunks.json"

    for required in (extraction_path, relationships_path):
        if not required.is_file():
            raise FileNotFoundError(f"Required artifact not found: {required}")

    if rebuild_chunks:
        with extraction_path.open("r", encoding="utf-8") as f:
            extracted = json.load(f)
        with relationships_path.open("r", encoding="utf-8") as f:
            relationships = json.load(f)

        # Match the existing pipeline's document naming convention.
        document_name_for_chunks = extracted.get("document", document_name)
        chunks = build_all_chunks(
            extracted,
            machine_name,
            document_name_for_chunks,
            relationships_data=relationships,
        )
        _validate_chunk_uuids(chunks, document_dir)
        save_chunks(chunks, chunks_path)
        print(f"Regenerated {len(chunks)} chunks with valid UUIDs: {chunks_path}")
    else:
        if not chunks_path.is_file():
            raise FileNotFoundError(f"Chunks file not found: {chunks_path}")
        with chunks_path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        chunks = raw if isinstance(raw, list) else raw.get(
            "chunks", raw.get("items", raw.get("records"))
        )
        count = _validate_chunk_uuids(chunks, document_dir)
        print(f"Validated {count} existing chunks: {chunks_path}")

    if generate_embeddings or load_to_bigquery:
        embedded_path = document_dir / "embedded_chunks.json"
        if not embedded_path.is_file() or generate_embeddings or rebuild_chunks:
            records = _generate_and_save_embeddings(
                chunks_path, relationships_path, embedded_path
            )
            print(f"Saved {len(records)} embedded chunks: {embedded_path}")
        else:
            print(f"Reusing existing embeddings: {embedded_path}")

    if load_to_bigquery and not upload_to_gcs:
        raise ValueError("BigQuery loading requires --upload-to-gcs so artifacts are available in GCS.")

    if upload_to_gcs:
        _upload_and_transfer_audit(document_dir, machine_name)

    if load_to_bigquery:
        # Imported only when requested, so PDF-only runs do not require the loader.
        from bigquery_loader import load_gcs_document_to_bigquery, TABLES

        selected_tables = TABLES if tables.strip().lower() == "all" else tuple(
            item.strip() for item in tables.split(",") if item.strip()
        )
        invalid = set(selected_tables) - set(TABLES)
        if not selected_tables or invalid:
            raise ValueError(
                f"Unsupported BigQuery tables: {sorted(invalid)}. "
                f"Available: {', '.join(TABLES)}"
            )
        gcs_folder = f"{machine_name}/{document_name}/"
        print(
            f"\nLoading gs:// configured-bucket/processed/{gcs_folder} "
            f"into BigQuery ({'COMMIT' if commit_bigquery else 'DRY RUN'})."
        )
        counts = load_gcs_document_to_bigquery(
            gcs_folder, selected_tables, commit=commit_bigquery
        )
        print("BigQuery loader row counts:")
        for table, count in counts.items():
            print(f"  {table}: {count}")

    return document_dir


def process_pdf(
    pdf_path,
    machine_name,
    *,
    generate_embeddings=False,
    upload_to_gcs=False,
    load_to_bigquery=False,
    commit_bigquery=False,
    tables="all",
):
    """Process a PDF locally and optionally upload artifacts and audit records."""
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
    original_copy = document_dir / pdf_path.name
    if pdf_path.resolve() != original_copy.resolve():
        shutil.copy2(pdf_path, original_copy)

    segmentation_dir = document_dir / "segmentation"
    print(f"\nStep 1/4: Segmenting PDF: {pdf_path.name}")
    segmented_pdf, manifest_path, report_path = split_pdf(pdf_path, segmentation_dir)
    print(f"Saved segmented PDF: {segmented_pdf}")
    print(f"Saved segmentation manifest: {manifest_path}")
    print(f"Saved segmentation report: {report_path}")

    assets_dir = document_dir / "assets"
    (assets_dir / "images").mkdir(parents=True, exist_ok=True)
    print("Step 2/4: Extracting text, tables, images and links")
    extracted = extract_pdf(segmented_pdf, assets_dir)
    if not isinstance(extracted, dict):
        raise TypeError("extract_pdf() must return a dictionary")
    extracted = _apply_original_source_mapping(extracted, manifest_path)
    extracted["document"] = pdf_path.name

    extraction_path = document_dir / "extraction.json"
    extraction_path.write_text(
        json.dumps(extracted, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("Step 3/4: Building relationships and chunks")
    relationships = build_relationships(extracted)
    relationship_path = document_dir / "relationships.json"
    relationship_path.write_text(
        json.dumps(relationships, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    chunks = build_all_chunks(
        extracted, machine_name, pdf_path.name, relationships_data=relationships
    )
    _validate_chunk_uuids(chunks, document_dir)
    chunks_path = document_dir / "chunks.json"
    save_chunks(chunks, chunks_path)
    print(f"Saved {len(chunks)} chunks: {chunks_path}")

    if generate_embeddings or load_to_bigquery:
        embedded_path = document_dir / "embedded_chunks.json"
        records = _generate_and_save_embeddings(
            chunks_path, relationship_path, embedded_path
        )
        print(f"Saved {len(records)} embedded chunks: {embedded_path}")

    if load_to_bigquery and not upload_to_gcs:
        raise ValueError("BigQuery loading requires upload_to_gcs=True.")

    if upload_to_gcs:
        _upload_and_transfer_audit(document_dir, machine_name)

    if load_to_bigquery:
        from bigquery_loader import load_gcs_document_to_bigquery, TABLES
        selected_tables = TABLES if tables.strip().lower() == "all" else tuple(
            item.strip() for item in tables.split(",") if item.strip()
        )
        invalid = set(selected_tables) - set(TABLES)
        if not selected_tables or invalid:
            raise ValueError(
                f"Unsupported BigQuery tables: {sorted(invalid)}. "
                f"Available: {', '.join(TABLES)}"
            )
        gcs_folder = f"{machine_name}/{document_name}/"
        print(
            f"\nLoading gs:// configured-bucket/processed/{gcs_folder} "
            f"into BigQuery ({'COMMIT' if commit_bigquery else 'DRY RUN'})."
        )
        counts = load_gcs_document_to_bigquery(
            gcs_folder, selected_tables, commit=commit_bigquery
        )
        print("BigQuery loader row counts:")
        for table, count in counts.items():
            print(f"  {table}: {count}")

    print(f"\nLocal processing complete: {document_dir}")
    return document_dir


def process_all_pdfs(
    *,
    generate_embeddings=False,
    upload_to_gcs=False,
    load_to_bigquery=False,
    commit_bigquery=False,
    tables="all",
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
                generate_embeddings=(generate_embeddings or load_to_bigquery),
                upload_to_gcs=upload_to_gcs,
                load_to_bigquery=load_to_bigquery,
                commit_bigquery=commit_bigquery,
                tables=tables,
            )
        except Exception as exc:
            failures.append((pdf_path, str(exc)))
            print(f"FAILED: {pdf_path}\nReason: {exc}")

    print(f"\nPDFs found: {len(pdf_files)}; failed: {len(failures)}")
    for path, reason in failures:
        print(f"- {path}: {reason}")
    if failures:
        raise RuntimeError(f"{len(failures)} PDF(s) failed. Review the errors above.")
