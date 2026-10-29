"""Unit checks for the maintenance projection; no renderer or services needed."""

from copy import deepcopy
import unittest

from app.responses.maintenance_summary import performed_steps


def reviewed(step_id='reviewed', **values):
    step = dict(step_id=step_id, number=7, title='Inspect blade',
                instruction='Inspect the blade teeth.', status='Completed',
                confirmed_by_uid='technician-1',
                confirmation_recorded_at='2026-10-29T09:00:00Z')
    step.update(values)
    return step


class MaintenanceSummaryTests(unittest.TestCase):
    def test_recorded_order_numbers_and_exact_output_keys(self):
        steps = [reviewed('third', number=9), reviewed('first', number=2)]
        result = performed_steps({'steps': steps})
        self.assertEqual([step['step_id'] for step in result], ['third', 'first'])
        self.assertEqual([step['number'] for step in result], [9, 2])
        self.assertEqual(set(result[0]), {
            'step_id', 'number', 'title', 'action', 'observation', 'status',
            'result', 'precautions', 'parts_used',
        })

    def test_workflow_membership_is_completion_evidence_without_a_timestamp(self):
        report = {'steps': [dict(step_id='done', number=4, status='Completed',
                                 instruction='Replace damaged blade.')],
                  'workflow': {'completed_steps': [{'step_id': 'done'}]}}
        result = performed_steps(report)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['number'], 4)
        self.assertEqual(result[0]['observation'], 'Not provided')
        self.assertNotIn('completed_at', result[0])
        self.assertNotIn('completed_at', report['steps'][0])

    def test_review_retractions_override_old_workflow_completions(self):
        for status in ('Pending', 'Skipped', 'Not performed'):
            with self.subTest(status=status):
                step = reviewed(status=status, performed_by='Technician',
                                completed_at='2026-10-28T09:00:00Z')
                report = {'steps': [step], 'workflow': {'completed_steps': [
                    dict(step_id=step['step_id'], status='Completed')]}}
                self.assertEqual(performed_steps(report), [])

    def test_unconfirmed_pending_current_and_future_steps_are_excluded(self):
        report = {'steps': [
            dict(step_id='pending', status='Pending'),
            dict(step_id='future', title='Complete final inspection', status='Completed',
                 planned_only=True, action_performed='Suggested inspection'),
            dict(step_id='failed', status='Failed', observation='Suggested failure'),
        ], 'workflow': {'current_step': reviewed('current'),
                        'presented_steps': [reviewed('presented')]}}
        self.assertEqual(performed_steps(report), [])

    def test_status_only_and_partial_confirmation_are_not_proof(self):
        for evidence in ({}, {'confirmed_by_uid': 'technician-1'},
                         {'confirmation_recorded_at': '2026-10-29T09:00:00Z'},
                         {'performed_by': 'Technician'},
                         {'completed_at': '2026-10-29T09:00:00Z'},
                         {'has_completed_at': True},
                         {'performed_by': 'Technician', 'has_completed_at': True},
                         {'performed_by': 'Technician', 'has_completed_at': False},
                         {'performed_by': 'Technician', 'has_completed_at': 'false'}):
            for status in ('Completed', 'Failed'):
                with self.subTest(evidence=evidence, status=status):
                    step = dict(step_id='tampered', status=status, **evidence)
                    self.assertEqual(performed_steps({'steps': [step]}), [])

    def test_review_confirmed_failed_attempt_preserves_actual_outcome(self):
        step = reviewed(status='Failed', action_performed='Tried to start motor.',
                        observation='Motor did not turn.', result='Failed')
        self.assertEqual(performed_steps({'steps': [step]})[0], dict(
            step_id='reviewed', number=7, title='Inspect blade',
            action='Tried to start motor.', observation='Motor did not turn.',
            status='Failed', result='Failed', precautions=[], parts_used=''))
        self.assertNotIn('completed_at', step)

    def test_legacy_performer_and_actual_completion_time_are_supported(self):
        step = dict(step_id='legacy', status='Completed',
                    performed_by='Recorded technician', completed_at='2026-10-29T09:00:00Z')
        self.assertEqual(len(performed_steps({'steps': [step]})), 1)

    def test_completion_boolean_with_performer_without_recorded_event_is_excluded(self):
        step = dict(step_id='flag-only', status='Completed',
                    performed_by='Recorded technician', has_completed_at=True)
        self.assertEqual(performed_steps({'steps': [step]}), [])

    def test_action_preference_and_whitespace_cleanup_without_rewriting(self):
        for action, instruction, title, expected in (
            ('  Viewed\n indicator.\t', 'Inspect valve.', 'Inspect', 'Viewed indicator.'),
            (' \t', '  Inspect\n valve. ', 'Inspect', 'Inspect valve.'),
            ('', '', '  Inspect\tvalve ', 'Inspect valve'),
            ('', '', '', ''),
        ):
            with self.subTest(action=action, instruction=instruction):
                step = reviewed(action_performed=action, instruction=instruction, title=title)
                self.assertEqual(performed_steps({'steps': [step]})[0]['action'], expected)

    def test_observations_and_results_are_never_inferred_from_other_content(self):
        step = reviewed(observation=' \n', expected_result='Motor runs normally.',
                        notes='Expected pass', description='AI description',
                        supporting_blocks=[{'type': 'text', 'content': 'FAKE_OBSERVATION'}])
        report = {'steps': [step], 'history': [dict(role='assistant', text='FAKE_HISTORY')],
                  'manual_guidance': ['FAKE_GUIDANCE'], 'functional_test': 'Pass'}
        result = performed_steps(report)[0]
        self.assertEqual(result['observation'], 'Not provided')
        self.assertEqual(result['result'], 'Not tested')
        self.assertNotIn('FAKE', str(result))

    def test_original_snapshot_and_chat_are_not_mutated_and_results_are_detached(self):
        report = {'steps': [reviewed(precautions=['Isolate power.'],
                                    parts_used=[{'part': 'B-1', 'quantity': 1}])],
                  'workflow': {'completed_steps': [{'step_id': 'reviewed'}]},
                  'history': [{'role': 'user', 'text': 'Recorded chat'}]}
        before = deepcopy(report)
        result = performed_steps(report)
        self.assertEqual(report, before)
        result[0]['precautions'].append('Do not change snapshot')
        result[0]['parts_used'][0]['quantity'] = 99
        self.assertEqual(report, before)

    def test_legacy_fallback_preserves_recorded_order_and_does_not_fabricate_times(self):
        completed = [dict(step_id='b', number=5, title='Replace blade',
                          instruction='Replace damaged blade.', observation='Broken teeth.'),
                     dict(step_id='a', number=2, instruction='Measure stock.')]
        report = {'workflow': {'completed_steps': completed,
                               'current_step': reviewed('future')}}
        before = deepcopy(report)
        result = performed_steps(report)
        self.assertEqual([step['step_id'] for step in result], ['b', 'a'])
        self.assertEqual([step['number'] for step in result], [5, 2])
        self.assertEqual([step['status'] for step in result], ['Completed', 'Completed'])
        self.assertEqual([step['observation'] for step in result], ['Broken teeth.', 'Not provided'])
        self.assertTrue(all('completed_at' not in step for step in result))
        self.assertEqual(report, before)

    def test_empty_normalized_steps_are_authoritative_and_missing_numbers_stay_missing(self):
        workflow = {'completed_steps': [dict(step_id='old', instruction='Inspect.') ]}
        self.assertEqual(performed_steps({'steps': [], 'workflow': workflow}), [])
        self.assertEqual(performed_steps({'steps': None, 'workflow': workflow})[0]['number'], '')
        self.assertEqual(performed_steps({}), [])
        self.assertEqual(performed_steps({'workflow': None}), [])


if __name__ == '__main__':
    unittest.main()
