"""Server-controlled repair acknowledgements and technician-confirmed reports."""

from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import HTTPException

SAFETY_WARNING = ('Before physical inspection or repair, follow the authorized shutdown, energy isolation and verification procedure. '
                  'Keep clear of moving parts. Use PPE specified for the task; do not wear gloves near rotating machinery.')


def new_workflow(topic, chunks):
    machines = list(dict.fromkeys(row.get('machine_name', '') for row in chunks
                                  if row.get('machine_name')))
    return {'workflow_id': str(uuid4()), 'machine': machines[0] if len(machines) == 1 else '',
            'issue': topic, 'status': 'awaiting_step', 'completed_steps': [],
            'reported_issues': [], 'current_step': None, 'plan_version': 1,
            'safety_confirmations': [], 'observations': [], 'presented_steps': []}


def validate_action(workflow, action):
    if not workflow or action.workflow_id != workflow['workflow_id']:
        raise HTTPException(409, 'This repair has changed. Use the latest repair controls.')
    if workflow['status'] in ('resolved', 'unresolved'):
        raise HTTPException(409, 'This repair already has a maintenance report.')
    current = workflow.get('current_step')
    if action.plan_version != workflow.get('plan_version', 1):
        raise HTTPException(409, 'The repair plan changed. Refresh the active step before continuing.')
    expected = current['step_id'] if current else ''
    if action.step_id != expected:
        raise HTTPException(409, 'This step has already changed. Use the latest repair controls.')
    if action.action == 'done' and (not current or workflow['status'] != 'awaiting_done'):
        raise HTTPException(409, 'There is no repair step waiting for completion.')
    if action.action == 'done' and current.get('branches'):
        if action.observation not in [branch['label'] for branch in current['branches']]:
            raise HTTPException(400, 'Select the observed result before continuing.')
    if action.action == 'report_issue':
        if workflow['status'] not in ('awaiting_done', 'awaiting_step', 'awaiting_resolution'):
            raise HTTPException(409, 'There is no repair step waiting for an issue report.')
        if not action.issue_report.strip():
            raise HTTPException(400, 'Describe what happened during this step.')
    draft = deepcopy(workflow)
    if action.action == 'done':
        if action.safety_confirmed:
            draft.setdefault('safety_confirmations', []).append({
                'step_id': current['step_id'], 'plan_version': draft.get('plan_version', 1),
                'confirmed_at': datetime.now(timezone.utc).isoformat(),
                'statement': 'Technician attested to following the applicable isolation and safety procedure.'})
        draft['completed_steps'].append({**draft['current_step'],
                                        'observation': action.observation,
                                        'confirmed_at': datetime.now(timezone.utc).isoformat()})
        draft['current_step'] = None
        draft['status'] = 'awaiting_step'
    elif action.action == 'report_issue':
        draft['plan_version'] = draft.get('plan_version', 1) + 1
        draft.setdefault('reported_issues', []).append({
            'step_id': (current or {}).get('step_id', ''),
            'step_number': (current or {}).get('number', len(draft['completed_steps'])),
            'instruction': (current or {}).get('instruction', ''),
            'report': action.issue_report.strip(),
            'reported_at': datetime.now(timezone.utc).isoformat(),
        })
        # Keep the current step visible until the agent supplies a revised,
        # evidence-grounded action. The next response may replace it safely.
        draft['status'] = 'awaiting_step'
    return draft


def workflow_response(result, workflow):
    current = workflow.get('current_step')
    if current and any(block.get('type') == 'text' and '### Step ' in block.get('content', '') for block in result.get('blocks', [])):
        current['blocks'] = deepcopy(result['blocks'])
        presented = workflow.setdefault('presented_steps', [])
        if not any(item['step_id'] == current['step_id'] for item in presented):
            presented.append(deepcopy(current))
    result['safety_warning'] = SAFETY_WARNING
    result['repair_workflow'] = deepcopy(workflow)
    if workflow.get('safety'):
        result['safety'] = deepcopy(workflow['safety'])
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
    resolved = action.action != 'unresolved'
    workflow['status'] = 'resolved' if resolved else 'unresolved'
    report_id = str(uuid4())
    created_at = datetime.now(timezone.utc).isoformat()
    report = {'report_id': report_id, 'report_number': f"MR-{created_at[:10].replace('-', '')}-{report_id.replace('-', '')[:8].upper()}", 'workflow_id': workflow['workflow_id'],
              'machine': action.machine.strip() or workflow['machine'] or 'Not recorded',
              'issue': action.issue.strip() or workflow['issue'],
              'cause': action.cause.strip() or 'Not confirmed',
              'action_taken': action.action_taken.strip() or '\n'.join(
                  step['instruction'] for step in workflow['completed_steps']) or 'Not recorded',
              'status': 'Technician reported resolved' if resolved else 'Unresolved', 'report_status': 'Pending Verification', 'supervisor_status': 'Not recorded',
              'created_at': created_at, 'updated_at': created_at,
              'confirmation': 'Technician reported resolution; independent verification not recorded' if resolved else 'Technician reported unresolved',
              'completed_steps': deepcopy(workflow['completed_steps']),
              'safety_checklist': {'power_isolated': False, 'lockout_tagout': False,
                                   'guards_secured': False, 'ppe_used': False},
              'verification': '', 'attachments': [], 'parts_used': [], 'audit_history': [],
              'ai_suggested_cause': '', 'root_cause_confidence': 'Not confirmed'}
    report['reported_issues'] = deepcopy(workflow.get('reported_issues', []))
    workflow['report'] = report
    return {'blocks': [{'type': 'text', 'content': 'Technician result recorded. Open the documentation to review and save the report.'}],
            'maintenance_report': report, 'repair_workflow': deepcopy(workflow)}
