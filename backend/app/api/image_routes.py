# this file returns saved chat images to the frontend

from fastapi import APIRouter, Depends
from fastapi.responses import Response
from app.sessions.session_store import store
from auth_system.security import require_approved

# creates a group for image routes
router = APIRouter()


# gets an image by its session asset id
@router.get("/images/{asset_id}")
async def original_image(asset_id: str, identity=Depends(require_approved)):
    # find the image data in the session store
    data = await store.image(asset_id)

    # return the image as png without caching it
    return Response(
        data,
        media_type="image/png",
        headers={
            "Cache-Control": "no-store",
        },
    )
