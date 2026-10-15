import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.responses.response_builder import final_blocks
from app.responses.schemas import AgentAnswer, AnswerBlock, ImageDescription


class ResponseTests(unittest.TestCase):
    def setUp(self):
        self.bundle = {'chunks': [
            {'chunk_id': 'c1', 'document_name': 'Manual', 'page_number': 1},
            {'chunk_id': 'c2', 'document_name': 'Other', 'page_number': 2},
        ]}
        self.media = {
            'images': [{'id': 'image-1', 'chunk_id': 'c1', 'bytes': b'diagram'}],
            'links': [{'id': 'video-1', 'chunk_id': 'c1', 'url': 'https://youtu.be/12345678901',
                       'is_video': True, 'origin': 'pdf_link'}],
        }
        self.answer = AgentAnswer(
            blocks=[AnswerBlock(type='text', content='Panel explanation', evidence_ids=['c1'])],
            relevant_asset_ids=['image-1', 'image-1'],
            image_descriptions=[ImageDescription(asset_id='image-1', description='Visible controls and labels.')],
            supported=True, finished=False,
        )

    def test_media_completeness_descriptions_and_sources(self):
        with patch('app.responses.response_builder.store.put_image', return_value='saved'):
            response = final_blocks(self.answer, self.bundle, self.media, SimpleNamespace(), True)
        blocks = response['blocks']
        self.assertEqual([b['type'] for b in blocks], ['text', 'image', 'link', 'text'])
        self.assertEqual(blocks[1]['description'], 'Visible controls and labels.')
        self.assertIn('Manual', blocks[-1]['content'])

    def test_unknown_evidence_is_rejected(self):
        self.answer.blocks[0].evidence_ids = ['invented']
        with self.assertRaises(ValueError):
            final_blocks(self.answer, self.bundle, self.media, SimpleNamespace(), False)

    def test_missing_image_description_is_rejected(self):
        self.answer.image_descriptions = []
        with self.assertRaisesRegex(ValueError, 'image description'):
            final_blocks(self.answer, self.bundle, self.media, SimpleNamespace(), False)

    def test_video_from_unrelated_manual_is_excluded(self):
        self.answer.relevant_asset_ids = []
        self.media['links'][0]['chunk_id'] = 'c2'
        response = final_blocks(self.answer, self.bundle, self.media, SimpleNamespace(), False)
        self.assertEqual([b['type'] for b in response['blocks']], ['text'])
