"""CLI entry point for the manufacturing RAG ingestion workflow."""
import argparse
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from google.cloud import storage
from pipeline import process_all_pdfs, process_pdf, process_existing_document


def process_gcs_uri(
    gcs_uri: str, *, upload_to_gcs=False, load_to_bigquery=False,
    commit_bigquery=False, tables="all",
):
    """Download one raw GCS PDF temporarily and process it locally."""
    parsed = urlparse(gcs_uri)
    if parsed.scheme != "gs" or not parsed.netloc:
        raise ValueError("Expected a GCS URI like gs://bucket-name/raw/MACHINE_NAME/manual.pdf")

    bucket_name = parsed.netloc
    object_name = parsed.path.lstrip("/")
    path_parts = object_name.split("/")
    if len(path_parts) < 3:
        raise ValueError("Expected object path: raw/MACHINE_NAME/filename.pdf")
    if path_parts[0] != "raw":
        raise ValueError("The PDF must be inside the raw/ prefix")
    machine_name = path_parts[1]
    filename = path_parts[-1]
    if not filename.lower().endswith(".pdf"):
        raise ValueError("The GCS object must be a PDF")

    print(f"Reading PDF from: {gcs_uri}")
    print(f"Machine: {machine_name}")
    client = storage.Client()
    blob = client.bucket(bucket_name).blob(object_name)
    with tempfile.TemporaryDirectory(prefix="manufacturing_rag_") as temp_dir:
        local_pdf = Path(temp_dir) / filename
        blob.download_to_filename(str(local_pdf))
        print(f"Downloaded temporary PDF: {local_pdf}")
        return process_pdf(
            local_pdf, machine_name,
            generate_embeddings=load_to_bigquery,
            upload_to_gcs=(upload_to_gcs or load_to_bigquery),
            load_to_bigquery=load_to_bigquery,
            commit_bigquery=commit_bigquery,
            tables=tables,
        )


def main():
    parser = argparse.ArgumentParser(
        description="Process PDFs or reuse existing extracted artifacts for GCS/BigQuery."
    )
    parser.add_argument("pdf_path", nargs="?", help="Path to one PDF")
    parser.add_argument("machine_name", nargs="?", help="Machine folder name for one PDF")
    parser.add_argument("--all", action="store_true", help="Process all PDFs under RAW_DATA_DIR")
    parser.add_argument("--embeddings", action="store_true",
                        help="Generate and save embedded_chunks.json during PDF processing")
    parser.add_argument("--gcs-uri", help="Process one PDF directly from a raw GCS URI")
    parser.add_argument("--upload-to-gcs", action="store_true",
                        help="Upload processed artifacts to GCS")
    parser.add_argument("--load-to-bigquery", action="store_true",
                        help="Load processed artifacts into BigQuery (dry-run unless --commit-bigquery)")
    parser.add_argument("--commit-bigquery", action="store_true",
                        help="Commit the BigQuery load; requires --load-to-bigquery")
    parser.add_argument("--tables", default="all",
                        help="Comma-separated BigQuery tables or 'all'")

    # Existing processed artifacts path; does not rerun segmentation or PDF extraction.
    parser.add_argument("--existing-machine", help="Machine folder under data/processed, e.g. HAB")
    parser.add_argument("--existing-document", help="Exact document folder under that machine")
    parser.add_argument("--no-rebuild-chunks", action="store_true",
                        help="Validate existing chunks.json rather than rebuilding it")
    args = parser.parse_args()

    if args.commit_bigquery and not args.load_to_bigquery:
        parser.error("--commit-bigquery requires --load-to-bigquery")

    existing_mode = bool(args.existing_machine or args.existing_document or args.no_rebuild_chunks)
    if existing_mode:
        if not args.existing_machine or not args.existing_document:
            parser.error("--existing-machine and --existing-document must be provided together")
        if args.pdf_path or args.machine_name or args.all or args.gcs_uri:
            parser.error("Existing-artifact mode cannot be combined with PDF processing options")
        try:
            process_existing_document(
                args.existing_machine,
                args.existing_document,
                rebuild_chunks=not args.no_rebuild_chunks,
                upload_to_gcs=(args.upload_to_gcs or args.load_to_bigquery),
                load_to_bigquery=args.load_to_bigquery,
                commit_bigquery=args.commit_bigquery,
                tables=args.tables,
                generate_embeddings=args.embeddings,
            )
        except Exception as exc:
            print(f"\nExisting-artifact workflow failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        return

    if args.gcs_uri:
        if args.all or args.pdf_path or args.machine_name:
            parser.error("--gcs-uri cannot be combined with a PDF path, machine name, or --all")
        try:
            process_gcs_uri(
                args.gcs_uri,
                upload_to_gcs=(args.upload_to_gcs or args.load_to_bigquery),
                load_to_bigquery=args.load_to_bigquery,
                commit_bigquery=args.commit_bigquery,
                tables=args.tables,
            )
        except Exception as exc:
            print(f"\nGCS processing failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        return

    if args.all:
        if args.pdf_path or args.machine_name:
            parser.error("Do not provide a PDF path or machine name with --all")
        try:
            process_all_pdfs(
                generate_embeddings=(args.embeddings or args.load_to_bigquery),
                upload_to_gcs=(args.upload_to_gcs or args.load_to_bigquery),
                load_to_bigquery=args.load_to_bigquery,
                commit_bigquery=args.commit_bigquery,
                tables=args.tables,
            )
        except Exception as exc:
            print(f"\nProcessing failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc
        return

    if not args.pdf_path or not args.machine_name:
        parser.error('Provide "path/to/file.pdf" "MACHINE_NAME", use --all, or select existing-artifact mode')

    try:
        process_pdf(
            Path(args.pdf_path), args.machine_name,
            generate_embeddings=(args.embeddings or args.load_to_bigquery),
            upload_to_gcs=(args.upload_to_gcs or args.load_to_bigquery),
            load_to_bigquery=args.load_to_bigquery,
            commit_bigquery=args.commit_bigquery,
            tables=args.tables,
        )
    except Exception as exc:
        print(f"\nProcessing failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
