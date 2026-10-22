import asyncio
import base64
import io
import json
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from decimal import Decimal
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pymupdf
from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from app import config
from app.agent.chat_service import _advance_cached_plan, validate_branches, answer_question
from app.agent.repair_workflow import new_workflow, validate_action
from app.agent.safety import start_safety, SafetySelection
from app.main import app
from app.responses.documentation import create_document, render_pdf
from app.responses.schemas import AgentAnswer, RepairAction, RepairStep, QueryPlan
from app.sessions.session_store import store, SessionStore
from auth_system.security import Identity, require_approved


def png():
    output = io.BytesIO(); Image.new('RGB', (200, 120), '#2674bf').save(output, format='PNG')
    return output.getvalue()


class CaseApiTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.patches = [patch.object(config, 'BACKEND_ROOT', Path(self.directory.name)),
                        patch.object(config, 'API_LOG_DIRECTORY', Path(self.directory.name) / 'logs'),
                        patch.object(config, 'AUTH_ENABLED', False), patch.object(config, 'BQ_API_LOGGING_ENABLED', False)]
        for item in self.patches: item.start()
        self.identity = Identity('owner-a', 'a@example.com', True)
        app.dependency_overrides[require_approved] = lambda: self.identity
        self.context = TestClient(app); self.client = self.context.__enter__()
        self.session = self.client.post('/sessions').json()['session_id']

    def tearDown(self):
        self.context.__exit__(None, None, None)
        app.dependency_overrides.clear()
        for item in reversed(self.patches): item.stop()
        self.directory.cleanup()

    def seed(self):
        entry = store.entries[self.session]
        workflow = new_workflow('Coolant flow issue', [])
        workflow['current_step'] = {'step_id':'step-a', 'number':1, 'instruction':'Inspect approved indicator.'}
        workflow['status'] = 'awaiting_done'
        entry.repair_workflow = workflow
        asset = store.put_image(entry, png())
        entry.history = [
            {'id':'question-a', 'case_id':workflow['workflow_id'], 'role':'user', 'text':'ORIGINAL_CASE_A'},
            {'id':'answer-a', 'case_id':workflow['workflow_id'], 'role':'assistant', 'response':{
                'blocks':[{'type':'text','content':'SUPPORTED_CASE_A'},
                          {'type':'image','src':f'/images/{asset}','caption':'Approved figure'}],
                'sources':[{'document_name':'Coolant manual','page_number':4}]}},
            {'id':'answer-b','case_id':'other-case','role':'assistant','response':{'blocks':[{'type':'text','content':'PRIVATE_CASE_B'}]}},
        ]
        return entry, workflow, asset

    def test_conversation_image_report_and_progress_are_owner_scoped(self):
        _, workflow, asset = self.seed()
        response = self.client.post(f'/sessions/{self.session}/reports', json={'case_id':workflow['workflow_id']})
        self.assertEqual(response.status_code, 200, response.text)
        report = response.json(); report_id = report['report_id']
        self.assertNotIn('PRIVATE_CASE_B', json.dumps(report))
        self.assertEqual(self.client.get(f'/reports/{report_id}/pdf').status_code, 200)
        self.identity = Identity('owner-b','b@example.com',True)
        for url in [f'/sessions/{self.session}', f'/images/{asset}', f'/reports/{report_id}',
                    f'/reports/{report_id}/pdf', f'/reports/{report_id}/images/{asset}',
                    f'/sessions/{self.session}/progress/request-a']:
            self.assertEqual(self.client.get(url).status_code, 404, url)
        self.assertEqual(self.client.post('/chat', data={'session_id':self.session,'message':'hello'}).status_code, 404)

    def test_save_edits_preserves_original_evidence_and_rejects_stale_version(self):
        _, workflow, _ = self.seed()
        report = self.client.post(f'/sessions/{self.session}/reports', json={'case_id':workflow['workflow_id']}).json()
        data = {'revision':1,'fields':{'cause':'Not confirmed','notes':'Technician note'}}
        saved = self.client.post(f'/reports/{report["report_id"]}', json=data)
        self.assertEqual(saved.status_code, 200)
        self.assertEqual(saved.json()['history'], report['history'])
        self.assertEqual(self.client.post(f'/reports/{report["report_id"]}',json=data).status_code,409)
        self.assertEqual(self.client.post(f'/reports/{report["report_id"]}',json={'revision':2,'fields':{'history':'override'}}).status_code,400)

    def test_duplicate_request_returns_saved_result_without_second_transition(self):
        reply = {'blocks':[{'type':'text','content':'Information response'}]}
        with patch('app.api.chat_routes.answer_question',new=AsyncMock(return_value=(reply,False))) as model:
            data={'session_id':self.session,'message':'Explain the component'}
            first=self.client.post('/chat',data=data,headers={'X-Client-Request-ID':'request-one'})
            second=self.client.post('/chat',data=data,headers={'X-Client-Request-ID':'request-one'})
        self.assertEqual(first.status_code,200,first.text)
        self.assertEqual(first.json(),second.json())
        self.assertEqual(model.call_count,1)
        self.assertEqual(len(store.entries[self.session].history),2)

    def test_unresolved_case_opens_documentation_without_marking_work_done(self):
        _, workflow, _ = self.seed()
        response=self.client.post('/chat',data={'session_id':self.session,'repair_action':'unresolved',
            'workflow_id':workflow['workflow_id'],'step_id':'step-a','plan_version':1})
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['documentation']['status'],'Unresolved')
        self.assertEqual(response.json()['documentation']['workflow']['completed_steps'],[])

    def test_pdf_contains_only_selected_case_and_embeds_its_image(self):
        entry, workflow, _ = self.seed()
        report=create_document(entry,self.identity,case_id=workflow['workflow_id'])
        report['content']='Recorded observation. ' * 450
        data=render_pdf(report)
        doc=pymupdf.open(stream=data,filetype='pdf')
        text=''.join(page.get_text() for page in doc)
        self.assertIn('SUPPORTED_CASE_A',text)
        self.assertNotIn('PRIVATE_CASE_B',text)
        self.assertIn('Coolant manual',text)
        self.assertGreater(sum(len(page.get_images()) for page in doc),0)
        self.assertGreater(len(doc),1)
        self.assertIn('Page 1 of',doc[0].get_text())

    def test_information_report_includes_only_its_question_and_answer(self):
        entry, _, _=self.seed()
        report=create_document(entry,self.identity,message_id='answer-a')
        self.assertEqual([message['id'] for message in report['history']],['question-a','answer-a'])
        self.assertNotIn('PRIVATE_CASE_B',json.dumps(report))


class PlanTests(unittest.IsolatedAsyncioTestCase):
    async def test_main_generation_runs_while_safety_selection_is_pending(self):
        main_started=threading.Event()
        def safety(_question):
            if not main_started.wait(1):
                raise RuntimeError('Main generation incorrectly waited for safety')
            return SafetySelection(relevant=True,precaution_ids=['pressure'])
        async def generate(**kwargs):
            main_started.set()
            answer=AgentAnswer(blocks=[],repair_step=RepairStep(instruction='Inspect the approved indicator.',evidence_ids=['evidence-1']),
                image_descriptions=[],relevant_asset_ids=[],supported=True,finished=False,repair_plan_complete=True)
            yield SimpleNamespace(is_final_response=lambda:True,content=SimpleNamespace(parts=[SimpleNamespace(text=answer.model_dump_json(),thought=False)]))
        entry=SimpleNamespace(id='test',user_id='owner',history=[],repair_workflow=None,repair_cache=None,current_progress={},safety_task=None)
        plan=QueryPlan(query='indicator fault',topic='indicator fault',follow_up=False,guided=True,repair=True,
                       advance=False,wants_visual=False,wants_video=False,wants_sources=False,clarify='',out_of_scope=False,model_mismatch=False)
        bundle={'chunks':[{'chunk_id':'one','content':'Inspect the approved indicator.','document_name':'Manual','page_number':1}],'images':[],'links':[]}
        with (patch('app.agent.safety.select_safety',side_effect=safety),
              patch('app.agent.chat_service.understand',return_value=plan),
              patch('app.agent.chat_service.search_manual',return_value=bundle),
              patch('app.agent.chat_service.prepare_media',return_value={'images':[],'links':[],'warnings':[]}),
              patch('app.agent.chat_service.runner.run_async',side_effect=generate),
              patch('app.agent.chat_service.store.state',new=AsyncMock(return_value={})),
              patch('app.agent.chat_service.store.get',new=AsyncMock()),
              patch('app.agent.chat_service.store.update_state',new=AsyncMock())):
            response,_=await asyncio.wait_for(answer_question(entry,'Repair the indicator fault',None),3)
        self.assertTrue(main_started.is_set())
        self.assertIn('stored-pressure',' '.join(response['safety']['bullets']))
        self.assertNotIn('remaining_answers',json.dumps(response))

    async def test_safety_is_independent_and_does_not_claim_confirmation(self):
        entry=SimpleNamespace(safety_task=None,current_progress={})
        with patch('app.agent.safety.select_safety', side_effect=RuntimeError('offline')):
            task=start_safety(entry,'No hydraulic pressure in the machine')
            self.assertFalse(task.done())
            result=await task
        self.assertIn('stored-pressure', ' '.join(result['bullets']))
        self.assertFalse(result['confirmed'])
        self.assertEqual(entry.current_progress['safety'],result)
        self.assertNotIn('Use safety gloves', ' '.join(result['bullets']))

    async def test_branch_requires_observation_and_reveals_cached_target_only(self):
        workflow=new_workflow('Test fault',[])
        workflow.update(status='awaiting_done',current_step={'step_id':'one','number':1,'instruction':'Observe indicator.',
            'branches':[{'label':'Off','next_step_key':'three'},{'label':'On','next_step_key':'END'}]})
        with self.assertRaises(HTTPException):
            validate_action(workflow,RepairAction(action='done',workflow_id=workflow['workflow_id'],step_id='one'))
        updated=validate_action(workflow,RepairAction(action='done',workflow_id=workflow['workflow_id'],step_id='one',observation='Off'))
        answers=[AgentAnswer(repair_step=RepairStep(step_key=key,instruction=f'Inspect {key}.',evidence_ids=['c1']),
             blocks=[],image_descriptions=[],relevant_asset_ids=[],supported=True,finished=False) for key in ['two','three']]
        cached={'remaining_answers':answers,'bundle':{'chunks':[{'chunk_id':'c1','document_name':'Manual','page_number':1}]},
                'media':{'images':[],'links':[],'warnings':[]},'wants_sources':True}
        entry=SimpleNamespace()
        with patch('app.agent.chat_service.store.update_state',new=AsyncMock()):
            response,_=await _advance_cached_plan(entry,updated,cached)
        self.assertIn('Inspect three.',response['blocks'][0]['content'])
        self.assertNotIn('Inspect two.',json.dumps(response))
        self.assertEqual(len(response['repair_workflow']['completed_steps']),1)
        self.assertEqual(cached['remaining_answers'],[])

    async def test_old_plan_version_cannot_complete_step(self):
        workflow=new_workflow('Fault',[])
        workflow.update(plan_version=2,status='awaiting_done',current_step={'step_id':'one','number':1,'instruction':'Observe.'})
        with self.assertRaises(HTTPException) as error:
            validate_action(workflow,RepairAction(action='done',workflow_id=workflow['workflow_id'],step_id='one',plan_version=1))
        self.assertEqual(error.exception.status_code,409)

    async def test_snapshot_restores_private_plan_and_images_without_exposing_future_steps(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(config,'BACKEND_ROOT',Path(directory)):
            first=SessionStore(); entry=await first.create('owner')
            entry.repair_workflow=new_workflow('Fault',[])
            entry.repair_cache={'remaining_answers':[AgentAnswer(blocks=[],image_descriptions=[],relevant_asset_ids=[],supported=True,finished=False)],
                                'bundle_metadata':{'timestamp':datetime.now(timezone.utc),'value':Decimal('1.25')}}
            asset=first.put_image(entry,png()); await first.checkpoint(entry)
            entry.timer.cancel()
            # Simulate a new worker with a new ADK in-memory service.
            from google.adk.sessions import InMemorySessionService
            second=SessionStore(service=InMemorySessionService())
            restored=await second.owned(entry.id,Identity('owner','a@example.com',True))
            self.assertEqual(restored.repair_workflow,entry.repair_workflow)
            self.assertIsInstance(restored.repair_cache['remaining_answers'][0],AgentAnswer)
            self.assertEqual(restored.repair_cache['bundle_metadata'],entry.repair_cache['bundle_metadata'])
            self.assertEqual(await second.image(asset),png())
            await second.delete(entry.id)

    async def test_invalid_branch_is_rejected_before_plan_is_saved(self):
        with self.assertRaises(ValueError):
            validate_branches([RepairStep(step_key='one',instruction='Observe.',evidence_ids=['c1'],branches=[{'label':'Off','next_step_key':'missing'}])])
