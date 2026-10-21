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
from app.agent.chat_service import answer_question
from app.agent.repair_workflow import SAFETY_WARNING, new_workflow, validate_action, resolve_report


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
        self.entry = SimpleNamespace(id='session', user_id='user', history=[], repair_workflow=None)

    async def ask(self, instruction='Inspect the lubrication indicator.', selected_plan=None,
                  repair_action=None, supported=True, evidence='evidence-1', finished=False,
                  blocks=None):
        sent = []
        async def run(**kwargs):
            sent.append(json.loads(kwargs['new_message'].parts[0].text))
            answer = dict(blocks=blocks if blocks is not None else [dict(type='text',
                          content='Do not render extra repair actions.', evidence_ids=['evidence-1'])], image_descriptions=[],
                          relevant_asset_ids=[], supported=supported, finished=finished,
                          repair_step=dict(instruction=instruction, evidence_ids=[evidence]) if instruction else None)
            yield SimpleNamespace(is_final_response=lambda: True,
                                  content=SimpleNamespace(parts=[SimpleNamespace(text=json.dumps(answer), thought=False)]))
        with (
            patch('app.agent.chat_service.store.state', new=AsyncMock(return_value={})),
            patch('app.agent.chat_service.store.get', new=AsyncMock()),
            patch('app.agent.chat_service.store.update_state', new=AsyncMock()),
            patch('app.agent.chat_service.understand', return_value=selected_plan or plan()) as planner,
            patch('app.agent.chat_service.search_manual', return_value=deepcopy(BUNDLE)),
            patch('app.agent.chat_service.prepare_media', return_value={'images': [], 'links': [], 'warnings': []}),
            patch('app.agent.chat_service.runner.run_async', side_effect=run) as runner,
        ):
            response, _ = await answer_question(self.entry, 'Help with spindle overheating', None, repair_action)
        return response, sent, planner, runner

    async def test_first_repair_is_one_step_with_safety_even_without_guided_flag(self):
        response, sent, _, _ = await self.ask(finished=True)
        self.assertEqual(response['safety_warning'], SAFETY_WARNING)
        self.assertEqual(response['blocks'], [{'type': 'text', 'content': '### Step 1\nInspect the lubrication indicator.'}])
        self.assertTrue(sent[0]['workflow']['REQUEST_STEP'])
        self.assertEqual(response['repair_workflow']['status'], 'awaiting_done')
        self.assertNotIn('maintenance_report', response)

    async def test_done_advances_once_and_stale_click_is_rejected(self):
        first, _, _, _ = await self.ask()
        clicked = action(first['repair_workflow'])
        second, sent, planner, _ = await self.ask('Inspect the manual-specified filter.', repair_action=clicked)
        self.assertIn('Step 2', second['blocks'][0]['content'])
        self.assertEqual(len(second['repair_workflow']['completed_steps']), 1)
        planner.assert_not_called()
        self.assertTrue(sent[0]['workflow']['ADVANCE'])
        with self.assertRaises(HTTPException) as rejected:
            await self.ask('Another instruction.', repair_action=clicked)
        self.assertEqual(rejected.exception.status_code, 409)

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
            await self.ask('Inspect filter.', repair_action=action(previous), evidence='invented')
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
                              action_taken='Cleaned lubrication line', status='Resolved'))
        planner.assert_not_called()
        runner.assert_not_called()
        self.assertEqual(sent, [])
        self.assertEqual(report['completed_steps'], [])  # Proposed action was never marked Done.
        with self.assertRaises(HTTPException):
            await self.ask(repair_action=action(self.entry.repair_workflow, 'resolve'))

    async def test_new_issue_resets_step_and_rejects_old_controls(self):
        await self.ask()
        old = action(self.entry.repair_workflow)
        response, _, _, _ = await self.ask('Inspect the new issue indicator.',
                                          selected_plan=plan(topic='Different issue'))
        self.assertNotEqual(response['repair_workflow']['workflow_id'], old.workflow_id)
        self.assertIn('Step 1', response['blocks'][0]['content'])
        with self.assertRaises(HTTPException):
            validate_action(self.entry.repair_workflow, old)

    async def test_information_only_has_no_safety_or_workflow(self):
        response, _, _, _ = await self.ask(None, selected_plan=plan(repair=False))
        self.assertNotIn('safety_warning', response)
        self.assertNotIn('repair_workflow', response)

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
        await self.ask()
        original = deepcopy(self.entry.repair_workflow)
        response, sent, _, _ = await self.ask(None, selected_plan=plan(
            repair=False, follow_up=False, topic='Machine specifications', query='Machine specifications'))
        self.assertFalse(sent[0]['workflow']['GUIDED'])
        self.assertEqual(self.entry.repair_workflow, original)
        self.assertNotIn('repair_workflow', response)
        resumed, sent, _, _ = await self.ask('Inspect the manual-specified filter.',
                                            repair_action=action(original))
        self.assertIn('Step 2', resumed['blocks'][0]['content'])
        self.assertEqual(sent[0]['workflow']['MAIN_TOPIC'], original['issue'])


class RepairValidationTests(unittest.TestCase):
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
                self.assertEqual(response.json()['maintenance_report']['status'], 'Resolved')
                history = client.get('/sessions/' + session).json()
                self.assertEqual(history['messages'][-1]['response']['maintenance_report']['cause'], 'Lubrication blockage')
                self.assertEqual(history['repair_workflow']['status'], 'resolved')
                self.assertEqual(client.post('/chat', data=data).status_code, 409)
                self.assertEqual(client.post('/chat', data={'session_id': session, 'repair_action': 'skip'}).status_code, 400)
                client.delete('/sessions/' + session)
                self.assertEqual(client.get('/sessions/' + session).status_code, 410)
