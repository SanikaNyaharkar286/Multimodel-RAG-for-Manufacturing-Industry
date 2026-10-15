
import os
from pathlib import Path
from dotenv import load_dotenv

# In local development this file lives in src/simple_ingestion/config.py.
# In Cloud Functions, the deployment source directory becomes the function root.
_HERE = Path(__file__).resolve().parent
if _HERE.name == "simple_ingestion" and _HERE.parent.name == "src":
    PROJECT_ROOT = _HERE.parents[1]
else:
    PROJECT_ROOT = _HERE

# Local .env is not required in Cloud Functions; deployment env vars are used there.
load_dotenv(PROJECT_ROOT / ".env")
load_dotenv(_HERE / ".env", override=False)

# Google Cloud configuration
GCP_PROJECT_ID = os.getenv("GCP_PROJECT_ID")
GCP_REGION = os.getenv("GCP_REGION", "us-central1")
EMBEDDING_DIMENSIONS = int(os.getenv("EMBEDDING_DIMENSIONS", "768"))

# Google Cloud Storage
GCS_BUCKET_NAME = os.getenv("GCS_BUCKET_NAME")
GCS_RAW_PREFIX = os.getenv("GCS_RAW_PREFIX", "raw")
GCS_PROCESSED_PREFIX = os.getenv(
    "GCS_PROCESSED_PREFIX", "processed"
)

# BigQuery
BIGQUERY_DATASET = os.getenv("BIGQUERY_DATASET")
BIGQUERY_CHUNKS_TABLE = os.getenv(
    "BIGQUERY_CHUNKS_TABLE", "document_chunks"
)
BIGQUERY_JOBS_TABLE = os.getenv(
    "BIGQUERY_JOBS_TABLE", "ingestion_jobs"
)
BIGQUERY_AUDIT_TABLE = os.getenv(
    "BIGQUERY_AUDIT_TABLE", "ingestion_audit"
)

# Local development directories
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
PROCESSED_DATA_DIR = PROJECT_ROOT / "data" / "processed"


def get_gcs_raw_prefix(machine_name: str) -> str:
    return f"{GCS_RAW_PREFIX}/{machine_name}/"


def get_gcs_processed_prefix(
    machine_name: str,
    document_id: str,
) -> str:
    return (
        f"{GCS_PROCESSED_PREFIX}/"
        f"{machine_name}/{document_id}/"
    )


def get_bigquery_table(table_name: str) -> str:
    return (
        f"{GCP_PROJECT_ID}."
        f"{BIGQUERY_DATASET}."
        f"{table_name}"
    )


# Fail early if required configuration is missing
required_settings = {
    "GCP_PROJECT_ID": GCP_PROJECT_ID,
    "GCS_BUCKET_NAME": GCS_BUCKET_NAME,
    "BIGQUERY_DATASET": BIGQUERY_DATASET,
}

missing_settings = [
    name for name, value in required_settings.items()
    if not value
]

if missing_settings:
    raise ValueError(
        "Missing configuration in .env: "
        + ", ".join(missing_settings)
    )
