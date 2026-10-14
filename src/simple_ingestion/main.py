"""CLI entry point. Workflow logic lives in pipeline.py and gcs_ingestor.py."""

import argparse
import sys
from pathlib import Path

from pipeline import process_all_pdfs, process_pdf


def main():
    parser = argparse.ArgumentParser(description="Run the manufacturing manual ingestion pipeline.")
    parser.add_argument("pdf_path", nargs="?", help="Path to one PDF")
    parser.add_argument("machine_name", nargs="?", help="Machine folder name for one PDF")
    parser.add_argument("--all", action="store_true", help="Process all PDFs under RAW_DATA_DIR")
    parser.add_argument("--no-embeddings", action="store_true", help="Skip embedding generation")
    parser.add_argument("--no-gcs", action="store_true", help="Skip GCS upload")
    args = parser.parse_args()

    if args.all:
        if args.pdf_path or args.machine_name:
            parser.error("Do not provide a PDF path or machine name with --all")
        process_all_pdfs(
            generate_embeddings=not args.no_embeddings,
            upload_to_gcs=not args.no_gcs,
        )
        return

    if not args.pdf_path or not args.machine_name:
        parser.error('Provide "path/to/file.pdf" "MACHINE_NAME", or use --all')

    try:
        process_pdf(
            Path(args.pdf_path),
            args.machine_name,
            generate_embeddings=not args.no_embeddings,
            upload_to_gcs=not args.no_gcs,
        )
    except Exception as exc:
        print(f"\nProcessing failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
