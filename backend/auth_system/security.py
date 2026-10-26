"""Firebase authentication and server-controlled approval dependencies."""

from dataclasses import dataclass
from functools import lru_cache
from collections import OrderedDict
import re
import threading
import time

from fastapi import Depends, Header, HTTPException
from app import config as cfg
from app.observability.timing import metadata, span


@dataclass(frozen=True)
class Identity:
    uid: str
    email: str
    email_verified: bool


_PROFILE_CACHE = OrderedDict()
_PROFILE_CACHE_LOCK = threading.Lock()
_PROFILE_CACHE_MAX = 512


def invalidate_user_profile(uid: str | None = None) -> None:
    """Drop cached approval data after a registration or admin decision."""
    with _PROFILE_CACHE_LOCK:
        if uid is None:
            _PROFILE_CACHE.clear()
        else:
            _PROFILE_CACHE.pop(uid, None)


def normalize_email_domain(email: str) -> str:
    value = email.strip().lower()
    if value.count("@") != 1:
        return ""
    local, domain = value.rsplit("@", 1)
    return domain if local and re.fullmatch(r"[a-z0-9.-]+", domain) else ""


def is_allowed_domain(email: str) -> bool:
    return normalize_email_domain(email) in cfg.ALLOWED_EMAIL_DOMAINS


def is_allowed_identity(email: str) -> bool:
    value = email.strip().lower()
    return value in cfg.ADMIN_EMAILS or is_allowed_domain(value)


@lru_cache(maxsize=1)
def firebase_app():
    import firebase_admin
    from firebase_admin import credentials
    try:
        return firebase_admin.get_app()
    except ValueError:
        return firebase_admin.initialize_app(
            credentials.ApplicationDefault(),
            {"projectId": cfg.FIREBASE_PROJECT},
        )


def current_identity(authorization: str | None = Header(default=None)) -> Identity:
    if not cfg.AUTH_ENABLED:
        return Identity("local-development", "local@example.com", True)
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Authentication is required.")
    try:
        with span('auth_token_verify'):
            from firebase_admin import auth
            decoded = auth.verify_id_token(authorization[7:].strip(), check_revoked=True,
                                           app=firebase_app())
    except Exception as exc:
        raise HTTPException(401, "Invalid or expired authentication.") from exc
    identity = Identity(str(decoded["uid"]), str(decoded.get("email", "")),
                        bool(decoded.get("email_verified", False)))
    if not identity.email_verified or not is_allowed_identity(identity.email):
        raise HTTPException(403, "Verify your company email before continuing.")
    return identity


def require_approved(identity: Identity = Depends(current_identity)) -> Identity:
    if not cfg.AUTH_ENABLED:
        return identity
    profile = user_profile(identity.uid)
    if not profile:
        raise HTTPException(403, "Complete registration before using the application.")
    if profile.get("status") != "approved":
        raise HTTPException(403, {"status": profile.get("status", "pending"),
                                  "message": "Administrator approval is required."})
    return identity


def require_admin(identity: Identity = Depends(current_identity)) -> Identity:
    profile = user_profile(identity.uid)
    if not profile or profile.get("role") != "admin" or profile.get("status") != "approved":
        raise HTTPException(403, "Administrator access is required.")
    return identity


def user_profile(uid: str) -> dict | None:
    if not cfg.AUTH_ENABLED:
        return {"uid": uid, "role": "admin", "status": "approved"}
    ttl = cfg.AUTH_PROFILE_CACHE_TTL_SECONDS
    now = time.monotonic()
    if ttl:
        with _PROFILE_CACHE_LOCK:
            cached = _PROFILE_CACHE.get(uid)
            if cached is not None and now - cached[0] < ttl:
                _PROFILE_CACHE.move_to_end(uid)
                metadata(auth_profile_cache_hit=True)
                return dict(cached[1]) if cached[1] is not None else None
            _PROFILE_CACHE.pop(uid, None)
    with span('auth_profile_read'):
        from firebase_admin import firestore
        snapshot = firestore.client(app=firebase_app(), database_id=cfg.FIRESTORE_DATABASE_ID).collection("users").document(uid).get()
        profile = snapshot.to_dict() if snapshot.exists else None
    if ttl:
        with _PROFILE_CACHE_LOCK:
            _PROFILE_CACHE[uid] = (now, dict(profile) if profile is not None else None)
            _PROFILE_CACHE.move_to_end(uid)
            while len(_PROFILE_CACHE) > _PROFILE_CACHE_MAX:
                _PROFILE_CACHE.popitem(last=False)
    return profile
