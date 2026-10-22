"""Eventarc handler for PDF uploads under raw/<MACHINE_NAME>/<filename>.pdf."""

import logging
import os
import tempfile
from pathlib import Path

import functions_framework
from cloudevents.http import CloudEvent
from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import storage

# main.py adds src/simple_ingestion to sys.path before importing this module.
from pipeline import process_pdf

logger = logging.getLogger(__name__)
logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))

EXPECTED_BUCKET = os.getenv(
    "GCS_BUCKET_NAME", "market-lens-506611-manufacturing-rag"
)
RAW_PREFIX = os.getenv("GCS_RAW_PREFIX", "raw").strip("/")
LOCK_PREFIX = os.getenv("GCS_LOCK_PREFIX", "_ingestion_locks").strip("/")


def _parse_raw_pdf_object(object_name: str) -> tuple[str, str] | None:
    """Return (machine_name, filename) for valid raw/<machine>/<file>.pdf paths."""
    parts = object_name.split("/")
    if len(parts) != 3:
        return None

    prefix, machine_name, filename = parts
    if prefix != RAW_PREFIX or not machine_name or not filename:
        return None
    if not filename.lower().endswith(".pdf"):
        return None
    if machine_name in {".", ".."} or filename in {".", ".."}:
        return None
    return machine_name, filename


def _lock_object_name(object_name: str, generation: str) -> str:
    """Create a generation-specific marker path for duplicate-event protection."""
    safe_name = object_name.replace("/", "__")
    return f"{LOCK_PREFIX}/{safe_name}/generation-{generation}.done"


def _acquire_generation_marker(bucket, marker_name: str) -> bool:
    """Atomically create a marker; return False if this generation was claimed."""
    marker = bucket.blob(marker_name)
    try:
        marker.upload_from_string(
            "processing", content_type="text/plain", if_generation_match=0
        )
        return True
    except PreconditionFailed:
        logger.info("Generation already claimed or processed: gs://%s/%s",
                    bucket.name, marker_name)
        return False


def _remove_marker(bucket, marker_name: str) -> None:
    """Remove a failed run's marker so an Eventarc retry can try again."""
    try:
        bucket.blob(marker_name).delete()
    except NotFound:
        pass
    except Exception:
        logger.exception("Could not remove failed-ingestion marker: %s", marker_name)


@functions_framework.cloud_event
def ingest_raw_pdf(cloud_event: CloudEvent):
    """Download and process one finalized PDF upload from the configured GCS bucket."""
    data = cloud_event.data or {}
    bucket_name = data.get("bucket")
    object_name = data.get("name", "")
    generation = str(data.get("generation", ""))
    event_id = cloud_event.get("id", "unknown")

    logger.info(
        "Received GCS event id=%s bucket=%s object=%s generation=%s",
        event_id, bucket_name, object_name, generation,
    )

    if bucket_name != EXPECTED_BUCKET:
        logger.info("Ignoring event from unexpected bucket: %s", bucket_name)
        return

    parsed = _parse_raw_pdf_object(object_name)
    if parsed is None:
        logger.info("Ignoring object outside expected raw PDF layout: %s", object_name)
        return

    if not generation:
        raise ValueError("Cloud Storage event is missing object generation.")

    machine_name, filename = parsed
    storage_client = storage.Client()
    bucket = storage_client.bucket(bucket_name)
    marker_name = _lock_object_name(object_name, generation)

    if not _acquire_generation_marker(bucket, marker_name):
        return

    try:
        source_blob = bucket.blob(object_name, generation=int(generation))
        with tempfile.TemporaryDirectory(prefix="manufacturing-rag-") as temp_dir:
            local_pdf = Path(temp_dir) / filename
            source_blob.download_to_filename(str(local_pdf))
            logger.info(
                "Starting ingestion for machine=%s filename=%s generation=%s",
                machine_name, filename, generation,
            )

            # Existing pipeline: extract -> relationships -> chunks -> embeddings
            # -> GCS processed artifacts -> BigQuery MERGE.
            process_pdf(
                local_pdf,
                machine_name,
                generate_embeddings=True,
                upload_to_gcs=True,
                load_to_bigquery=True,
            )

        # Keep the marker after success so duplicate events do not repeat paid work.
        bucket.blob(marker_name).upload_from_string(
            "completed", content_type="text/plain"
        )
        logger.info(
            "Ingestion completed: gs://%s/%s (generation %s)",
            bucket_name, object_name, generation,
        )
    except Exception:
        logger.exception(
            "Ingestion failed for gs://%s/%s (generation %s)",
            bucket_name, object_name, generation,
        )
        _remove_marker(bucket, marker_name)
        raise
