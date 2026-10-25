"""Configuration for PDF segmentation, extraction, and GCS uploads."""

import os
from pathlib import Path

from dotenv import load_dotenv

# Project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Load optional local environment variables
load_dotenv(PROJECT_ROOT / ".env", override=False)

# Local data directories
RAW_DATA_DIR = Path(
    os.getenv("RAW_DATA_DIR", str(PROJECT_ROOT / "data" / "raw"))
)

PROCESSED_DATA_DIR = Path(
    os.getenv("PROCESSED_DATA_DIR", str(PROJECT_ROOT / "data" / "processed"))
)

# Google Cloud configuration
GCS_BUCKET_NAME = os.getenv(
    "GCS_BUCKET_NAME",
    "market-lens-506611-manufacturing-rag",
)

GOOGLE_CLOUD_PROJECT = os.getenv(
    "GOOGLE_CLOUD_PROJECT",
    "market-lens-506611",
)

# GCS object prefixes
GCS_RAW_PREFIX = os.getenv("GCS_RAW_PREFIX", "raw").strip("/")
GCS_PROCESSED_PREFIX = os.getenv(
    "GCS_PROCESSED_PREFIX", "processed"
).strip("/")

# Prevent overwriting existing PDFs by default
GCS_ALLOW_OVERWRITE = (
    os.getenv("GCS_ALLOW_OVERWRITE", "false").lower()
    in ("1", "true", "yes")
)

# BigQuery dataset
BIGQUERY_DATASET = os.getenv(
    "BIGQUERY_DATASET",
    "manufacturing_rag",
)

# BigQuery table names
BQ_API_CALL_LOGS_TABLE = os.getenv(
    "BQ_API_CALL_LOGS_TABLE", "api_call_logs"
)
BQ_CHUNK_RELATIONSHIPS_TABLE = os.getenv(
    "BQ_CHUNK_RELATIONSHIPS_TABLE", "chunk_relationships"
)
BQ_DOCUMENT_CHUNK_GEOMETRY_TABLE = os.getenv(
    "BQ_DOCUMENT_CHUNK_GEOMETRY_TABLE", "document_chunk_geometry"
)
BQ_DOCUMENT_CHUNKS_TABLE = os.getenv(
    "BQ_DOCUMENT_CHUNKS_TABLE", "document_chunks"
)
BQ_DOCUMENT_IMAGES_TABLE = os.getenv(
    "BQ_DOCUMENT_IMAGES_TABLE", "document_images"
)
BQ_DOCUMENT_LINKS_TABLE = os.getenv(
    "BQ_DOCUMENT_LINKS_TABLE", "document_links"
)
BQ_DOCUMENT_QR_LINKS_TABLE = os.getenv(
    "BQ_DOCUMENT_QR_LINKS_TABLE", "document_qr_links"
)
BQ_DOCUMENT_SECTIONS_TABLE = os.getenv(
    "BQ_DOCUMENT_SECTIONS_TABLE", "document_sections"
)
BQ_DOCUMENT_TABLE_ROWS_TABLE = os.getenv(
    "BQ_DOCUMENT_TABLE_ROWS_TABLE", "document_table_rows"
)
BQ_SECTION_ELEMENTS_TABLE = os.getenv(
    "BQ_SECTION_ELEMENTS_TABLE", "section_elements"
)

# GCS audit configuration
GCS_AUDIT_PREFIX = os.getenv(
    "GCS_AUDIT_PREFIX",
    "audit/gcs_upload_audit",
).strip("/")

GCS_AUDIT_FALLBACK_DIR = Path(
    os.getenv(
        "GCS_AUDIT_FALLBACK_DIR",
        str(PROJECT_ROOT / "data" / "audit_fallback"),
    )
)

# BigQuery audit table names
GCS_UPLOAD_AUDIT_TABLE = os.getenv(
    "GCS_UPLOAD_AUDIT_TABLE",
    "gcs_upload_audit",
)

BIGQUERY_UPLOAD_AUDIT_TABLE = os.getenv(
    "BIGQUERY_UPLOAD_AUDIT_TABLE",
    "bigquery_upload_audit",
)
