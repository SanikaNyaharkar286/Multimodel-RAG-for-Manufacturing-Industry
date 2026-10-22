import asyncio
from copy import deepcopy
import os
import smtplib
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from google.api_core.exceptions import AlreadyExists
from app import config as cfg
from app.api.auth_routes import Registration, registration
from app.services.email_service import send_admin_approval_notification
from auth_system.firestore import complete_registration, decide, retry_pending_approval_notifications

FIELDS = dict(companyId='company', fullName='New Technician', employeeId='E123',
              department='Maintenance', site='Plant A', designation='Technician',
              phone='1234567890', requestReason='Maintain equipment')
EMAIL_ENV = dict(ADMIN_NOTIFICATION_EMAIL='admin@example.com', SMTP_HOST='smtp.example.com',
                 SMTP_PORT='587', SMTP_USERNAME='sender@example.com', SMTP_PASSWORD='test-secret',
                 SMTP_FROM_EMAIL='sender@example.com', SMTP_FROM_NAME='Manufacturing Equipment Copilot',
                 SMTP_USE_TLS='true', APPROVAL_PORTAL_URL='https://copilot.example.com')


class ApprovalEmailTests(unittest.TestCase):
    def setUp(self):
        self.profile = None
        self.client = Mock()
        self.ref = self.client.collection.return_value.document.return_value
        self.ref.get.side_effect = lambda: SimpleNamespace(
            exists=self.profile is not None, to_dict=lambda: deepcopy(self.profile))
        self.ref.update.side_effect = lambda values: self.profile.update(values)
        self.client.batch.return_value.commit.side_effect = self.commit
        patches = [patch.dict(os.environ, EMAIL_ENV),
                   patch('auth_system.firestore.db', return_value=self.client),
                   patch.object(cfg, 'COMPANY_NAME', 'Example Manufacturing'),
                   patch('app.services.email_service.smtplib.SMTP')]
        for item in patches:
            value = item.start()
            self.addCleanup(item.stop)
        self.transport = value
        self.smtp = self.transport.return_value.__enter__.return_value
        self.smtp.send_message.return_value = {}

    def commit(self):
        self.profile = deepcopy(self.client.batch.return_value.create.call_args_list[0].args[1])

    def register(self):
        return complete_registration('new-user', 'technician@example.com', FIELDS)

    def test_new_profile_saved_before_email_and_required_events_are_logged(self):
        def accepted(*args, **kwargs):
            self.assertEqual(self.profile['status'], 'pending')
            return {}
        self.smtp.send_message.side_effect = accepted
        with self.assertLogs('uvicorn.error.approval_email', level='INFO') as logs:
            profile = self.register()
        self.assertEqual(profile['status'], 'pending')
        self.assertIn('Pending approval request created', '\n'.join(logs.output))
        self.assertIn('Admin email notification sent successfully', '\n'.join(logs.output))
        self.smtp.starttls.assert_called_once()

    def test_subject_body_and_sender_include_submitted_details(self):
        self.register()
        message = self.smtp.send_message.call_args.args[0]
        self.assertEqual(message['Subject'], 'New Technician Approval Request')
        for text in ['New Technician', 'technician@example.com', 'Example Manufacturing',
                     'E123', '1234567890', 'Requested at:', 'UTC', 'Status: Pending approval',
                     'admin=approvals', 'request=new-user']:
            self.assertIn(text, message.get_content())
        self.assertIn('Manufacturing Equipment Copilot', message['From'])

    def test_same_pending_registration_retry_does_not_send_again(self):
        self.register()
        self.register()
        self.smtp.send_message.assert_called_once()
        self.client.batch.return_value.commit.assert_called_once()

    def test_failed_notification_is_recorded_for_background_retry(self):
        self.smtp.login.side_effect = OSError('temporary failure')
        self.register()
        updates = [call.args[0] for call in self.ref.update.call_args_list]
        self.assertTrue(any(update.get('emailStatus') == 'failed' for update in updates))

    def test_background_retry_sends_failed_pending_notification_once(self):
        notification_ref = Mock()
        notification = SimpleNamespace(
            id='new-user', reference=notification_ref,
            to_dict=lambda: {'type': 'access_request', 'uid': 'new-user',
                             'emailStatus': 'failed', 'emailAttempts': 1})
        notifications = Mock()
        notifications.stream.return_value = [notification]
        user_ref = Mock()
        user_ref.get.return_value = SimpleNamespace(
            exists=True, to_dict=lambda: {**FIELDS, 'uid': 'new-user',
                                           'email': 'technician@example.com',
                                           'status': 'pending'})
        users = Mock()
        users.document.return_value = user_ref
        client = Mock()
        client.collection.side_effect = lambda name: (
            notifications if name == 'adminNotifications' else users)
        with patch('auth_system.firestore.db', return_value=client), \
             patch('auth_system.firestore.send_admin_approval_notification', return_value=True) as send:
            self.assertEqual(retry_pending_approval_notifications(), 1)
        send.assert_called_once()
        self.assertEqual(notification_ref.update.call_args.args[0]['emailStatus'], 'sent')

    def test_background_retry_skips_request_that_is_no_longer_pending(self):
        notification = SimpleNamespace(
            id='new-user', reference=Mock(),
            to_dict=lambda: {'type': 'access_request', 'uid': 'new-user',
                             'emailStatus': 'failed', 'emailAttempts': 1})
        notifications = Mock()
        notifications.stream.return_value = [notification]
        users = Mock()
        users.document.return_value.get.return_value = SimpleNamespace(
            exists=True, to_dict=lambda: {'uid': 'new-user', 'status': 'approved'})
        client = Mock()
        client.collection.side_effect = lambda name: (
            notifications if name == 'adminNotifications' else users)
        with patch('auth_system.firestore.db', return_value=client), \
             patch('auth_system.firestore.send_admin_approval_notification') as send:
            self.assertEqual(retry_pending_approval_notifications(), 0)
        send.assert_not_called()

    def test_failed_firestore_commit_never_sends_email(self):
        self.client.batch.return_value.commit.side_effect = RuntimeError('database unavailable')
        with self.assertRaises(RuntimeError):
            self.register()
        self.transport.assert_not_called()

    def test_concurrent_create_loser_does_not_send_duplicate(self):
        def concurrent_create():
            self.commit()
            raise AlreadyExists('already created')
        self.client.batch.return_value.commit.side_effect = concurrent_create
        self.assertEqual(self.register()['status'], 'pending')
        self.transport.assert_not_called()

    def test_smtp_failure_keeps_pending_and_does_not_log_provider_secrets(self):
        self.smtp.login.side_effect = smtplib.SMTPAuthenticationError(535, b'test-secret private reply')
        with self.assertLogs('uvicorn.error.approval_email', level='INFO') as logs:
            self.assertEqual(self.register()['status'], 'pending')
        output = '\n'.join(logs.output)
        self.assertIn('Admin email notification failed', output)
        self.assertIn('535', output)
        self.assertNotIn('test-secret', output)
        self.assertNotIn('private reply', output)

    def test_missing_recipient_or_smtp_settings_skip_without_breaking_registration(self):
        for key in ['ADMIN_NOTIFICATION_EMAIL', 'SMTP_HOST', 'SMTP_USERNAME', 'SMTP_PASSWORD', 'SMTP_FROM_EMAIL']:
            with self.subTest(setting=key), patch.dict(os.environ, {key: ''}):
                self.profile = None
                with self.assertLogs('uvicorn.error.approval_email', level='WARNING') as logs:
                    self.assertEqual(self.register()['status'], 'pending')
                self.assertIn(key, '\n'.join(logs.output))
        self.transport.assert_not_called()

    def test_invalid_port_address_or_tls_skip_safely(self):
        for values in [{'SMTP_PORT': ''}, {'SMTP_PORT': 'invalid'}, {'SMTP_USE_TLS': 'invalid'},
                       {'ADMIN_NOTIFICATION_EMAIL': 'invalid'}, {'SMTP_FROM_NAME': 'bad\nheader'}]:
            with self.subTest(values=values), patch.dict(os.environ, values):
                self.assertFalse(send_admin_approval_notification({'uid': 'test'}))
        self.transport.assert_not_called()

    def test_admin_decisions_do_not_send_email(self):
        self.register()
        self.smtp.send_message.reset_mock()
        for status in ['approved', 'rejected', 'suspended']:
            decide('new-user', status, 'admin-user')
        self.smtp.send_message.assert_not_called()

    def test_existing_profile_update_does_not_send_email(self):
        self.register()
        self.profile['status'] = 'approved'
        self.smtp.send_message.reset_mock()
        result = complete_registration('new-user', 'technician@example.com', {**FIELDS, 'fullName': 'Edited'})
        self.assertEqual(result['status'], 'approved')
        self.smtp.send_message.assert_not_called()

    def test_notification_bug_still_does_not_undo_profile(self):
        with patch('auth_system.firestore.send_admin_approval_notification', side_effect=RuntimeError('private error')):
            with self.assertLogs('uvicorn.error.approval_email', level='ERROR'):
                self.assertEqual(self.register()['status'], 'pending')

    def test_registration_endpoint_offloads_blocking_work_and_returns_safe_success(self):
        main_thread = threading.get_ident()
        worker_threads = []
        original = complete_registration
        def worker(*args):
            worker_threads.append(threading.get_ident())
            return original(*args)
        with patch.object(cfg, 'AUTH_ENABLED', True), patch.object(cfg, 'COMPANY_ID', 'company'), \
             patch('app.api.auth_routes.is_allowed_domain', return_value=True), \
             patch('app.api.auth_routes.complete_registration', side_effect=worker):
            self.smtp.login.side_effect = OSError('private error')
            result = asyncio.run(registration(Registration(**FIELDS),
                identity=SimpleNamespace(uid='new-user', email='technician@example.com')))
        self.assertEqual(result['message'], 'Approval request submitted successfully.')
        self.assertEqual(result['status'], 'pending')
        self.assertNotEqual(worker_threads[0], main_thread)
        self.assertNotIn('private error', str(result))
