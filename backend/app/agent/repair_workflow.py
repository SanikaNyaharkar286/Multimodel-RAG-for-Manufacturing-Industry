"""Server-controlled repair acknowledgements and technician-confirmed reports."""

from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException

SAFETY_WARNING = 'Switch off power. Use safety gloves. Follow lockout/tagout.'


def new_workflow(topic, chunks):
    machines = list(dict.fromkeys(row.get('machine_name', '') for row in chunks
                                  if row.get('machine_name')))
    return {'workflow_id': str(uuid4()), 'machine': machines[0] if len(machines) == 1 else '',
            'issue': topic, 'status': 'awaiting_step', 'completed_steps': [], 'current_step': None}


def validate_action(workflow, action):
    if not workflow or action.workflow_id != workflow['workflow_id']:
        raise HTTPException(409, 'This repair has changed. Use the latest repair controls.')
    if workflow['status'] == 'resolved':
        raise HTTPException(409, 'This repair already has a maintenance report.')
    current = workflow.get('current_step')
    expected = current['step_id'] if current else ''
    if action.step_id != expected:
        raise HTTPException(409, 'This step has already changed. Use the latest repair controls.')
    if action.action == 'done' and (not current or workflow['status'] != 'awaiting_done'):
        raise HTTPException(409, 'There is no repair step waiting for completion.')
    draft = deepcopy(workflow)
    if action.action == 'done':
        draft['completed_steps'].append({**draft['current_step'],
                                        'confirmed_at': datetime.now(timezone.utc).isoformat()})
        draft['current_step'] = None
        draft['status'] = 'awaiting_step'
    return draft


def workflow_response(result, workflow):
    result['safety_warning'] = SAFETY_WARNING
    result['repair_workflow'] = deepcopy(workflow)
    return result


def no_further_step(workflow):
    workflow['status'] = 'awaiting_resolution'
    return workflow_response({'blocks': [{'type': 'text', 'content': (
        'No further supported repair step is available from the retrieved manual evidence. '
        'If the issue is resolved, select Issue resolved to create the maintenance report. '
        'Otherwise describe the remaining symptoms; do not assume the repair is complete.'
    )}]}, workflow)


def resolve_report(workflow, action):
    # Resolution is an explicit technician acknowledgement, never a model guess.
    workflow['status'] = 'resolved'
    report = {'report_id': str(uuid4()), 'workflow_id': workflow['workflow_id'],
              'machine': action.machine.strip() or workflow['machine'] or 'Not recorded',
              'issue': action.issue.strip() or workflow['issue'],
              'cause': action.cause.strip() or 'Not confirmed',
              'action_taken': action.action_taken.strip() or '\n'.join(
                  step['instruction'] for step in workflow['completed_steps']) or 'Not recorded',
              'status': 'Resolved', 'created_at': datetime.now(timezone.utc).isoformat(),
              'confirmation': 'Resolution and report details confirmed by technician',
              'completed_steps': deepcopy(workflow['completed_steps'])}
    workflow['report'] = report
    return {'blocks': [{'type': 'text', 'content': 'Repair marked resolved. Maintenance report created.'}],
            'maintenance_report': report, 'repair_workflow': deepcopy(workflow)}
