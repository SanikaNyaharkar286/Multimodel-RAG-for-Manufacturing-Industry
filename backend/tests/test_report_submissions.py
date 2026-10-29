"""Exercise real submission/storage/transaction code with external services fake.

The fake implements atomic create preconditions and transaction read-before-write
checks, so concurrency/ownership tests do not rely on canned API responses.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
import os
import smtplib
from threading import RLock
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from uuid import uuid4
from google.api_core.exceptions import AlreadyExists, PreconditionFailed, FailedPrecondition
from google.cloud import firestore
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from app import config as cfg
from app.api import report_routes, report_submission_routes
from app.reports import repository, service, storage, email_worker
from app.reports.schemas import ReviewReport
from auth_system.security import Identity, current_identity


class Snapshot:
    def __init__(self, ref, value):
        self.reference, self.id, self.value = ref, ref.id, deepcopy(value)
        self.exists = value is not None
    def to_dict(self):
        return deepcopy(self.value)


class Reference:
    def __init__(self, client, path):
        self.client, self.path, self.id = client, path, path.split('/')[-1]
    def get(self, transaction=None):
        if transaction and transaction.writes:
            raise AssertionError('Firestore requires all transaction reads before writes')
        with self.client.lock:
            return Snapshot(self, self.client.data.get(self.path))
    def create(self, value):
        batch = self.client.batch(); batch.create(self, value); batch.commit()
    def update(self, value):
        batch = self.client.batch(); batch.update(self, value); batch.commit()
    def set(self, value, merge=False):
        self.client.data[self.path] = deepcopy(value)
    def collection(self, name):
        return Collection(self.client, f'{self.path}/{name}')


class Collection:
    def __init__(self, client, path, filters=(), ordering=(), offset=0, limit=None, after=None):
        self.client, self.path, self.filters = client, path, filters
        self.ordering, self.skip, self.take = ordering, offset, limit
        self.after = after
    def clone(self, **changes):
        return Collection(self.client, self.path, **{
            'filters': self.filters, 'ordering': self.ordering, 'offset': self.skip, 'limit': self.take, 'after': self.after, **changes})
    def document(self, key=None):
        return Reference(self.client, f'{self.path}/{key or uuid4().hex}')
    def where(self, filter):
        return self.clone(filters=(*self.filters, filter))
    def order_by(self, key, direction='ASCENDING'):
        return self.clone(ordering=(*self.ordering, (key, direction)))
    def offset(self, amount):
        return self.clone(offset=amount)
    def limit(self, amount):
        return self.clone(limit=amount)
    def start_after(self, values):
        return self.clone(after=values)
    def stream(self, transaction=None):
        if transaction and transaction.writes:
            raise AssertionError('Read after write')
        with self.client.lock:
            rows = [Snapshot(Reference(self.client, key), value) for key, value in self.client.data.items()
                    if key.rsplit('/', 1)[0] == self.path]
        for condition in self.filters:
            def match(snapshot):
                value = snapshot.value.get(condition.field_path)
                expected = condition.value
                if condition.op_string == '==': return value == expected
                if condition.op_string == '<=': return value is not None and value <= expected
                if condition.op_string == 'array_contains': return expected in (value or [])
                raise AssertionError('Unhandled query operator')
            rows = [row for row in rows if match(row)]
        for key, direction in reversed(self.ordering):
            rows.sort(key=lambda row: row.id if key == '__name__' else row.value[key], reverse=direction == 'DESCENDING')
        if self.after:
            rows = [row for row in rows if (row.value['createdAt'], row.id) > (self.after['createdAt'], self.after['__name__'].id)]
        return iter(rows[self.skip:None if self.take is None else self.skip+self.take])
    def count(self):
        return SimpleNamespace(get=lambda: [[SimpleNamespace(value=len(list(self.stream())))]])


class Batch:
    def __init__(self, client): self.client, self.writes = client, []
    def create(self, ref, data): self.writes.append(('create', ref.path, data))
    def update(self, ref, data): self.writes.append(('update', ref.path, data))
    def commit(self):
        with self.client.lock:
            if self.client.fail:
                raise OSError('database unavailable')
            for kind, path, data in self.writes:
                if kind == 'create' and path in self.client.data: raise AlreadyExists('duplicate')
                if kind == 'update' and path not in self.client.data: raise AssertionError('update missing')
            for kind, path, data in self.writes:
                data = {key: datetime.now(timezone.utc) if value is firestore.SERVER_TIMESTAMP else deepcopy(value) for key, value in data.items()}
                self.client.data[path] = {**(self.client.data.get(path, {}) if kind == 'update' else {}), **data}


class Database:
    def __init__(self): self.data, self.lock, self.fail = {}, RLock(), False
    def collection(self, path): return Collection(self, path)
    def batch(self): return Batch(self)
    def transaction(self): return Batch(self)


def transactional(function):
    def run(transaction):
        with transaction.client.lock:
            result = function(transaction)
            transaction.commit()
            return result
    return run


class Blob:
    def __init__(self, client, name, generation=None):
        self.client, self.name, self.requested_generation = client, name, generation
        self.metadata, self.generation, self.size = {}, None, None
    def upload_from_string(self, data, **options):
        if self.client.fail: raise OSError('upload failed')
        with self.client.lock:
            assert options['if_generation_match'] == 0
            if self.name in self.client.objects: raise PreconditionFailed('exists')
            self.client.objects[self.name] = (data, deepcopy(self.metadata), 1)
            self.client.uploads += 1
    def reload(self, **options):
        data, metadata, generation = self.client.objects[self.name]
        self.metadata, self.generation, self.size = deepcopy(metadata), generation, len(data)
    def download_as_bytes(self, **options):
        data, metadata, generation = self.client.objects[self.name]
        assert options['if_generation_match'] == self.requested_generation == generation
        self.client.downloads += 1
        return data


class Storage:
    def __init__(self): self.objects, self.lock, self.fail, self.uploads, self.downloads = {}, RLock(), False, 0, 0
    def bucket(self, bucket): return self
    def blob(self, name, generation=None): return Blob(self, name, generation)


class SubmissionTests(unittest.TestCase):
    def setUp(self):
        self.database, self.gcs = Database(), Storage()
        self.identity = Identity('tech-a', 'tech@example.com', True)
        self.report = {'report_id': 'report-a', 'report_number': 'TMR-20261029-ABC',
            'revision': 2, 'machine': 'HAB-3232', 'model': '', 'issue': 'Hydraulic pump not starting',
            'case_id': 'case-a', 'session_id': 'session-a', 'cause': 'Not confirmed', 'images': {},
            'steps': [{'step_id': 'step-a', 'status': 'Pending', 'instruction': 'Inspect indicator.'}]}
        self.database.data['users/admin-a'] = {'role': 'admin', 'status': 'approved'}
        self.database.data['users/admin-b'] = {'role': 'admin', 'status': 'approved'}
        patches = [patch.object(cfg, 'AUTH_ENABLED', True),
            patch('app.reports.repository.db', return_value=self.database),
            patch('google.cloud.firestore.transactional', transactional),
            patch('app.reports.storage.storage_client', return_value=self.gcs),
            patch('app.reports.storage.render_pdf', side_effect=lambda report: b'%PDF-original findings'),
            patch('app.reports.service.upgrade_report', side_effect=lambda report: report),
            patch('app.reports.service.persistence.load_report', side_effect=lambda rid, uid: deepcopy(self.report) if rid == 'report-a' and uid == 'tech-a' else None),
            patch('app.reports.service.user_profile', return_value={'fullName': 'Technician A'}),
            patch.dict(os.environ, {'ADMIN_NOTIFICATION_EMAILS': 'admin1@example.com,admin2@example.com',
                'REPORT_EMAIL_MAX_MESSAGE_BYTES': '20971520'})]
        for item in patches:
            item.start(); self.addCleanup(item.stop)
    def submit(self): return service.submit('report-a', self.identity, 2)
    def records(self, collection): return list(self.database.collection(collection).stream())

    def test_submission_is_durable_private_and_atomic_with_per_admin_inboxes_and_jobs(self):
        value = self.submit()
        self.assertEqual(value['email_status'], 'pending')
        self.assertEqual(value['review_status'], 'new')
        self.assertNotIn('owner_uid', value)
        self.assertNotIn('pdf_gcs_path', value)
        record = repository.get_submission('report-a')
        self.assertIsInstance(record['submitted_at'], datetime)
        self.assertEqual(len(self.records(repository.REPORTS)), 1)
        self.assertEqual(len(self.records(repository.NOTIFICATIONS)), 2)
        self.assertEqual(len(self.records(repository.JOBS)), 2)
        self.assertEqual(storage.read_snapshot(record), self.report)
        self.assertEqual(storage.read_object(record), b'%PDF-original findings')
        self.assertEqual(record['machine_model'], '', 'Unknown models must not be invented')
        self.assertEqual(len(self.gcs.objects), 2)

    def test_new_submissions_and_email_attachments_use_the_compact_three_page_pdf(self):
        import pymupdf
        from app.responses.documentation import render_pdf
        self.report['status'] = 'Unresolved'
        self.report['history'] = [{'role':'assistant','text':'PRIVATE RAW CHAT NOT FOR PDF'}]
        with patch('app.reports.storage.render_pdf', side_effect=render_pdf):
            self.submit()
        record = repository.get_submission('report-a')
        data = storage.read_object(record)
        with pymupdf.open(stream=data, filetype='pdf') as pdf:
            self.assertEqual(len(pdf), 3)
            self.assertIn('8. Approval & Signatures', pdf[2].get_text())
            self.assertNotIn('PRIVATE RAW CHAT NOT FOR PDF', ''.join(page.get_text() for page in pdf))
        job = self.records(repository.JOBS)[0].to_dict()
        settings = SimpleNamespace(portal_url='https://copilot.example.com', sender_name='Copilot', sender='sender@example.com')
        message = email_worker.build_message(record, job, settings, data)
        self.assertEqual(list(message.iter_attachments())[0].get_payload(decode=True), data)
        self.assertEqual(storage.read_snapshot(record), self.report)

    def test_duplicate_and_concurrent_retries_create_one_report_and_no_new_jobs(self):
        with ThreadPoolExecutor(4) as pool:
            results = list(pool.map(lambda _: self.submit(), range(4)))
        self.assertTrue(all(item['report_id'] == 'report-a' for item in results))
        self.assertEqual(len(self.records(repository.REPORTS)), 1)
        self.assertEqual(len(self.records(repository.NOTIFICATIONS)), 2)
        self.assertEqual(len(self.records(repository.JOBS)), 2)
        self.assertEqual(self.gcs.uploads, 2)

    def test_storage_failure_never_creates_a_submission(self):
        self.gcs.fail = True
        with self.assertRaises(OSError): self.submit()
        for name in (repository.REPORTS, repository.NOTIFICATIONS, repository.JOBS): self.assertFalse(self.records(name))

    def test_firestore_failure_can_retry_using_the_same_gcs_objects(self):
        self.database.fail = True
        with self.assertRaises(OSError): self.submit()
        self.assertEqual(self.gcs.uploads, 2)
        self.assertFalse(self.records(repository.REPORTS))
        self.database.fail = False
        self.submit()
        self.assertEqual(self.gcs.uploads, 2)

    def test_wrong_owner_and_stale_revision_are_rejected_before_upload(self):
        with self.assertRaises(HTTPException) as exc: service.submit('report-a', Identity('other', 'other@example.com', True), 2)
        self.assertEqual(exc.exception.status_code, 404)
        with self.assertRaises(HTTPException) as exc: service.submit('report-a', self.identity, 1)
        self.assertEqual(exc.exception.status_code, 409)
        self.assertEqual(self.gcs.uploads, 0)
        self.submit()
        with self.assertRaises(HTTPException): repository.get_submission('report-a', 'other')

    def test_admin_review_visible_to_technician_without_modifying_pdf(self):
        self.submit(); original = deepcopy(self.gcs.objects)
        value = repository.review('report-a', 'admin-a', ReviewReport(review_status='under_review', admin_comment='Check the gauge.', version=1))
        self.assertEqual(value['version'], 2)
        value = repository.review('report-a', 'admin-b', ReviewReport(review_status='reviewed', admin_comment='Recorded.', version=2))
        self.assertIsInstance(value['reviewed_at'], datetime)
        self.assertEqual(value['assigned_admin_uid'], 'admin-b')
        listed = repository.list_reports(owner_uid='tech-a')
        self.assertEqual(listed['reports'][0]['review_status'], 'reviewed')
        self.assertEqual(listed['reports'][0]['admin_comment'], 'Recorded.')
        self.assertEqual(self.gcs.objects, original)
        with self.assertRaises(HTTPException) as exc: repository.review('report-a', 'admin-a', ReviewReport(review_status='new', version=1))
        self.assertEqual(exc.exception.status_code, 409)
        again = repository.review('report-a', 'admin-b', ReviewReport(review_status='reviewed', admin_comment='Recorded.', version=3))
        self.assertEqual(again['version'], 3)

    def test_admin_listing_search_filter_counts_and_ownership(self):
        self.submit()
        self.assertEqual(repository.list_reports(search='hydraulic')['total'], 1)
        self.assertEqual(repository.list_reports(search='3232')['total'], 1)
        self.assertEqual(repository.list_reports(search='unknown')['total'], 0)
        self.assertEqual(repository.list_reports(status='reviewed')['total'], 0)
        self.assertEqual(repository.list_reports(owner_uid='other')['total'], 0)

    def test_notifications_are_independent_durable_and_legacy_registration_reused(self):
        self.submit()
        self.database.data['adminNotifications/technician-b'] = {'type': 'access_request', 'uid': 'technician-b', 'createdAt': datetime.now(timezone.utc)}
        inbox = repository.notifications('admin-a')
        self.assertEqual(inbox['unread_count'], 2)
        target = inbox['notifications'][0]['notification_id']
        repository.mark_read('admin-a', target)
        self.assertEqual(repository.notifications('admin-a')['unread_count'], 1)
        self.assertEqual(repository.notifications('admin-b')['unread_count'], 2)
        with self.assertRaises(HTTPException): repository.mark_read('admin-b', target)
        self.assertEqual(repository.mark_all_read('admin-a'), 1)
        self.assertEqual(repository.notifications('admin-a')['unread_count'], 0)
        self.assertEqual(len(repository.notifications('admin-a')['notifications']), 2)

    def test_pdf_integrity_and_path_are_checked(self):
        self.submit(); record = repository.get_submission('report-a')
        record['pdf_storage']['path'] = 'other-users/report.pdf'
        with self.assertRaises(HTTPException): storage.read_object(record)
        record = repository.get_submission('report-a')
        path = record['pdf_storage']['path']
        data, meta, generation = self.gcs.objects[path]
        self.gcs.objects[path] = (b'%PDF-tampered', meta, generation)
        with self.assertRaises(HTTPException): storage.read_object(record)

    def test_missing_email_configuration_does_not_lose_submission(self):
        with patch.dict(os.environ, {'ADMIN_NOTIFICATION_EMAILS': '', 'ADMIN_NOTIFICATION_EMAIL': ''}):
            value = self.submit()
        self.assertEqual(value['email_status'], 'failed')
        self.assertEqual(len(self.records(repository.REPORTS)), 1)
        self.assertEqual(len(self.records(repository.NOTIFICATIONS)), 2)
        self.assertEqual(len(self.records(repository.JOBS)), 0)
        self.assertEqual(storage.read_object(repository.get_submission('report-a')), b'%PDF-original findings')

    def test_email_claim_retry_and_recovery_are_durable_and_idempotent(self):
        self.submit(); job_id = self.records(repository.JOBS)[0].id
        job = email_worker.claim(job_id)
        self.assertEqual(job['attempts'], 1)
        self.assertIsNone(email_worker.claim(job_id))
        email_worker.finish(job, 'failed', 'temporary_failure', retryable=True)
        record = self.database.data[f'{repository.JOBS}/{job_id}']
        self.assertEqual(record['status'], 'pending')
        self.assertGreater(record['next_attempt_at'], datetime.now(timezone.utc))
        self.assertIsNone(email_worker.claim(job_id))
        record['next_attempt_at'] = datetime.now(timezone.utc) - timedelta(seconds=1)
        job = email_worker.claim(job_id); email_worker.transmitting(job)
        self.database.data[f'{repository.JOBS}/{job_id}']['lease_until'] = datetime.now(timezone.utc) - timedelta(seconds=1)
        self.assertIsNone(email_worker.claim(job_id))
        self.assertEqual(self.database.data[f'{repository.JOBS}/{job_id}']['error_code'], 'delivery_uncertain')
        self.assertIsNone(email_worker.claim(job_id))

    def test_email_accepted_is_not_claimed_delivered_and_worker_does_not_resend(self):
        self.submit(); job_id = self.records(repository.JOBS)[0].id
        settings = SimpleNamespace(portal_url='https://copilot.example.com', sender_name='Copilot', sender='sender@example.com', host='smtp.example.com', port=587, use_tls=True, username='sender', password='test-secret')
        with patch('app.reports.email_worker.load_settings', return_value=settings), patch('app.reports.email_worker.smtplib.SMTP') as smtp:
            smtp.return_value.__enter__.return_value.send_message.return_value = {}
            for row in self.records(repository.JOBS): email_worker.deliver(email_worker.claim(row.id))
            self.assertEqual(smtp.return_value.__enter__.return_value.send_message.call_count, 2)
            self.assertIsNone(email_worker.claim(job_id))
            message = smtp.return_value.__enter__.return_value.send_message.call_args.args[0]
            attachment = list(message.iter_attachments())[0]
            self.assertEqual(attachment.get_content_type(), 'application/pdf')
            self.assertEqual(attachment.get_payload(decode=True), b'%PDF-original findings')
        record = repository.get_submission('report-a')
        self.assertEqual(record['email_status'], 'sent')
        self.assertEqual(record['email_delivery'], 'accepted_by_smtp')

    def test_large_attachment_uses_authenticated_admin_page_not_public_storage(self):
        self.submit(); record = repository.get_submission('report-a'); job = self.records(repository.JOBS)[0].to_dict()
        settings = SimpleNamespace(portal_url='https://copilot.example.com', sender_name='Copilot', sender='sender@example.com')
        with patch.dict(os.environ, {'REPORT_EMAIL_MAX_MESSAGE_BYTES': '10240'}):
            message = email_worker.build_message(record, job, settings, b'x' * 12000)
        self.assertFalse(list(message.iter_attachments()))
        self.assertIn('admin=reports&report=report-a', message.get_content())
        self.assertNotIn('gs://', message.get_content())

    def test_email_transient_failure_retries_and_ambiguous_send_does_not(self):
        self.submit(); row = self.records(repository.JOBS)[0]
        settings = SimpleNamespace(portal_url='https://copilot.example.com', sender_name='Copilot', sender='sender@example.com', host='smtp.example.com', port=587, use_tls=True, username='sender', password='test-secret')
        with patch('app.reports.email_worker.load_settings', return_value=settings), patch('app.reports.email_worker.smtplib.SMTP') as smtp:
            client = smtp.return_value.__enter__.return_value
            client.send_message.side_effect = smtplib.SMTPDataError(451, b'temporary')
            email_worker.deliver(email_worker.claim(row.id))
            job = self.database.data[row.reference.path]
            self.assertEqual(job['status'], 'pending')
            job['next_attempt_at'] = datetime.now(timezone.utc) - timedelta(seconds=1)
            client.send_message.side_effect = smtplib.SMTPServerDisconnected('unknown acceptance')
            email_worker.deliver(email_worker.claim(row.id))
            self.assertEqual(self.database.data[row.reference.path]['status'], 'failed')
            self.assertEqual(self.database.data[row.reference.path]['error_code'], 'delivery_uncertain')
            self.assertIsNone(email_worker.claim(row.id))

    def test_deleted_report_is_not_retried_or_sent_and_admin_gets_missing_record_error(self):
        self.submit(); row = self.records(repository.JOBS)[0]
        del self.database.data[f'{repository.REPORTS}/report-a']
        self.assertIsNone(email_worker.claim(row.id))
        self.assertEqual(self.database.data[row.reference.path]['error_code'], 'report_missing')
        self.assertEqual(self.database.data[row.reference.path]['status'], 'failed')

    def test_last_crashed_preparation_exhausts_attempts_without_sticking_in_sending(self):
        self.submit(); row = self.records(repository.JOBS)[0]
        self.database.data[row.reference.path].update(status='sending', phase='preparing', attempts=email_worker.MAX_ATTEMPTS,
            lease_until=datetime.now(timezone.utc) - timedelta(seconds=1))
        self.assertIsNone(email_worker.claim(row.id))
        self.assertEqual(self.database.data[row.reference.path]['status'], 'failed')

    def test_routes_enforce_auth_approval_admin_ownership_and_pagination(self):
        self.submit()
        app = FastAPI()
        app.include_router(report_submission_routes.router); app.include_router(report_routes.router)
        app.dependency_overrides[current_identity] = lambda: self.identity
        profiles = {'tech-a': {'role': 'technician', 'status': 'approved'}, 'admin-a': {'role': 'admin', 'status': 'approved'}, 'other': {'role': 'technician', 'status': 'approved'}}
        with patch('auth_system.security.user_profile', side_effect=lambda uid: profiles.get(uid)), TestClient(app) as client:
            self.assertEqual(client.get('/reports/my').json()['total'], 1)
            self.assertEqual(client.get('/reports/my?page_size=1000').status_code, 422)
            self.assertEqual(client.post('/reports/report-a/submit', json={'revision': 2, 'owner_uid': 'other'}).status_code, 422)
            self.assertEqual(client.get('/admin/reports').status_code, 403)
            self.assertEqual(client.get('/admin/notifications').status_code, 403)
            self.assertEqual(client.get('/reports/report-a').json()['submission']['email_status'], 'pending')
            self.assertEqual(client.get('/reports/report-a/pdf').content, b'%PDF-original findings')
            self.assertEqual(client.post('/reports/report-a', json={'revision': 2, 'fields': {'notes': 'changed'}}).status_code, 409)
            app.dependency_overrides[current_identity] = lambda: Identity('other', 'other@example.com', True)
            before = self.gcs.downloads
            self.assertEqual(client.get('/reports/report-a/pdf').status_code, 404)
            self.assertEqual(self.gcs.downloads, before)
            self.assertEqual(client.post('/reports/report-a/submit', json={'revision': 2}).status_code, 404)
            app.dependency_overrides[current_identity] = lambda: Identity('admin-a', 'admin@example.com', True)
            self.assertEqual(client.get('/admin/reports').json()['total'], 1)
            self.assertEqual(client.get('/admin/reports/report-a').json()['report']['cause'], 'Not confirmed')
            self.assertEqual(client.get('/admin/reports/report-a/pdf').status_code, 200)
            self.assertEqual(client.patch('/admin/reports/report-a/review', json={'version': 1, 'review_status': 'reviewed', 'admin_comment': 'Recorded.'}).status_code, 200)
            self.assertEqual(client.get('/admin/notifications/unread-count').json()['unread_count'], 1)
            self.assertEqual(client.post('/admin/notifications/mark-all-read').json()['marked_read'], 1)
            app.dependency_overrides[current_identity] = lambda: Identity('pending', 'pending@example.com', True)
            self.assertEqual(client.post('/reports/report-a/submit', json={'revision': 2}).status_code, 403)
        app.dependency_overrides.clear()
        with TestClient(app) as client:
            self.assertEqual(client.get('/reports/my').status_code, 401)
            self.assertEqual(client.get('/admin/reports').status_code, 401)
            self.assertEqual(client.post('/reports/report-a/submit', json={'revision': 2}).status_code, 401)

    def test_submitted_pdf_survives_absence_of_draft_and_session_data(self):
        self.submit()
        app = FastAPI()
        app.include_router(report_routes.router)
        app.dependency_overrides[current_identity] = lambda: self.identity
        with patch('auth_system.security.user_profile', return_value={'status': 'approved', 'role': 'technician'}), \
             patch('app.sessions.persistence.load_report', side_effect=AssertionError('No draft/session read after submission')), TestClient(app) as client:
            self.assertEqual(client.get('/reports/report-a/pdf').content, b'%PDF-original findings')
            self.assertEqual(client.get('/reports/report-a').json()['machine'], 'HAB-3232')

    def test_missing_or_building_indexes_return_retryable_status_without_hiding_data(self):
        app = FastAPI()
        app.include_router(report_submission_routes.router)
        app.dependency_overrides[current_identity] = lambda: Identity('admin-a', 'admin@example.com', True)
        with patch('auth_system.security.user_profile', return_value={'status': 'approved', 'role': 'admin'}), TestClient(app) as client:
            for operation, path in [('notifications', '/admin/notifications'),
                                    ('notifications', '/admin/notifications/unread-count'),
                                    ('list_reports', '/admin/reports')]:
                with patch(f'app.reports.repository.{operation}', side_effect=FailedPrecondition('The query requires an index. That index is currently building.')):
                    response = client.get(path)
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.headers['Retry-After'], '30')
                self.assertIn('retry shortly', response.json()['detail'])
                self.assertNotIn('unread_count', response.json())


class ReportErrorMappingTests(unittest.IsolatedAsyncioTestCase):
    async def test_unrelated_preconditions_are_not_disguised_as_index_readiness(self):
        error = FailedPrecondition('Transaction requires a valid state')
        with self.assertRaises(FailedPrecondition) as raised:
            await report_submission_routes.report_call(Mock(side_effect=error))
        self.assertIs(raised.exception, error)


if __name__ == '__main__': unittest.main()
