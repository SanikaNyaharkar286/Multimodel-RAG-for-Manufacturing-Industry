
"""Transfer GCS audit JSON events into the BigQuery audit table."""

import json
import sys
from datetime import datetime, timezone

from google.cloud import bigquery, storage

from config import (
    GCS_BUCKET_NAME,
    GOOGLE_CLOUD_PROJECT,
    GCS_AUDIT_PREFIX,
    BIGQUERY_DATASET,
    GCS_UPLOAD_AUDIT_TABLE,
)


def to_timestamp(value):
    """Convert ISO timestamps or Unix epoch seconds to UTC ISO strings."""
    if value is None or value == "":
        return None

    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(
            value, tz=timezone.utc
        ).isoformat()

    if isinstance(value, str):
        try:
            numeric_value = float(value)
            return datetime.fromtimestamp(
                numeric_value, tz=timezone.utc
            ).isoformat()
        except ValueError:
            pass

        return value

    return None


def build_bigquery_row(event):
    """Map one GCS audit JSON event to the BigQuery table schema."""

    audit_id = event.get("audit_event_id") or event.get("audit_id")

    if not audit_id:
        raise ValueError("Audit event has no audit_event_id or audit_id")

    return {
        "audit_id": str(audit_id),
        "run_id": event.get("run_id"),
        "event_timestamp": to_timestamp(
            event.get("audit_timestamp")
            or event.get("event_timestamp")
        ),
        "upload_stage": event.get("upload_stage"),
        "machine_name": event.get("machine_name"),
        "document_name": event.get("document_name"),
        "source_path": event.get("source_path"),
        "bucket_name": event.get("bucket_name"),
        "object_name": event.get("object_name"),
        "file_size_bytes": event.get("file_size_bytes"),
        "crc32c": event.get("crc32c"),
        "status": event.get("status"),
        "attempt_number": event.get("attempt_number", 1),
        "started_at": to_timestamp(event.get("started_at")),
        "completed_at": to_timestamp(event.get("completed_at")),
        "duration_ms": event.get("duration_ms"),
        "error_type": event.get("error_type"),
        "error_message": event.get("error_message"),
    }


def transfer_audits(dry_run=False):
    """Read GCS audit JSON objects and insert missing events into BigQuery."""

    if not GCS_BUCKET_NAME:
        raise ValueError("GCS_BUCKET_NAME is missing.")

    if not GOOGLE_CLOUD_PROJECT:
        raise ValueError("GOOGLE_CLOUD_PROJECT is missing.")

    if not BIGQUERY_DATASET or not GCS_UPLOAD_AUDIT_TABLE:
        raise ValueError("BigQuery dataset or audit table is missing.")

    project_id = GOOGLE_CLOUD_PROJECT
    table_id = (
        f"{project_id}.{BIGQUERY_DATASET}.{GCS_UPLOAD_AUDIT_TABLE}"
    )

    storage_client = storage.Client(project=project_id)
    bq_client = bigquery.Client(project=project_id)
    bucket = storage_client.bucket(GCS_BUCKET_NAME)

    prefix = GCS_AUDIT_PREFIX.strip("/") + "/"

    print(f"Reading audit events from gs://{GCS_BUCKET_NAME}/{prefix}")
    print(f"BigQuery destination: {table_id}")

    events = []
    invalid_count = 0

    for blob in bucket.list_blobs(prefix=prefix):
        if not blob.name.lower().endswith(".json"):
            continue

        try:
            event = json.loads(blob.download_as_text(encoding="utf-8"))
            row = build_bigquery_row(event)
            events.append((blob.name, row))
        except Exception as exc:
            invalid_count += 1
            print(f"[ERROR] Invalid audit file: {blob.name}: {exc}")

    print(f"Audit files parsed: {len(events)}")
    print(f"Invalid audit files: {invalid_count}")

    if not events:
        print("No valid audit events found.")
        return 1 if invalid_count else 0

    if dry_run:
        print("[DRY RUN] No BigQuery rows were inserted.")
        for blob_name, row in events:
            print(f"[DRY RUN] {row['audit_id']} <- {blob_name}")
        return 1 if invalid_count else 0

    # Find IDs already present in BigQuery so reruns skip existing events.
    existing_ids = set()

    for start in range(0, len(events), 500):
        batch = events[start:start + 500]
        ids = [row["audit_id"] for _, row in batch]

        query = f"""
            SELECT audit_id
            FROM `{table_id}`
            WHERE audit_id IN UNNEST(@audit_ids)
        """

        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ArrayQueryParameter(
                    "audit_ids", "STRING", ids
                )
            ]
        )

        query_job = bq_client.query(query, job_config=job_config)
        existing_ids.update(
            result.audit_id for result in query_job.result()
        )

    rows_to_insert = [
        row for _, row in events
        if row["audit_id"] not in existing_ids
    ]

    skipped_count = len(events) - len(rows_to_insert)

    print(f"Already in BigQuery: {skipped_count}")
    print(f"New records to insert: {len(rows_to_insert)}")

    if not rows_to_insert:
        print("BigQuery audit table is already up to date.")
        return 1 if invalid_count else 0

    # Insert in batches and report any rejected rows.
    inserted_count = 0
    failed_count = 0

    for start in range(0, len(rows_to_insert), 500):
        batch = rows_to_insert[start:start + 500]

        errors = bq_client.insert_rows_json(
            table_id,
            batch,
            row_ids=[None] * len(batch),
        )

        if errors:
            failed_indexes = set()

            for error in errors:
                index = error.get("index")
                if index is not None:
                    failed_indexes.add(index)
                print(f"[BQ INSERT ERROR] {error}")

            failed_count += len(failed_indexes) if failed_indexes else len(errors)
            inserted_count += (
                len(batch) - len(failed_indexes)
                if failed_indexes
                else max(0, len(batch) - len(errors))
            )
        else:
            inserted_count += len(batch)

    print("\nBigQuery audit transfer complete.")
    print(f"Inserted successfully: {inserted_count}")
    print(f"Skipped existing: {skipped_count}")
    print(f"Failed inserts: {failed_count}")

    return 1 if failed_count or invalid_count else 0


def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Transfer GCS audit JSON events to BigQuery."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Read and validate audit files without inserting into BigQuery.",
    )
    args = parser.parse_args()

    try:
        return transfer_audits(dry_run=args.dry_run)
    except Exception as exc:
        print(f"[FATAL] Audit transfer failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
