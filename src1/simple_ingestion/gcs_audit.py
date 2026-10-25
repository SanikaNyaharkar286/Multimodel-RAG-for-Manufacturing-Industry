
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from google.cloud import storage

from config import (
    GCS_BUCKET_NAME,
    GOOGLE_CLOUD_PROJECT,
    GCS_AUDIT_PREFIX,
    GCS_AUDIT_FALLBACK_DIR,
)

def write_gcs_audit(event: dict, client=None) -> None:
    """Write one audit event to GCS; use a local fallback if that fails."""
    event = {
        **event,
        "audit_event_id": str(uuid.uuid4()),
        "audit_timestamp": datetime.now(timezone.utc).isoformat(),
    }

    try:
        if client is None:
            client = storage.Client(
                project=GOOGLE_CLOUD_PROJECT or None
            )

        bucket = client.bucket(GCS_BUCKET_NAME)
        run_id = event.get("run_id", "unknown-run")
        object_name = (
            f"{GCS_AUDIT_PREFIX}/{run_id}/"
            f"{event['audit_event_id']}.json"
        )

        bucket.blob(object_name).upload_from_string(
            json.dumps(event, ensure_ascii=False, default=str),
            content_type="application/json",
            if_generation_match=0,
        )
        print(
            f"[AUDIT OK] gs://{GCS_BUCKET_NAME}/{object_name}",
            flush=True,
        )

    except Exception as audit_error:
        # Keep a local copy if the audit bucket write fails.
        fallback_dir = GCS_AUDIT_FALLBACK_DIR
        fallback_dir.mkdir(parents=True, exist_ok=True)

        fallback_file = fallback_dir / "gcs_upload_audit.jsonl"
        event["audit_write_error"] = str(audit_error)

        with fallback_file.open("a", encoding="utf-8") as file:
            file.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")

        print(
            f"[WARNING] GCS audit write failed; local fallback: {fallback_file}",
            flush=True,
        )
