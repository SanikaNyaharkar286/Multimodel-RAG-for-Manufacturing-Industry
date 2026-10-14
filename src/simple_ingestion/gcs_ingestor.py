"""Upload a processed document directory and its assets to Google Cloud Storage."""

import json
import os
from pathlib import Path
from google.cloud import storage


def upload_document_to_gcs(
    document_dir: str | Path,
    machine_name: str,
    document_name: str,
    *,
    bucket_name: str | None = None,
    prefix: str = "processed",
) -> str:
    """Upload files under gs://<bucket>/<prefix>/<machine>/<document>/.

    Uses Application Default Credentials (ADC). It uploads files as-is and does
    not modify local files, rewrite JSON references, or write to BigQuery.
    Returns the GCS prefix for the uploaded document.
    """
    document_dir = Path(document_dir)
    if not document_dir.is_dir():
        raise FileNotFoundError(f"Processed document directory not found: {document_dir}")
    if not machine_name or Path(machine_name).name != machine_name:
        raise ValueError("machine_name must be a single folder name")
    if not document_name or Path(document_name).name != document_name:
        raise ValueError("document_name must be a single folder name")

    bucket_name = bucket_name or os.getenv("GCS_BUCKET_NAME")
    if not bucket_name:
        raise RuntimeError("Set GCS_BUCKET_NAME to your existing GCS bucket name.")

    client = storage.Client(project=os.getenv("GOOGLE_CLOUD_PROJECT") or None)
    bucket = client.bucket(bucket_name)
    gcs_prefix = "/".join(part.strip("/") for part in (prefix, machine_name, document_name) if part)

    files = sorted(path for path in document_dir.rglob("*") if path.is_file())
    if not files:
        print(f"No files to upload in {document_dir}")
        return f"gs://{bucket_name}/{gcs_prefix}/"

    # Map both absolute local paths and document-relative paths to their GCS URIs.
    asset_uri_map = {}
    for local_path in files:
        if "assets" not in local_path.relative_to(document_dir).parts:
            continue
        relative_path = local_path.relative_to(document_dir).as_posix()
        asset_uri_map[str(local_path.resolve())] = f"gs://{bucket_name}/{gcs_prefix}/{relative_path}"
        asset_uri_map[relative_path] = f"gs://{bucket_name}/{gcs_prefix}/{relative_path}"

    for local_path in files:
        relative_path = local_path.relative_to(document_dir).as_posix()
        object_name = f"{gcs_prefix}/{relative_path}"
        blob = bucket.blob(object_name)

        if local_path.suffix.lower() == ".json":
            # Rewrite known local asset paths in the upload payload only;
            # leave the local JSON file untouched.
            with local_path.open("r", encoding="utf-8") as file:
                data = json.load(file)
            records = data if isinstance(data, list) else data.get("chunks", []) if isinstance(data, dict) else []
            for record in records:
                if not isinstance(record, dict):
                    continue
                uris = record.get("asset_uris", [])
                if not isinstance(uris, list):
                    uris = [uris] if uris else []
                rewritten = []
                for uri in uris:
                    value = str(uri)
                    mapped = asset_uri_map.get(value)
                    if mapped is None:
                        try:
                            mapped = asset_uri_map.get(str(Path(value).resolve()))
                        except (OSError, ValueError):
                            mapped = None
                    rewritten.append(mapped or value)
                if "asset_uris" in record:
                    record["asset_uris"] = list(dict.fromkeys(rewritten))
            blob.upload_from_string(
                json.dumps(data, ensure_ascii=False, indent=2),
                content_type="application/json",
            )
        else:
            blob.upload_from_filename(str(local_path))

        print(f"Uploaded: gs://{bucket_name}/{object_name}")

    result = f"gs://{bucket_name}/{gcs_prefix}/"
    print(f"Uploaded {len(files)} file(s) to {result}")
    return result
