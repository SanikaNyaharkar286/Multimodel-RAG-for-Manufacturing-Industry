
"""Central configuration for local development and Cloud Run deployment."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Project root: two levels above src/simple_ingestion.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Load local .env values without overriding existing environment variables.
# Cloud Run should use configured service environment variables.
load_dotenv(PROJECT_ROOT / ".env", override=False)

# Google Cloud
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID")
GCP_REGION = os.getenv("GCP_REGION", "us-central1")

# Google Cloud Storage
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME")
GCS_RAW_PREFIX = os.getenv("GCS_RAW_PREFIX", "raw").strip("/")
GCS_PROCESSED_PREFIX = os.getenv(
    "GCS_PROCESSED_PREFIX", "processed"
).strip("/")
GCS_LOCK_PREFIX = os.getenv(
    "GCS_LOCK_PREFIX", "_ingestion_locks"
).strip("/")

# BigQuery
BIGQUERY_DATASET = os.getenv("BIGQUERY_DATASET")
BIGQUERY_CHUNKS_TABLE = os.getenv(
    "BIGQUERY_CHUNKS_TABLE", "document_chunks"
)

# Existing BigQuery tables
BIGQUERY_RELATIONSHIPS_TABLE = "chunk_relationships"
BIGQUERY_GEOMETRY_TABLE = "document_chunk_geometry"
BIGQUERY_IMAGES_TABLE = "document_images"
BIGQUERY_LINKS_TABLE = "document_links"
BIGQUERY_SECTIONS_TABLE = "document_sections"
BIGQUERY_TABLE_ROWS_TABLE = "document_table_rows"
BIGQUERY_SECTION_ELEMENTS_TABLE = "section_elements"


# Local and cloud-writable processing directories.
# For Cloud Run, configure PROCESSED_DATA_DIR=/tmp/data/processed.
RAW_DATA_DIR = Path(
    os.getenv("RAW_DATA_DIR", str(PROJECT_ROOT / "data" / "raw"))
)
PROCESSED_DATA_DIR = Path(
    os.getenv(
        "PROCESSED_DATA_DIR",
        str(PROJECT_ROOT / "data" / "processed"),
    )
)


def get_gcs_raw_prefix(machine_name: str) -> str:
    return f"{GCS_RAW_PREFIX}/{machine_name}/"


def get_gcs_processed_prefix(
    machine_name: str,
    document_id: str,
) -> str:
    return f"{GCS_PROCESSED_PREFIX}/{machine_name}/{document_id}/"


def get_bigquery_table(table_name: str) -> str:
    """Return a fully qualified BigQuery table ID."""
    if not GCP_PROJECT_ID or not BIGQUERY_DATASET:
        raise ValueError(
            "GCP_PROJECT_ID and BIGQUERY_DATASET must be configured."
        )

    return f"{GCP_PROJECT_ID}.{BIGQUERY_DATASET}.{table_name}"


# Fail early if required configuration is missing.
_REQUIRED_SETTINGS = {
    "GCP_PROJECT_ID": GCP_PROJECT_ID,
    "GCS_BUCKET_NAME": GCS_BUCKET_NAME,
    "BIGQUERY_DATASET": BIGQUERY_DATASET,
}

_MISSING_SETTINGS = [
    key for key, value in _REQUIRED_SETTINGS.items() if not value
]

if _MISSING_SETTINGS:
    raise ValueError(
        "Missing required environment variable(s): "
        + ", ".join(_MISSING_SETTINGS)
    )
