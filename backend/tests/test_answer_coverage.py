import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.agent.coverage import CoverageItem, EvidenceReview, contains_term, coverage_errors, review_evidence, merge_bundles
from app.agent.repair_workflow import manufacturer_support
from app.responses.schemas import AgentAnswer, AnswerBlock, RepairStep
from app.retrieval.manual_search import machine_names_in_question


def answer(steps=(), blocks=(), complete=True):
    return AgentAnswer(repair_step=steps[0] if steps else None,
        remaining_repair_steps=list(steps[1:]), blocks=list(blocks),
        image_descriptions=[], relevant_asset_ids=[], supported=True, finished=False,
        repair_plan_complete=complete)


class CoverageTests(unittest.TestCase):
    def test_ocr_spacing_and_fraction_typography_preserve_the_same_manual_detail(self):
        self.assertTrue(contains_term('The front vise will only open', 'TheFrontVisewillonlyopen'))
        self.assertTrue(contains_term('Use ISO32 oil at 12 bar.', 'ISO 32'))
        self.assertTrue(contains_term('Minimum level is ¾ full.', '3/4'))
        self.assertTrue(contains_term('Ifthelevelisbelow¾fullyouwillneed', 'below ¾ full'))
        self.assertFalse(contains_term('Use ISO 320 oil.', 'ISO 32'))
        self.assertFalse(contains_term('Pressure is 120 bar.', '12 bar'))
        self.assertFalse(contains_term('Use ISO 46 oil.', 'ISO 32'))
        self.assertFalse(contains_term('Pressure is 1 2 bar.', '12 bar'))

    def test_claiming_complete_does_not_hide_missing_manual_actions(self):
        review = EvidenceReview(review_complete=True, items=[
            CoverageItem(id='indicator', kind='action', description='Inspect the indicator.', evidence_ids=['operation']),
            CoverageItem(id='control', kind='action', description='Check the closing control.', evidence_ids=['controls']),
            CoverageItem(id='supply', kind='action', description='Inspect the supply.', evidence_ids=['installation']),
        ])
        draft = answer([RepairStep(instruction='Inspect the indicator.', evidence_ids=['operation'], coverage_ids=['indicator'])])
        errors = coverage_errors(draft, review, True)
        self.assertTrue(any('Missing control' in error for error in errors))
        self.assertTrue(any('Missing supply' in error for error in errors))

    def test_false_plan_complete_flag_is_respected(self):
        draft = answer([RepairStep(instruction='Inspect the indicator.', evidence_ids=['row'])], complete=False)
        self.assertTrue(coverage_errors(draft, EvidenceReview(review_complete=True), True))

    def test_one_source_can_require_several_distinct_actions(self):
        review = EvidenceReview(review_complete=True, items=[
            CoverageItem(id=key, kind='action', description=key, evidence_ids=['same-table'])
            for key in ['inspect', 'adjust']])
        collapsed = answer([RepairStep(instruction='Inspect the setting.', evidence_ids=['same-table'],
                                      coverage_ids=['inspect', 'adjust'])])
        self.assertTrue(any('split distinct' in error for error in coverage_errors(collapsed, review, True)))
        complete = answer([RepairStep(instruction='Inspect the setting.', evidence_ids=['same-table'], coverage_ids=['inspect']),
                           RepairStep(instruction='Adjust the setting.', evidence_ids=['same-table'], coverage_ids=['adjust'])])
        self.assertEqual(coverage_errors(complete, review, True), [])

    def test_compound_model_step_is_normalized_into_ordered_manual_actions(self):
        from app.agent.chat_service import _split_checklist_actions
        review = EvidenceReview(review_complete=True, items=[
            CoverageItem(id='check_wheel_balance', kind='action',
                         description='Check the wheel balance.', evidence_ids=['manual']),
            CoverageItem(id='check_lubrication_level', kind='action',
                         description='Check the lubrication level.', evidence_ids=['manual']),
        ])
        draft = answer([RepairStep(step_key='maintenance',
            instruction='Check the wheel balance and check the lubrication level.',
            evidence_ids=['manual'], coverage_ids=['check_wheel_balance', 'check_lubrication_level'])])
        normalized = _split_checklist_actions(draft, review)
        steps = [normalized.repair_step, *normalized.remaining_repair_steps]
        self.assertEqual([step.coverage_ids for step in steps],
                         [['check_wheel_balance'], ['check_lubrication_level']])
        self.assertEqual([step.instruction for step in steps],
                         ['Check the wheel balance.', 'Check the lubrication level.'])
        self.assertEqual(coverage_errors(normalized, review, True), [])

    def test_compound_step_is_split_when_optional_checklist_review_is_disabled(self):
        from app.agent.chat_service import _split_checklist_actions
        draft = answer([RepairStep(step_key='inspection',
            instruction='Inspect the wheel guard and check the electric panel breakers.',
            evidence_ids=['manual'])])
        normalized = _split_checklist_actions(draft, EvidenceReview(review_complete=True))
        steps = [normalized.repair_step, *normalized.remaining_repair_steps]
        self.assertEqual([step.instruction for step in steps], [
            'Inspect the wheel guard', 'check the electric panel breakers'])
        self.assertFalse(any(' and ' in step.instruction.lower() for step in steps))

    def test_values_and_table_rows_must_actually_be_displayed(self):
        review = EvidenceReview(review_complete=True, items=[CoverageItem(id='table', kind='table',
            description='Original table', evidence_ids=['table'], exact_terms=['12 bar', 'ISO 32'])])
        draft = answer(blocks=[{'type':'text', 'content':'Check the specification.',
                               'evidence_ids':['table'], 'coverage_ids':['table']}])
        self.assertTrue(coverage_errors(draft, review, False))
        draft.blocks = [AnswerBlock(
            type='table', columns=['Pressure', 'Oil'], rows=[['12 bar', 'ISO 32']],
            evidence_ids=['table'], coverage_ids=['table'])]
        self.assertEqual(coverage_errors(draft, review, False), [])

    def test_shortening_an_original_table_cannot_pass_coverage(self):
        review = EvidenceReview(review_complete=True, items=[CoverageItem(id='schedule', kind='table',
            description='Complete schedule', evidence_ids=['schedule'], table_columns=['Task', 'Interval'],
            table_rows=[['Inspect gauge', 'As needed'], ['Lubricate bearing', 'Monthly']])])
        block = AnswerBlock(type='table', columns=['Task', 'Interval'], rows=[['Inspect gauge', 'As needed']],
                            evidence_ids=['schedule'], coverage_ids=['schedule'])
        draft = answer(blocks=[block])
        self.assertTrue(any('rows were omitted' in error for error in coverage_errors(draft, review, False)))
        block.rows.append(['Lubricate bearing', 'Monthly'])
        self.assertEqual(coverage_errors(draft, review, False), [])

    def test_internal_ids_and_different_numbers_do_not_count_as_visible_manual_values(self):
        review = EvidenceReview(review_complete=True, items=[CoverageItem(id='requirement-12', kind='detail',
            description='The documented control number', evidence_ids=['evidence-12'], exact_terms=['12'])])
        block = AnswerBlock(type='text', content='Control 120', evidence_ids=['evidence-12'], coverage_ids=['requirement-12'])
        self.assertTrue(coverage_errors(answer(blocks=[block]), review, False))
        block.content = 'Control 12'
        self.assertEqual(coverage_errors(answer(blocks=[block]), review, False), [])

    def test_independent_action_clauses_are_rejected_but_component_lists_are_allowed(self):
        for instruction in [
            'Verify the requirements, adjust the arms, and press START.',
            'Inspect the indicator and start the machine.',
            'Check the switch. Replace the fuse.',
        ]:
            with self.subTest(instruction=instruction):
                draft = answer([RepairStep(instruction=instruction, evidence_ids=['row'])])
                self.assertTrue(any('independent action' in error for error in coverage_errors(
                    draft, EvidenceReview(review_complete=True), True)))
        for instruction in ['Inspect the pump and motor.', 'If the level is low, refill the reservoir.']:
            draft = answer([RepairStep(instruction=instruction, evidence_ids=['row'])])
            self.assertEqual(coverage_errors(draft, EvidenceReview(review_complete=True), True), [])

    def test_legacy_multi_action_snapshot_can_be_read_but_must_be_replanned(self):
        from app.agent.chat_service import _cache_ready
        old = answer([RepairStep(instruction='Check the indicator and press START.', evidence_ids=['row'])])
        restored = AgentAnswer.model_validate_json(old.model_dump_json())
        self.assertFalse(_cache_ready({'plan_complete': True, 'remaining_answers': [restored]}))

    def test_done_alone_does_not_satisfy_a_required_observation(self):
        review = EvidenceReview(review_complete=True, items=[CoverageItem(id='check', kind='action',
            description='Inspect status.', evidence_ids=['status'], observation_required=True)])
        draft = answer([RepairStep(instruction='Inspect status.', evidence_ids=['status'], coverage_ids=['check'])])
        self.assertTrue(any('observed result' in error for error in coverage_errors(draft, review, True)))

    def test_failed_observation_cannot_reach_an_operation_requiring_a_passing_result(self):
        review = EvidenceReview(review_complete=True, items=[
            CoverageItem(id='check', kind='action', description='Inspect status.', evidence_ids=['status'], observation_required=True),
            CoverageItem(id='start', kind='action', description='Start when permitted.', evidence_ids=['start'], requires_pass_of=['check'])])
        check = RepairStep(step_key='check', instruction='Inspect status.', evidence_ids=['status'], coverage_ids=['check'],
            observation_prompt='What is shown?', branches=[{'label':'Ready','next_step_key':'start'},
                                                         {'label':'Not ready','next_step_key':'start'}])
        start = RepairStep(step_key='start', instruction='Press START.', evidence_ids=['start'], coverage_ids=['start'],
            requires_observations=[{'step_key':'check', 'allowed_observations':['Ready']}])
        self.assertTrue(any('failing observation' in error for error in coverage_errors(answer([check, start]), review, True)))
        check.branches[1].next_step_key = 'END'
        self.assertEqual(coverage_errors(answer([check, start]), review, True), [])

    def test_runtime_cannot_release_a_step_with_an_unsatisfied_observation_prerequisite(self):
        from app.agent.chat_service import _apply_repair_step
        draft = answer([RepairStep(step_key='operate', instruction='Press START.', evidence_ids=['operation'],
            requires_observations=[dict(step_key='observe', allowed_observations=['Ready'])])])
        workflow = {'completed_steps':[{'step_key':'observe', 'observation':'Not ready', 'instruction':'Observe indicator.'}]}
        with self.assertRaisesRegex(ValueError, 'observed result'):
            _apply_repair_step(draft, workflow)
        self.assertNotIn('current_step', workflow)

    def test_machine_names_of_any_format_are_matched_from_dataset(self):
        documents = [{'machine_name':name} for name in ['Press Unit_X', 'Machine A', 'Z7', 'Other']]
        self.assertEqual(machine_names_in_question('Troubleshoot PRESS UNIT_X please', documents), ['Press Unit_X'])
        self.assertEqual(machine_names_in_question('Compare Machine A and Z7', documents), ['Machine A', 'Z7'])
        self.assertEqual(machine_names_in_question('Z70', documents), [])
        self.assertEqual(machine_names_in_question('Help with Machine A-Pro', [
            {'machine_name':'Machine A'}, {'machine_name':'Machine A-Pro'}]), ['Machine A-Pro'])

    def test_contacts_are_taken_from_current_machine_evidence_only(self):
        chunks = [
            {'machine_name':'New Unit', 'document_name':'Service.pdf', 'page_number':8,
             'content':'Contact service: help@example.com. Telephone: +44 1234 567890'},
            {'machine_name':'Other', 'document_name':'Other.pdf', 'page_number':1,
             'content':'Contact service: wrong@example.com'},
        ]
        support = manufacturer_support('New Unit', chunks)
        self.assertEqual(support['manufacturer'], 'New Unit')
        self.assertEqual(len(support['contacts']), 2)
        self.assertNotIn('wrong@example.com', json.dumps(support))
        self.assertIsNone(manufacturer_support('Unknown', chunks))
        self.assertIsNone(manufacturer_support('HAB-3232'))

    def test_review_rejects_invented_values_and_out_of_dataset_machine_names(self):
        bundle = {'chunks':[{'chunk_id':'c', 'machine_name':'Unit_X', 'document_name':'Manual',
                             'page_number':1, 'content':'Specified pressure is 12 bar.'}], 'images':[], 'links':[]}
        client = Mock()
        review = dict(review_complete=True, machine_names=['Unit_X'], items=[dict(
            id='pressure', kind='detail', description='Pressure', evidence_ids=['review-1'], exact_terms=['99 bar'])])
        client.models.generate_content.return_value = SimpleNamespace(text=json.dumps(review))
        with patch('app.agent.coverage.model_client', return_value=client), self.assertRaisesRegex(ValueError, 'invented'):
            review_evidence('pressure', bundle, False)
        review['machine_names'] = ['Wrong machine']
        client.models.generate_content.return_value = SimpleNamespace(text=json.dumps(review))
        with patch('app.agent.coverage.model_client', return_value=client), self.assertRaisesRegex(ValueError, 'unknown machine'):
            review_evidence('pressure', bundle, False)
        client.close.assert_called()

    def test_supplemental_search_results_merge_without_mutating_original_evidence(self):
        original = {'chunks':[{'chunk_id':'a'}], 'images':[], 'links':[], 'warnings':[]}
        saved = deepcopy(original)
        merged = merge_bundles([original, {'chunks':[{'chunk_id':'b'}], 'images':[], 'links':[], 'warnings':[]}])
        self.assertEqual({row['chunk_id'] for row in merged['chunks']}, {'a','b'})
        self.assertEqual(original, saved)

    def test_final_review_retains_valid_findings_when_it_requests_more_searches(self):
        bundle = {'chunks':[{'chunk_id':'spec', 'machine_name':'Custom Press_12',
                  'content':'Supply pressure is 12 bar.'}], 'images':[], 'links':[]}
        client = Mock()
        client.models.generate_content.return_value = SimpleNamespace(text=json.dumps(dict(
            review_complete=True, machine_names=['Custom Press_12'], items=[dict(
                id='pressure', kind='detail', description='Supply pressure',
                evidence_ids=['review-1'], exact_terms=['12 bar'])],
            additional_searches=[dict(query='supply inspection', evidence_ids=['review-1'])])))
        with patch('app.agent.coverage.model_client', return_value=client):
            result = review_evidence('pressure', bundle, False, allow_search=False)
        self.assertFalse(result.review_complete)
        self.assertEqual(result.additional_searches, [])
        self.assertEqual(result.items[0].evidence_ids, ['spec'])
        draft = answer(blocks=[dict(type='text', content='Supply pressure is 12 bar.',
                       evidence_ids=['spec'], coverage_ids=['pressure'])])
        self.assertEqual(coverage_errors(draft, result, False, require_review=False), [])
