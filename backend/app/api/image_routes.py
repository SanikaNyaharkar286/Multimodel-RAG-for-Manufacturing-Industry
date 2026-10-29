# api route that sends saved chat images back to the frontend.

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from app.sessions.session_store import store
from auth_system.security import require_approved

# group for the image routes
router = APIRouter()


# gives back a saved image by its id.
# it first checks that the image belongs to the logged-in user's session
@router.get("/images/{asset_id}")
async def original_image(asset_id: str, session_id: str = '', identity=Depends(require_approved)):
    owner = store.asset_owners.get(asset_id)
    if not owner and session_id:
        # After reload the image-owner index is empty until its owning session
        # is restored. A session hint allows owner-checked recovery of the image.
        await store.owned(session_id, identity)
        owner = store.asset_owners.get(asset_id)
    if not owner:
        raise HTTPException(404, 'Image unavailable or session expired.')
    await store.owned(owner, identity)
    # get the image data from the session store
    data = await store.image(asset_id)

    # send the image as png and do not let the browser cache it
    return Response(
        data,
        media_type="image/png",
        headers={
            "Cache-Control": "no-store",
        },
    )
