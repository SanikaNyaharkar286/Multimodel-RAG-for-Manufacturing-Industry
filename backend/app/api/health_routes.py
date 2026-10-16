# this file provides an endpoint to check the backend status

from fastapi import APIRouter
from app import config as cfg

# creates a group for health routes
router = APIRouter()


# returns the backend status and selected settings
@router.get("/health")
def health():
    return {
        "status": "running",
        "session_service": "ADK InMemorySessionService",
        "session_max_seconds": cfg.TTL,
        "machine": cfg.MACHINE,
    }