"""Server-owned Firestore operations for registration and admin decisions."""

from datetime import datetime, timezone
from .security import firebase_app
from app import config as cfg


def db():
    from firebase_admin import firestore
    return firestore.client(app=firebase_app(), database_id=cfg.FIRESTORE_DATABASE_ID)


def complete_registration(uid: str, email: str, fields: dict) -> dict:
    ref = db().collection("users").document(uid)
    existing = ref.get()
    if existing.exists:
        return existing.to_dict()
    now = datetime.now(timezone.utc)
    profile = {"uid": uid, "email": email.lower(), "role": "technician",
               "status": "pending", "companyId": fields["companyId"],
               "fullName": fields["fullName"], "employeeId": fields["employeeId"],
               "department": fields["department"], "site": fields["site"],
               "designation": fields["designation"], "phone": fields.get("phone", ""),
               "requestReason": fields["requestReason"], "createdAt": now, "updatedAt": now}
    ref.create(profile)
    db().collection("adminNotifications").add({"type": "access_request", "uid": uid,
        "read": False, "createdAt": now})
    return profile


def decide(uid: str, status: str, admin_uid: str, reason: str = "") -> dict:
    if status not in {"approved", "rejected", "suspended"}:
        raise ValueError("Invalid status")
    ref = db().collection("users").document(uid)
    snapshot = ref.get()
    if not snapshot.exists:
        raise LookupError("User profile not found")
    previous = snapshot.to_dict().get("status")
    now = datetime.now(timezone.utc)
    ref.update({"status": status, "reviewedBy": admin_uid,
        "reviewedAt": now, "decisionReason": reason, "updatedAt": now})
    db().collection("auditLogs").add({"uid": uid, "actingAdminUid": admin_uid,
        "previousStatus": previous, "newStatus": status, "reason": reason, "createdAt": now})
    return {"uid": uid, "previousStatus": previous, "status": status}
