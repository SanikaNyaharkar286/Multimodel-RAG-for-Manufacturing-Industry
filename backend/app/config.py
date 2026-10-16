# this file loads and checks settings used across the backend

import os
import re
from pathlib import Path

from dotenv import load_dotenv


# find the backend and project folders
HERE = Path(__file__).resolve().parent
BACKEND_ROOT = HERE.parent
PROJECT_ROOT = BACKEND_ROOT.parent

# load settings from backend/.env
load_dotenv(BACKEND_ROOT / ".env", override=False)


# gets a setting or uses its default value
def setting(name: str, default: str = "") -> str:
    value = os.getenv(name, default).strip()

    # stop startup if a required setting is missing
    if not value:
        raise RuntimeError(
            f"Set {name} in backend/.env"
        )

    return value


# google cloud project and bigquery settings
PROJECT = setting("GOOGLE_CLOUD_PROJECT")
DATASET = setting("BQ_DATASET", "manufacturing_rag")
# retrieval searches all indexed machines; machine names come from bigquery
MACHINE = ""
BQ_LOCATION = setting("BQ_LOCATION", "us-central1")

# agent model and location
MODEL = setting("AGENT_MODEL", "gemini-3.5-flash")
MODEL_LOCATION = setting("GOOGLE_CLOUD_LOCATION", "global")

# embedding model settings used for manual search
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

# cloud storage bucket for manual files and assets
BUCKET = setting("GCS_BUCKET_NAME")

# document ai and qr link settings
DOCUMENT_AI_PROJECT = os.getenv("DOCUMENT_AI_PROJECT", PROJECT).strip()
DOCUMENT_AI_LOCATION = os.getenv("DOCUMENT_AI_LOCATION", "us").strip()
DOCUMENT_AI_PROCESSOR_ID = os.getenv("DOCUMENT_AI_PROCESSOR_ID", "").strip()
DOCUMENT_AI_PROCESSOR_VERSION = os.getenv("DOCUMENT_AI_PROCESSOR_VERSION", "").strip()
QR_LINKS_TABLE = os.getenv("BIGQUERY_QR_LINKS_TABLE", "document_qr_links").strip()

# folders used for processed files and original manuals
PROCESSED_PREFIX = setting(
    "GCS_PROCESSED_PREFIX",
    "processed",
).strip("/")

RAW_PREFIX = setting(
    "GCS_RAW_PREFIX",
    "raw",
).strip("/")

# session and request time limits
TTL = int(setting("SESSION_TTL_SECONDS", "3600"))
TIMEOUT = int(setting("REQUEST_TIMEOUT_SECONDS", "120"))

# frontend address allowed to connect to the backend
ORIGIN = setting(
    "FRONTEND_ORIGIN",
    "http://localhost:5173",
)

# controls whether manual video links are checked
RESOLVE_VIDEOS = (
    os.getenv(
        "RESOLVE_MANUAL_VIDEO_LINKS",
        "true",
    ).lower()
    == "true"
)

# websites allowed for checking manual video links
VIDEO_HOSTS = {
    host.strip().lower()
    for host in os.getenv(
        "MANUAL_VIDEO_HOSTS",
        "haascnc.com,www.haascnc.com",
    ).split(",")
    if host.strip()
}

# shared app name and message for unsupported questions
APP_NAME = "maintenance_agent"

UNAVAILABLE = (
    "The required information is not available "
    "in the provided PDF manuals."
)


# check that configured ids use the expected format
# these values come from settings, not from users or gemini
if not re.fullmatch(r"[a-z][a-z0-9-]+", PROJECT):
    raise RuntimeError("Invalid configured project ID")

if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", DATASET):
    raise RuntimeError("Invalid configured dataset ID")

if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", QR_LINKS_TABLE):
    raise RuntimeError("Invalid configured QR links table ID")

# make sure the session duration is no more than one hour
if not 1 <= TTL <= 3600:
    raise RuntimeError(
        "SESSION_TTL_SECONDS must be between 1 and 3600"
    )