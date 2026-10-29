import unittest
from unittest.mock import patch

from google.api_core.exceptions import NotFound

from app import config as cfg
from app.media.manual_images import (
    MAX_SOURCE_BYTES, _MISSING_CACHE, _MISSING_CACHE_TTL_SECONDS, blob_bytes, read_image,
)
from app.media.prepare_media import _IMAGE_CACHE, prepare_media


class MediaLatencyTests(unittest.TestCase):
    @patch('app.media.manual_images.storage_client')
    def test_bounded_download_keeps_size_limit_without_metadata_request(self, client):
        blob = client.return_value.bucket.return_value.blob.return_value
        blob.download_as_bytes.return_value = b'image bytes'

        self.assertEqual(blob_bytes('processed/machine/image.png'), b'image bytes')
        blob.download_as_bytes.assert_called_once_with(
            start=0, end=MAX_SOURCE_BYTES, timeout=4, retry=None)
        blob.reload.assert_not_called()

        for data in (b'', b'x' * (MAX_SOURCE_BYTES + 1)):
            with self.subTest(size=len(data)):
                blob.download_as_bytes.return_value = data
                with self.assertRaises(ValueError):
                    blob_bytes('processed/machine/image.png')

    @patch('app.media.manual_images.storage_client')
    def test_missing_candidate_does_not_search_unrelated_manual_folders(self, client):
        blob = client.return_value.bucket.return_value.blob
        blob.return_value.download_as_bytes.side_effect = [NotFound('missing'), b'image bytes']

        with self.assertRaises(FileNotFoundError):
            read_image('latency-test-panel.png', 'latency-test-manual.pdf', 'machine')
        self.assertEqual(blob.return_value.download_as_bytes.call_count, 1)

    @patch('app.media.manual_images.time.monotonic')
    @patch('app.media.manual_images.storage_client')
    def test_missing_assets_are_cached_briefly_and_rechecked_after_expiry(self, client, monotonic):
        blob = client.return_value.bucket.return_value.blob.return_value
        blob.download_as_bytes.side_effect = [NotFound('missing'), b'repaired asset']
        monotonic.return_value = 100

        for _ in range(2):
            with self.assertRaises(NotFound):
                blob_bytes('processed/machine/repaired-later.png')
        self.assertEqual(blob.download_as_bytes.call_count, 1)
        monotonic.return_value = 100 + _MISSING_CACHE_TTL_SECONDS + 1

        self.assertEqual(blob_bytes('processed/machine/repaired-later.png'), b'repaired asset')
        self.assertEqual(blob.download_as_bytes.call_count, 2)

    @patch('app.media.manual_images.storage_client')
    def test_missing_absolute_gcs_reference_is_skipped_without_pdf_fallback(self, client):
        client.return_value.bucket.return_value.blob.return_value.download_as_bytes.side_effect = NotFound('missing')
        self.bundle['images'] = [{
            'image_path': f'gs://{cfg.BUCKET}/{cfg.PROCESSED_PREFIX}/machine/missing.png',
            'document_name': 'gcs-fallback-manual.pdf', 'machine_name': 'machine',
            'page_number': 3, 'chunk_id': 'chunk-1',
        }]

        result = prepare_media(self.bundle)

        self.assertEqual(result['images'], [])
        self.assertTrue(result['warnings'])

    def setUp(self):
        _IMAGE_CACHE.clear()
        _MISSING_CACHE.clear()
        self.bundle = {
            'images': [], 'chunks': [], 'warnings': [],
            'links': [{'url': 'https://www.haascnc.com/manual-page', 'chunk_id': 'chunk-1'}],
        }

    @patch.object(cfg, 'MANUAL_IMAGE_TRANSPORT', 'inline')
    @patch('app.media.prepare_media.load_manual_reference', return_value=b'normalized')
    def test_duplicate_manual_image_is_loaded_once_with_both_sources(self, read):
        record = {
            'image_path': 'assets/images/panel.png', 'document_name': 'manual.pdf',
            'machine_name': 'machine', 'page_number': 3,
            'geometry': [{'x0': 1, 'y0': 2, 'x1': 3, 'y1': 4}],
        }
        self.bundle['images'] = [{**record, 'chunk_id': 'chunk-1'},
                                 {**record, 'chunk_id': 'chunk-2'}]

        result = prepare_media(self.bundle)

        read.assert_called_once()
        self.assertEqual([image['chunk_id'] for image in result['images']],
                         ['chunk-1', 'chunk-2'])
        self.assertEqual([image['bytes'] for image in result['images']],
                         [b'normalized', b'normalized'])

    @patch.object(cfg, 'MANUAL_IMAGE_TRANSPORT', 'gcs')
    @patch('app.media.prepare_media.load_manual_reference', side_effect=AssertionError('No eager downloads'))
    def test_all_selected_figures_are_kept_without_duplicate_full_pages(
        self, read,
    ):
        self.bundle['chunks'] = [
            {'chunk_id': f'chunk-{number}', 'document_name': 'manual.pdf',
             'page_number': 3, 'machine_name': 'machine'}
            for number in range(10)
        ]
        self.bundle['images'] = [
            {'chunk_id': f'chunk-{number}', 'document_name': 'manual.pdf',
             'page_number': 3, 'machine_name': 'machine',
             'image_path': f'figure-{number}.png'}
            for number in range(10)
        ]

        result = prepare_media(self.bundle, wants_visual=True)

        self.assertEqual(len(result['images']), 10)
        read.assert_not_called()
        self.assertTrue(all('reference' in image and 'bytes' not in image for image in result['images']))

    def test_missing_original_figures_never_render_a_pdf_page(self):
        self.bundle['chunks'] = [
            {'chunk_id': 'chunk-1', 'document_name': 'manual.pdf',
             'page_number': 3, 'machine_name': 'machine'},
        ]

        self.assertEqual(prepare_media(self.bundle, wants_visual=True)['images'], [])

    @patch('app.media.prepare_media.load_manual_reference')
    def test_text_only_skips_all_image_loading(self, read):
        self.bundle['images'] = [{'image_path': 'not-used.png'}]

        self.assertEqual(prepare_media(self.bundle, include_images=False)['images'], [])

        read.assert_not_called()

    @patch('app.media.prepare_media.manual_video_targets')
    def test_regular_answer_keeps_manual_link_without_video_check(self, check):
        result = prepare_media(self.bundle, wants_video=False)

        check.assert_not_called()
        self.assertEqual([item['url'] for item in result['links']],
                         [self.bundle['links'][0]['url']])

    @patch('app.media.prepare_media.manual_video_targets')
    def test_video_request_still_discovers_video_links(self, check):
        check.return_value = ['https://youtu.be/abcdefghijk']

        result = prepare_media(self.bundle, wants_video=True)

        check.assert_called_once_with(self.bundle['links'][0]['url'])
        self.assertEqual(len(result['links']), 2)
        self.assertEqual(result['links'][1]['origin'], 'manual_linked_video')
        self.assertTrue(result['links'][1]['is_video'])


if __name__ == '__main__':
    unittest.main()
