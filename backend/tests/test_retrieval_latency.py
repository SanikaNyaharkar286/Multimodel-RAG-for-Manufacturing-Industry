import unittest
from unittest.mock import patch

from app.retrieval import manual_search


class RetrievalLatencyTests(unittest.TestCase):
    def setUp(self):
        manual_search._SEARCH_CACHE.clear()
        manual_search._REFERENCE_CACHE.clear()
        self.bundle = {'chunks': [{'chunk_id': 'table-1', 'content': 'Approved table text',
                                   'content_type': 'table'}],
                       'images': [], 'links': [], 'warnings': []}

    @patch('app.retrieval.manual_search._search_manual_uncached')
    def test_repeated_search_reuses_result_without_sharing_mutable_session_data(self, search):
        search.return_value = self.bundle

        first = manual_search.search_manual('hydraulic pressure')
        first['chunks'][0]['content'] = 'Session-specific change'
        second = manual_search.search_manual('hydraulic pressure')
        second['warnings'].append('Session-specific warning')
        third = manual_search.search_manual('hydraulic pressure')

        search.assert_called_once()
        self.assertEqual(second['chunks'][0]['content'], 'Approved table text')
        self.assertEqual(third['warnings'], [])

    @patch('app.retrieval.manual_search._search_manual_uncached')
    def test_equivalent_question_casing_and_spacing_reuses_search(self, search):
        search.return_value = self.bundle

        manual_search.search_manual('  Hydraulic   Pressure ')
        manual_search.search_manual('hydraulic pressure')

        search.assert_called_once()

    @patch('app.retrieval.manual_search._search_manual_uncached')
    def test_media_flags_and_embedding_settings_have_distinct_cached_results(self, search):
        search.return_value = self.bundle

        manual_search.search_manual('hydraulic pressure')
        manual_search.search_manual('hydraulic pressure', wants_visual=True)
        manual_search.search_manual('hydraulic pressure', wants_video=True)
        with patch.object(manual_search.cfg, 'DIMENSIONS', 999):
            manual_search.search_manual('hydraulic pressure')

        self.assertEqual(search.call_count, 4)

    @patch('app.retrieval.manual_search.time.monotonic')
    @patch('app.retrieval.manual_search._search_manual_uncached')
    def test_cache_expires_and_does_not_cache_failed_queries(self, search, monotonic):
        search.side_effect = [RuntimeError('temporary retrieval failure'), self.bundle, self.bundle]
        monotonic.return_value = 100
        with self.assertRaises(RuntimeError):
            manual_search.search_manual('hydraulic pressure')
        manual_search.search_manual('hydraulic pressure')
        monotonic.return_value = 100 + manual_search._SEARCH_CACHE_TTL_SECONDS + 1
        manual_search.search_manual('hydraulic pressure')

        self.assertEqual(search.call_count, 3)

    @patch('app.retrieval.manual_search.get_embedding', return_value=[0.1])
    @patch('app.retrieval.manual_search.query')
    def test_enrichment_preserves_table_chunk_content_without_unused_table_rows_join(self, query, embedding):
        seed = {'chunk_id': 'table-1', 'document_name': 'manual.pdf', 'machine_name': 'HAB-3232',
                'page_number': 3, 'content': 'Approved table text', 'content_type': 'table',
                'distance': 0.1}
        query.side_effect = [[seed], [{**seed, 'images': [], 'links': []}]]

        bundle = manual_search.search_manual('hydraulic pressure')

        self.assertEqual(bundle['chunks'][0]['content'], 'Approved table text')
        enrichment_sql = query.call_args_list[1].args[0]
        self.assertNotIn('document_table_rows', enrichment_sql)
        self.assertIn('document_sections', enrichment_sql)

    def test_manual_reference_parser_finds_section_title_and_chapter(self):
        references = manual_search.manual_references([{'content':
            'CORRECTIVE ACTION: Refer to the section 3.2 Blade Installation '
            'of the Installation chapter for adjusting the tension of the blade.'}])

        self.assertEqual(references, [{
            'number': '3.2', 'title': 'blade installation',
            'chapter': 'installation'}])

    @patch('app.retrieval.manual_search.get_embedding', return_value=[0.1])
    @patch('app.retrieval.manual_search.query')
    def test_cross_reference_is_resolved_before_enrichment(self, query, embedding):
        seed = {'chunk_id': 'troubleshooting', 'document_name': 'troubleshooting.pdf',
                'machine_name': 'HAB-3232', 'page_number': 3,
                'content': 'Refer to section 3.2 Blade Installation of the Installation chapter '
                           'for adjusting the tension of the blade.',
                'content_type': 'table', 'distance': 0.1}
        referenced = {'chunk_id': 'installation-step', 'document_name': 'installation.pdf',
                      'machine_name': 'HAB-3232', 'page_number': 8,
                      'content': 'Turn the blade tension selector to the specified position.',
                      'content_type': 'paragraph', 'images': [], 'links': []}
        query.side_effect = [[seed], [{'chunk_id': 'installation-step'}],
                             [{**seed, 'images': [], 'links': []}, referenced]]

        bundle = manual_search.search_manual('blade shifts during operation')

        self.assertEqual(query.call_count, 3)
        self.assertIn('bq_reference_query', query.call_args_list[1].kwargs.values())
        enrichment_parameters = query.call_args_list[2].args[1]
        self.assertIn('installation-step', enrichment_parameters[0].values)
        self.assertIn('Turn the blade tension selector',
                      ' '.join(row['content'] for row in bundle['chunks']))


if __name__ == '__main__':
    unittest.main()
