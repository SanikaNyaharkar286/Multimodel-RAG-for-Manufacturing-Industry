# this file provides api routes for managing chat sessions

from fastapi import APIRouter
from app.sessions.session_store import store

# creates a group for session routes
router = APIRouter()


# creates a new chat session
@router.post("/sessions")
async def create_session():
    entry = await store.create()

    # return the session id and expiry time
    return {
        "session_id": entry.id,
        "expires_at": entry.expires_at,
    }


# gets a session and its saved messages
@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    entry = await store.get(session_id)

    # return the session details and chat history
    return {
        "session_id": entry.id,
        "expires_at": entry.expires_at,
        "messages": entry.history,
    }


# deletes a chat session
@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    await store.delete(session_id)

    # confirm that the session was deleted
    return {"deleted": True}