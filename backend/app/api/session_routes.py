# this file provides api routes for managing chat sessions

import asyncio
import hashlib
import json
import time

from fastapi import APIRouter, Depends
from app.sessions.session_store import store
from app import config as cfg
from app.observability.timing import session_metadata
from auth_system.security import require_approved, current_identity

# creates a group for session routes
router = APIRouter()


def _progress_cursor(progress: dict) -> str:
    """Create an opaque change token without putting progress text in the URL."""
    encoded = json.dumps(progress, sort_keys=True, separators=(',', ':'), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]


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
async def request_progress(session_id: str, request_id: str, cursor: str = '',
                           identity=Depends(require_approved)):
    entry = await store.owned(session_id, identity)
    # Hold the request briefly when the client already has the current state.
    # This preserves live progress while avoiding an authenticated HTTP request
    # every few hundred milliseconds. The main /chat request remains authoritative.
    deadline = time.monotonic() + 10
    while True:
        progress = entry.progress.get(
            request_id, {'stage': 'Waiting for request', 'status': 'waiting'})
        current = _progress_cursor(progress)
        if (not cursor or cursor != current
                or progress.get('status') in {'completed', 'error'}):
            return {**progress, 'cursor': current}
        if time.monotonic() >= deadline:
            return {**progress, 'cursor': current}
        await asyncio.sleep(.15)
