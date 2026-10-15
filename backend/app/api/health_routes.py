from fastapi import APIRouter
from app import config as cfg

router = APIRouter()


@router.get("/health")
def health():
    return {
        "status": "running",
        "session_service": "ADK InMemorySessionService",
        "session_max_seconds": cfg.TTL,
        "machine": cfg.MACHINE,
    }
