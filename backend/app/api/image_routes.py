from fastapi import APIRouter
from fastapi.responses import Response
from app.sessions.session_store import store

router = APIRouter()


@router.get("/images/{asset_id}")
async def original_image(asset_id: str):
    data = await store.image(asset_id)

    return Response(
        data,
        media_type="image/png",
        headers={
            "Cache-Control": "no-store",
        },
    )
