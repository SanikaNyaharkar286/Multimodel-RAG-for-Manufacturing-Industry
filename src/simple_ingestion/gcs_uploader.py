
"""
Upload a validated PDF to Google Cloud Storage.

Default destination:
    gs://<bucket>/raw/<machine_name>/<filename>.pdf

Dry-run mode validates the PDF and prints the destination without
connecting to Google Cloud or uploading anything.
"""
import time
import uuid
from gcs_audit import write_gcs_audit
import argparse
import os
import re
import sys
from pathlib import Path
from config import (
    GCS_BUCKET_NAME,
    GOOGLE_CLOUD_PROJECT,
    GCS_RAW_PREFIX,
    GCS_ALLOW_OVERWRITE,
)
try:
    import pymupdf
except ImportError:
    pymupdf = None


def validate_machine_name(machine_name: str) -> str:
    """Allow simple machine names for safe GCS object paths."""
    machine_name = machine_name.strip()

    if not machine_name:
        raise ValueError("Machine name cannot be empty.")

    if not re.fullmatch(r"[A-Za-z0-9._-]+", machine_name):
        raise ValueError(
            "Machine name can contain only letters, numbers, "
            "periods, underscores, and hyphens."
        )

    return machine_name


def validate_pdf(file_path: Path) -> dict:
    """Check that the file exists and is a readable PDF."""

    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    if not file_path.is_file():
        raise ValueError(f"Path is not a file: {file_path}")

    if file_path.suffix.lower() != ".pdf":
        raise ValueError("The input file must have a .pdf extension.")

    file_size = file_path.stat().st_size

    if file_size == 0:
        raise ValueError("The PDF file is empty.")

    with file_path.open("rb") as file:
        header = file.read(1024)

    if b"%PDF-" not in header:
        raise ValueError("The file does not appear to contain a PDF header.")

    if pymupdf is None:
        raise ImportError(
            "PyMuPDF is not installed. Install it with:\n"
            "python -m pip install pymupdf"
        )

    try:
        with pymupdf.open(str(file_path)) as pdf:
            if pdf.is_encrypted:
                raise ValueError(
                    "The PDF is encrypted. Decrypt it before uploading."
                )

            page_count = pdf.page_count

            if page_count < 1:
                raise ValueError("The PDF has no pages.")

            # Force PyMuPDF to read each page to detect basic read errors.
            for page in pdf:
                page.get_text()

    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"Could not read the PDF: {exc}") from exc

    return {
        "path": file_path,
        "size": file_size,
        "pages": page_count,
    }


def build_object_name(
    machine_name: str,
    filename: str,
    raw_prefix: str = "raw",
) -> str:
    """Build the destination path inside the GCS bucket."""

    prefix = raw_prefix.strip("/")

    if not prefix:
        prefix = "raw"

    return f"{prefix}/{machine_name}/{filename}"

def ensure_gcs_prefix_exists(bucket, prefix: str) -> None:
    """
    Reuse an existing GCS prefix.
    If no object exists under the prefix, create a folder marker.

    GCS folders are normally object-name prefixes, not real directories.
    """
    prefix = prefix.strip("/")

    if not prefix:
        raise ValueError("GCS prefix cannot be empty.")

    folder_prefix = f"{prefix}/"

    # If any object already exists under this prefix, reuse it.
    existing_objects = bucket.list_blobs(
        prefix=folder_prefix,
        max_results=1,
    )

    if next(iter(existing_objects), None) is not None:
        print(f"[INFO] GCS folder/prefix already exists: {folder_prefix}")
        return

    # No objects exist under this prefix: create an explicit marker.
    marker = bucket.blob(folder_prefix)

    try:
        marker.upload_from_string(
            "",
            content_type="application/x-directory",
            if_generation_match=0,
        )
        print(f"[OK] Created GCS folder marker: {folder_prefix}")

    except Exception:
        # Another process may have created the marker concurrently.
        # Check again before deciding that creation failed.
        if not bucket.blob(folder_prefix).exists():
            raise

        print(f"[INFO] GCS folder marker already exists: {folder_prefix}")

def upload_pdf(
    file_path: Path,
    machine_name: str,
    bucket_name: str,
    project_id: str | None = None,
    raw_prefix: str = "raw",
    allow_overwrite: bool = False,
) -> str:
    """Upload the PDF and verify the uploaded object size."""

    try:
        from google.cloud import storage
    except ImportError as exc:
        raise ImportError(
            "Google Cloud Storage library is missing. Install it with:\n"
            "python -m pip install google-cloud-storage"
        ) from exc

    client = (
        storage.Client(project=project_id)
        if project_id
        else storage.Client()
    )

    bucket = client.bucket(bucket_name)
    ensure_gcs_prefix_exists(bucket, raw_prefix)
    ensure_gcs_prefix_exists(
        bucket,
        f"{raw_prefix}/{machine_name}",
    )

    object_name = build_object_name(
        machine_name,
        file_path.name,
        raw_prefix,
    )

    blob = bucket.blob(object_name)

    upload_options = {
        "content_type": "application/pdf",
        "checksum": "auto",
    }

    # Prevent accidental overwriting of an existing object by default.
    if not allow_overwrite:
        upload_options["if_generation_match"] = 0

    print(f"[INFO] Uploading to gs://{bucket_name}/{object_name}")

    blob.upload_from_filename(str(file_path), **upload_options)

    # Reload metadata from GCS to verify the uploaded object.
    blob.reload()

    expected_size = file_path.stat().st_size

    if blob.size != expected_size:
        raise RuntimeError(
            f"Upload verification failed. Local size: {expected_size}; "
            f"GCS size: {blob.size}."
        )

    print("[OK] Upload completed and size verified.")
    print(f"[OK] GCS URI: gs://{bucket_name}/{object_name}")

    return f"gs://{bucket_name}/{object_name}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate a PDF and optionally upload it to GCS."
    )

    parser.add_argument(
        "--file",
        required=True,
        help="Full path to the local PDF file.",
    )

    parser.add_argument(
        "--machine",
        required=True,
        help="Machine name, for example HSG-2550.",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate the PDF and show its destination without uploading.",
    )

    args = parser.parse_args()

    try:
        file_path = Path(args.file).expanduser().resolve()
        machine_name = validate_machine_name(args.machine)

        info = validate_pdf(file_path)

        print("[OK] PDF validation successful.")
        print(f"[INFO] File: {info['path']}")
        print(f"[INFO] Size: {info['size']:,} bytes")
        print(f"[INFO] Pages: {info['pages']}")

        
        raw_prefix = GCS_RAW_PREFIX or "raw"
        bucket_name = GCS_BUCKET_NAME
        project_id = GOOGLE_CLOUD_PROJECT or None
        allow_overwrite = GCS_ALLOW_OVERWRITE

        if args.dry_run:
            object_name = build_object_name(
                machine_name,
                file_path.name,
                raw_prefix,
            )

            print("[DRY RUN] No cloud connection or upload was attempted.")
            print(f"[DRY RUN] Destination: gs://{bucket_name}/{object_name}")
            return 0

        if not bucket_name:
            raise ValueError("GCS_BUCKET_NAME is empty in config.py/.env.")
        run_id = str(uuid.uuid4())
        started_at = time.time()


                
        try:
            gcs_uri = upload_pdf(
                file_path=file_path,
                machine_name=machine_name,
                bucket_name=bucket_name,
                project_id=project_id,
                raw_prefix=raw_prefix,
                allow_overwrite=allow_overwrite,
            )

            write_gcs_audit({
                "run_id": run_id,
                "upload_stage": "RAW_PDF",
                "machine_name": machine_name,
                "document_name": file_path.name,
                "source_path": str(file_path),
                "bucket_name": bucket_name,
                "object_name": gcs_uri.removeprefix(
                    f"gs://{bucket_name}/"
                ),
                "file_size_bytes": info["size"],
                "status": "SUCCESS",
                "started_at": started_at,
                "completed_at": time.time(),
                "duration_ms": int((time.time() - started_at) * 1000),
            })

            return 0

        except Exception as upload_error:
            write_gcs_audit({
                "run_id": run_id,
                "upload_stage": "RAW_PDF",
                "machine_name": machine_name,
                "document_name": file_path.name,
                "source_path": str(file_path),
                "bucket_name": bucket_name,
                "file_size_bytes": info["size"],
                "status": "FAILED",
                "started_at": started_at,
                "completed_at": time.time(),
                "duration_ms": int((time.time() - started_at) * 1000),
                "error_type": type(upload_error).__name__,
                "error_message": str(upload_error),
            })

            raise


    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

