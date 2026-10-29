"""Firestore metadata, per-admin inboxes and durable email outbox.

The existing maintenanceReports collection remains the editable draft store.
All new records are server-owned, using the existing Firebase app/database.
"""
import hashlib
import re
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from google.api_core.exceptions import AlreadyExists
from fastapi import HTTPException
from auth_system.firestore import db
from app import config as cfg

REPORTS = 'maintenance_reports'
NOTIFICATIONS = 'admin_notifications'
JOBS = 'maintenance_report_email_jobs'
STAMP = firestore.SERVER_TIMESTAMP


def doc_key(value):
    # Never use untrusted UIDs, addresses or legacy notification IDs as paths.
    return hashlib.sha256(value.encode()).hexdigest()


def search_tokens(record):
    tokens = set()
    for field in ('report_id', 'report_number', 'technician_name', 'technician_email',
                  'machine_name', 'machine_model', 'issue_description'):
        text = re.sub(r'\s+', ' ', record.get(field, '').casefold()).strip()[:2000]
        for start in [0, *[m.end() for m in re.finditer(r'[\s@_\-/]+', text)]]:
            tail = text[start:start + 80]
            for i in range(1, len(tail) + 1):
                tokens.add(tail[:i])
                if len(tokens) >= 2000:
                    return sorted(tokens)
    return sorted(tokens)


def get_submission(report_id, owner_uid=None):
    if not cfg.AUTH_ENABLED:
        return None
    snap = db().collection(REPORTS).document(report_id).get()
    record = snap.to_dict() if snap.exists else None
    if record and owner_uid is not None and record['owner_uid'] != owner_uid:
        raise HTTPException(404, 'Report not found.')
    return record


def notification_record(admin_uid, kind, reference_id, title, message):
    return {'type': kind, 'target_admin_uid': admin_uid, 'title': title,
            'message': message, 'report_id': reference_id if kind == 'maintenance_report_submitted' else '',
            'request_uid': reference_id if kind == 'registration_request' else '',
            'is_read': False, 'created_at': STAMP, 'read_at': None}


def approved_admins():
    return [snap.id for snap in db().collection('users').where(
        filter=FieldFilter('role', '==', 'admin')).stream()
        if (snap.to_dict() or {}).get('status') == 'approved']


def create_submission(record, admin_uids, recipients):
    client = db()
    batch = client.batch()
    ref = client.collection(REPORTS).document(record['report_id'])
    values = {**record, 'created_at': STAMP, 'submitted_at': STAMP, 'updated_at': STAMP,
              'search_tokens': search_tokens(record)}
    batch.create(ref, values)
    for uid in admin_uids:
        key = doc_key(f'report:{record["report_id"]}:{uid}')
        batch.create(client.collection(NOTIFICATIONS).document(key), notification_record(
            uid, 'maintenance_report_submitted', record['report_id'],
            f'New Maintenance Report — {record["machine_name"] or "Machine not recorded"}',
            f'{record["technician_name"] or record["technician_email"]} submitted {record["report_number"]}.'))
    # One recipient per delivery prevents a partial SMTP acceptance resending to
    # recipients who already accepted. No email side effects in this transaction.
    for recipient in recipients:
        key = doc_key(f'{record["report_id"]}:{recipient.casefold()}')
        batch.create(client.collection(JOBS).document(key), {
            'report_id': record['report_id'], 'recipient': recipient,
            'status': 'pending', 'attempts': 0, 'phase': 'queued',
            'next_attempt_at': STAMP, 'created_at': STAMP, 'updated_at': STAMP,
            'lease_until': None, 'lease_token': '', 'provider_message_id': '',
            'message_id': f'<{key}@maintenance-copilot.local>', 'error_code': ''})
    try:
        # Firestore's atomic create preconditions make concurrent submissions,
        # notification creation and outbox registration an all-or-nothing write.
        batch.commit()
    except AlreadyExists:
        existing = get_submission(record['report_id'], record['owner_uid'])
        if not existing:
            raise
        return existing
    return get_submission(record['report_id'], record['owner_uid'])


def count(query):
    return int(query.count().get()[0][0].value)


def list_reports(owner_uid=None, status='', search='', page=1, page_size=20, sort='desc'):
    base = db().collection(REPORTS)
    if owner_uid is not None:
        base = base.where(filter=FieldFilter('owner_uid', '==', owner_uid))
    if search:
        base = base.where(filter=FieldFilter('search_tokens', 'array_contains', search.casefold().strip()))
    counts = {value: count(base.where(filter=FieldFilter('review_status', '==', value)))
              for value in ('new', 'under_review', 'reviewed')}
    query = base.where(filter=FieldFilter('review_status', '==', status)) if status else base
    total = counts[status] if status else sum(counts.values())
    direction = firestore.Query.DESCENDING if sort == 'desc' else firestore.Query.ASCENDING
    rows = query.order_by('submitted_at', direction=direction).order_by('__name__', direction=direction)
    return {'reports': [s.to_dict() for s in rows.offset((page-1)*page_size).limit(page_size).stream()],
            'counts': counts, 'total': total, 'page': page, 'page_size': page_size}


def review(report_id, admin_uid, edit):
    client = db()
    ref = client.collection(REPORTS).document(report_id)
    @firestore.transactional
    def update(transaction):
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            raise HTTPException(404, 'Submitted report not found.')
        record = snap.to_dict()
        comment = edit.admin_comment if edit.admin_comment is not None else record.get('admin_comment') or ''
        if edit.version != record['version']:
            raise HTTPException(409, 'Review changed. Reload before saving.')
        if (record['review_status'], record.get('admin_comment') or '') == (edit.review_status, comment):
            return
        transaction.update(ref, {'review_status': edit.review_status, 'admin_comment': comment,
            'assigned_admin_uid': admin_uid, 'reviewed_at': STAMP if edit.review_status == 'reviewed' else None,
            'updated_at': STAMP, 'version': record['version'] + 1})
        transaction.create(ref.collection('review_events').document(), {
            'admin_uid': admin_uid, 'previous_status': record['review_status'],
            'review_status': edit.review_status, 'admin_comment': comment, 'created_at': STAMP})
    update(client.transaction())
    return get_submission(report_id)


def sync_registration_notifications(admin_uid):
    """Adapt existing registration records, without changing registration/email.

    Deterministic per-admin mirrors preserve independent read state; the legacy
    adminNotifications collection is still the registration delivery source.
    """
    client = db()
    cursor_ref = client.collection('admin_notification_sync').document(doc_key(admin_uid))
    cursor = cursor_ref.get()
    query = client.collection('adminNotifications').where(
        filter=FieldFilter('type', '==', 'access_request')).order_by('createdAt').order_by('__name__')
    if cursor.exists:
        previous = cursor.to_dict()
        query = query.start_after({'createdAt': previous['created_at'],
            '__name__': client.collection('adminNotifications').document(previous['legacy_id'])})
    for snapshot in query.limit(200).stream():
        old = snapshot.to_dict() or {}
        uid = old.get('uid') or snapshot.id
        key = doc_key(f'registration:{snapshot.id}:{admin_uid}')
        value = notification_record(admin_uid, 'registration_request', uid,
                                    'New User Registration Request', 'A technician requested workspace access.')
        value['created_at'] = old.get('createdAt') or STAMP
        if old.get('read') is True:
            value.update(is_read=True, read_at=old.get('readAt'))
        try:
            client.collection(NOTIFICATIONS).document(key).create(value)
        except AlreadyExists:
            pass
        # Advance only after this legacy record has a durable per-admin mirror.
        # A crash before checkpointing just retries its deterministic create.
        cursor_ref.set({'created_at': value['created_at'], 'legacy_id': snapshot.id})


def notifications(admin_uid, page=1, page_size=20):
    sync_registration_notifications(admin_uid)
    query = db().collection(NOTIFICATIONS).where(filter=FieldFilter('target_admin_uid', '==', admin_uid))
    return {'notifications': [{**snap.to_dict(), 'notification_id': snap.id} for snap in
            query.order_by('created_at', direction=firestore.Query.DESCENDING).order_by('__name__', direction=firestore.Query.DESCENDING)
            .offset((page-1)*page_size).limit(page_size).stream()],
            'unread_count': count(query.where(filter=FieldFilter('is_read', '==', False))),
            'page': page, 'page_size': page_size}


def mark_read(admin_uid, notification_id):
    client = db()
    ref = client.collection(NOTIFICATIONS).document(notification_id)
    @firestore.transactional
    def update(transaction):
        snap = ref.get(transaction=transaction)
        if not snap.exists or snap.to_dict().get('target_admin_uid') != admin_uid:
            raise HTTPException(404, 'Notification not found.')
        if not snap.to_dict()['is_read']:
            transaction.update(ref, {'is_read': True, 'read_at': STAMP})
    update(client.transaction())


def mark_all_read(admin_uid):
    query = db().collection(NOTIFICATIONS).where(filter=FieldFilter('target_admin_uid', '==', admin_uid)).where(
        filter=FieldFilter('is_read', '==', False))
    # Only mark the captured inbox; notifications arriving afterwards stay unread.
    refs = [snap.reference for snap in query.stream()]
    for start in range(0, len(refs), 400):
        batch = db().batch()
        for ref in refs[start:start+400]:
            batch.update(ref, {'is_read': True, 'read_at': STAMP})
        batch.commit()
    return len(refs)
