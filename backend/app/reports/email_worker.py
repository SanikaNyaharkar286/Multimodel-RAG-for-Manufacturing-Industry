"""Durable Firestore outbox consumer, run independently of the API.

    python -m app.reports.email_worker             # supervised worker
    python -m app.reports.email_worker --once      # scheduled Cloud Run Job

SMTP cannot promise exactly-once delivery. Ambiguous DATA/crash outcomes are
held for operator investigation instead of automatically sending duplicates.
"""
import argparse
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr
import logging
import os
import smtplib
import ssl
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4
from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter
from app.services.email_service import load_settings
from app import config as cfg
from . import repository, storage

logger = logging.getLogger('report_email_worker')
MAX_ATTEMPTS = 8
LEASE_SECONDS = 300


def now():
    return datetime.now(timezone.utc)


def claim(job_id):
    client = repository.db()
    ref = client.collection(repository.JOBS).document(job_id)
    @firestore.transactional
    def acquire(transaction):
        snap = ref.get(transaction=transaction)
        if not snap.exists:
            return None
        job = snap.to_dict()
        if job['status'] in {'sent', 'failed'}:
            return None
        report_ref = client.collection(repository.REPORTS).document(job['report_id'])
        if not report_ref.get(transaction=transaction).exists:
            transaction.update(ref, {'status': 'failed', 'error_code': 'report_missing',
                'phase': 'finished', 'updated_at': repository.STAMP, 'lease_until': None})
            return None
        moment = now()
        if job['status'] == 'sending':
            if job.get('lease_until') and job['lease_until'] > moment:
                return None
            if job.get('phase') == 'transmitting':
                transaction.update(ref, {'status': 'failed', 'error_code': 'delivery_uncertain',
                    'updated_at': repository.STAMP, 'lease_until': None})
                transaction.update(client.collection(repository.REPORTS).document(job['report_id']), {
                    'email_status': 'failed', 'email_delivery': 'not_confirmed',
                    'email_error': 'Email outcome is uncertain; administrator investigation is required.'})
                return None
        if job.get('next_attempt_at') and job['next_attempt_at'] > moment:
            return None
        if job['attempts'] >= MAX_ATTEMPTS:
            transaction.update(ref, {'status': 'failed', 'error_code': 'attempts_exhausted',
                                    'updated_at': repository.STAMP, 'lease_until': None})
            transaction.update(client.collection(repository.REPORTS).document(job['report_id']), {
                'email_status': 'failed', 'email_error': 'Email retry attempts were exhausted.'})
            return None
        values = {'status': 'sending', 'phase': 'preparing', 'lease_token': uuid4().hex,
                  'lease_until': moment + timedelta(seconds=LEASE_SECONDS),
                  'attempts': job['attempts'] + 1, 'updated_at': repository.STAMP}
        transaction.update(ref, values)
        transaction.update(client.collection(repository.REPORTS).document(job['report_id']),
                           {'email_status': 'sending', 'email_error': ''})
        return {**job, **values, 'job_id': job_id}
    return acquire(client.transaction())


def transmitting(job):
    client = repository.db()
    ref = client.collection(repository.JOBS).document(job['job_id'])
    @firestore.transactional
    def update(transaction):
        snap = ref.get(transaction=transaction)
        current = snap.to_dict() if snap.exists else {}
        if (current.get('lease_token') != job['lease_token'] or current.get('status') != 'sending'
                or current.get('lease_until', now()) <= now()):
            raise RuntimeError('Delivery lease expired')
        transaction.update(ref, {'phase': 'transmitting', 'updated_at': repository.STAMP})
    update(client.transaction())


def finish(job, status, error_code='', retryable=False):
    client = repository.db()
    ref = client.collection(repository.JOBS).document(job['job_id'])
    @firestore.transactional
    def update(transaction):
        snap = ref.get(transaction=transaction)
        current = snap.to_dict() if snap.exists else {}
        if current.get('lease_token') != job['lease_token'] or current.get('status') != 'sending':
            return
        query = client.collection(repository.JOBS).where(filter=FieldFilter('report_id', '==', job['report_id']))
        siblings = {s.id: s.to_dict() for s in query.stream(transaction=transaction)}
        report_ref = client.collection(repository.REPORTS).document(job['report_id'])
        report_exists = report_ref.get(transaction=transaction).exists
        retry = retryable and job['attempts'] < MAX_ATTEMPTS
        values = {'status': 'pending' if retry else status, 'phase': 'queued' if retry else 'finished',
            'error_code': error_code, 'updated_at': repository.STAMP, 'lease_until': None,
            'next_attempt_at': now() + timedelta(seconds=min(3600, 30 * 2 ** (job['attempts']-1))),
            'accepted_at': repository.STAMP if status == 'sent' else None}
        siblings[job['job_id']] = {**current, **values}
        states = [item['status'] for item in siblings.values()]
        aggregate = ('sent' if all(s == 'sent' for s in states) else 'sending' if 'sending' in states
                     else 'failed' if 'failed' in states else 'pending')
        transaction.update(ref, values)
        if report_exists:
            transaction.update(report_ref, {
                'email_status': aggregate,
                'email_delivery': 'accepted_by_smtp' if aggregate == 'sent' else 'not_confirmed',
                'email_error': ('Email delivery needs administrator attention.' if aggregate == 'failed'
                                else 'Email notification will be retried.' if retry else '')})
    update(client.transaction())


def build_message(record, job, settings, pdf):
    url = urlsplit(settings.portal_url)
    params = dict(parse_qsl(url.query))
    params.pop('request', None)
    params.update(admin='reports', report=record['report_id'])
    link = urlunsplit((url.scheme, url.netloc, url.path, urlencode(params), ''))
    message = EmailMessage()
    message['Subject'] = f'[Maintenance Copilot] New Maintenance Report - {record["report_number"]}'
    message['From'] = formataddr((settings.sender_name, settings.sender))
    message['To'] = job['recipient']
    message['Message-ID'] = job['message_id']
    limit = max(1024, min(25 * 1024 * 1024, int(os.getenv('REPORT_EMAIL_MAX_MESSAGE_BYTES', str(20*1024*1024)))))
    body = ('Hello Admin,\n\nA new technical maintenance report has been submitted by a technician.\n\n'
        f'Report ID: {record["report_number"]}\nTechnician: {record["technician_name"]}\n'
        f'Technician Email: {record["technician_email"]}\nMachine: {record["machine_name"]}\n'
        f'Reported Issue: {record["issue_description"]}\nSubmitted At: {record["submitted_at"]}\nStatus: New\n\n')
    # Leave room for base64 expansion and MIME/SMTP headers; check actual wire
    # size as well, since long issue text can also consume the provider limit.
    attach = bool(pdf) and len(pdf) * 4 // 3 + len(body.encode()) + 8192 <= limit
    message.set_content(body + ('The maintenance report PDF is attached.\n\n' if attach else
        'The PDF exceeds the attachment limit. View or download it after signing in.\n\n') +
        f'Please log in to the Manufacturing Maintenance Copilot Admin Portal to review the report.\n{link}\n\n'
        'Regards,\nManufacturing Maintenance Copilot\n')
    if attach:
        message.add_attachment(pdf, maintype='application', subtype='pdf', filename=f'{record["report_number"]}.pdf')
        if len(message.as_bytes()) > limit:
            # Rebuild without the attachment, retaining only an authenticated link.
            return build_message(record, job, settings, b'')
    return message


def deliver(job):
    phase = 'preparing'
    try:
        settings = load_settings(recipient=job['recipient'])
        if settings is None:
            finish(job, 'failed', 'email_configuration', retryable=True)
            return
        record = repository.get_submission(job['report_id'])
        if not record:
            finish(job, 'failed', 'report_missing')
            return
        size_limit = int(os.getenv('REPORT_EMAIL_MAX_MESSAGE_BYTES', str(20*1024*1024)))
        pdf = storage.read_object(record) if record['pdf_storage']['size'] * 4 // 3 + 8192 <= size_limit else b''
        message = build_message(record, job, settings, pdf)
        with smtplib.SMTP(settings.host, settings.port, timeout=20) as smtp:
            smtp.ehlo()
            if settings.use_tls:
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            smtp.login(settings.username, settings.password)
            transmitting(job)
            phase = 'transmitting'
            refused = smtp.send_message(message, from_addr=settings.sender, to_addrs=[job['recipient']])
            if refused:
                raise smtplib.SMTPRecipientsRefused(refused)
            phase = 'accepted'
            # Record acceptance before QUIT, which may itself disconnect. This
            # is SMTP acceptance, never proof of delivery to the admin inbox.
            finish(job, 'sent')
    except Exception as exc:
        logger.error('Report email attempt failed job=%s error_type=%s', job['job_id'], type(exc).__name__)
        if phase == 'accepted':
            # A Firestore failure after SMTP acceptance leaves the transmitting
            # lease for recovery as uncertain; never automatically resend.
            return
        code = getattr(exc, 'smtp_code', None)
        if isinstance(exc, smtplib.SMTPRecipientsRefused):
            code = next(iter(exc.recipients.values()))[0]
        known_rejection = isinstance(code, int)
        retryable = (400 <= code < 500) if known_rejection else phase == 'preparing'
        finish(job, 'failed', 'smtp_rejected' if known_rejection else
               'delivery_uncertain' if phase == 'transmitting' else 'temporary_failure', retryable)


def run_once(limit=50):
    if not cfg.AUTH_ENABLED:
        raise RuntimeError('The report email worker requires AUTH_ENABLED=true')
    client = repository.db()
    # Two bounded indexed queues: due jobs and crashed-worker lease recovery.
    jobs = client.collection(repository.JOBS)
    due = jobs.where(filter=FieldFilter('status', '==', 'pending')).where(
        filter=FieldFilter('next_attempt_at', '<=', now())).order_by('next_attempt_at').limit(limit)
    expired = jobs.where(filter=FieldFilter('status', '==', 'sending')).where(
        filter=FieldFilter('lease_until', '<=', now())).order_by('lease_until').limit(limit)
    handled = 0
    for snapshot in [*due.stream(), *expired.stream()]:
        job = claim(snapshot.id)
        if job:
            deliver(job)
            handled += 1
    return handled


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once', action='store_true')
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    while True:
        try:
            run_once()
        except Exception as exc:
            logger.error('Report outbox polling failed error_type=%s', type(exc).__name__)
            if args.once:
                raise SystemExit(1)
        if args.once:
            break
        time.sleep(20)
