"""Coordinate local PDF ingestion and optionally upload its outputs to GCS."""

import json
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
from pdf_extractor import extract_pdf
from relationship_builder import build_relationships
from gcs_ingestor import upload_document_to_gcs
from bigquery_loader import load_gcs_document_to_bigquery


def _generate_and_save_embeddings(chunks_path, relationships_path, output_path):
    with Path(chunks_path).open("r", encoding="utf-8") as f:
        raw = json.load(f)
    chunks = raw if isinstance(raw, list) else raw.get("chunks", raw.get("items", raw.get("records")))
    if not isinstance(chunks, list):
        raise ValueError(f"Unsupported chunks JSON structure: {chunks_path}")

    with Path(relationships_path).open("r", encoding="utf-8") as f:
        relationships = json.load(f)

    chunks = enrich_chunks(chunks, relationships)
    records = [normalize_record(chunk, DEFAULT_MODEL, DEFAULT_DIMENSIONS) for chunk in chunks]
    records = create_embeddings(
        records,
        model=DEFAULT_MODEL,
        dimensions=DEFAULT_DIMENSIONS,
        batch_size=16,
    )

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as f:
        json.dump({
            "model": DEFAULT_MODEL,
            "dimensions": DEFAULT_DIMENSIONS,
            "count": len(records),
            "embeddings_generated": True,
            "chunks": records,
        }, f, ensure_ascii=False, indent=2)
    return records


def process_pdf(
    pdf_path,
    machine_name,
    *,
    generate_embeddings=True,
    upload_to_gcs=True,
    load_to_bigquery=True,
):
    pdf_path = Path(pdf_path)
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")
    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError("Input file must be a PDF")
    if not machine_name or Path(machine_name).name != machine_name:
        raise ValueError("Provide a valid machine folder name")

    document_name = pdf_path.stem
    document_dir = PROCESSED_DATA_DIR / machine_name / document_name
    assets_dir = document_dir / "assets"
    (assets_dir / "images").mkdir(parents=True, exist_ok=True)

    print(f"\nProcessing: {pdf_path.name}\nMachine: {machine_name}")
    extracted = extract_pdf(pdf_path, assets_dir)
    if not isinstance(extracted, dict):
        raise TypeError("extract_pdf() must return a dictionary")

    document_dir.mkdir(parents=True, exist_ok=True)
    extraction_path = document_dir / "extraction.json"
    extraction_path.write_text(json.dumps(extracted, ensure_ascii=False, indent=2), encoding="utf-8")

    relationships = build_relationships(extracted)
    relationship_path = document_dir / "relationships.json"
    relationship_path.write_text(json.dumps(relationships, ensure_ascii=False, indent=2), encoding="utf-8")

    chunks = build_all_chunks(
        extracted,
        machine_name,
        extracted.get("document", pdf_path.name),
        relationships_data=relationships,
    )
    chunks_path = document_dir / "chunks.json"
    save_chunks(chunks, chunks_path)
    print(f"Saved {len(chunks)} chunks: {chunks_path}")

    if generate_embeddings:
        embedded_path = document_dir / "embedded_chunks.json"
        records = _generate_and_save_embeddings(chunks_path, relationship_path, embedded_path)
        print(f"Saved {len(records)} embedded chunks: {embedded_path}")

    if upload_to_gcs:
        gcs_prefix = upload_document_to_gcs(document_dir, machine_name, document_name)

        # BigQuery ingestion is a separate module and runs only after the GCS
        # uploader has returned successfully for the whole document directory.
        if load_to_bigquery and generate_embeddings:
            folder = f"processed/{machine_name}/{document_name}/"
            print(f"\nLoading uploaded artifacts into BigQuery: {gcs_prefix}")
            load_gcs_document_to_bigquery(folder, commit=True)
        elif load_to_bigquery and not generate_embeddings:
            print("[SKIP] BigQuery load requires embeddings; --no-embeddings was enabled.")
    elif load_to_bigquery:
        print("[SKIP] BigQuery load requires a successful GCS upload; upload_to_gcs=False.")

    return document_dir


def process_all_pdfs(*, generate_embeddings=True, upload_to_gcs=True, load_to_bigquery=True):
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
                load_to_bigquery=load_to_bigquery,
            )
        except Exception as exc:
            failures.append((pdf_path, str(exc)))
            print(f"FAILED: {pdf_path}\nReason: {exc}")

    print(f"\nPDFs found: {len(pdf_files)}; failed: {len(failures)}")
    for path, reason in failures:
        print(f"- {path}: {reason}")
