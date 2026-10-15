import os
import re
from pathlib import Path

from dotenv import load_dotenv


HERE = Path(__file__).resolve().parent
BACKEND_ROOT = HERE.parent
PROJECT_ROOT = BACKEND_ROOT.parent

load_dotenv(BACKEND_ROOT / ".env", override=False)


def setting(name: str, default: str = "") -> str:
    value = os.getenv(name, default).strip()

    if not value:
        raise RuntimeError(
            f"Set {name} in backend/.env"
        )

    return value


PROJECT = setting("GOOGLE_CLOUD_PROJECT")
DATASET = setting("BQ_DATASET", "manufacturing_rag")
# Retrieval searches all indexed machines; machine names come from BigQuery.
MACHINE = ""
BQ_LOCATION = setting("BQ_LOCATION", "us-central1")

MODEL = setting("AGENT_MODEL", "gemini-3.5-flash")
MODEL_LOCATION = setting("GOOGLE_CLOUD_LOCATION", "global")

EMBEDDING_MODEL = setting(
    "EMBEDDING_MODEL",
    "gemini-embedding-001",
)

EMBEDDING_LOCATION = setting(
    "EMBEDDING_LOCATION",
    "us-central1",
)

DIMENSIONS = int(
    setting("EMBEDDING_DIMENSIONS", "768")
)

BUCKET = setting("GCS_BUCKET_NAME")

DOCUMENT_AI_PROJECT = os.getenv("DOCUMENT_AI_PROJECT", PROJECT).strip()
DOCUMENT_AI_LOCATION = os.getenv("DOCUMENT_AI_LOCATION", "us").strip()
DOCUMENT_AI_PROCESSOR_ID = os.getenv("DOCUMENT_AI_PROCESSOR_ID", "").strip()
DOCUMENT_AI_PROCESSOR_VERSION = os.getenv("DOCUMENT_AI_PROCESSOR_VERSION", "").strip()
QR_LINKS_TABLE = os.getenv("BIGQUERY_QR_LINKS_TABLE", "document_qr_links").strip()

PROCESSED_PREFIX = setting(
    "GCS_PROCESSED_PREFIX",
    "processed",
).strip("/")

RAW_PREFIX = setting(
    "GCS_RAW_PREFIX",
    "raw",
).strip("/")

TTL = int(setting("SESSION_TTL_SECONDS", "3600"))
TIMEOUT = int(setting("REQUEST_TIMEOUT_SECONDS", "120"))

ORIGIN = setting(
    "FRONTEND_ORIGIN",
    "http://localhost:5173",
)

RESOLVE_VIDEOS = (
    os.getenv(
        "RESOLVE_MANUAL_VIDEO_LINKS",
        "true",
    ).lower()
    == "true"
)

VIDEO_HOSTS = {
    host.strip().lower()
    for host in os.getenv(
        "MANUAL_VIDEO_HOSTS",
        "haascnc.com,www.haascnc.com",
    ).split(",")
    if host.strip()
}

APP_NAME = "maintenance_agent"

UNAVAILABLE = (
    "The required information is not available "
    "in the provided PDF manuals."
)


# These identifiers come from configuration, not from users or Gemini.
if not re.fullmatch(r"[a-z][a-z0-9-]+", PROJECT):
    raise RuntimeError("Invalid configured project ID")

if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", DATASET):
    raise RuntimeError("Invalid configured dataset ID")

if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", QR_LINKS_TABLE):
    raise RuntimeError("Invalid configured QR links table ID")

if not 1 <= TTL <= 3600:
    raise RuntimeError(
        "SESSION_TTL_SECONDS must be between 1 and 3600"
    )
