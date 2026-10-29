"""Renderer-only checks: no API, database, retrieval or model dependencies."""

import base64
from copy import deepcopy
from io import BytesIO
import re
import unittest
from unittest.mock import patch

from PIL import Image
import pymupdf

from app.responses.maintenance_pdf import render_maintenance_pdf


def png():
    output = BytesIO()
    Image.new('RGB', (120, 70), '#c8def5').save(output, format='PNG')
    return 'data:image/png;base64,' + base64.b64encode(output.getvalue()).decode()


def snapshot(**values):
    report = dict(
        title='Technical Service & Maintenance Report', report_number='TMR-TEST-001',
        report_id='report-1', created_at='2026-10-28T09:00:00+00:00',
        updated_at='2026-10-28T10:00:00+00:00', timezone='Asia/Calcutta',
        machine='Surface grinder', model='HSG-2550', asset_id='',
        status='Under Investigation', workflow={'workflow_id': 'case-a'}, case_id='case-a',
        steps=[], evidence=[], images={}, history=[],
    )
    report.update(values)
    return report


def read(report):
    result = render_maintenance_pdf(report)
    document = pymupdf.open(stream=result, filetype='pdf')
    return document, '\n'.join(page.get_text() for page in document)


def four_step_example():
    """Explicit HAB-3232 example data, not facts inferred from recommendations."""
    actions = [
        'Inspect saw blade for broken teeth',
        'Replace damaged saw blade',
        'Measure stock dimensions',
        'Select correct TPI blade',
    ]
    completed = [dict(step_id=f'hab-{number}', number=number, title=action,
                      instruction=action, confirmed_at='2026-10-28T10:00:00Z',
                      observation='Broken teeth detected' if number == 1 else '')
                 for number, action in enumerate(actions, 1)]
    steps = [dict(step, status='Completed') for step in completed]
    steps.append(dict(step_id='next', number=5, title='PENDING_RECOMMENDATION',
                      instruction='PENDING_RECOMMENDATION', status='Pending'))
    return snapshot(machine='Horizontal band saw', model='HAB-3232',
                    issue='Saw blade vibration', technician_name='Example Technician',
                    technician_email='technician@example.test',
                    workflow={'workflow_id': 'case-a', 'completed_steps': completed}, steps=steps)


class MaintenancePdfTests(unittest.TestCase):
    def assert_three_page_layout(self, document):
        self.assertEqual(len(document), 3)
        for index, page in enumerate(document):
            self.assertAlmostEqual(page.rect.width, 595.276, places=2)
            self.assertAlmostEqual(page.rect.height, 841.89, places=2)
            self.assertIn(f'Page {index + 1} of 3', page.get_text())
            self.assertIn('MANUFACTURING MAINTENANCE COPILOT', page.get_text())
            self.assertIn('TECHNICAL SERVICE & MAINTENANCE REPORT', page.get_text())
            # Check extracted text and actual image rectangles, including footers.
            for word in page.get_text('words'):
                self.assertGreaterEqual(word[0], 0)
                self.assertGreaterEqual(word[1], 0)
                self.assertLessEqual(word[2], page.rect.width + .5)
                self.assertLessEqual(word[3], page.rect.height + .5)
            for image in page.get_image_info():
                self.assertTrue(page.rect.contains(pymupdf.Rect(image['bbox'])))

    def test_minimal_report_has_all_sections_and_no_invented_authorization(self):
        document, text = read(snapshot(return_to_service='Authorized', status='Resolved',
                                       approval_status='Approved', cause='Unknown'))
        self.assert_three_page_layout(document)
        for page, sections in zip(document, (
            ('1. Machine Details', '2. Technician Details', '3. Reported Issue'),
            ('4. Troubleshooting & Corrective Actions', '5. Troubleshooting Summary'),
            ('6. Final Maintenance Status', '7. Recommendations', '8. Approval & Signatures'),
        )):
            for section in sections:
                self.assertIn(section, page.get_text())
        self.assertIn('No technician-confirmed performed steps recorded.', text)
        self.assertIn('Not authorized', text)
        self.assertIn('Resolved', document[2].get_text())
        self.assertIn('Not tested', document[2].get_text())
        self.assertIn('Approval Status: Pending', text)
        self.assertIn('Report Reviewed By: Pending', text)
        self.assertNotIn('Approved', text)
        self.assertNotIn('Confirmed root cause', text)
        self.assertNotIn('Previous Machine Maintenance History', text)
        self.assertNotIn('Customer / Company Details', text)
        self.assertIn('03:30 PM IST', text)

    def test_four_hab_steps_preserve_order_and_actual_observations_only(self):
        report = four_step_example()
        before = deepcopy(report)
        document, text = read(report)
        self.assert_three_page_layout(document)
        self.assertEqual(report, before)
        page = document[1]
        text = page.get_text()
        actions = [step['instruction'] for step in report['steps'][:4]]
        positions = [text.index(action) for action in actions]
        self.assertEqual(positions, sorted(positions))
        for action in actions:
            self.assertEqual(text.count(action), 1)
        self.assertEqual(text.count('Not provided'), 3)
        self.assertIn('4 completed; 0 attempted unsuccessfully', text)
        for number, action in enumerate(actions, 1):
            rect = page.search_for(action)[0]
            row = page.get_text(clip=pymupdf.Rect(42, rect.y0 - 2, 554, rect.y1 + 2))
            self.assertIn(str(number), row)
            self.assertIn('Broken teeth detected' if number == 1 else 'Not provided', row)
            self.assertIn('Completed', row)
        self.assertNotIn('PENDING_RECOMMENDATION', '\n'.join(p.get_text() for p in document))

    def test_report_review_confirmation_supports_performed_failed_attempts(self):
        report = four_step_example()
        report['workflow']['completed_steps'] = []
        for step in report['steps'][:4]:
            step.update(confirmed_by_uid='technician-1',
                        confirmation_recorded_at='2026-10-28T10:00:00Z')
        report['steps'][1].update(status='Failed', observation='Blade could not be removed.')
        report['steps'].extend([
            dict(step_id='fake', number=6, status='Failed', instruction='UNCONFIRMED_FAILURE'),
            dict(step_id='tampered', number=7, status='Completed', instruction='STATUS_ONLY_ACTION'),
        ])
        document, text = read(report)
        self.assert_three_page_layout(document)
        self.assertIn('Blade could not be removed.', text)
        self.assertIn('3 completed; 1 attempted unsuccessfully', text)
        self.assertNotIn('UNCONFIRMED_FAILURE', text)
        self.assertNotIn('STATUS_ONLY_ACTION', text)

    def test_retracted_review_status_excludes_previous_session_completion(self):
        report = four_step_example()
        for step, status in zip(report['steps'], ('Pending', 'Skipped', 'Not performed')):
            step.update(status=status, confirmed_by_uid='technician-1',
                        confirmation_recorded_at='2026-10-28T11:00:00Z')
        document, text = read(report)
        self.assert_three_page_layout(document)
        for step in report['steps'][:3]:
            self.assertNotIn(step['instruction'], text)
        self.assertIn('Select correct TPI blade', text)
        self.assertIn('1 completed; 0 attempted unsuccessfully', text)

    def test_information_preserves_selected_blocks_tables_content_and_diagrams(self):
        report = snapshot(workflow=None, case_id='', title='Machine information', history=[
            dict(id='q', role='user', text='Selected question'),
            dict(id='a', role='assistant', response=dict(blocks=[
                dict(type='text', content='SUPPORTED_CASE_A\n### Readings\n**Safe** <b>literal</b> & pressure ≤ 4 bar.'),
                dict(type='table', columns=['Setting', 'Limit'], rows=[['FIRST', '4'], ['SECOND', '7']]),
                dict(type='image', src='/images/diagram', caption='Manual diagram', description='Diagram labels.'),
            ], sources=[dict(document_name='Original manual name.pdf', page_number=7)])),
        ], images={'diagram': png()}, content='Custom observation. ' * 900)
        document, text = read(report)
        for value in ('SUPPORTED_CASE_A', 'Original manual name.pdf', 'Diagram labels.', '<b>literal</b>',
                      'Custom observation.', 'Page 1 of'):
            self.assertIn(value, text)
        self.assertEqual(text.count('observation.'), 900)
        self.assertLess(text.index('FIRST'), text.index('SECOND'))
        self.assertGreater(sum(len(page.get_images()) for page in document), 0)
        self.assertNotIn('Approval & Report Sign-Off', text)

    def test_long_table_cell_splits_and_headers_repeat(self):
        text_value = 'Long cell content for testing row splitting. ' * 1500
        report = snapshot(workflow=None, case_id='', manual_guidance=[dict(blocks=[dict(
            type='table', columns=['Original column', 'Original content'],
            rows=[['ROW_ONE', text_value], ['ROW_TWO', 'LAST_ROW_MARKER']],
        )], sources=[])])
        document, text = read(report)
        self.assertIn('LAST_ROW_MARKER', text)
        self.assertEqual(text.count('splitting.'), 1500)
        table_pages = [page.get_text() for page in document if 'Long cell content' in page.get_text()]
        self.assertGreater(len(table_pages), 2)
        self.assertTrue(all('Original column' in page for page in table_pages))
        self.assertLess(text.index('ROW_ONE'), text.index('ROW_TWO'))

    def test_snapshot_scope_and_no_network_or_external_image_loading(self):
        report = four_step_example()
        report.update(history=[
            dict(role='assistant', case_id='case-a', response=dict(blocks=[
                dict(type='text', content='HISTORY_RESPONSE_SENTINEL'),
                dict(type='image', src='https://example.invalid/secret.png')])),
            dict(role='user', case_id='case-a', text='HISTORY_USER_SENTINEL'),
            dict(role='assistant', case_id='case-b', text='OTHER_CASE_SENTINEL'),
        ], manual_guidance=[dict(blocks=[dict(type='text', content='GUIDANCE_SENTINEL')])],
            content='CONTENT_SENTINEL')
        report['steps'][0].update(description='DESCRIPTION_SENTINEL',
            blocks=[dict(type='text', content='BLOCK_SENTINEL')],
            supporting_blocks=[dict(type='text', content='SUPPORT_SENTINEL')])
        before = deepcopy(report)
        with patch('urllib.request.urlopen', side_effect=AssertionError('No network')) as network:
            document, text = read(report)
        network.assert_not_called()
        self.assert_three_page_layout(document)
        self.assertNotIn('SENTINEL', text)
        self.assertEqual(report, before)

    def test_html_is_literal_text_and_external_images_are_never_loaded(self):
        report = four_step_example()
        report['steps'] = report['steps'][:1]
        report['steps'][0]['action_performed'] = '<b>literal</b> & pressure < 4 bar'
        report['steps'][0]['observation'] = '<img src="file:///private"/>'
        report.update(images={'external': 'https://example.invalid/image.png',
                              'local': 'file:///private'}, evidence=[
            dict(image_id=key, caption='EXTERNAL_IMAGE_SENTINEL', step_id='hab-1')
            for key in ('external', 'local')])
        with patch('urllib.request.urlopen', side_effect=AssertionError('No network')) as network:
            document, text = read(report)
        network.assert_not_called()
        self.assert_three_page_layout(document)
        self.assertIn('<b>literal</b> & pressure < 4 bar', text)
        self.assertIn('<img src="file:///private"/>', text)
        self.assertNotIn('EXTERNAL_IMAGE_SENTINEL', text)
        self.assertEqual(sum(len(page.get_images()) for page in document), 0)
        self.assertTrue(all(not page.get_links() for page in document))

    def test_missing_and_invalid_images_do_not_fail_or_generate_empty_boxes(self):
        for value in ('data:image/png;base64,broken!', 'https://example.invalid/image.png',
                      'data:image/png;base64,' + base64.b64encode(b'not a png').decode()):
            with self.subTest(value=value):
                report = four_step_example()
                report.update(evidence=[dict(image_id='bad', caption='INVALID_CAPTION', step_id='hab-1')],
                              images={'bad': value})
                document, text = read(report)
                self.assert_three_page_layout(document)
                self.assertNotIn('INVALID_CAPTION', text)
                self.assertEqual(sum(len(page.get_images()) for page in document), 0)

    def test_information_legacy_fallback_retains_selected_assistant_response_and_source(self):
        report = snapshot(workflow=None, case_id='', history=[dict(role='assistant', response=dict(
            blocks=[dict(type='text', content='SUPPORTED_CASE_A'),
                    dict(type='image', src='/images/diagram', caption='Approved figure')],
            sources=[dict(document_name='Coolant manual', page_number=4)]))],
            images={'diagram': png()}, content='Recorded observation. ' * 450)
        document, text = read(report)
        self.assertIn('SUPPORTED_CASE_A', text)
        self.assertIn('Coolant manual', text)
        self.assertEqual(text.count('observation.'), 450)
        self.assertGreater(sum(len(page.get_images()) for page in document), 0)

    def test_orphan_guidance_and_zero_duration_preserved(self):
        _, text = read(snapshot(fault_started_at='2026-10-28', fault_duration_days=0,
            manual_guidance=[dict(step_id='unmatched-step', blocks=[dict(type='text', content='ORPHAN_GUIDANCE')])]))
        self.assertNotIn('ORPHAN_GUIDANCE', text)
        self.assertIn('0 calendar day(s)', text)

    def test_technician_recommendations_are_not_relabelled_manual_verified(self):
        document, text = read(snapshot(preventive_recommendations='Technician recommendation fixture.',
                                       preparation_status='Technician reviewed'))
        self.assertIn('Technician recommendation fixture.', document[2].get_text())
        self.assertIn('Recorded follow-up recommendations, not completed work:', document[2].get_text())
        self.assertNotIn('Technician recommendation fixture.', document[1].get_text())
        self.assertNotIn('Manual-supported preventive', text)

    def test_report_id_metadata_footers_and_preparer_are_visible(self):
        report = four_step_example()
        before = deepcopy(report)
        document, text = read(report)
        self.assert_three_page_layout(document)
        self.assertEqual(report, before)
        self.assertIn('Report ID', document[0].get_text())
        self.assertIn('TMR-TEST-001', document[0].get_text(clip=pymupdf.Rect(42, 100, 554, 210)))
        for page in document:
            self.assertIn('TMR-TEST-001', page.get_text(clip=pymupdf.Rect(0, 790, 596, 842)))
        self.assertIn('Report Prepared By: Example Technician', document[2].get_text())
        self.assertNotIn('Report Prepared By:', document[0].get_text())
        self.assertNotIn('Report Prepared By:', document[1].get_text())
        self.assertEqual(document.metadata['title'], 'Technical Service & Maintenance Report')
        self.assertEqual(document.metadata['author'], 'Example Technician')

    def test_page_three_signature_table_is_at_bottom_with_blank_signature_cells(self):
        document, _ = read(four_step_example())
        page = document[2]
        for label in ('Role', 'Name', 'Signature', 'Date', 'Maintenance Technician',
                      'Maintenance Supervisor / Admin', 'Customer Representative'):
            rects = page.search_for(label)
            self.assertTrue(rects, label)
            self.assertTrue(any(570 <= rect.y0 <= rect.y1 <= 755 for rect in rects), label)
        # Signature/date columns under the table header must remain handwritten blanks.
        self.assertEqual(page.get_text(clip=pymupdf.Rect(335, 600, 554, 725)).strip(), '')
        for label in ('Report Prepared By:', 'Report Reviewed By:', 'Approval Status: Pending'):
            rect = page.search_for(label)[0]
            self.assertGreater(rect.y0, 700)
            self.assertLess(rect.y1, 790)
        for earlier in document[:2]:
            self.assertNotIn('8. Approval & Signatures', earlier.get_text())

    def test_long_actions_and_descriptions_disclose_abbreviation_and_preserve_input(self):
        report = four_step_example()
        report['steps'][0]['action_performed'] = 'Recorded inspection detail. ' * 400
        report['steps'][0]['observation'] = 'Recorded long observation. ' * 300
        report['fault_description'] = 'Recorded fault detail. ' * 400
        report['notes'] = 'Recorded final remarks. ' * 400
        before = deepcopy(report)
        document, text = read(report)
        self.assert_three_page_layout(document)
        self.assertIn('Abbreviated PDF:', text)
        self.assertIn('full records remain in the saved report.', text)
        self.assertIn('Recorded inspection detail.', text)
        self.assertIn('...', text)
        self.assertEqual(report, before)

    def test_thousand_confirmed_steps_have_explicit_omitted_count_without_mutation(self):
        report = four_step_example()
        report['steps'] = [dict(step_id=f'large-{number}', number=number, status='Completed',
            instruction=f'Action {number:04d} recorded', confirmed_by_uid='technician-1',
            confirmation_recorded_at='2026-10-28T10:00:00Z') for number in range(1, 1001)]
        report['workflow']['completed_steps'] = []
        before = deepcopy(report)
        document, text = read(report)
        self.assert_three_page_layout(document)
        page_text = document[1].get_text()
        self.assertIn('1000 completed; 0 attempted unsuccessfully', page_text)
        shown = re.findall(r'Action \d{4} recorded', page_text)
        omitted = re.search(r'(\d+) additional confirmed performed steps', page_text)
        self.assertIsNotNone(omitted)
        self.assertGreater(len(shown), 0)
        self.assertLess(len(shown), 1000)
        self.assertEqual(len(shown) + int(omitted.group(1)), 1000)
        self.assertIn('Action 0001 recorded', page_text)
        self.assertIn('Action 1000 recorded', page_text)
        self.assertIn('Abbreviated PDF:', text)
        self.assertEqual(report, before)

    def test_only_relevant_performed_step_png_is_shown_when_space_allows(self):
        report = four_step_example()
        report['steps'] = [report['steps'][0], report['steps'][-1]]
        report.update(images={'valid': png(), 'pending': png(), 'orphan': png()}, evidence=[
            dict(image_id='pending', step_id='next', caption='PENDING_IMAGE_SENTINEL'),
            dict(image_id='orphan', step_id='unknown', caption='ORPHAN_IMAGE_SENTINEL'),
            dict(image_id='valid', step_id='hab-1', caption='Recorded blade inspection'),
        ])
        before = deepcopy(report)
        document, text = read(report)
        self.assert_three_page_layout(document)
        self.assertEqual([len(page.get_image_info()) for page in document], [0, 1, 0])
        self.assertIn('Recorded blade inspection', document[1].get_text())
        self.assertNotIn('PENDING_IMAGE_SENTINEL', text)
        self.assertNotIn('ORPHAN_IMAGE_SENTINEL', text)
        self.assertEqual(report, before)

    def test_unresolved_issue_and_remaining_work_are_recorded_without_approval(self):
        report = four_step_example()
        report.update(status='Unresolved', operational_status='Not Operational',
                      functional_test='Fail', remaining_work='Awaiting replacement blade',
                      notes='Technician stopped the machine.')
        document, text = read(report)
        self.assert_three_page_layout(document)
        for value in ('Unresolved', 'Not Operational', 'Fail', 'Awaiting replacement blade',
                      'Technician stopped the machine.', 'Not authorized', 'Approval Status: Pending'):
            self.assertIn(value, document[2].get_text())


if __name__ == '__main__':
    unittest.main()
