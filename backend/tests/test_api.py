import asyncio
import io
import unittest
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from google.genai.errors import ClientError
from PIL import Image

from app import config
from app.main import app
from app.sessions.session_store import store


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.auth_enabled = patch.object(config, 'AUTH_ENABLED', False)
        self.auth_enabled.start()
        self.cloud_logging = patch.object(config, 'BQ_API_LOGGING_ENABLED', False)
        self.cloud_logging.start()
        self.log_directory = tempfile.TemporaryDirectory()
        self.log_directory_patch = patch.object(config, 'API_LOG_DIRECTORY', Path(self.log_directory.name))
        self.log_directory_patch.start()
        self.context = TestClient(app)
        self.client = self.context.__enter__()
        self.session_id = self.client.post('/sessions').json()['session_id']

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.cloud_logging.stop()
        self.auth_enabled.stop()
        self.log_directory_patch.stop()
        self.log_directory.cleanup()

    def test_health_cors_and_session_lifecycle(self):
        self.assertEqual(self.client.get('/health').json()['status'], 'running')
        self.assertEqual(self.client.get('/sessions/' + self.session_id).json()['messages'], [])
        self.assertEqual(self.client.get('/sessions/' + self.session_id).json()['request_timeout_seconds'], config.TIMEOUT)
        self.assertEqual(self.client.post('/sessions').json()['request_timeout_seconds'], config.TIMEOUT)
        response = self.client.options('/sessions', headers={
            'Origin': config.ORIGIN, 'Access-Control-Request-Method': 'POST',
        })
        self.assertEqual(response.headers['access-control-allow-origin'], config.ORIGIN)
        for origin in ('http://localhost:5173', 'http://127.0.0.1:5173'):
            response = self.client.options('/sessions', headers={
                'Origin': origin, 'Access-Control-Request-Method': 'POST',
                'Access-Control-Request-Headers': 'authorization,content-type,x-client-request-id',
            })
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.headers['access-control-allow-origin'], origin)
        self.assertEqual(self.client.delete('/sessions/' + self.session_id).status_code, 200)
        self.assertEqual(self.client.get('/sessions/' + self.session_id).status_code, 410)

    def test_invalid_chat_does_not_call_model(self):
        with patch('app.api.chat_routes.answer_question', new_callable=AsyncMock) as answer:
            self.assertEqual(self.client.post('/chat', data={'session_id': self.session_id}).status_code, 400)
            self.assertEqual(self.client.post('/chat', data={
                'session_id': self.session_id, 'message': 'x' * 6001,
            }).status_code, 400)
            answer.assert_not_called()

    def test_upload_reply_history_and_image_route(self):
        image = io.BytesIO()
        Image.new('RGB', (8, 8), 'blue').save(image, format='PNG')

        async def answer(entry, question, photo):
            self.assertEqual(question, 'Explain this image')
            self.assertTrue(photo.startswith(b'\x89PNG'))
            asset_id = store.put_image(entry, photo)
            return {'blocks': [{
                'type': 'image', 'src': '/images/' + asset_id,
                'caption': 'Diagram', 'description': 'Visible components.',
            }]}, False

        with patch('app.api.chat_routes.answer_question', side_effect=answer):
            response = self.client.post('/chat', data={
                'session_id': self.session_id, 'message': 'Explain this image',
            }, files={'image': ('photo.png', image.getvalue(), 'image/png')})
        self.assertEqual(response.status_code, 200, response.text)
        block = response.json()['blocks'][0]
        self.assertEqual(block['description'], 'Visible components.')
        self.assertEqual(self.client.get(block['src']).headers['content-type'], 'image/png')
        self.assertEqual(len(self.client.get('/sessions/' + self.session_id).json()['messages']), 2)
        self.client.delete('/sessions/' + self.session_id)
        self.assertEqual(self.client.get(block['src']).status_code, 404)

    def test_model_timeout_and_busy_errors(self):
        for error, expected in [
            (asyncio.TimeoutError(), 504),
            (ClientError(429, {'error': {'code': 429, 'message': 'Busy'}}), 503),
        ]:
            with self.subTest(error=type(error).__name__):
                with patch('app.api.chat_routes.answer_question', new_callable=AsyncMock, side_effect=error):
                    response = self.client.post('/chat', data={
                        'session_id': self.session_id, 'message': 'Explain the panel',
                    })
                self.assertEqual(response.status_code, expected)
                if expected == 503:
                    self.assertEqual(response.headers['retry-after'], '5')

    def test_failed_request_keeps_validated_upload_for_followup(self):
        image = io.BytesIO()
        Image.new('RGB', (8, 8), 'blue').save(image, format='PNG')
        with patch('app.api.chat_routes.answer_question', new_callable=AsyncMock,
                   side_effect=asyncio.TimeoutError()):
            response = self.client.post('/chat', data={
                'session_id': self.session_id, 'message': 'What is this?',
            }, files={'image': ('photo.png', image.getvalue(), 'image/png')})
        self.assertEqual(response.status_code, 504)
        self.assertTrue(store.entries[self.session_id].uploaded_photo.startswith(b'\x89PNG'))
        self.assertEqual(store.entries[self.session_id].history, [])
        self.client.delete('/sessions/' + self.session_id)
        self.assertNotIn(self.session_id, store.entries)

    def test_timeout_identifies_the_stalled_image_stage(self):
        async def stalled(entry, question, photo):
            entry.request_stage = 'manual image and link loading'
            raise asyncio.TimeoutError()
        with patch('app.api.chat_routes.answer_question', side_effect=stalled):
            response = self.client.post('/chat', data={
                'session_id': self.session_id, 'message': 'Explain the image',
            })
        self.assertEqual(response.status_code, 504)
        self.assertIn('manual image and link loading', response.json()['detail'])
        self.assertEqual(store.entries[self.session_id].request_stage, '')
