# api routes for managing chat sessions: create, open, delete, and check the progress of a request.

import asyncio
import hashlib
import json
import time

from fastapi import APIRouter, Depends
from app.sessions.session_store import store
from app import config as cfg
from app.observability.timing import session_metadata
from app.agent.chat_service import cached_step_previews
from auth_system.security import require_approved, current_identity

# group for the session routes
router = APIRouter()


# makes a short code from the progress. it changes when the progress changes.
# this way the progress text is not put in the url
def _progress_cursor(progress: dict) -> str:
    """makes a short code that changes when the progress changes, without putting the progress text in the url."""
    encoded = json.dumps(progress, sort_keys=True, separators=(',', ':'), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()[:24]


# starts a new chat session for the logged-in user
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


# opens a session and gives back its saved messages
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
        "next_step_previews": cached_step_previews(entry),
        'processing': entry.current_progress,
    }


# deletes a chat session
@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str, identity=Depends(require_approved)):
    await store.owned(session_id, identity)
    await store.delete(session_id)

    # confirm that the session was deleted
    return {"deleted": True}


# tells the frontend how far a chat request has got.
# if nothing changed since the last check, it waits up to 10 seconds before replying,
# so the frontend does not need to ask again and again
@router.get('/sessions/{session_id}/progress/{request_id}')
async def request_progress(session_id: str, request_id: str, cursor: str = '',
                           identity=Depends(require_approved)):
    entry = await store.owned(session_id, identity)
    # wait a little if the client already has the latest progress.
    # this keeps the progress live without a request every few hundred milliseconds.
    # the main /chat request is still the real answer
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
