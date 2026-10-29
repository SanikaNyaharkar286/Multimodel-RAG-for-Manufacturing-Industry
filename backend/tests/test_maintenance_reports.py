"""Snapshot, confirmation and PDF safety checks; no cloud or LLM requests."""
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import pymupdf
from PIL import Image

from app.responses.documentation import create_document, render_pdf
from app.responses.maintenance_report import (LOCAL_ZONE, StepConfirmation, apply_profile,
    update_confirmations, validate_details, upgrade_report)
from app.responses.schemas import AgentAnswer, RepairStep
from tests import test_case_flow


def fixture():
    identity = SimpleNamespace(uid='fixture-owner', email='technician@example.test')
    completed = {'step_id':'step-1', 'number':1, 'title':'Inspect the indicator',
        'instruction':'Inspect the indicator.', 'observation':'Indicator remained off.',
        'confirmed_at':'2026-10-20T10:00:00+05:30'}
    current = {'step_id':'step-2', 'number':2, 'instruction':'Check the supply.',
               'title':'Check the supply', 'precautions':['Follow the documented isolation procedure.']}
    workflow = {'workflow_id':'fixture-case', 'machine':'Custom Unit_X', 'issue':'Reported indicator fault',
        'status':'awaiting_done', 'completed_steps':[completed], 'current_step':current,
        'presented_steps':[deepcopy(completed), deepcopy(current)]}
    image = BytesIO()
    Image.new('RGB', (240, 130), '#c4e2f4').save(image, 'PNG')
    photo = 'data:image/png;base64,' + base64.b64encode(image.getvalue()).decode()
    entry = SimpleNamespace(id='fixture-session', repair_workflow=workflow,
        cases={}, assets={'manual-image':('image/png', image.getvalue())}, repair_cache={
            'workflow_id':'fixture-case', 'remaining_answers':[AgentAnswer(
                supported=True, finished=False, blocks=[], image_descriptions=[], relevant_asset_ids=[],
                repair_step=RepairStep(step_key='future', instruction='Inspect the documented connection.',
                                       evidence_ids=['manual-chunk']))]}, history=[
            {'id':'reported', 'case_id':'fixture-case', 'role':'user', 'text':'Reported indicator fault',
             'image':photo, 'step_id':'step-1'},
            {'id':'answer', 'case_id':'fixture-case', 'role':'assistant', 'response':{
                'repair_workflow':{'current_step':current},
                'blocks':[{'type':'text','content':'Approved instruction from the selected manual.'},
                          {'type':'image','src':'/images/manual-image','caption':'Indicator assembly',
                           'description':'- Description: Indicator assembly.\n- Technician check: Inspect the indicator.',
                           'source':{'document_name':'Custom Unit Service Manual.pdf', 'page_number':7}}],
                'sources':[{'document_name':'Custom Unit Service Manual.pdf', 'page_number':7}]}},
            {'id':'other', 'case_id':'other-case', 'role':'user', 'image':photo, 'text':'OTHER CASE PRIVATE'},
        ])
    return entry, identity


class ReportSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.entry, self.identity = fixture()
        self.original = deepcopy(self.entry.repair_workflow)
        self.report = create_document(self.entry, self.identity, case_id='fixture-case')

    def test_all_available_steps_are_distinguished_from_actual_work(self):
        self.assertEqual([step['status'] for step in self.report['steps']], ['Completed','Pending','Pending'])
        self.assertEqual(self.report['steps'][0]['completed_at'], '2026-10-20T10:00:00+05:30')
        self.assertEqual(self.report['steps'][1]['performed_by'], '')
        self.assertEqual(self.report['steps'][0]['result'], 'Not tested')
        self.assertEqual(self.entry.repair_workflow, self.original)
        self.assertEqual(self.report['model'], '')
        self.assertEqual(self.report['asset_id'], '')

    def test_machine_profile_and_missing_optional_values_are_truthful(self):
        apply_profile(self.report, self.identity, {'fullName':'Fixture Technician','employeeId':'EMP-TEST'})
        self.assertEqual(self.report['technician_name'], 'Fixture Technician')
        self.assertEqual(self.report['employee_id'], 'EMP-TEST')
        self.assertEqual(self.report['steps'][0]['performed_by'], 'Fixture Technician')
        self.assertEqual(self.report['serial_number'], '')
        self.assertEqual(self.report['manufacturer'], '')

    def test_foreign_case_plan_and_images_are_not_collected(self):
        self.assertEqual({item['image_id'] for item in self.report['evidence']}, {'reported','manual-image'})
        self.entry.repair_cache['workflow_id'] = 'other-case'
        report = create_document(self.entry, self.identity, case_id='fixture-case')
        self.assertEqual(len(report['steps']), 2)
        self.assertNotIn('OTHER CASE PRIVATE', str(report))

    def test_completed_recommendations_do_not_imply_repairs_or_authorization(self):
        self.entry.repair_workflow['status'] = 'resolved'
        report = create_document(self.entry, self.identity, case_id='fixture-case')
        self.assertEqual(report['status'], 'Resolved')
        self.assertEqual(report['return_to_service'], 'Not authorized')
        self.assertEqual(report['functional_test'], 'Not tested')
        self.assertEqual(report['components_replaced'], '')
        self.assertEqual(report['approval_status'], 'Pending')

    def test_legacy_reports_keep_authored_facts_and_can_use_the_new_review(self):
        legacy = deepcopy(self.report)
        legacy.pop('report_version')
        legacy['status'] = 'Technician reported resolved'
        legacy['notes'] = 'Previously recorded technician note.'
        result = upgrade_report(legacy)
        self.assertEqual(result['status'], 'Resolved')
        self.assertEqual(result['notes'], 'Previously recorded technician note.')
        self.assertEqual(result['revision'], 1)
        self.assertEqual(result['return_to_service'], 'Not authorized')
        self.assertEqual(len(result['steps']), 2)
        validate_details(result)

    def test_unresolved_and_partial_incidents_can_be_documented(self):
        self.entry.repair_workflow['status'] = 'unresolved'
        report = create_document(self.entry, self.identity, case_id='fixture-case')
        self.assertEqual(report['status'], 'Unresolved')
        self.assertEqual(report['escalation_required'], 'Yes')
        report['status'] = 'Partially Resolved'
        validate_details(report)
        self.assertEqual(report['status'], 'Partially Resolved')

    def test_confirmed_calendar_duration_is_not_chat_age_or_downtime(self):
        validate_details(self.report)
        self.assertIsNone(self.report['fault_duration_days'])
        self.report['fault_started_at'] = (datetime.now(LOCAL_ZONE) - timedelta(days=3)).date().isoformat()
        self.report['downtime'] = '20 minutes, technician recorded'
        validate_details(self.report)
        self.assertEqual(self.report['fault_duration_days'], 3)
        self.assertEqual(self.report['downtime'], '20 minutes, technician recorded')

    def test_invalid_dates_and_false_approvals_are_rejected(self):
        for key, value in [('fault_started_at','2026-02-30'), ('service_start','2026-10-20T12:00:00'),
                           ('return_to_service','Authorized'), ('escalation_status','Submitted')]:
            report = deepcopy(self.report); report[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError):
                validate_details(report)
        report = deepcopy(self.report)
        report.update(service_start='2026-10-20T11:00:00+05:30', service_end='2026-10-20T10:00:00+05:30')
        with self.assertRaises(ValueError):
            validate_details(report)

    def test_confirmations_can_record_skipped_failed_and_completed_steps(self):
        update_confirmations(self.report, [StepConfirmation(step_id='step-2', status='Skipped', notes='Access unavailable.')], self.identity)
        self.assertEqual(self.report['steps'][1]['status'], 'Skipped')
        self.assertEqual(self.report['steps'][1]['performed_by'], '')
        update_confirmations(self.report, [StepConfirmation(step_id='step-2', status='Failed',
            result='Failed', observation='Supply test failed.', action_performed='Measured the supply.')], self.identity)
        self.assertIn('Supply test failed.', self.report['diagnostic_findings'])
        self.assertEqual(self.report['steps'][1]['confirmed_by_uid'], self.identity.uid)
        self.assertEqual(self.entry.repair_workflow, self.original)

    def test_retracting_a_confirmation_also_retracts_the_derived_work_summary(self):
        self.assertIn('Inspect the indicator.', self.report['action_taken'])
        update_confirmations(self.report, [StepConfirmation(step_id='step-1', status='Skipped')], self.identity)
        self.assertNotIn('Inspect the indicator.', self.report['action_taken'])
        self.assertEqual(self.entry.repair_workflow, self.original)

    def test_confirmations_cannot_rewrite_another_step_or_fake_pending_work(self):
        for item in [dict(step_id='foreign-step',status='Completed'),
                     dict(step_id='step-2',status='Pending',result='Passed')]:
            with self.subTest(item=item), self.assertRaises(ValueError):
                update_confirmations(self.report, [StepConfirmation(**item)], self.identity)

    def test_pdf_contains_actual_evidence_and_pending_approvals_without_new_llm(self):
        with patch('app.agent.chat_service._generate_answer', side_effect=AssertionError('No model in report generation')):
            data = render_pdf(self.report)
        with pymupdf.open(stream=data, filetype='pdf') as pdf:
            text = '\n'.join(page.get_text() for page in pdf)
            self.assertIn('TECHNICAL SERVICE', text)
            self.assertIn('Custom Unit_X', text)
            self.assertIn('Pending', text)
            self.assertEqual(len(pdf), 3)
            self.assertIn('Approval & Signatures', text)
            self.assertIn('Not authorized', text)
            self.assertIn('Inspect the indicator.', text)
            self.assertNotIn('Check the supply.', text)
            self.assertNotIn('Custom Unit Service Manual.pdf', text, 'Pending-step diagrams are not performed-work evidence')
            self.assertNotIn('Previous Machine Maintenance History', text)
            self.assertNotIn('Customer / Company Details', text)
            self.assertNotIn('OTHER CASE PRIVATE', text)
            self.assertGreater(sum(len(page.get_images()) for page in pdf), 0)
            for page in pdf:
                self.assertAlmostEqual(page.rect.width, 595.28, delta=1)
                for block in page.get_text('blocks'):
                    self.assertGreaterEqual(block[0], 20)
                    self.assertLessEqual(block[2], page.rect.width - 20)
                    self.assertLessEqual(block[3], page.rect.height - 10)

    def test_raw_guidance_and_content_are_preserved_but_not_printed_in_the_summary(self):
        self.report['images'] = {}; self.report['evidence'] = []
        self.report['content'] = 'Long technician notes. ' * 500
        block = {'type':'table', 'columns':['Inspection', 'Reference'],
                 'rows':[[f'Recorded row {index}', 'Original manual reference. ' * 20] for index in range(45)]}
        self.report['manual_guidance'] = [{'blocks':[block], 'sources':[]}]
        data = render_pdf(self.report)
        with pymupdf.open(stream=data,filetype='pdf') as pdf:
            text = ''.join(page.get_text() for page in pdf)
            self.assertNotIn('Recorded row 44', text)
            self.assertNotIn('Long technician notes.', text)
            self.assertEqual(len(pdf), 3)
            self.assertEqual(len(self.report['manual_guidance'][0]['blocks'][0]['rows']), 45)


class ProfessionalReportApiTests(unittest.TestCase):
    setUp = test_case_flow.CaseApiTests.setUp
    tearDown = test_case_flow.CaseApiTests.tearDown
    seed = test_case_flow.CaseApiTests.seed

    def test_new_review_inputs_preserve_identity_and_workflow(self):
        entry, workflow, _ = self.seed()
        report = self.client.post(f'/sessions/{self.session}/reports',json={'case_id':workflow['workflow_id']}).json()
        original = deepcopy(entry.repair_workflow)
        saved = self.client.post(f'/reports/{report["report_id"]}',json={'revision':1,
            'fields':{'status':'Partially Resolved', 'operational_status':'Under Maintenance'},
            'step_confirmations':[{'step_id':'step-a','status':'Completed','result':'Inconclusive',
                                    'observation':'Indicator unchanged.'}]})
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()['steps'][0]['performed_by'], self.identity.email)
        self.assertEqual(entry.repair_workflow, original)
        forged = self.client.post(f'/reports/{report["report_id"]}',json={'revision':2,
                                   'fields':{'technician_email':'other@example.test'}})
        self.assertEqual(forged.status_code, 400)

    def test_operational_approval_and_foreign_steps_cannot_be_forged(self):
        _, workflow, _ = self.seed()
        report = self.client.post(f'/sessions/{self.session}/reports',json={'case_id':workflow['workflow_id']}).json()
        for body in [{'revision':1,'fields':{'return_to_service':'Authorized'}},
                     {'revision':1,'fields':{},'step_confirmations':[{'step_id':'foreign','status':'Completed'}]}]:
            response = self.client.post(f'/reports/{report["report_id"]}',json=body)
            self.assertEqual(response.status_code, 400, response.text)
        self.assertEqual(self.client.get(f'/reports/{report["report_id"]}').json()['revision'], 1)
