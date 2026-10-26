"""Server-owned Firestore operations for registration and admin decisions."""

from datetime import datetime, timezone
import logging
from .security import firebase_app, invalidate_user_profile
from app import config as cfg
from google.api_core.exceptions import AlreadyExists
from app.services.email_service import send_admin_approval_notification

logger = logging.getLogger('uvicorn.error.approval_email')


def db():
    from firebase_admin import firestore
    return firestore.client(app=firebase_app(), database_id=cfg.FIRESTORE_DATABASE_ID)


def complete_registration(uid: str, email: str, fields: dict) -> dict:
    client = db()
    ref = client.collection("users").document(uid)
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
    # Save the profile and existing in-app notification before attempting email.
    batch = client.batch()
    batch.create(ref, profile)
    notification_ref = client.collection('adminNotifications').document(uid)
    batch.create(notification_ref, {
        'type': 'access_request', 'uid': uid, 'read': False, 'createdAt': now,
        'emailStatus': 'pending', 'emailAttempts': 0})
    try:
        batch.commit()
    except AlreadyExists:
        # Another simultaneous request may have completed the same registration.
        existing = ref.get()
        if not existing.exists:
            raise
        return existing.to_dict()
    invalidate_user_profile(uid)
    logger.info('Pending approval request created uid=%s', uid)
    try:
        sent = send_admin_approval_notification(profile)
        notification_ref.update({
            'emailStatus': 'sent' if sent else 'failed',
            'emailAttempts': 1,
            'emailLastAttemptAt': datetime.now(timezone.utc),
            **({'emailSentAt': datetime.now(timezone.utc)} if sent else {}),
        })
    except Exception as exc:
        # Defend the saved registration even if a notification implementation fails.
        logger.error('Admin email notification failed uid=%s error_type=%s', uid, type(exc).__name__)
    return profile


def retry_pending_approval_notifications(limit: int = 20) -> int:
    """Retry failed email only while its original technician request is pending."""
    client = db()
    sent_count = 0
    checked = 0
    for snapshot in client.collection('adminNotifications').stream():
        notification = snapshot.to_dict() or {}
        if notification.get('type') != 'access_request':
            continue
        if notification.get('emailStatus', 'pending') == 'sent':
            continue
        if int(notification.get('emailAttempts', 0) or 0) >= 10:
            continue
        uid = notification.get('uid') or snapshot.id
        profile_snapshot = client.collection('users').document(uid).get()
        if not profile_snapshot.exists:
            continue
        profile = profile_snapshot.to_dict()
        if profile.get('status') != 'pending':
            continue
        checked += 1
        sent = send_admin_approval_notification(profile)
        attempts = int(notification.get('emailAttempts', 0) or 0) + 1
        values = {
            'emailStatus': 'sent' if sent else 'failed',
            'emailAttempts': attempts,
            'emailLastAttemptAt': datetime.now(timezone.utc),
        }
        if sent:
            values['emailSentAt'] = datetime.now(timezone.utc)
            sent_count += 1
        snapshot.reference.update(values)
        if checked >= limit:
            break
    return sent_count


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
    invalidate_user_profile(uid)
    db().collection("auditLogs").add({"uid": uid, "actingAdminUid": admin_uid,
        "previousStatus": previous, "newStatus": status, "reason": reason, "createdAt": now})
    return {"uid": uid, "previousStatus": previous, "status": status}
