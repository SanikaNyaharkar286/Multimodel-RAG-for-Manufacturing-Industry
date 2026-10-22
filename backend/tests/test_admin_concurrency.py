import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from app.api.auth_routes import Decision, decision, requests


class AdminRouteConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_admin_listing_does_not_run_firestore_on_event_loop(self):
        loop_thread = threading.get_ident()

        def read_requests():
            self.assertNotEqual(threading.get_ident(), loop_thread)
            return {'requests': [{'status': 'pending'}]}

        with patch('app.api.auth_routes._list_technician_requests', side_effect=read_requests):
            result = await requests(identity=SimpleNamespace(uid='admin'))
        self.assertEqual(result['requests'][0]['status'], 'pending')

    async def test_admin_decision_runs_off_loop_and_keeps_validation_errors(self):
        loop_thread = threading.get_ident()

        def invalid_decision(*args):
            self.assertNotEqual(threading.get_ident(), loop_thread)
            raise ValueError('Invalid decision')

        with patch('app.api.auth_routes.decide', side_effect=invalid_decision):
            with self.assertRaises(HTTPException) as error:
                await decision('technician', Decision(status='unknown'),
                               identity=SimpleNamespace(uid='admin'))
        self.assertEqual(error.exception.status_code, 400)
