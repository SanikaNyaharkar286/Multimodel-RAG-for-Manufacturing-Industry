import unittest
from unittest.mock import patch

from google.api_core.exceptions import NotFound

from app.media.manual_images import MAX_SOURCE_BYTES, blob_bytes, read_image
from app.media.prepare_media import prepare_media


class MediaLatencyTests(unittest.TestCase):
    @patch('app.media.manual_images.storage_client')
    def test_bounded_download_keeps_size_limit_without_metadata_request(self, client):
        blob = client.return_value.bucket.return_value.blob.return_value
        blob.download_as_bytes.return_value = b'image bytes'

        self.assertEqual(blob_bytes('processed/machine/image.png'), b'image bytes')
        blob.download_as_bytes.assert_called_once_with(
            start=0, end=MAX_SOURCE_BYTES, timeout=15)
        blob.reload.assert_not_called()

        for data in (b'', b'x' * (MAX_SOURCE_BYTES + 1)):
            with self.subTest(size=len(data)):
                blob.download_as_bytes.return_value = data
                with self.assertRaises(ValueError):
                    blob_bytes('processed/machine/image.png')

    @patch('app.media.manual_images.storage_client')
    def test_missing_candidate_still_tries_next_location(self, client):
        blob = client.return_value.bucket.return_value.blob
        blob.return_value.download_as_bytes.side_effect = [NotFound('missing'), b'image bytes']

        result = read_image('latency-test-panel.png', 'latency-test-manual.pdf', 'machine')

        self.assertEqual(result, b'image bytes')
        self.assertEqual(blob.return_value.download_as_bytes.call_count, 2)

    def setUp(self):
        self.bundle = {
            'images': [], 'chunks': [], 'warnings': [],
            'links': [{'url': 'https://www.haascnc.com/manual-page', 'chunk_id': 'chunk-1'}],
        }

    @patch('app.media.prepare_media.normalize_image', return_value=b'normalized')
    @patch('app.media.prepare_media.read_image', return_value=b'original')
    def test_duplicate_manual_image_is_loaded_once_with_both_sources(self, read, normalize):
        record = {
            'image_path': 'assets/images/panel.png', 'document_name': 'manual.pdf',
            'machine_name': 'machine', 'page_number': 3,
            'geometry': [{'x0': 1, 'y0': 2, 'x1': 3, 'y1': 4}],
        }
        self.bundle['images'] = [{**record, 'chunk_id': 'chunk-1'},
                                 {**record, 'chunk_id': 'chunk-2'}]

        result = prepare_media(self.bundle)

        read.assert_called_once()
        normalize.assert_called_once_with(b'original')
        self.assertEqual([image['chunk_id'] for image in result['images']],
                         ['chunk-1', 'chunk-2'])
        self.assertEqual([image['bytes'] for image in result['images']],
                         [b'normalized', b'normalized'])

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
