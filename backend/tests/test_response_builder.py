import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app.responses.response_builder import final_blocks
from app.responses.schemas import AgentAnswer, AnswerBlock, ImageDescription, RepairStep


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
        self.assertEqual([b['type'] for b in blocks], ['text', 'image', 'link'])
        self.assertEqual(blocks[1]['description'], '- Visible controls and labels.')
        self.assertEqual(response['sources'][0]['document_name'], 'Manual')
        self.assertEqual(response['sources'][0]['links'][0]['url'], 'https://youtu.be/12345678901')

    def test_sources_are_deduplicated_by_manual_page(self):
        self.bundle['chunks'].append(
            {'chunk_id': 'c3', 'document_name': 'Manual', 'page_number': 1})
        self.answer.blocks.append(
            AnswerBlock(type='text', content='More details', evidence_ids=['c3']))
        with patch('app.responses.response_builder.store.put_image', return_value='saved'):
            response = final_blocks(self.answer, self.bundle, self.media, SimpleNamespace(), True)
        self.assertEqual(response['sources'][0]['document_name'], 'Manual')
        self.assertEqual(response['sources'][0]['links'][0]['url'], 'https://youtu.be/12345678901')
        self.assertFalse(any((block.get('content') or '').startswith('### Sources')
                             for block in response['blocks']))

    def test_unknown_evidence_is_rejected(self):
        self.answer.blocks[0].evidence_ids = ['invented']
        with self.assertRaises(ValueError):
            final_blocks(self.answer, self.bundle, self.media, SimpleNamespace(), False)

    def test_malformed_grounded_table_is_preserved_as_text(self):
        self.answer.blocks = [AnswerBlock(
            type='table', title='Approved limits', columns=['Setting', 'Value'],
            rows=[['Pressure', '12 bar'], ['Oil grade']], evidence_ids=['c1'])]
        self.answer.relevant_asset_ids = []
        self.answer.image_descriptions = []
        response = final_blocks(self.answer, self.bundle, self.media, SimpleNamespace(), False)
        self.assertEqual(response['blocks'][0]['type'], 'text')
        self.assertIn('Pressure | 12 bar', response['blocks'][0]['content'])
        self.assertIn('Oil grade', response['blocks'][0]['content'])
        self.assertEqual(response['sources'][0]['document_name'], 'Manual')
        self.assertEqual(response['sources'][0]['links'][0]['url'], 'https://youtu.be/12345678901')

    def test_missing_image_description_is_rejected(self):
        self.answer.image_descriptions = []
        with self.assertRaisesRegex(ValueError, 'image description'):
            final_blocks(self.answer, self.bundle, self.media, SimpleNamespace(), False)

    def test_duplicate_image_bytes_are_not_repeated_and_unselected_pages_stay_hidden(self):
        self.media['images'] += [{'id':'image-2','chunk_id':'c1','bytes':b'diagram'},
                                 {'id':'image-3','chunk_id':'c2','bytes':b'unrelated-page'}]
        self.answer.relevant_asset_ids=['image-1','image-2']
        self.answer.image_descriptions.append(ImageDescription(asset_id='image-2',description='Same approved figure.'))
        with patch('app.responses.response_builder.store.put_image',return_value='saved'):
            response=final_blocks(self.answer,self.bundle,self.media,SimpleNamespace(),True)
        images=[block for block in response['blocks'] if block['type']=='image']
        self.assertEqual(len(images),1)
        self.assertEqual(images[0]['source'],{'document_name':'Manual','page_number':1})

    def test_video_from_unrelated_manual_is_excluded(self):
        self.answer.relevant_asset_ids = []
        self.media['links'][0]['chunk_id'] = 'c2'
        response = final_blocks(self.answer, self.bundle, self.media, SimpleNamespace(), False)
        self.assertEqual([b['type'] for b in response['blocks']], ['text'])

    def test_guided_step_does_not_append_an_unselected_video_from_the_same_page(self):
        self.answer.relevant_asset_ids = []
        self.answer.repair_step = RepairStep(instruction='Read the panel.', evidence_ids=['c1'])
        response = final_blocks(self.answer, self.bundle, self.media, SimpleNamespace(), False)
        self.assertEqual([block['type'] for block in response['blocks']], ['text'])

    def test_callout_numbers_are_not_inferred_from_neighbouring_text(self):
        self.bundle['chunks'][0]['content'] = 'Locate the set screw on the block [1].'
        with patch('app.responses.response_builder.store.put_image', return_value='saved'):
            response = final_blocks(self.answer, self.bundle, self.media, SimpleNamespace(), True)
        self.assertNotIn('Callout 1', response['blocks'][1]['description'])
