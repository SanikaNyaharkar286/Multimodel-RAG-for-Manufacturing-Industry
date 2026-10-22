import asyncio
from datetime import datetime, timezone
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import HTTPException

from app import config as cfg
from app.api.auth_routes import me
from app.observability.timing import Trace, current_trace
from auth_system import security


class CurrentProfileTests(unittest.TestCase):
    def test_me_returns_only_safe_fields_for_verified_identity(self):
        identity = SimpleNamespace(uid='current-user', email='tech@example.com')
        profile = {
            'uid': 'current-user', 'fullName': 'Current Technician',
            'email': 'tech@example.com', 'phone': '1234567890',
            'employeeId': 'EMP001', 'companyId': 'company',
            'role': 'technician', 'status': 'approved',
            'createdAt': datetime(2026, 10, 21, tzinfo=timezone.utc),
            'requestReason': 'Private reason', 'reviewedBy': 'admin-user',
            'decisionReason': 'Private decision', 'bootstrappedBy': 'internal',
        }
        with patch('app.api.auth_routes.user_profile', return_value=profile), \
             patch.object(cfg, 'COMPANY_NAME', 'Example Manufacturing'):
            result = asyncio.run(me(identity=identity))
        self.assertEqual(result['uid'], 'current-user')
        self.assertEqual(result['name'], 'Current Technician')
        self.assertEqual(result['company_name'], 'Example Manufacturing')
        self.assertEqual(result['status'], 'approved')
        for field in ('phone', 'requestReason', 'reviewedBy', 'decisionReason', 'bootstrappedBy'):
            self.assertNotIn(field, result)

    def test_me_uses_token_uid_and_omits_unavailable_optional_fields(self):
        identity = SimpleNamespace(uid='token-user', email='fallback@example.com')
        profile = {'uid': 'different-value', 'companyId': 'company',
                   'role': 'admin', 'status': 'approved'}
        with patch('app.api.auth_routes.user_profile', return_value=profile), \
             patch.object(cfg, 'COMPANY_NAME', 'COMPANY_NAME'):
            result = asyncio.run(me(identity=identity))
        self.assertEqual(result['uid'], 'token-user')
        self.assertEqual(result['email'], 'fallback@example.com')
        self.assertEqual(result['company_id'], 'company')
        self.assertNotIn('employee_id', result)

    def test_me_rejects_missing_profile(self):
        identity = SimpleNamespace(uid='missing', email='missing@example.com')
        with patch('app.api.auth_routes.user_profile', return_value=None):
            with self.assertRaises(HTTPException) as error:
                asyncio.run(me(identity=identity))
        self.assertEqual(error.exception.status_code, 404)


class SecurityTimingTests(unittest.TestCase):
    def test_token_timing_preserves_revocation_verification_and_hides_identity(self):
        trace = Trace('GET')
        token = current_trace.set(trace)
        firebase = object()
        try:
            with patch.object(cfg, 'AUTH_ENABLED', True), \
                 patch.object(security, 'firebase_app', return_value=firebase), \
                 patch.object(security, 'is_allowed_identity', return_value=True), \
                 patch('firebase_admin.auth.verify_id_token', return_value={
                     'uid': 'SECRET-user', 'email': 'SECRET@example.com', 'email_verified': True,
                 }) as verify:
                identity = security.current_identity('Bearer SECRET-token')
            row = trace.snapshot('/auth/me', 200, close=True)
        finally:
            current_trace.reset(token)
        self.assertEqual(identity.uid, 'SECRET-user')
        verify.assert_called_once_with('SECRET-token', check_revoked=True, app=firebase)
        self.assertIn('auth_token_verify_ms', row['timings_json']['stages_ms'])
        self.assertNotIn('SECRET', str(row))

    def test_profile_read_timing_preserves_firestore_lookup_and_pending_restriction(self):
        profile = {'uid': 'SECRET-user', 'status': 'pending', 'role': 'technician'}
        snapshot = Mock(exists=True)
        snapshot.to_dict.return_value = profile
        client = Mock()
        client.collection.return_value.document.return_value.get.return_value = snapshot
        trace = Trace('POST')
        token = current_trace.set(trace)
        try:
            with patch.object(cfg, 'AUTH_ENABLED', True), \
                 patch.object(security, 'firebase_app', return_value=object()), \
                 patch('firebase_admin.firestore.client', return_value=client):
                with self.assertRaises(HTTPException) as error:
                    security.require_approved(security.Identity('SECRET-user', 'SECRET@example.com', True))
            row = trace.snapshot('/chat', 403, close=True)
        finally:
            current_trace.reset(token)
        self.assertEqual(error.exception.status_code, 403)
        client.collection.assert_called_once_with('users')
        client.collection.return_value.document.assert_called_once_with('SECRET-user')
        self.assertIn('auth_profile_read_ms', row['timings_json']['stages_ms'])
        self.assertNotIn('SECRET', str(row))


if __name__ == '__main__':
    unittest.main()
