"""Submit an existing report without changing findings or calling the agent."""
import logging
import os
import re
from fastapi import HTTPException
from app.sessions import persistence
from app.responses.maintenance_report import upgrade_report
from auth_system.security import user_profile
from . import repository, storage
from .schemas import Submission

logger = logging.getLogger('uvicorn.error.report_submission')


def recipients():
    value = os.getenv('ADMIN_NOTIFICATION_EMAILS') or os.getenv('ADMIN_NOTIFICATION_EMAIL', '')
    addresses = list(dict.fromkeys(a.strip().casefold() for a in re.split(r'[,;]', value) if a.strip()))
    if len(addresses) > 100 or any(not re.fullmatch(r'[^\s<>@,;]+@[^\s<>@,;]+\.[^\s<>@,;]+', a) for a in addresses):
        logger.error('Report email recipients configuration is invalid')
        return []
    return addresses


def public(record):
    return Submission.model_validate(record).model_dump(mode='json')


def submit(report_id, identity, revision):
    existing = repository.get_submission(report_id, identity.uid)
    if existing:
        return public(existing)
    report = persistence.load_report(report_id, identity.uid)
    if not report:
        raise HTTPException(404, 'Report not found.')
    report = upgrade_report(report)
    if report['revision'] != revision:
        raise HTTPException(409, 'Report changed. Reopen it before submitting.')
    profile = user_profile(identity.uid) or {}
    # Identity comes from verified auth/profile, never browser-supplied fields.
    addresses = recipients()
    admins = repository.approved_admins()
    if len(admins) > 250:
        raise HTTPException(503, 'Report notification recipients exceed the configured capacity.')
    stored = storage.persist_report(report, identity.uid)
    record = {'submission_id': report_id, 'report_id': report_id,
        'report_number': report.get('report_number') or report_id, 'owner_uid': identity.uid,
        'technician_name': profile.get('fullName') or report.get('technician_name', ''),
        'technician_email': identity.email, 'machine_name': report.get('machine', ''),
        'machine_model': report.get('model', ''), 'issue_description': report.get('issue', '')[:2000],
        'session_id': report.get('session_id', ''), 'case_id': report.get('case_id', ''),
        'report_type': 'troubleshooting' if report.get('case_id') else 'information',
        'revision': report['revision'], 'review_status': 'new', 'admin_comment': None,
        'assigned_admin_uid': None, 'reviewed_at': None, 'version': 1,
        'email_status': 'pending' if addresses else 'failed', 'email_delivery': 'not_confirmed',
        'email_error': '' if addresses else 'Email notification is not configured.', **stored}
    return public(repository.create_submission(record, admins, addresses))


def detail(record):
    from app.responses.documentation import public_document
    snapshot = public_document(storage.read_snapshot(record))
    # Do not send chat history/user photo data in an administrative metadata view.
    # The immutable PDF, guidance, confirmations and evidence are available below.
    snapshot.pop('history', None)
    return {'submission': public(record), 'report': snapshot}
