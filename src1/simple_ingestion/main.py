
"""Local CLI entry point for the segmentation-first manufacturing PDF pipeline."""

import argparse
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from google.cloud import storage
from pipeline import process_all_pdfs, process_pdf


def process_gcs_uri(gcs_uri: str, *, upload_to_gcs=False):
    """Download one raw GCS PDF temporarily and process it locally."""

    parsed = urlparse(gcs_uri)

    if parsed.scheme != "gs" or not parsed.netloc:
        raise ValueError(
            "Expected a GCS URI like "
            "gs://bucket-name/raw/MACHINE_NAME/manual.pdf"
        )

    bucket_name = parsed.netloc
    object_name = parsed.path.lstrip("/")
    path_parts = object_name.split("/")

    if len(path_parts) < 3:
        raise ValueError(
            "Expected object path: raw/MACHINE_NAME/filename.pdf"
        )

    if path_parts[0] != "raw":
        raise ValueError("The PDF must be inside the raw/ prefix")

    machine_name = path_parts[1]
    filename = path_parts[-1]

    if not filename.lower().endswith(".pdf"):
        raise ValueError("The GCS object must be a PDF")

    print(f"Reading PDF from: {gcs_uri}")
    print(f"Machine: {machine_name}")

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(object_name)

    with tempfile.TemporaryDirectory(
        prefix="manufacturing_rag_"
    ) as temp_dir:
        local_pdf = Path(temp_dir) / filename

        blob.download_to_filename(str(local_pdf))
        print(f"Downloaded temporary PDF: {local_pdf}")

        return process_pdf(
            local_pdf,
            machine_name,
            generate_embeddings=False,
            upload_to_gcs=upload_to_gcs,
        )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Run PDF segmentation, extraction, relationship building, "
            "chunking, and optional GCS upload."
        )
    )

    parser.add_argument(
        "pdf_path",
        nargs="?",
        help="Path to one PDF",
    )
    parser.add_argument(
        "machine_name",
        nargs="?",
        help="Machine folder name for one PDF",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Process all PDFs under RAW_DATA_DIR",
    )
    parser.add_argument(
        "--embeddings",
        action="store_true",
        help=(
            "Generate Gemini embeddings "
            "(requires Google auth and may incur charges)"
        ),
    )
    parser.add_argument(
        "--gcs-uri",
        help="Process one PDF directly from a GCS URI",
    )
    parser.add_argument(
        "--upload-to-gcs",
        action="store_true",
        help="Upload processed artifacts to GCS after processing",
    )

    args = parser.parse_args()

    if args.gcs_uri:
        if args.all or args.pdf_path or args.machine_name:
            parser.error(
                "--gcs-uri cannot be combined with a PDF path, "
                "machine name, or --all"
            )

        try:
            process_gcs_uri(
                args.gcs_uri,
                upload_to_gcs=args.upload_to_gcs,
            )
        except Exception as exc:
            print(f"\nGCS processing failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc

        return

    if args.all:
        if args.pdf_path or args.machine_name:
            parser.error(
                "Do not provide a PDF path or machine name with --all"
            )

        try:
            process_all_pdfs(
                generate_embeddings=args.embeddings,
                upload_to_gcs=args.upload_to_gcs,
            )
        except Exception as exc:
            print(f"\nProcessing failed: {exc}", file=sys.stderr)
            raise SystemExit(1) from exc

        return

    if not args.pdf_path or not args.machine_name:
        parser.error(
            'Provide "path/to/file.pdf" "MACHINE_NAME", or use --all'
        )

    try:
        process_pdf(
            Path(args.pdf_path),
            args.machine_name,
            generate_embeddings=args.embeddings,
            upload_to_gcs=args.upload_to_gcs,
        )
    except Exception as exc:
        print(f"\nProcessing failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
