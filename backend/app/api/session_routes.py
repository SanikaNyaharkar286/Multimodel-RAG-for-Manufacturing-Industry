# this file provides api routes for managing chat sessions

from fastapi import APIRouter, Depends
from app.sessions.session_store import store
from app import config as cfg
from app.observability.timing import session_metadata
from auth_system.security import require_approved, current_identity

# creates a group for session routes
router = APIRouter()


# creates a new chat session
@router.post("/sessions")
async def create_session(identity=Depends(require_approved)):
    entry = await store.create(identity.uid)
    await store.checkpoint(entry)
    session_metadata(entry.id)

    # return the session id and expiry time
    return {
        "session_id": entry.id,
        "expires_at": entry.expires_at,
        "request_timeout_seconds": cfg.TIMEOUT,
    }


# gets a session and its saved messages
@router.get("/sessions/{session_id}")
async def get_session(session_id: str, identity=Depends(require_approved)):
    entry = await store.owned(session_id, identity)

    # return the session details and chat history
    return {
        "session_id": entry.id,
        "expires_at": entry.expires_at,
        "request_timeout_seconds": cfg.TIMEOUT,
        "messages": entry.history,
        "repair_workflow": entry.repair_workflow,
        'processing': entry.current_progress,
    }


# deletes a chat session
@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, identity=Depends(require_approved)):
    await store.owned(session_id, identity)
    await store.delete(session_id)

    # confirm that the session was deleted
    return {"deleted": True}


@router.get('/sessions/{session_id}/progress/{request_id}')
async def request_progress(session_id: str, request_id: str, identity=Depends(require_approved)):
    entry = await store.owned(session_id, identity)
    return entry.progress.get(request_id, {'stage': 'Waiting for request', 'status': 'waiting'})
