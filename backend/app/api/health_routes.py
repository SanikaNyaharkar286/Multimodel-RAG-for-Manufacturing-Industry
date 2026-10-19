# this file provides an endpoint to check the backend status

from fastapi import APIRouter, Request
from app import config as cfg

# creates a group for health routes
router = APIRouter()


# returns the backend status and selected settings
@router.get("/health")
def health(request: Request):
    return {
        "status": "running",
        "session_service": "ADK InMemorySessionService",
        "session_max_seconds": cfg.TTL,
        "machine": cfg.MACHINE,
        "api_logging": request.app.state.api_log_sink.status() if getattr(
            request.app.state, 'api_log_sink', None) else {'started': False},
    }
