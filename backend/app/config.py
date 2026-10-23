# this file loads and checks settings used across the backend

import os
import re
import secrets
from pathlib import Path

from dotenv import load_dotenv


# find the backend and project folders
HERE = Path(__file__).resolve().parent
BACKEND_ROOT = HERE.parent
PROJECT_ROOT = BACKEND_ROOT.parent
env_path = BACKEND_ROOT / ".env"
print(f"👉 Python is looking for the .env file at: {env_path.resolve()}")
print(f"👉 Does the file exist there? {env_path.exists()}")

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
# Use a faster model for intent and search planning; final answers keep MODEL.
PLANNER_MODEL = setting("PLANNER_MODEL", "gemini-3.5-flash-lite")
# Fast final-answer model for lightweight text-only responses. Complex repair
# and multimodal answers continue using MODEL.
FINAL_LITE_MODEL = setting("FINAL_LITE_MODEL", PLANNER_MODEL)
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
# Maximum time allowed for the final Gemini generation request. Keep this
# below the overall request timeout so the approved-evidence fallback can run.
MODEL_TIMEOUT_MS = int(setting("MODEL_TIMEOUT_MS", "45000"))

# frontend address allowed to connect to the backend
ORIGIN = setting(
    "FRONTEND_ORIGIN",
    "http://localhost:5173",
)

# Firebase Identity Platform and Firestore approval settings.
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "false").lower() == "true"
FIREBASE_PROJECT = os.getenv("FIREBASE_PROJECT", PROJECT).strip()
COMPANY_ID = os.getenv("COMPANY_ID", "COMPANY_ID").strip()
COMPANY_NAME = os.getenv("COMPANY_NAME", "COMPANY_NAME").strip()
FIRESTORE_DATABASE_ID = os.getenv("FIRESTORE_DATABASE_ID", "manufacturing-agent").strip()
ALLOWED_EMAIL_DOMAINS = {
    value.strip().lower().lstrip("@")
    for value in os.getenv("ALLOWED_EMAIL_DOMAINS", "example.com").split(",")
    if value.strip()
}
ADMIN_EMAILS = {
    value.strip().lower()
    for value in os.getenv("ADMIN_EMAILS", "").split(",")
    if value.strip()
}

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

# Performance logging. Local files are always active; cloud export is opt-in.
API_LOG_DIRECTORY = BACKEND_ROOT / 'logs'
BQ_API_LOGGING_ENABLED = os.getenv('BQ_API_LOGGING_ENABLED', 'false').lower() == 'true'
API_TIMING_DEBUG = os.getenv('API_TIMING_DEBUG', 'false').lower() == 'true'
API_LOG_DATASET = os.getenv('API_LOG_DATASET', DATASET).strip()
API_LOG_TABLE = os.getenv('API_LOG_TABLE', 'api_call_logs').strip()
API_LOG_CREDENTIALS_FILE = os.getenv('API_LOG_CREDENTIALS_FILE', '').strip()
API_LOG_HASH_KEY = os.getenv('API_LOG_HASH_KEY', '') or secrets.token_hex(32)
API_LOG_RETENTION_DAYS = int(os.getenv('API_LOG_RETENTION_DAYS', '30'))
API_LOG_QUEUE_SIZE = int(os.getenv('API_LOG_QUEUE_SIZE', '1000'))
API_LOG_BATCH_SIZE = int(os.getenv('API_LOG_BATCH_SIZE', '20'))
API_LOG_FLUSH_SECONDS = float(os.getenv('API_LOG_FLUSH_SECONDS', '2'))
for identifier in (API_LOG_DATASET, API_LOG_TABLE):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', identifier):
        raise RuntimeError('Invalid API logging dataset or table')
if min(API_LOG_RETENTION_DAYS, API_LOG_QUEUE_SIZE, API_LOG_BATCH_SIZE, API_LOG_FLUSH_SECONDS) <= 0:
    raise RuntimeError('API logging limits must be positive')


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
if not 5000 <= MODEL_TIMEOUT_MS < TIMEOUT * 1000:
    raise RuntimeError("MODEL_TIMEOUT_MS must be at least 5000 ms and below REQUEST_TIMEOUT_SECONDS")
