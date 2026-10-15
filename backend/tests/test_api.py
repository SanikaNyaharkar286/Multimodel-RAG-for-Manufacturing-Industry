import asyncio
import io
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from google.genai.errors import ClientError
from PIL import Image

from app import config
from app.main import app
from app.sessions.session_store import store


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.context = TestClient(app)
        self.client = self.context.__enter__()
        self.session_id = self.client.post('/sessions').json()['session_id']

    def tearDown(self):
        self.context.__exit__(None, None, None)

    def test_health_cors_and_session_lifecycle(self):
        self.assertEqual(self.client.get('/health').json()['status'], 'running')
        self.assertEqual(self.client.get('/sessions/' + self.session_id).json()['messages'], [])
        response = self.client.options('/sessions', headers={
            'Origin': config.ORIGIN, 'Access-Control-Request-Method': 'POST',
        })
        self.assertEqual(response.headers['access-control-allow-origin'], config.ORIGIN)
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
