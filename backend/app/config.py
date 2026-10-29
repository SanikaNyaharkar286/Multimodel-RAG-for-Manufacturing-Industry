#read all environment variables from backend/.env, assign default values, validate configurations,
#  and make those settings available to other backend files.

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


# reads one setting from .env. uses the default if not found. stops the app if it is empty.
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
# search covers all machines. machine names come from bigquery
MACHINE = ""
BQ_LOCATION = setting("BQ_LOCATION", "us-central1")

# ai model settings
MODEL = setting("AGENT_MODEL", "gemini-3.5-flash")
# faster model for planning the search. final answers use MODEL
PLANNER_MODEL = setting("PLANNER_MODEL", "gemini-3.5-flash-lite")
# fast model for simple text answers. hard repair answers and image answers use MODEL
FINAL_LITE_MODEL = setting("FINAL_LITE_MODEL", PLANNER_MODEL)
MODEL_LOCATION = setting("GOOGLE_CLOUD_LOCATION", "global")

# embedding model settings used to search the manuals
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

# cloud storage bucket for manual files
BUCKET = setting("GCS_BUCKET_NAME")

# document ai and qr link settings
DOCUMENT_AI_PROJECT = os.getenv("DOCUMENT_AI_PROJECT", PROJECT).strip()
DOCUMENT_AI_LOCATION = os.getenv("DOCUMENT_AI_LOCATION", "us").strip()
DOCUMENT_AI_PROCESSOR_ID = os.getenv("DOCUMENT_AI_PROCESSOR_ID", "").strip()
DOCUMENT_AI_PROCESSOR_VERSION = os.getenv("DOCUMENT_AI_PROCESSOR_VERSION", "").strip()
QR_LINKS_TABLE = os.getenv("BIGQUERY_QR_LINKS_TABLE", "document_qr_links").strip()

# folders for processed files and original manuals
PROCESSED_PREFIX = setting(
    "GCS_PROCESSED_PREFIX",
    "processed",
).strip("/")

RAW_PREFIX = setting(
    "GCS_RAW_PREFIX",
    "raw",
).strip("/")

# session time and request time limits
TTL = int(setting("SESSION_TTL_SECONDS", "3600"))
CHAT_SESSION_CHECKPOINTS_ENABLED = os.getenv("CHAT_SESSION_CHECKPOINTS_ENABLED", "false").lower() == "true"
TIMEOUT = int(setting("REQUEST_TIMEOUT_SECONDS", "120"))
# max time for the final gemini answer. keep it below the request timeout
MODEL_TIMEOUT_MS = int(setting("MODEL_TIMEOUT_MS", "45000"))
MODEL_MAX_OUTPUT_TOKENS = int(setting("MODEL_MAX_OUTPUT_TOKENS", "32000"))
# Legacy deployment settings remain readable for compatibility. The complete
# evidence path no longer truncates context using these two budgets.
MODEL_EVIDENCE_MAX_CHUNKS = int(os.getenv("MODEL_EVIDENCE_MAX_CHUNKS", "128"))
MODEL_EVIDENCE_MAX_CHARS = int(os.getenv("MODEL_EVIDENCE_MAX_CHARS", "120000"))
MANUAL_IMAGE_TRANSPORT = os.getenv('MANUAL_IMAGE_TRANSPORT', 'gcs').lower()
MANUAL_IMAGE_CACHE_BYTES = int(os.getenv('MANUAL_IMAGE_CACHE_BYTES', str(256 * 1024 * 1024)))
MANUAL_CORPUS_VERSION = os.getenv('MANUAL_CORPUS_VERSION', '1')
MANUAL_INVENTORY_CACHE_SECONDS = float(os.getenv('MANUAL_INVENTORY_CACHE_SECONDS', '30'))
# The independent review is an optional extra model call, outside the original
# fast flow. Structural/evidence/safety validation still runs when it is off.
MANUAL_COVERAGE_REVIEW_ENABLED = os.getenv("MANUAL_COVERAGE_REVIEW_ENABLED", "false").lower() == "true"
# cache search results to save time and cost. results stay the same
RETRIEVAL_CACHE_TTL_SECONDS = int(os.getenv("RETRIEVAL_CACHE_TTL_SECONDS", "900"))

# frontend address allowed to connect to the backend
ORIGIN = setting(
    "FRONTEND_ORIGIN",
    "http://localhost:5173",
)

# firebase login and firestore approval settings
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "false").lower() == "true"
FIREBASE_PROJECT = os.getenv("FIREBASE_PROJECT", PROJECT).strip()
COMPANY_ID = os.getenv("COMPANY_ID", "COMPANY_ID").strip()
COMPANY_NAME = os.getenv("COMPANY_NAME", "COMPANY_NAME").strip()
FIRESTORE_DATABASE_ID = os.getenv("FIRESTORE_DATABASE_ID", "manufacturing-agent").strip()
# keeps user profile in memory for a few seconds to avoid a firestore call on every request
# the login token is still checked on every request
AUTH_PROFILE_CACHE_TTL_SECONDS = float(os.getenv("AUTH_PROFILE_CACHE_TTL_SECONDS", "10"))
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

# turns manual video link checking on or off
RESOLVE_VIDEOS = (
    os.getenv(
        "RESOLVE_MANUAL_VIDEO_LINKS",
        "true",
    ).lower()
    == "true"
)

# websites allowed for video links
VIDEO_HOSTS = {
    host.strip().lower()
    for host in os.getenv(
        "MANUAL_VIDEO_HOSTS",
        "",
    ).split(",")
    if host.strip()
}

# app name and message shown when the answer is not in the manuals
APP_NAME = "maintenance_agent"

UNAVAILABLE = (
    "The required information is not available "
    "in the provided PDF manuals."
)

# api logging settings. local logs are always on. cloud logs are optional
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
if not 0 <= AUTH_PROFILE_CACHE_TTL_SECONDS <= 60:
    raise RuntimeError("AUTH_PROFILE_CACHE_TTL_SECONDS must be between 0 and 60")


# check that the ids use the right format
# these values come from settings, not from users or gemini
if not re.fullmatch(r"[a-z][a-z0-9-]+", PROJECT):
    raise RuntimeError("Invalid configured project ID")

if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", DATASET):
    raise RuntimeError("Invalid configured dataset ID")

if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", QR_LINKS_TABLE):
    raise RuntimeError("Invalid configured QR links table ID")

# make sure the session time is not more than one hour
if not 1 <= TTL <= 3600:
    raise RuntimeError(
        "SESSION_TTL_SECONDS must be between 1 and 3600"
    )
if not 5000 <= MODEL_TIMEOUT_MS < TIMEOUT * 1000:
    raise RuntimeError("MODEL_TIMEOUT_MS must be at least 5000 ms and below REQUEST_TIMEOUT_SECONDS")
if not 1024 <= MODEL_MAX_OUTPUT_TOKENS <= 65536:
    raise RuntimeError("MODEL_MAX_OUTPUT_TOKENS must be between 1024 and 65536")
if not 8 <= MODEL_EVIDENCE_MAX_CHUNKS <= 500:
    raise RuntimeError("MODEL_EVIDENCE_MAX_CHUNKS must be between 8 and 500")
if not 12000 <= MODEL_EVIDENCE_MAX_CHARS <= 200000:
    raise RuntimeError("MODEL_EVIDENCE_MAX_CHARS must be between 12000 and 200000")
if MANUAL_IMAGE_TRANSPORT not in {'gcs', 'inline'}:
    raise RuntimeError('MANUAL_IMAGE_TRANSPORT must be gcs or inline')
if MANUAL_IMAGE_CACHE_BYTES < 0 or MANUAL_INVENTORY_CACHE_SECONDS < 0:
    raise RuntimeError('Manual cache sizes and lifetimes cannot be negative')
if not 60 <= RETRIEVAL_CACHE_TTL_SECONDS <= 86400:
    raise RuntimeError("RETRIEVAL_CACHE_TTL_SECONDS must be between 60 and 86400")
