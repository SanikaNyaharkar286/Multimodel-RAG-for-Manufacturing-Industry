
"""Upload a processed document directory to Google Cloud Storage."""

import argparse
import mimetypes
import sys
from pathlib import Path
import time
import uuid
from gcs_audit import write_gcs_audit
from google.cloud import storage

from config import (
    GCS_BUCKET_NAME,
    GOOGLE_CLOUD_PROJECT,
    GCS_PROCESSED_PREFIX,
    GCS_ALLOW_OVERWRITE,
)


def ensure_prefix_exists(bucket, prefix: str) -> None:
    """Reuse an existing GCS prefix or create a folder marker if absent."""
    prefix = prefix.strip("/") + "/"

    if next(iter(bucket.list_blobs(prefix=prefix, max_results=1)), None):
        print(f"[INFO] Prefix already exists: {prefix}")
        return

    marker = bucket.blob(prefix)

    try:
        marker.upload_from_string(
            "",
            content_type="application/x-directory",
            if_generation_match=0,
        )
        print(f"[OK] Created folder marker: {prefix}")
    except Exception:
        # A concurrent process may have created the marker.
        if not marker.exists():
            raise
        print(f"[INFO] Prefix created by another process: {prefix}")


def upload_directory(
    source_dir: Path,
    machine_name: str,
    dry_run: bool = False,
    run_id: str | None = None,
) -> int:
    """Upload all files while preserving their relative paths."""
    if run_id is None:
        run_id = str(uuid.uuid4())

    if not source_dir.is_dir():
        raise FileNotFoundError(f"Directory not found: {source_dir}")

    if not GCS_BUCKET_NAME:
        raise ValueError("GCS_BUCKET_NAME is missing from config.")

    files = sorted(
        path for path in source_dir.rglob("*")
        if path.is_file() and path.name != ".gitkeep"
    )

    if not files:
        raise ValueError(f"No files found in: {source_dir}")

    prefix = GCS_PROCESSED_PREFIX.strip("/") or "processed"
    document_name = source_dir.name

    document_prefix = (
        f"{prefix}/{machine_name}/{document_name}"
    )

    print(f"[INFO] Source directory: {source_dir}")
    print(f"[INFO] Files found: {len(files)}")
    print(f"[INFO] Destination: gs://{GCS_BUCKET_NAME}/{document_prefix}/")

    if dry_run:
        for path in files:
            relative_path = path.relative_to(source_dir).as_posix()
            print(f"[DRY RUN] {document_prefix}/{relative_path}")
        print("[DRY RUN] No cloud connection or upload was attempted.")
        return 0

    client = storage.Client(project=GOOGLE_CLOUD_PROJECT or None)
    bucket = client.bucket(GCS_BUCKET_NAME)

    ensure_prefix_exists(
        bucket,
        f"{prefix}/{machine_name}",
    )
    ensure_prefix_exists(bucket, document_prefix)

    failures = 0

    for path in files:
        relative_path = path.relative_to(source_dir).as_posix()
        object_name = f"{document_prefix}/{relative_path}"
        blob = bucket.blob(object_name)
        started_at = time.time()

        content_type, _ = mimetypes.guess_type(str(path))
        content_type = content_type or "application/octet-stream"

        options = {
            "content_type": content_type,
            "checksum": "auto",
        }

        if not GCS_ALLOW_OVERWRITE:
            options["if_generation_match"] = 0

        try:
            print(f"[INFO] Uploading: {relative_path}")

            blob.upload_from_filename(str(path), **options)
            blob.reload()

            if blob.size != path.stat().st_size:
                raise RuntimeError("Uploaded size does not match local size.")

            print(f"[OK] Verified: gs://{GCS_BUCKET_NAME}/{object_name}")
            write_gcs_audit({
                "run_id": run_id,
                "upload_stage": "PROCESSED_ARTIFACT",
                "machine_name": machine_name,
                "document_name": document_name,
                "source_path": str(path),
                "bucket_name": GCS_BUCKET_NAME,
                "object_name": object_name,
                "file_size_bytes": path.stat().st_size,
                "status": "SUCCESS",
                "started_at": started_at,
                "completed_at": time.time(),
                "duration_ms": int((time.time() - started_at) * 1000),
            })

        except Exception as exc:
            failures += 1
            print(f"[ERROR] {relative_path}: {exc}", file=sys.stderr)
            write_gcs_audit({
                "run_id": run_id,
                "upload_stage": "PROCESSED_ARTIFACT",
                "machine_name": machine_name,
                "document_name": document_name,
                "source_path": str(path),
                "bucket_name": GCS_BUCKET_NAME,
                "object_name": object_name,
                "file_size_bytes": (
                    path.stat().st_size if path.exists() else None
                ),
                "status": "FAILED",
                "started_at": started_at,
                "completed_at": time.time(),
                "duration_ms": int((time.time() - started_at) * 1000),
                "error_type": type(exc).__name__,
                "error_message": str(exc),
            })

    print(f"\nFiles found: {len(files)}")
    print(f"Successful uploads: {len(files) - failures}")
    print(f"Failed uploads: {failures}")

    return failures


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Upload a processed document directory to GCS."
    )
    parser.add_argument(
        "--directory",
        required=True,
        help="Local processed document directory.",
    )
    parser.add_argument(
        "--machine",
        required=True,
        help="Machine name, e.g. HSG-2550.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show planned uploads without connecting to GCS.",
    )

    args = parser.parse_args()

    try:
        failures = upload_directory(
            Path(args.directory).expanduser().resolve(),
            args.machine,
            args.dry_run,
        )
        return 1 if failures else 0
    except Exception as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
