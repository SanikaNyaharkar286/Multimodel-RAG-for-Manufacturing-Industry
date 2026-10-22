"""Firebase registration, status, and admin approval endpoints."""

import asyncio
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from app import config as cfg
from auth_system.security import current_identity, require_admin, user_profile, is_allowed_domain
from auth_system.firestore import complete_registration, decide

router = APIRouter(prefix="/auth", tags=["auth"])


class Registration(BaseModel):
    companyId: str
    fullName: str = Field(min_length=1, max_length=120)
    employeeId: str = Field(min_length=1, max_length=80)
    department: str = Field(min_length=1, max_length=120)
    site: str = Field(min_length=1, max_length=120)
    designation: str = Field(min_length=1, max_length=120)
    phone: str = Field(default="", max_length=40)
    requestReason: str = Field(min_length=1, max_length=1000)


class Decision(BaseModel):
    status: str
    reason: str = Field(default="", max_length=1000)


def safe_profile(identity, profile: dict) -> dict:
    """Return the current user's display fields without review or admin metadata."""
    values = {
        'uid': identity.uid,
        'name': profile.get('fullName'),
        'email': profile.get('email') or identity.email.lower(),
        'employee_id': profile.get('employeeId'),
        'role': profile.get('role'),
        'status': profile.get('status'),
        'created_at': profile.get('createdAt'),
    }
    company_name = cfg.COMPANY_NAME.strip()
    if profile.get('companyId') and company_name and company_name != 'COMPANY_NAME':
        values['company_name'] = company_name
    elif profile.get('companyId'):
        values['company_id'] = profile['companyId']
    return {key: value for key, value in values.items() if value not in (None, '')}


@router.get("/config")
async def auth_config():
    return {"enabled": cfg.AUTH_ENABLED, "companyId": cfg.COMPANY_ID,
            "companyName": cfg.COMPANY_NAME, "allowedEmailDomains": sorted(cfg.ALLOWED_EMAIL_DOMAINS)}


@router.post("/registration")
async def registration(data: Registration, identity=Depends(current_identity)):
    if not cfg.AUTH_ENABLED:
        raise HTTPException(503, "Authentication is not configured.")
    if data.companyId != cfg.COMPANY_ID or not is_allowed_domain(identity.email):
        raise HTTPException(403, "Company registration details are not allowed.")
    # Firestore and SMTP are blocking clients; neither runs on the event loop.
    profile = await asyncio.to_thread(complete_registration, identity.uid, identity.email, data.model_dump())
    return {**profile, 'message': 'Approval request submitted successfully.'}


@router.get("/status")
async def status(identity=Depends(current_identity)):
    profile = await asyncio.to_thread(user_profile, identity.uid)
    if profile:
        return profile
    if identity.email.strip().lower() in cfg.ADMIN_EMAILS:
        return {"uid": identity.uid, "email": identity.email.lower(), "role": "admin",
                "status": "admin_setup_required"}
    return {"uid": identity.uid, "status": "incomplete"}


@router.get('/me')
async def me(identity=Depends(current_identity)):
    profile = await asyncio.to_thread(user_profile, identity.uid)
    if not profile:
        raise HTTPException(404, 'User profile not found.')
    return safe_profile(identity, profile)


def _list_technician_requests():
    from auth_system.security import firebase_app
    from firebase_admin import firestore
    rows = firestore.client(app=firebase_app(), database_id=cfg.FIRESTORE_DATABASE_ID).collection("users").where("role", "==", "technician").stream()
    return {"requests": [row.to_dict() for row in rows]}


@router.get("/admin/requests")
async def requests(identity=Depends(require_admin)):
    # Firestore streaming is synchronous and must not stall ongoing chat calls.
    return await asyncio.to_thread(_list_technician_requests)


@router.post("/admin/users/{uid}/decision")
async def decision(uid: str, data: Decision, identity=Depends(require_admin)):
    try:
        return await asyncio.to_thread(decide, uid, data.status, identity.uid, data.reason)
    except (ValueError, LookupError) as exc:
        raise HTTPException(400, str(exc)) from exc
