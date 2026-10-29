
import hashlib
import json
import logging
import os

import functions_framework
from cloudevents.http import CloudEvent
from google.api_core.exceptions import PreconditionFailed
from google.cloud import storage

from main import process_gcs_uri

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

BUCKET_NAME = os.environ["GCS_BUCKET_NAME"]
RAW_PREFIX = os.environ.get("GCS_RAW_PREFIX", "raw").strip("/")
LOCK_PREFIX = os.environ.get("GCS_LOCK_PREFIX", "_ingestion_locks")


@functions_framework.cloud_event
def process_gcs_pdf(cloud_event: CloudEvent) -> None:
    """Process newly uploaded PDFs from raw/MACHINE_NAME/filename.pdf."""

    data = cloud_event.data
    bucket_name = data.get("bucket")
    object_name = data.get("name", "")
    generation = str(data.get("generation", ""))

    if bucket_name != BUCKET_NAME:
        logger.info("Ignoring unexpected bucket: %s", bucket_name)
        return

    parts = object_name.split("/")

    # Accept exactly raw/MACHINE_NAME/filename.pdf.
    if (
        not object_name.startswith(f"{RAW_PREFIX}/")
        or len(parts) != 3
        or not parts[1]
        or not parts[2].lower().endswith(".pdf")
    ):
        logger.info("Ignoring object outside the expected PDF path: %s", object_name)
        return

    gcs_uri = f"gs://{bucket_name}/{object_name}"

    # Use the object generation so a new version is a distinct event.
    event_key = hashlib.sha256(
        f"{bucket_name}/{object_name}/{generation}".encode("utf-8")
    ).hexdigest()

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    lock = bucket.blob(f"{LOCK_PREFIX}/{event_key}.json")

    # Prevent the same event from starting concurrently or being processed
    # again after a successful run.
    try:
        lock.upload_from_string(
            json.dumps({
                "bucket": bucket_name,
                "object": object_name,
                "generation": generation,
                "status": "processing",
            }),
            content_type="application/json",
            if_generation_match=0,
        )
    except PreconditionFailed:
        logger.info("Event already claimed or processed; skipping: %s", gcs_uri)
        return

    try:
        logger.info("Starting ingestion: %s", gcs_uri)

        process_gcs_uri(
            gcs_uri,
            upload_to_gcs=True,
            load_to_bigquery=True,
            commit_bigquery=True,
            tables="all",
        )

        # Mark successful completion in the lock object.
        lock.reload()
        lock.upload_from_string(
            json.dumps({
                "bucket": bucket_name,
                "object": object_name,
                "generation": generation,
                "status": "completed",
            }),
            content_type="application/json",
            if_generation_match=lock.generation,
        )

        logger.info("Ingestion completed: %s", gcs_uri)

    except Exception:
        logger.exception("Ingestion failed: %s", gcs_uri)

        # Allow a retried event to try again after a normal processing failure.
        try:
            lock.reload()
            lock.delete(if_generation_match=lock.generation)
        except Exception:
            logger.exception("Could not release ingestion lock: %s", gcs_uri)

        raise
