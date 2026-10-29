"""Latency changes must preserve exact evidence and never download chat images."""
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from PIL import Image
from app import config as cfg
from app.media import manual_assets
from app.media.prepare_media import prepare_media
from app.retrieval import manual_search
from app.retrieval.cache_revision import invalidate_manual_caches
from app.services.singleflight import SingleFlight


def png(color='blue'):
    stream = BytesIO()
    Image.new('RGB', (30, 20), color).save(stream, 'PNG')
    return stream.getvalue()


class SingleFlightTests(unittest.TestCase):
    def test_concurrent_readers_share_one_result_and_failures_are_retryable(self):
        waiting, release = Event(), Event()
        class ObservedFuture(Future):
            def result(self, *args, **kwargs):
                waiting.set()
                return super().result(*args, **kwargs)
        flight = SingleFlight()
        calls = []
        def read():
            calls.append(1)
            self.assertTrue(release.wait(2))
            return b'exact original'
        with patch('app.services.singleflight.Future', ObservedFuture), ThreadPoolExecutor(2) as pool:
            first = pool.submit(flight.run, 'same-source', read)
            second = pool.submit(flight.run, 'same-source', read)
            self.assertTrue(waiting.wait(2))
            release.set()
            self.assertEqual(first.result(), b'exact original')
            self.assertEqual(second.result(), b'exact original')
        self.assertEqual(len(calls), 1)
        with self.assertRaises(ValueError):
            flight.run('failed', lambda: (_ for _ in ()).throw(ValueError('temporary')))
        self.assertEqual(flight.run('failed', lambda: 'recovered'), 'recovered')


class RetrievalPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = patch.object(cfg, 'BACKEND_ROOT', Path(self.temp.name))
        root.start()
        self.addCleanup(root.stop)
        manual_search._INVENTORY_CACHE.clear()
        manual_search._SEARCH_CACHE.clear()

    def test_inventory_cache_keeps_exact_records_and_invalidates_after_ingestion(self):
        original = [{'machine_name': 'Unit_X', 'document_name': 'Full Manual.pdf'}]
        with patch('app.retrieval.manual_search.query', return_value=original) as query:
            first = manual_search.list_available_documents()
            first[0]['document_name'] = 'Modified by caller'
            self.assertEqual(manual_search.list_available_documents(), original)
            self.assertEqual(query.call_count, 1)
            invalidate_manual_caches()
            self.assertEqual(manual_search.list_available_documents(), original)
            self.assertEqual(query.call_count, 2)

    def test_embedding_and_inventory_overlap_without_changing_retrieval(self):
        embedding_started, inventory_started = Event(), Event()
        def embed(_question):
            embedding_started.set()
            self.assertTrue(inventory_started.wait(2), 'Inventory must not wait for embedding')
            return [0.1]
        def inventory():
            inventory_started.set()
            self.assertTrue(embedding_started.wait(2), 'Embedding must not wait for inventory')
            return [{'machine_name': 'Unit_X', 'document_name': 'Manual.pdf'}]
        row = {'chunk_id': 'c', 'machine_name': 'Unit_X', 'document_name': 'Manual.pdf',
               'content': 'Complete pressure procedure and original 12 bar reference.',
               'page_number': 1, 'distance': .1, 'images': [], 'links': []}
        with patch('app.retrieval.manual_search.get_embedding', side_effect=embed), \
             patch('app.retrieval.manual_search.list_available_documents', side_effect=inventory), \
             patch('app.retrieval.manual_search.query', side_effect=[[row], [row]]):
            result = manual_search.search_manual('Unit_X pressure')
        self.assertEqual(result['chunks'][0]['content'], row['content'])

    def test_image_embedding_search_has_no_top_k_and_uses_the_matched_machine(self):
        images = [{'chunk_id': f'figure-{i}', 'machine_name': 'Unit_X', 'distance': .1} for i in range(73)]
        seeds = [{'chunk_id': 'text', 'machine_name': 'Unit_X', 'distance': .2, 'keyword_hits': 2}]
        with patch('app.retrieval.manual_search.query', return_value=images) as query:
            result = manual_search._search_image_seeds('SELECT * FROM approved_embedding_rows', [], seeds)
        self.assertEqual(result, images)
        sql, params = query.call_args.args
        self.assertIn('document_images', sql)
        self.assertIn('chunk_relationships', sql)
        self.assertIn('section_elements', sql)
        self.assertIn('i.machine_name = a.machine_name', sql)
        self.assertNotIn('LIMIT', sql.upper())
        self.assertIn('PARTITION BY i.chunk_id', sql)
        self.assertEqual(next(item for item in params if item.name == 'image_machines').values, ['Unit_X'])
        self.assertEqual(query.call_args.kwargs['operation'], 'bq_image_query')

    def test_same_page_does_not_add_images_not_selected_by_embedding(self):
        seed = {'chunk_id': 'text', 'machine_name': 'Unit_X', 'document_name': 'Manual.pdf',
                'page_number': 1, 'content': 'Pressure gauge inspection.', 'content_type': 'paragraph',
                'distance': .1, 'images': [], 'links': []}
        figures = [{**seed, 'chunk_id': f'figure-{i}', 'content_type': 'image',
                    'images': [{'image_id': f'image-{i}', 'image_path': f'assets/{i}.png'}]} for i in range(20)]
        unrelated = {**figures[0], 'chunk_id': 'same-page-other-component',
                     'images': [{'image_id': 'other', 'image_path': 'assets/other.png'}]}
        with patch('app.retrieval.manual_search.get_embedding', return_value=[.1]), \
             patch('app.retrieval.manual_search.list_available_documents', return_value=[]), \
             patch('app.retrieval.manual_search.query', side_effect=[[seed], [seed, *figures, unrelated]]), \
             patch('app.retrieval.manual_search._search_image_seeds', return_value=figures):
            result = manual_search.search_manual('pressure gauge', wants_visual=True)
        self.assertEqual(len(result['images']), 20)
        self.assertNotIn('other', {item['image_id'] for item in result['images']})
        self.assertEqual(len(result['chunks']), 22, 'Full retrieved text must be retained')

    def test_equal_local_image_ids_in_different_manuals_do_not_hide_required_figures(self):
        first = {'chunk_id': 'a', 'machine_name': 'Unit_X', 'document_name': 'Operation.pdf',
                 'content': 'Pressure gauge.', 'content_type': 'image', 'page_number': 1, 'distance': .1,
                 'images': [{'image_id': 'figure-1', 'image_path': 'assets/gauge.png'}], 'links': []}
        second = {**first, 'chunk_id': 'b', 'document_name': 'Maintenance.pdf'}
        duplicate_source = {**first, 'chunk_id': 'a-associated-text'}
        rows = [first, second, duplicate_source]
        with patch('app.retrieval.manual_search.get_embedding', return_value=[.1]), \
             patch('app.retrieval.manual_search.list_available_documents', return_value=[]), \
             patch('app.retrieval.manual_search.query', side_effect=[[first], rows]), \
             patch('app.retrieval.manual_search._search_image_seeds', return_value=rows):
            result = manual_search.search_manual('pressure gauge', wants_visual=True)
        self.assertEqual(len(result['images']), 2)
        self.assertEqual({image['document_name'] for image in result['images']}, {'Operation.pdf', 'Maintenance.pdf'})
        self.assertEqual(result['images'][0]['source_chunk_ids'], ['a', 'a-associated-text'])
        with patch.object(cfg, 'MANUAL_IMAGE_TRANSPORT', 'gcs'):
            media = prepare_media(result, wants_visual=True)
        self.assertEqual(media['images'][0]['source_chunk_ids'], ['a', 'a-associated-text'])


class ManualImageCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = patch.object(cfg, 'BACKEND_ROOT', Path(self.temp.name))
        root.start()
        self.addCleanup(root.stop)
        manual_assets._MEMORY.clear()
        self.reference = manual_assets.manual_reference('assets/gauge.png', 'Manual.pdf', 'Unit_X')

    def test_reference_transport_keeps_all_figures_without_storage_download_or_metadata_calls(self):
        bundle = {'chunks': [], 'links': [], 'images': [
            {'image_path': f'assets/figure-{i}.png', 'document_name': 'Manual.pdf',
             'machine_name': 'Unit_X', 'page_number': 1, 'chunk_id': f'figure-{i}'} for i in range(73)]}
        before = deepcopy(bundle)
        with patch.object(cfg, 'MANUAL_IMAGE_TRANSPORT', 'gcs'), \
             patch('app.media.manual_assets.storage_client', side_effect=AssertionError('No chat image I/O')):
            result = prepare_media(bundle, wants_visual=True)
        self.assertEqual(len(result['images']), 73)
        self.assertTrue(all('bytes' not in item for item in result['images']))
        self.assertTrue(all(item['reference']['uri'].startswith(f'gs://{cfg.BUCKET}/') for item in result['images']))
        self.assertEqual(bundle, before)

    def test_persistent_cache_survives_memory_reset_and_revalidates_object_generation(self):
        with patch('app.media.manual_assets.storage_client') as client:
            blob = client.return_value.bucket.return_value.blob.return_value
            blob.generation = '7'
            blob.size = len(png())
            blob.download_as_bytes.return_value = png()
            first = manual_assets.load_manual_reference(self.reference)
            manual_assets._MEMORY.clear()  # equivalent to a fresh worker
            self.assertEqual(manual_assets.load_manual_reference(self.reference), first)
            self.assertEqual(blob.download_as_bytes.call_count, 1)
            self.assertEqual(blob.reload.call_count, 2)
            blob.generation = '8'
            blob.download_as_bytes.return_value = png('red')
            second = manual_assets.load_manual_reference(self.reference)
            self.assertNotEqual(first, second)
            self.assertEqual(blob.download_as_bytes.call_count, 2)
            self.assertEqual(blob.download_as_bytes.call_args.kwargs['if_generation_match'], 8)

    def test_corrupt_disk_cache_is_refetched_instead_of_serving_wrong_evidence(self):
        with patch('app.media.manual_assets.storage_client') as client:
            blob = client.return_value.bucket.return_value.blob.return_value
            blob.generation = '7'
            blob.size = len(png())
            blob.download_as_bytes.return_value = png()
            expected = manual_assets.load_manual_reference(self.reference)
            path = next((Path(self.temp.name) / '.cache' / 'manual-images').glob('*.image'))
            path.write_bytes(b'corrupt cache')
            manual_assets._MEMORY.clear()
            self.assertEqual(manual_assets.load_manual_reference(self.reference), expected)
            self.assertEqual(blob.download_as_bytes.call_count, 2)

    def test_reference_validation_rejects_other_buckets_paths_and_changed_manual_revisions(self):
        for path in ('https://example.com/gauge.png', '../other.png', f'gs://other-bucket/path.png'):
            with self.subTest(path=path), self.assertRaises(ValueError):
                manual_assets.manual_reference(path, 'Manual.pdf', 'Unit_X')
        forged = {**self.reference, 'uri': self.reference['uri'] + '-different'}
        with self.assertRaises(ValueError):
            manual_assets.validate_reference(forged)
        invalidate_manual_caches()
        with self.assertRaisesRegex(ValueError, 'changed'):
            manual_assets.validate_reference(self.reference)


if __name__ == '__main__':
    unittest.main()
