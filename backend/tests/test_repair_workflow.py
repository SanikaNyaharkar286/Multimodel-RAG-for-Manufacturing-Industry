import json
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import config
from app.main import app
from app.sessions.session_store import store
from app.responses.schemas import QueryPlan, RepairAction, RepairStep
from app.agent.chat_service import answer_question, _model_fallback_response, _model_evidence_chunks, cached_step_previews
from app.agent.repair_workflow import SAFETY_WARNING, new_workflow, validate_action, resolve_report
from app.agent.coverage import EvidenceReview


BUNDLE = {'chunks': [{'chunk_id': 'chunk-1', 'machine_name': 'Haas VF-2',
                     'document_name': 'Manual', 'page_number': 1,
                     'content': 'Approved procedure evidence'}], 'images': [], 'links': []}


def plan(**changes):
    return QueryPlan(**(dict(query='spindle overheating', topic='Spindle overheating',
        follow_up=False, repair=True, guided=False, advance=False, wants_visual=False,
        wants_video=False, wants_sources=False, clarify='', out_of_scope=False,
        model_mismatch=False) | changes))


def action(workflow, kind='done', **details):
    return RepairAction(action=kind, workflow_id=workflow['workflow_id'],
                        step_id=(workflow.get('current_step') or {}).get('step_id', ''), **details)


class RepairTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.entry = SimpleNamespace(id='session', user_id='user', history=[], repair_workflow=None, assets={})

    async def ask(self, instruction='Inspect the lubrication indicator.', selected_plan=None,
                  repair_action=None, supported=True, evidence='evidence-1', finished=False,
                  blocks=None, remaining=None, start_new_case=False, question='Help with spindle overheating',
                  bundle=None, media=None, step_details=None, image_descriptions=None, relevant_assets=None,
                  review=None, generated_answers=None, review_sequence=None, search_results=None,
                  coverage_review_enabled=None):
        sent = []
        messages = []
        async def run(**kwargs):
            messages.append(kwargs['new_message'])
            sent.append(json.loads(kwargs['new_message'].parts[0].text))
            answer = dict(blocks=blocks if blocks is not None else [dict(type='text',
                          content='Do not render extra repair actions.', evidence_ids=['evidence-1'])], image_descriptions=image_descriptions or [],
                          relevant_asset_ids=relevant_assets or [], supported=supported, finished=finished,
                          repair_step=dict(instruction=instruction, evidence_ids=[evidence], **(step_details or {})) if instruction else None,
                          remaining_repair_steps=remaining or [], repair_plan_complete=True)
            if generated_answers:
                answer.update(generated_answers.pop(0))
            yield SimpleNamespace(is_final_response=lambda: True,
                                  content=SimpleNamespace(parts=[SimpleNamespace(text=json.dumps(answer), thought=False)]))
        with (
            patch.object(config, 'MANUAL_COVERAGE_REVIEW_ENABLED',
                         coverage_review_enabled if coverage_review_enabled is not None
                         else review is not None or review_sequence is not None),
            patch('app.agent.chat_service.store.state', new=AsyncMock(return_value={})),
            patch('app.agent.chat_service.store.get', new=AsyncMock()),
            patch('app.agent.chat_service.store.update_state', new=AsyncMock()),
            patch('app.agent.safety.select_safety', return_value=SimpleNamespace(precaution_ids=[])),
            patch('app.agent.chat_service.review_evidence', return_value=review or EvidenceReview(review_complete=True), side_effect=review_sequence) as reviewer,
            patch('app.agent.chat_service.understand', return_value=selected_plan or plan()) as planner,
            patch('app.agent.chat_service.search_manual', return_value=deepcopy(bundle or BUNDLE), side_effect=search_results),
            patch('app.agent.chat_service.prepare_media', return_value=media or {'images': [], 'links': [], 'warnings': []}),
            patch('app.agent.chat_service.runner.run_async', side_effect=run) as runner,
            patch('app.agent.chat_service.lite_runner.run_async', side_effect=run) as lite_runner,
        ):
            response, _ = await answer_question(self.entry, question, None, repair_action, start_new_case)
        # Return whichever final-answer runner was selected by the service so
        # call-count assertions remain independent of model routing.
        selected_runner = lite_runner if lite_runner.called else runner
        self.last_review_calls = reviewer.call_count
        self.last_model_messages = messages
        return response, sent, planner, selected_runner

    async def test_original_flow_skips_auxiliary_review_without_claiming_verified_coverage(self):
        first, sent, _, runner = await self.ask(coverage_review_enabled=False,
            remaining=[dict(instruction='Inspect the supply.', evidence_ids=['evidence-1'])])
        self.assertEqual(self.last_review_calls, 0)
        self.assertEqual(runner.call_count, 1)
        self.assertFalse(sent[0]['EVIDENCE_CHECKLIST']['enabled'])
        self.assertFalse(sent[0]['EVIDENCE_CHECKLIST']['review_complete'])
        self.assertEqual(sent[0]['EVIDENCE_CHECKLIST']['items'], [])
        self.assertFalse(first['coverage_verified'])
        self.assertTrue(self.entry.repair_cache['plan_complete'])
        second, sent, planner, runner = await self.ask(repair_action=action(first['repair_workflow']))
        self.assertIn('Inspect the supply.', second['blocks'][0]['content'])
        self.assertEqual(self.last_review_calls, 0)
        self.assertEqual(sent, [])
        planner.assert_not_called()
        runner.assert_not_called()

    async def test_every_supplied_image_reaches_the_model_without_mutating_evidence(self):
        media = {'images': [{'id': f'image-{i}', 'chunk_id': 'chunk-1', 'bytes': f'pixels-{i}'.encode()}
                            for i in range(6)], 'links': [], 'warnings': []}
        before = deepcopy(media)
        _, sent, _, _ = await self.ask(media=media, coverage_review_enabled=False)
        self.assertEqual([item['id'] for item in sent[0]['allowed_images']], [f'image-{i}' for i in range(6)])
        pixels = [part.inline_data.data for part in self.last_model_messages[0].parts if part.inline_data]
        self.assertEqual(pixels, [f'pixels-{i}'.encode() for i in range(6)])
        self.assertEqual(media, before)
        self.assertTrue(sent[0]['APPROVED_EVIDENCE'])

    async def test_multi_diagram_question_sends_every_original_reference_without_downloads(self):
        from app.media.manual_assets import manual_reference
        media = {'images': [{'id': f'image-{i}', 'chunk_id': 'chunk-1', 'bytes': b'pixels'}
                            for i in range(25)], 'links': [], 'warnings': []}
        for i, image in enumerate(media['images']):
            del image['bytes']
            image['reference'] = manual_reference(f'assets/figure-{i}.png', 'Manual', 'Haas VF-2')
        with patch('app.media.manual_assets.storage_client', side_effect=AssertionError('Chat must not fetch image bytes')):
            _, sent, _, _ = await self.ask(media=media)
        self.assertEqual(len(sent[0]['allowed_images']), 25)
        parts = self.last_model_messages[0].parts
        self.assertEqual(sum(part.file_data is not None for part in parts), 25)
        self.assertFalse(any(part.inline_data is not None for part in parts))
        self.assertEqual(parts[-1].file_data.file_uri, media['images'][-1]['reference']['uri'])

    async def test_every_model_selected_diagram_is_displayed_with_source_and_description(self):
        from app.media.manual_assets import manual_reference
        media = {'images': [{'id': f'image-{i}', 'chunk_id': 'chunk-1',
                             'reference': manual_reference(f'assets/{i}.png', 'Manual', 'Haas VF-2')}
                            for i in range(14)], 'links': [], 'warnings': []}
        blocks = [dict(type='text', content='Complete supported explanation.', evidence_ids=['evidence-1'])]
        blocks += [dict(type='image', asset_id=image['id'], caption=f'Required diagram {i}')
                   for i, image in enumerate(media['images'])]
        descriptions = [dict(asset_id=image['id'], description=f'- Description: Required original figure {i}.\n- Technician check: Identify the documented component.')
                        for i, image in enumerate(media['images'])]
        with patch('app.media.manual_assets.storage_client', side_effect=AssertionError('Do not download chat images')):
            response, sent, _, _ = await self.ask(instruction=None, selected_plan=plan(repair=False),
                media=media, blocks=blocks, relevant_assets=[item['id'] for item in media['images']], image_descriptions=descriptions)
        images = [block for block in response['blocks'] if block['type'] == 'image']
        self.assertEqual(len(images), 14)
        self.assertTrue(all(item['src'].startswith('/images/') and item['source']['document_name'] == 'Manual' for item in images))
        self.assertTrue(all(item['description'] for item in images))
        self.assertEqual(len(sent[0]['allowed_images']), 14)

    async def test_missing_checklist_action_is_repaired_before_caching_and_done_stays_local(self):
        review = EvidenceReview(review_complete=True, items=[
            dict(id='indicator', kind='action', description='Inspect indicator.', evidence_ids=['chunk-1']),
            dict(id='supply', kind='action', description='Inspect supply.', evidence_ids=['chunk-1'])])
        first, sent, _, runner = await self.ask('Inspect the indicator.', review=review,
            step_details=dict(step_key='indicator', coverage_ids=['indicator']),
            remaining=[dict(step_key='supply', instruction='Inspect the supply.', evidence_ids=['evidence-1'], coverage_ids=['supply'])],
            generated_answers=[{'remaining_repair_steps': []}, {}])
        self.assertEqual(runner.call_count, 2)
        self.assertEqual(len(sent[0]['EVIDENCE_CHECKLIST']['items']), 2)
        self.assertEqual([item['coverage_id'] for item in sent[0]['REQUIRED_STEP_OUTLINE']],
                         ['indicator', 'supply'])
        self.assertTrue(self.entry.repair_cache['plan_complete'])
        self.assertEqual(self.entry.repair_cache['coverage_requirement_count'], 2)
        self.assertNotIn('Inspect the supply.', json.dumps(first))
        second, sent, planner, runner = await self.ask(repair_action=action(first['repair_workflow']))
        self.assertIn('Inspect the supply.', second['blocks'][0]['content'])
        self.assertEqual(sent, [])
        planner.assert_not_called()
        runner.assert_not_called()

    async def test_incomplete_plan_is_never_marked_complete_when_correction_still_fails(self):
        with self.assertRaisesRegex(ValueError, 'coverage validation'):
            await self.ask(generated_answers=[{'repair_plan_complete': False}, {'repair_plan_complete': False}])
        self.assertFalse((self.entry.repair_cache or {}).get('plan_complete', False))
        self.assertIsNone(self.entry.repair_workflow)

    async def test_optional_review_failure_preserves_the_grounded_step_answer_and_cache(self):
        response, sent, _, runner = await self.ask(review_sequence=[ValueError('Review incomplete')],
            remaining=[dict(instruction='Inspect the supply.', evidence_ids=['evidence-1'])])
        self.assertFalse(response['coverage_verified'])
        self.assertEqual(response['repair_workflow']['status'], 'awaiting_done')
        self.assertIn('Inspect the lubrication indicator.', response['blocks'][0]['content'])
        self.assertFalse(self.entry.repair_cache['plan_complete'])
        self.assertTrue(self.entry.repair_cache['plan_ready'])
        self.assertEqual(len(sent), 1)
        self.assertEqual(runner.call_count, 1)
        self.assertEqual(len(cached_step_previews(self.entry)), 1)
        next_response, sent, planner, runner = await self.ask(repair_action=action(response['repair_workflow']))
        self.assertIn('Inspect the supply.', next_response['blocks'][0]['content'])
        self.assertFalse(next_response['repair_workflow']['coverage_verified'])
        self.assertEqual(sent, [])
        planner.assert_not_called()
        runner.assert_not_called()

    async def test_optional_review_failure_does_not_allow_outside_evidence(self):
        with self.assertRaisesRegex(ValueError, 'unknown evidence'):
            await self.ask(review_sequence=[ValueError('Review incomplete')], evidence='outside-knowledge')
        self.assertIsNone(self.entry.repair_workflow)

    async def test_manual_explanation_without_a_repair_procedure_does_not_invent_actions(self):
        review = EvidenceReview(review_complete=True, items=[dict(
            id='limitation', kind='detail', description='The manual records a prerequisite but no repair.',
            evidence_ids=['chunk-1'])])
        response, _, _, _ = await self.ask(instruction=None, review=review,
            blocks=[dict(type='text', content='A starting prerequisite is documented; a repair procedure is not.',
                         evidence_ids=['evidence-1'], coverage_ids=['limitation'])])
        self.assertNotIn('repair_workflow', response)
        self.assertIn('prerequisite', response['blocks'][0]['content'])
        self.assertTrue(response['coverage_verified'])

    async def test_evidence_dependency_expands_search_before_generating_an_information_answer(self):
        initial = {'chunks':[{'chunk_id':'operation', 'machine_name':'Press Unit_X', 'document_name':'Operation.pdf',
                             'page_number':2, 'content':'Clamping requires the supply to be on.'}], 'images':[], 'links':[]}
        extra = {'chunks':[{'chunk_id':'controls', 'machine_name':'Press Unit_X', 'document_name':'Controls.pdf',
                           'page_number':4, 'content':'Control 12 enables the supply.'}], 'images':[], 'links':[]}
        initial_review = EvidenceReview(review_complete=True, machine_names=['Press Unit_X'],
            additional_searches=[dict(query='Press Unit_X supply controls', evidence_ids=['operation'])])
        final_review = EvidenceReview(review_complete=True, machine_names=['Press Unit_X'], items=[
            dict(id='condition', kind='detail', description='Supply prerequisite', evidence_ids=['operation']),
            dict(id='table', kind='table', description='Supply control table', evidence_ids=['controls'], exact_terms=['12'])])
        response, sent, _, _ = await self.ask(instruction=None,
            selected_plan=plan(repair=False, query='Press Unit_X clamping requirements'),
            bundle=initial, search_results=[initial, extra], review_sequence=[initial_review, final_review],
            blocks=[dict(type='text', content='Clamping requires the supply to be on.',
                         evidence_ids=['evidence-1'], coverage_ids=['condition']),
                    dict(type='table', columns=['Control'], rows=[['12']],
                         evidence_ids=['evidence-2'], coverage_ids=['table'])])
        self.assertEqual(response['blocks'][1]['rows'], [['12']])
        self.assertEqual(len(sent[0]['APPROVED_EVIDENCE']), 2)
        self.assertEqual(sent[0]['EVIDENCE_CHECKLIST']['machine_names'], ['Press Unit_X'])
        self.assertNotIn('repair_workflow', response)

    async def test_first_repair_is_one_step_with_safety_even_without_guided_flag(self):
        response, sent, _, _ = await self.ask(finished=True)
        self.assertEqual(response['safety_warning'], SAFETY_WARNING)
        self.assertEqual(response['blocks'][0], {'type': 'text', 'content': '### Step 1\nInspect the lubrication indicator.'})
        self.assertEqual(response['sources'][0]['page_number'], 1)
        self.assertTrue(sent[0]['workflow']['REQUEST_STEP'])
        self.assertEqual(response['repair_workflow']['status'], 'awaiting_done')
        self.assertNotIn('maintenance_report', response)

    async def test_model_failure_fallback_keeps_repair_step_flow(self):
        workflow = new_workflow('Saw blade shifting', [{**BUNDLE['chunks'][0],
            'content': 'SYMPTOM: Blade shifts. CORRECTIVE ACTION: Loosen the black knobs to sandwich the blade between the carbide guides.'}])
        self.entry.repair_workflow = workflow
        result = _model_fallback_response({'images': [], 'links': [], 'warnings': []},
            {'chunks': [{**BUNDLE['chunks'][0],
                         'content': 'SYMPTOM: Blade shifts. CORRECTIVE ACTION: Loosen the black knobs to sandwich the blade between the carbide guides.'}]},
            self.entry, True)
        self.assertIn('### Step 1', result['blocks'][0]['content'])
        self.assertEqual(result['repair_workflow']['status'], 'awaiting_done')
        self.assertEqual(result['repair_workflow']['current_step']['instruction'],
                         'Loosen the black knobs to sandwich the blade between the carbide guides')

    async def test_done_advances_once_and_stale_click_is_rejected(self):
        first, _, _, _ = await self.ask(remaining=[dict(instruction='Inspect the manual-specified filter.', evidence_ids=['evidence-1'])])
        clicked = action(first['repair_workflow'])
        second, sent, planner, _ = await self.ask('Inspect the manual-specified filter.', repair_action=clicked)
        self.assertIn('Step 2', second['blocks'][0]['content'])
        self.assertEqual(len(second['repair_workflow']['completed_steps']), 1)
        planner.assert_not_called()
        self.assertEqual(sent, [])
        with self.assertRaises(HTTPException) as rejected:
            await self.ask('Another instruction.', repair_action=clicked)
        self.assertEqual(rejected.exception.status_code, 409)

    async def test_done_uses_private_validated_plan_without_model(self):
        first, _, _, _ = await self.ask(
            remaining=[dict(instruction='Inspect the manual-specified filter.', evidence_ids=['evidence-1'])]
        )
        clicked = action(first['repair_workflow'])
        second, sent, planner, runner = await self.ask(
            'This text is ignored for a server-controlled Done action.', repair_action=clicked
        )
        self.assertIn('Step 2', second['blocks'][0]['content'])
        self.assertEqual(len(second['repair_workflow']['completed_steps']), 1)
        planner.assert_not_called()
        runner.assert_not_called()
        self.assertEqual(sent, [])

    async def test_browser_preview_uses_prepared_result_without_recording_completion(self):
        first, _, _, _ = await self.ask(remaining=[dict(step_key='next',
            instruction='Inspect the manual-specified filter.', evidence_ids=['evidence-1'])])
        workflow = deepcopy(self.entry.repair_workflow)
        cache = deepcopy(self.entry.repair_cache)
        with patch('app.agent.chat_service.final_blocks', side_effect=AssertionError('Must use cache')):
            previews = cached_step_previews(self.entry)
        self.assertEqual(len(previews), 1)
        self.assertEqual(previews[0]['step_id'], workflow['current_step']['step_id'])
        self.assertEqual(previews[0]['response']['blocks'][0]['content'], '### Step 2\nInspect the manual-specified filter.')
        self.assertEqual(self.entry.repair_workflow, workflow)
        self.assertEqual(self.entry.repair_cache, cache)
        actual, sent, _, _ = await self.ask(repair_action=action(first['repair_workflow']))
        self.assertEqual(actual['blocks'], previews[0]['response']['blocks'])
        self.assertEqual(sent, [])

    async def test_each_step_keeps_its_details_table_and_media_private_until_done(self):
        bundle = {'chunks': [
            {'chunk_id': 'c1', 'machine_name': 'Press Unit_X', 'document_name': 'Operation.pdf',
             'page_number': 1, 'content': 'Check the oil level against the 3/4 mark.'},
            {'chunk_id': 'c2', 'machine_name': 'Press Unit_X', 'document_name': 'Operation.pdf',
             'page_number': 1, 'content': 'Inspect the pressure valve. Specified pressure is 12 bar.'}],
            'images': [], 'links': []}
        media = {'images': [
            {'id': 'image-1', 'chunk_id': 'c1', 'bytes': b'level-figure'},
            {'id': 'image-2', 'chunk_id': 'c2', 'bytes': b'valve-figure'},
            {'id': 'unused-candidate', 'chunk_id': 'c2', 'bytes': b'unused-image'}],
            'links': [{'id': 'video-2', 'chunk_id': 'c2', 'url': 'https://example.com/valve',
                       'is_video': True, 'origin': 'pdf_link'}], 'warnings': []}
        descriptions = [dict(asset_id='image-1', description='- Description: Oil level gauge.\n- Technician check: Read the mark.'),
                        dict(asset_id='image-2', description='- Description: Valve gauge.\n- Technician check: Read valve pressure.')]
        with patch('app.responses.response_builder.store.put_image', side_effect=['level', 'valve']):
            first, _, _, _ = await self.ask('Check the oil level.', bundle=bundle, media=media,
                image_descriptions=descriptions, relevant_assets=['image-1', 'image-2', 'video-2'],
                step_details=dict(step_key='level', asset_ids=['image-1'], supporting_blocks=[
                    dict(type='text', content='Compare the level with the 3/4 mark.', evidence_ids=['evidence-1']),
                    dict(type='image', asset_id='image-1', caption='Oil level'),
                    dict(type='table', columns=['Threshold'], rows=[['3/4']], evidence_ids=['evidence-1'])]),
                remaining=[dict(step_key='valve', instruction='Inspect the pressure valve.', evidence_ids=['evidence-2'],
                    asset_ids=['image-2', 'video-2'], supporting_blocks=[
                        dict(type='text', content='Use the documented valve reference.', evidence_ids=['evidence-2']),
                        dict(type='image', asset_id='image-2', caption='Valve pressure'),
                        dict(type='table', columns=['Pressure'], rows=[['12 bar']], evidence_ids=['evidence-2']),
                        dict(type='link', asset_id='video-2', title='Valve video')])])
        self.assertEqual([b['type'] for b in first['blocks']], ['text', 'text', 'image', 'table'])
        self.assertEqual(first['repair_workflow']['machine'], 'Press Unit_X')
        self.assertNotIn('valve', json.dumps(first).lower())
        self.assertNotIn('remaining_repair_steps', json.dumps(first))
        self.assertEqual({item['id'] for item in self.entry.repair_cache['media']['images']}, {'image-1', 'image-2'})
        with patch('app.agent.chat_service.final_blocks', side_effect=AssertionError('Must use pre-rendered next step')):
            second, sent, planner, runner = await self.ask(repair_action=action(first['repair_workflow']))
        self.assertEqual([b['type'] for b in second['blocks']], ['text', 'text', 'image', 'table', 'link'])
        self.assertEqual(second['blocks'][3]['rows'], [['12 bar']])
        self.assertEqual(second['blocks'][2]['src'], '/images/valve')
        self.assertNotIn('Oil level', json.dumps(second['blocks']))
        self.assertEqual(sent, [])
        planner.assert_not_called()
        runner.assert_not_called()

    async def test_invalid_future_support_reference_is_rejected_before_first_step(self):
        with self.assertRaisesRegex(ValueError, 'unknown evidence'):
            await self.ask(remaining=[dict(instruction='Inspect the filter.', evidence_ids=['evidence-1'],
                supporting_blocks=[dict(type='table', columns=['Limit'], rows=[['4']], evidence_ids=['invented'])])])

    async def test_observed_outcome_selects_prepared_step_and_keeps_visible_numbering(self):
        first, _, _, _ = await self.ask('Observe the indicator.',
            step_details=dict(step_key='observe', observation_prompt='What is the indicator state?',
                branches=[dict(label='Off', next_step_key='valve'), dict(label='On', next_step_key='END')]),
            remaining=[
                dict(step_key='filter', instruction='Inspect the filter.', evidence_ids=['evidence-1']),
                dict(step_key='valve', instruction='Inspect the valve.', evidence_ids=['evidence-1'],
                     supporting_blocks=[dict(type='text', content='Only valve-specific details.', evidence_ids=['evidence-1'])])])
        previews = {item['observation']: item['response'] for item in cached_step_previews(self.entry)}
        self.assertEqual(previews['Off']['blocks'][0]['content'], '### Step 2\nInspect the valve.')
        self.assertEqual(previews['On']['repair_workflow']['status'], 'awaiting_resolution')
        self.assertEqual(self.entry.repair_workflow['completed_steps'], [])
        with patch('app.agent.chat_service.final_blocks', side_effect=AssertionError('Must use prepared branch')):
            second, sent, planner, runner = await self.ask(
                repair_action=action(first['repair_workflow'], observation='Off'))
        self.assertEqual(second['blocks'][0]['content'], '### Step 2\nInspect the valve.')
        self.assertEqual(second['blocks'][1]['content'], 'Only valve-specific details.')
        self.assertNotIn('filter', json.dumps(second).lower())
        self.assertEqual(sent, [])
        planner.assert_not_called()
        runner.assert_not_called()

    async def test_report_issue_stays_in_workflow_and_requests_reassessment(self):
        first, _, _, _ = await self.ask()
        clicked = action(first['repair_workflow'], 'report_issue',
                         issue_report='The indicator is still showing zero pressure.')
        second, sent, planner, runner = await self.ask(
            'Inspect the pressure supply for the reported zero reading.', repair_action=clicked)
        self.assertIn('Step 1', second['blocks'][0]['content'])
        self.assertEqual(len(second['repair_workflow']['completed_steps']), 0)
        self.assertEqual(len(second['repair_workflow']['reported_issues']), 1)
        self.assertIn('zero pressure', second['repair_workflow']['reported_issues'][0]['report'])
        self.assertEqual(second['repair_workflow']['current_step']['instruction'],
                         'Inspect the pressure supply for the reported zero reading.')
        planner.assert_not_called()
        self.assertEqual(len(sent), 1)
        self.assertEqual(runner.call_count, 1)

    async def test_typed_next_step_does_not_advance(self):
        await self.ask()
        first = deepcopy(self.entry.repair_workflow)
        response, sent, _, _ = await self.ask(None, selected_plan=plan(follow_up=True, advance=True))
        self.assertFalse(sent[0]['workflow']['ADVANCE'])
        self.assertFalse(sent[0]['workflow']['REQUEST_STEP'])
        self.assertEqual(response['repair_workflow'], first)

    async def test_invalid_next_step_leaves_previous_step_retryable(self):
        await self.ask()
        previous = deepcopy(self.entry.repair_workflow)
        with self.assertRaises(ValueError):
            await self.ask('Inspect filter.', repair_action=action(previous, 'report_issue', issue_report='New evidence'), evidence='invented')
        self.assertEqual(self.entry.repair_workflow, previous)

    async def test_model_cannot_supply_an_unacknowledged_next_step(self):
        await self.ask()
        previous = deepcopy(self.entry.repair_workflow)
        with self.assertRaises(ValueError):
            await self.ask('Inspect another component.', selected_plan=plan(follow_up=True))
        self.assertEqual(self.entry.repair_workflow, previous)

    async def test_no_supported_next_step_does_not_claim_resolution(self):
        await self.ask()
        response, _, _, _ = await self.ask(None, repair_action=action(self.entry.repair_workflow), supported=False)
        self.assertEqual(response['repair_workflow']['status'], 'awaiting_resolution')
        self.assertEqual(len(response['repair_workflow']['completed_steps']), 1)
        self.assertNotIn('maintenance_report', response)

    async def test_confirmed_resolution_creates_exact_report_without_model(self):
        await self.ask()
        response, sent, planner, runner = await self.ask(repair_action=action(
            self.entry.repair_workflow, 'resolve', machine='Haas VF-2', issue='Spindle overheating',
            cause='Lubrication blockage', action_taken='Cleaned lubrication line'))
        report = response['maintenance_report']
        self.assertEqual({k: report[k] for k in ['machine', 'issue', 'cause', 'action_taken', 'status']},
                         dict(machine='Haas VF-2', issue='Spindle overheating', cause='Lubrication blockage',
                              action_taken='Cleaned lubrication line', status='Technician reported resolved'))
        planner.assert_not_called()
        runner.assert_not_called()
        self.assertEqual(sent, [])
        self.assertEqual(report['completed_steps'], [])  # Proposed action was never marked Done.
        with self.assertRaises(HTTPException):
            await self.ask(repair_action=action(self.entry.repair_workflow, 'resolve'))

    async def test_new_issue_is_answered_automatically_and_archives_previous_case(self):
        await self.ask()
        old = action(self.entry.repair_workflow)
        response, _, _, _ = await self.ask('Inspect the new issue indicator.',
                                          selected_plan=plan(topic='Different issue'))
        self.assertNotEqual(response['repair_workflow']['workflow_id'], old.workflow_id)
        self.assertNotIn('case_conflict', response)
        self.assertEqual(self.entry.cases[old.workflow_id]['current_step']['step_id'], old.step_id)
        self.assertIn('Inspect the new issue indicator.', response['blocks'][0]['content'])
        self.assertEqual(response['repair_workflow']['completed_steps'], [])
        with self.assertRaises(HTTPException):
            validate_action(self.entry.repair_workflow, old)

    async def test_repeated_original_question_remains_in_same_case_even_if_planner_says_new(self):
        original='Cutting efficiency is very low. Show the coolant tank or coolant-pump image and tell me how to fix this issue'
        await self.ask(selected_plan=plan(topic='Low cutting efficiency'),question=original)
        previous=deepcopy(self.entry.repair_workflow)
        self.entry.history=[{'role':'user','case_id':previous['workflow_id'],'text':original}]
        response,sent,_,_=await self.ask(None,selected_plan=plan(topic='Low cutting efficiency',follow_up=False),question=original+'?')
        self.assertEqual(response['repair_workflow']['workflow_id'],previous['workflow_id'])
        self.assertEqual(response['repair_workflow']['current_step']['step_id'],previous['current_step']['step_id'])
        self.assertFalse(sent[0]['workflow']['REQUEST_STEP'])
        self.assertNotIn('case_conflict',response)

    async def test_information_only_has_no_safety_or_workflow(self):
        response, _, _, _ = await self.ask(None, selected_plan=plan(repair=False))
        self.assertNotIn('safety_warning', response)
        self.assertNotIn('repair_workflow', response)

    async def test_explicit_separate_case_preserves_the_earlier_case(self):
        await self.ask()
        earlier=deepcopy(self.entry.repair_workflow)
        self.entry.cases={}
        response,_,_,_=await self.ask('Inspect the new indicator.',selected_plan=plan(topic='New fault'),start_new_case=True)
        self.assertNotEqual(response['repair_workflow']['workflow_id'],earlier['workflow_id'])
        self.assertEqual(self.entry.cases[earlier['workflow_id']],earlier)

    async def test_new_evidence_revises_plan_version_without_completing_current_action(self):
        await self.ask()
        original=deepcopy(self.entry.repair_workflow)
        response,_,_,_=await self.ask('Inspect the approved alternative.',selected_plan=plan(follow_up=True,revise_plan=True))
        self.assertTrue(response['plan_updated'])
        self.assertEqual(response['repair_workflow']['plan_version'],2)
        self.assertEqual(response['repair_workflow']['completed_steps'],original['completed_steps'])
        self.assertEqual(response['repair_workflow']['current_step']['number'],1)

    async def test_full_information_during_repair_does_not_inherit_guided_mode(self):
        await self.ask()
        original = deepcopy(self.entry.repair_workflow)
        blocks = [dict(type='text', content='Complete lubrication explanation.', evidence_ids=['evidence-1']),
                  dict(type='table', columns=['Part', 'Function'], rows=[['Pump', 'Supplies lubricant']],
                       evidence_ids=['evidence-1']),
                  dict(type='text', content='Additional supported details.', evidence_ids=['evidence-1'])]
        response, sent, _, _ = await self.ask(None, blocks=blocks, selected_plan=plan(
            repair=False, guided=True, follow_up=True, topic='Lubrication system functions',
            query='Explain the complete lubrication system'))
        self.assertFalse(sent[0]['workflow']['GUIDED'])
        self.assertFalse(sent[0]['workflow']['REQUEST_STEP'])
        self.assertEqual(len(response['blocks']), 3)
        self.assertEqual(response['blocks'][0]['content'], 'Complete lubrication explanation.')
        self.assertEqual(response['blocks'][1]['type'], 'table')
        self.assertEqual(response['blocks'][2]['content'], 'Additional supported details.')
        self.assertNotIn('safety_warning', response)
        self.assertNotIn('repair_workflow', response)
        self.assertNotIn('maintenance_report', response)
        self.assertEqual(self.entry.repair_workflow, original)

    async def test_unrelated_information_preserves_repair_for_later_done(self):
        await self.ask(remaining=[dict(instruction='Inspect the manual-specified filter.', evidence_ids=['evidence-1'])])
        original = deepcopy(self.entry.repair_workflow)
        response, sent, _, _ = await self.ask(None, selected_plan=plan(
            repair=False, follow_up=False, topic='Machine specifications', query='Machine specifications'))
        self.assertFalse(sent[0]['workflow']['GUIDED'])
        self.assertEqual(self.entry.repair_workflow, original)
        self.assertNotIn('repair_workflow', response)
        resumed, sent, _, _ = await self.ask('Inspect the manual-specified filter.',
                                            repair_action=action(original))
        self.assertIn('Step 2', resumed['blocks'][0]['content'])
        self.assertEqual(sent, [])


class RepairValidationTests(unittest.TestCase):
    def test_relevant_actions_references_and_tables_survive_incidental_context_budget(self):
        rows = [{'chunk_id': f'heading-{i}', 'content': 'Other heading', 'content_type': 'heading'}
                for i in range(80)]
        rows += [
            {'chunk_id': 'table', 'content': 'Pump pressure troubleshooting: check the valve.', 'content_type': 'table'},
            {'chunk_id': 'procedure', 'content': 'Specified pressure 12 bar.', 'retrieval_role': 'reference'},
            {'chunk_id': 'figure', 'content': 'Pressure dial', 'content_type': 'image'},
        ]
        with patch.object(config, 'MODEL_EVIDENCE_MAX_CHUNKS', 8):
            selected, _, count = _model_evidence_chunks({'chunks': rows},
                {'images': [{'chunk_id': 'figure'}], 'links': []}, 'Pump pressure troubleshooting')
        self.assertTrue({'table', 'procedure', 'figure'} <= {row['chunk_id'] for row in selected})
        self.assertEqual(selected, rows)
        self.assertEqual(count, sum(len(row['content']) for row in selected))

    def test_multiple_numbered_or_bulleted_steps_are_rejected(self):
        for content in ['Step 1 inspect. Step 2 clean.', 'Inspect.\nClean.', '1. Inspect 2. Clean', '  ']:
            with self.subTest(content=content), self.assertRaises(ValidationError):
                RepairStep(instruction=content, evidence_ids=['evidence-1'])

    def test_unknown_cause_and_unperformed_action_are_not_fabricated(self):
        workflow = new_workflow('Spindle overheating', BUNDLE['chunks'])
        report = resolve_report(workflow, action(workflow, 'resolve'))['maintenance_report']
        self.assertEqual(report['cause'], 'Not confirmed')
        self.assertEqual(report['action_taken'], 'Not recorded')


class RepairApiTests(unittest.TestCase):
    def test_information_response_does_not_get_active_repair_controls_injected(self):
        with patch.object(config, 'AUTH_ENABLED', False), patch.object(config, 'BQ_API_LOGGING_ENABLED', False):
            with TestClient(app) as client:
                session = client.post('/sessions').json()['session_id']
                store.entries[session].repair_workflow = new_workflow('Spindle overheating', BUNDLE['chunks'])
                with patch('app.api.chat_routes.answer_question', new=AsyncMock(return_value=(
                    {'blocks': [{'type': 'text', 'content': 'Full informational answer.'}]}, False))):
                    response = client.post('/chat', data={'session_id': session, 'message': 'Explain the spindle.'})
                self.assertEqual(response.status_code, 200)
                self.assertNotIn('repair_workflow', response.json())
                self.assertNotIn('safety_warning', response.json())
                self.assertEqual(client.get('/sessions/' + session).json()['repair_workflow']['issue'], 'Spindle overheating')

    def test_resolution_is_recorded_in_session_history_and_stale_request_conflicts(self):
        with patch.object(config, 'AUTH_ENABLED', False), patch.object(config, 'BQ_API_LOGGING_ENABLED', False):
            with TestClient(app) as client:
                session = client.post('/sessions').json()['session_id']
                workflow = new_workflow('Spindle overheating', BUNDLE['chunks'])
                workflow['current_step'] = {'step_id': 'pending', 'number': 1, 'instruction': 'Inspect indicator.'}
                workflow['status'] = 'awaiting_done'
                store.entries[session].repair_workflow = workflow
                data = dict(session_id=session, repair_action='resolve', workflow_id=workflow['workflow_id'],
                            step_id='pending', cause='Lubrication blockage', action_taken='Cleaned lubrication line')
                response = client.post('/chat', data=data)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual(response.json()['maintenance_report']['status'], 'Technician reported resolved')
                history = client.get('/sessions/' + session).json()
                self.assertEqual(history['messages'][-1]['response']['maintenance_report']['cause'], 'Lubrication blockage')
                self.assertEqual(history['repair_workflow']['status'], 'resolved')
                self.assertEqual(client.post('/chat', data=data).status_code, 409)
                self.assertEqual(client.post('/chat', data={'session_id': session, 'repair_action': 'skip'}).status_code, 400)
                client.delete('/sessions/' + session)
                self.assertEqual(client.get('/sessions/' + session).status_code, 410)
