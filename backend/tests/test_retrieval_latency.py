import unittest
from unittest.mock import patch

from app.retrieval import manual_search


class RetrievalLatencyTests(unittest.TestCase):
    def setUp(self):
        inventory = patch('app.retrieval.manual_search.list_available_documents', return_value=[])
        inventory.start()
        self.addCleanup(inventory.stop)
        image_search = patch('app.retrieval.manual_search._search_image_seeds', return_value=None)
        image_search.start()
        self.addCleanup(image_search.stop)
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

    @patch('app.retrieval.manual_search._search_manual_uncached')
    def test_empty_results_do_not_hide_newly_available_manual_evidence(self, search):
        search.side_effect = [{'chunks':[], 'images':[], 'links':[], 'warnings':[]}, self.bundle]
        self.assertEqual(manual_search.search_manual('oil level')['chunks'], [])
        self.assertTrue(manual_search.search_manual('oil level')['chunks'])
        self.assertEqual(search.call_count, 2)

    @patch('app.retrieval.manual_search.get_embedding', return_value=[0.1])
    @patch('app.retrieval.manual_search.query')
    def test_empty_vector_result_recovers_only_from_indexed_manual_text(self, query, embedding):
        seed = {'chunk_id':'oil', 'machine_name':'Unit_X', 'document_name':'Maintenance.pdf',
                'page_number':3, 'content':'Inspect oil level.', 'distance':1.0, 'content_type':'paragraph'}
        query.side_effect = [[], [seed], [{**seed, 'images':[], 'links':[]}]]
        result = manual_search.search_manual('oil level')
        self.assertEqual(result['chunks'][0]['chunk_id'], 'oil')
        self.assertEqual(query.call_args_list[1].kwargs['operation'], 'bq_keyword_recovery')
        self.assertIn('document_chunks', query.call_args_list[1].args[0])

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
    @patch('app.retrieval.manual_search.query', return_value=[])
    def test_supplemental_search_uses_exact_dataset_machine_scope(self, query, embedding):
        manual_search.search_manual('supply controls', machine_scope=('Press Unit_X',))
        sql, parameters = query.call_args.args
        self.assertIn('c.machine_name IN UNNEST(@machine_scope)', sql)
        scope = next(parameter for parameter in parameters if parameter.name == 'machine_scope')
        self.assertEqual(scope.values, ['Press Unit_X'])

    @patch('app.retrieval.manual_search.get_embedding', return_value=[0.1])
    @patch('app.retrieval.manual_search.query', return_value=[])
    def test_empty_machine_scope_handles_bigquery_null_array_parameters(self, query, embedding):
        manual_search.search_manual('hydraulic oil level')
        sql, parameters = query.call_args.args
        self.assertIn('IFNULL(ARRAY_LENGTH(@machine_scope), 0) = 0', sql)
        self.assertEqual(next(parameter for parameter in parameters if parameter.name == 'machine_scope').values, [])

    @patch('app.retrieval.manual_search.get_embedding', return_value=[0.1])
    @patch('app.retrieval.manual_search.query')
    def test_all_relevant_figures_and_links_survive_without_fixed_count_limits(self, query, embedding):
        rows = [{'chunk_id': f'c-{i}', 'document_name': 'Unit Manual.pdf', 'machine_name': 'Unit_X',
                 'page_number': 1, 'content': f'Pressure check diagram {i}', 'content_type': 'image',
                 'distance': 0.1, 'images': [{'image_id': f'figure-{i}', 'image_path': f'assets/{i}.png'}],
                 'links': [{'link_id': f'link-{i}', 'url': f'https://example.com/pressure/{i}'}]}
                for i in range(10)]
        rows.append({**rows[0], 'chunk_id': 'unrelated', 'content': 'Manufacturer company logo',
                     'images': [{'image_id': 'logo', 'image_path': 'assets/logo.png'}], 'links': []})
        query.side_effect = [[rows[0]], rows]
        result = manual_search.search_manual('pressure', wants_visual=True)
        self.assertEqual(len(result['images']), 10)
        self.assertEqual(len(result['links']), 10)
        self.assertNotIn('logo', {image['image_id'] for image in result['images']})
        source_ids = {row['chunk_id'] for row in result['chunks']}
        self.assertTrue(all(image['chunk_id'] in source_ids for image in result['images']))
        self.assertTrue(all(link['chunk_id'] in source_ids for link in result['links']))

    @patch('app.retrieval.manual_search.get_embedding', return_value=[0.1])
    @patch('app.retrieval.manual_search.query')
    def test_full_context_preserves_long_text_without_old_character_or_row_cuts(self, query, embedding):
        for size, expected in [(100, 80), (2000, 80)]:
            with self.subTest(size=size):
                manual_search._SEARCH_CACHE.clear()
                rows = [{'chunk_id': f'text-{i}', 'document_name': 'Pressure.pdf', 'machine_name': 'Unit_X',
                         'page_number': 1, 'content': 'p' * size, 'content_type': 'paragraph',
                         'distance': 0.1, 'images': [], 'links': []} for i in range(80)]
                query.side_effect = [[rows[0]], rows]
                bundle = manual_search.search_manual('pressure procedure')
                self.assertEqual(len(bundle['chunks']), expected)
                self.assertEqual(sum(len(row['content']) for row in bundle['chunks']), 80 * size)
                self.assertEqual(bundle['chunks'][0]['chunk_id'], 'text-0')
                self.assertNotIn('LIMIT 100', query.call_args.args[0])
                self.assertNotIn('LIMIT 500', query.call_args.args[0])
                self.assertEqual(bundle['images'], [])

    @patch('app.retrieval.manual_search.get_embedding', return_value=[0.1])
    @patch('app.retrieval.manual_search.query')
    def test_selected_figure_retains_its_source_beyond_the_text_budget(self, query, embedding):
        rows = [{'chunk_id': f'text-{i}', 'document_name': 'Pressure.pdf', 'machine_name': 'Unit_X',
                 'page_number': 1, 'content': 'Pressure gauge inspection.', 'content_type': 'paragraph',
                 'distance': 0.1, 'images': [], 'links': []} for i in range(20)]
        rows.append({**rows[0], 'chunk_id': 'figure-source', 'content_type': 'image',
                     'content': 'Pressure gauge diagram.',
                     'images': [{'image_id': 'gauge', 'image_path': 'assets/gauge.png'}]})
        query.side_effect = [[rows[0]], rows]
        bundle = manual_search.search_manual('pressure gauge', wants_visual=True)
        self.assertEqual(len(bundle['chunks']), 21)
        self.assertEqual([image['chunk_id'] for image in bundle['images']], ['figure-source'])
        self.assertIn('figure-source', {row['chunk_id'] for row in bundle['chunks']})

    def test_manual_reference_parser_normalizes_ocr_spaced_section_number(self):
        references = manual_search.manual_references([{'content':
            'CORRECTIVE ACTION: Use the correct teeth per inch. Refer to section '
            '4 .5 HAB Saw Blade Selection of the Operation chapter for more information.'}])

        self.assertEqual(references, [{
            'number': '4.5', 'title': 'hab saw blade selection',
            'chapter': 'operation'}])

    @patch('app.retrieval.manual_search.get_embedding', return_value=[0.1])
    @patch('app.retrieval.manual_search.query')
    def test_media_match_uses_figure_context_not_chapter_filename(self, query, embedding):
        seed = {'chunk_id':'level', 'machine_name':'Press Custom_07',
                'document_name':'Maintenance.pdf', 'page_number':1,
                'content':'Check hydraulic oil level.', 'content_type':'paragraph',
                'distance':0.1, 'images':[], 'links':[]}
        def figure(identifier, text):
            return {**seed, 'chunk_id':identifier, 'content_type':'image',
                    'content':('Machine: Press Custom_07\nDocument: Maintenance.pdf\n'
                               f'Page: 1\nImage ID: {identifier}\nRelated text candidates: {text}'),
                    'images':[{'image_id':identifier, 'image_path':f'assets/{identifier}.png'}]}
        query.side_effect = [[seed], [seed, figure('tank', 'Hydraulic oil gauge.'),
                                     figure('other', 'Shows a full view of the workbench.')]]
        result = manual_search.search_manual(
            'The hydraulic oil gauge shows less than three-quarters full. '
            'What maintenance procedure should I follow?', wants_visual=True)
        self.assertEqual([image['image_id'] for image in result['images']], ['tank'])

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
