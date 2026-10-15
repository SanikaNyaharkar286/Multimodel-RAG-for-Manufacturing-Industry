from fastapi import APIRouter
from app.sessions.session_store import store

router = APIRouter()


@router.post("/sessions")
async def create_session():
    entry = await store.create()

    return {
        "session_id": entry.id,
        "expires_at": entry.expires_at,
    }


@router.get("/sessions/{session_id}")
async def get_session(session_id: str):
    entry = await store.get(session_id)

    return {
        "session_id": entry.id,
        "expires_at": entry.expires_at,
        "messages": entry.history,
    }


@router.delete("/sessions/{session_id}")
async def delete_session(session_id: str):
    await store.delete(session_id)

    return {"deleted": True}
