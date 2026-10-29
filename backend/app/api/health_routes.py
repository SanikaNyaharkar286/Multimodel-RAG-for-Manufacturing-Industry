# api route to check if the backend is running.

from fastapi import APIRouter, Request
from app import config as cfg

# group for the health routes
router = APIRouter()


# gives the backend status, the session time limit, the machine name and the logging status
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