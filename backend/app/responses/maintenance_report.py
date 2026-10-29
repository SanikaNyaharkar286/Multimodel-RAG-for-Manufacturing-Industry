"""Owner-scoped report snapshots and technician confirmations. No model or DB calls."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import base64
import re
from app.media.manual_assets import stored_image_bytes

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal
from types import SimpleNamespace


LOCAL_ZONE = timezone(timedelta(hours=5, minutes=30))
ISSUE_STATUSES = {'Resolved', 'Unresolved', 'Partially Resolved', 'Under Investigation',
                  'Awaiting Spare Parts', 'Awaiting OEM Support', 'Information'}
OPERATIONAL_STATUSES = {'Operational', 'Not Operational', 'Restricted Operation',
                        'Under Maintenance', 'Awaiting Verification', 'Not Recorded'}
TEST_RESULTS = {'Pass', 'Fail', 'Not tested'}
EXTRA_FIELDS = {
    'machine_type', 'serial_number', 'installation_date', 'machine_location',
    'operating_hours', 'manufacturer', 'warranty_status', 'designation', 'contact_number',
    'service_company', 'supervisor_name', 'service_start', 'service_end',
    'fault_started_at', 'fault_reported_at', 'fault_occurrence', 'fault_frequency',
    'machine_state', 'severity', 'production_impact', 'downtime', 'reported_by',
    'operational_status', 'repair_status', 'functional_test', 'safety_test',
    'performance_observations', 'technician_verification', 'return_to_service',
    'remaining_work', 'next_action', 'responsible_person', 'expected_resolution_at',
    'components_inspected', 'components_repaired', 'components_replaced', 'part_numbers',
    'parts_quantity', 'replacement_status', 'repair_date', 'post_repair_test',
    'preventive_recommendations', 'escalation_required', 'assistance_requested',
    'escalation_status', 'escalation_date', 'follow_up_owner',
}
DATE_FIELDS = {'installation_date', 'service_start', 'service_end', 'fault_started_at',
               'fault_reported_at', 'expected_resolution_at', 'repair_date', 'escalation_date'}

# ==========================================================
# FUTURE FEATURE - CUSTOMER / COMPANY DETAILS
# Disabled: customer/plant/address/department/location/contact/manufacturer/
# supplier/telephone/email data is not currently stored as customer records.
# TODO: Enable only when verified customer/asset records become available.
# No rendering, database queries, authentication changes or required API fields.
# ==========================================================
# FUTURE FEATURE - PREVIOUS MACHINE MAINTENANCE HISTORY
# Disabled: previous service date/type/technician/fault/repair/replaced parts/
# provider/result/next maintenance are not currently stored as asset history.
# TODO: Enable after verified historical maintenance records become available.
# ==========================================================


class StepConfirmation(BaseModel):
    model_config = ConfigDict(extra='forbid')
    step_id: str = Field(min_length=1, max_length=160)
    status: Literal['Completed', 'Pending', 'Skipped', 'Failed', 'Not performed']
    observation: str = Field(default='', max_length=20000)
    result: Literal['Passed', 'Failed', 'Inconclusive', 'Not tested'] = 'Not tested'
    notes: str = Field(default='', max_length=20000)
    parts_used: str = Field(default='', max_length=4000)
    action_performed: str = Field(default='', max_length=20000)
    completed_at: str = Field(default='', max_length=100)


def parse_time(value, label='Date'):
    """Accept a confirmed calendar date or an explicitly timezone-aware instant."""
    value = str(value or '').strip()
    if not value:
        return None
    try:
        if re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            return datetime.combine(date.fromisoformat(value), datetime.min.time(), LOCAL_ZONE)
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None or result.utcoffset() is None:
            raise ValueError('Missing timezone')
        return result
    except (ValueError, TypeError) as error:
        raise ValueError(f'{label} must be a valid date or include a timezone.') from error


def apply_profile(report, identity, profile=None):
    """Identity fields are populated by trusted server authentication only."""
    profile = profile or {}
    report['technician_email'] = identity.email
    report['technician_name'] = profile.get('fullName') or profile.get('displayName') or ''
    report['employee_id'] = profile.get('employeeId') or profile.get('serviceId') or ''
    for target, keys in {
        'designation': ('designation',), 'contact_number': ('phoneNumber', 'contactNumber', 'phone'),
        'service_company': ('serviceCompany',), 'supervisor_name': ('supervisorName',),
        'assigned_date': ('assignedDate',),
    }.items():
        if not report.get(target):
            report[target] = next((profile[key] for key in keys if profile.get(key)), '')
    for step in report.get('steps', []):
        if step.get('performed_by') == identity.email and report['technician_name']:
            step['performed_by'] = report['technician_name']
    return report


def _plain_blocks(blocks):
    return '\n\n'.join(str(block.get('content', '')) for block in blocks
                       if block.get('type') == 'text' and not str(block.get('content', '')).startswith('### Step '))


def enrich_snapshot(report, entry):
    """Capture performed, presented and privately prepared steps without changing chat."""
    workflow = report.get('workflow') or {}
    for field in EXTRA_FIELDS:
        report.setdefault(field, '')
    report.update(report_version=2, preparation_status='Draft', review_status='Pending',
                  approval_status='Pending', return_to_service='Not authorized')
    report['source_machine'] = report.get('machine', '')
    report['operational_status'] = report.get('operational_status') or 'Not Recorded'
    report['functional_test'] = report.get('functional_test') or 'Not tested'
    report['safety_test'] = report.get('safety_test') or 'Not tested'
    report['repair_status'] = report.get('repair_status') or 'Not recorded'
    report['model'] = report.get('model') or workflow.get('model', '')
    report['asset_id'] = report.get('asset_id') or workflow.get('asset_id', '')
    for field in ('machine_type', 'serial_number', 'installation_date', 'machine_location',
                  'operating_hours', 'manufacturer', 'warranty_status'):
        report[field] = report.get(field) or workflow.get(field, '')
    report['reported_by'] = report.get('reported_by') or report['technician_email']
    report['status'] = ('Resolved' if workflow.get('status') == 'resolved' else
                        'Unresolved' if workflow.get('status') == 'unresolved' else
                        'Under Investigation' if workflow else 'Information')
    report['escalation_required'] = 'Yes' if workflow.get('status') == 'unresolved' else 'No'
    # Never use chat creation or report generation as the fault's onset date.
    report['fault_duration_days'] = None
    recorded_actions = '\n'.join(item['instruction'] for item in workflow.get('completed_steps', []))
    report['action_taken_source'] = ('guided_confirmations' if report.get('action_taken', '') == recorded_actions
                                     else 'technician_recorded')
    steps, evidence, guidance = {}, {}, []
    history = report.get('history', [])
    sources_by_step = {}
    for message in history:
        response = message.get('response') or {}
        current = (response.get('repair_workflow') or {}).get('current_step') or {}
        step_id = current.get('step_id') or message.get('step_id', '')
        sources_by_step[step_id] = response.get('sources', []) or sources_by_step.get(step_id, [])
        if response.get('blocks'):
            guidance.append({'step_id': step_id, 'blocks': deepcopy(response['blocks']),
                             'sources': deepcopy(response.get('sources', []))})

    def add_step(step, completed=False, future=False):
        if hasattr(step, 'model_dump'):
            step = step.model_dump()
        if not step or not step.get('instruction'):
            return
        key = step.get('step_id') or ('planned:' + str(step.get('step_key') or len(steps) + 1))
        if key in steps and not completed:
            return
        blocks = deepcopy(step.get('blocks') or step.get('supporting_blocks') or [])
        sources = sources_by_step.get(key, [])
        item = {'step_id': key, 'number': step.get('number', len(steps) + 1),
                'title': step.get('title', ''), 'instruction': step['instruction'],
                'description': _plain_blocks(blocks), 'status': 'Completed' if completed else 'Pending',
                'recommended': True, 'performed_by': (report.get('technician_name') or
                   report['technician_email']) if completed else '',
                'completed_at': step.get('confirmed_at', '') if completed else '',
                'observation': step.get('observation', '') if completed else '',
                'result': 'Not tested', 'action_performed': step['instruction'] if completed else '',
                'notes': '', 'parts_used': '', 'precautions': deepcopy(step.get('precautions', [])),
                'sources': deepcopy(sources), 'blocks': blocks, 'planned_only': future}
        steps[key] = item

    for step in workflow.get('presented_steps', []):
        add_step(step)
    add_step(workflow.get('current_step'))
    for step in workflow.get('completed_steps', []):
        add_step(step, completed=True)
    cache = getattr(entry, 'repair_cache', None) or {}
    if workflow and cache.get('workflow_id') == workflow.get('workflow_id'):
        # Only this case's already prepared plan. No retrieval or model request.
        for answer in cache.get('remaining_answers', []):
            step = answer if isinstance(answer, dict) else answer.repair_step
            add_step(step, future=True)
        for item in steps.values():
            if not item['planned_only']:
                continue
            prepared = next((value for value in cache.get('prepared_results', {}).values()
                if value.get('blocks') and value['blocks'][0].get('content', '').partition('\n')[2]
                == item['instruction']), None)
            if prepared:
                item['blocks'] = deepcopy(prepared['blocks'])
                item['description'] = _plain_blocks(item['blocks'])
                item['sources'] = deepcopy(prepared.get('sources', []))
    for observation in workflow.get('observations', []):
        item = steps.get(observation.get('step_id'))
        if item and observation.get('text'):
            item['notes'] = '\n'.join(filter(None, [item['notes'], observation['text']]))
    for incident in workflow.get('reported_issues', []):
        item = steps.get(incident.get('step_id'))
        if item:
            item['notes'] = '\n'.join(filter(None, [item['notes'], incident.get('report')]))

    def add_image(block, step_id='', photograph=False):
        if photograph:
            key = block['id']
            caption = 'Technician-uploaded photograph'
            source = None
        else:
            path = str(block.get('src', ''))
            if not path.startswith('/images/'):
                return
            key = path.rsplit('/', 1)[-1]
            caption = block.get('caption') or 'Manual reference diagram'
            source = block.get('source')
            if key not in report['images'] and key in entry.assets:
                report['images'][key] = 'data:image/png;base64,' + base64.b64encode(stored_image_bytes(entry.assets[key][1])).decode()
        if key not in report['images']:
            return
        evidence.setdefault(key, {'image_id': key, 'caption': caption,
            'description': block.get('description', ''), 'step_id': step_id,
            'source_type': 'Technician photograph' if photograph else 'Approved manual diagram',
            'source': deepcopy(source)})

    for message in history:
        if message.get('image'):
            add_image(message, message.get('step_id', ''), photograph=True)
        response = message.get('response') or {}
        current = (response.get('repair_workflow') or {}).get('current_step') or {}
        for block in response.get('blocks', []):
            if block.get('type') == 'image':
                add_image(block, current.get('step_id', ''))
    for item in steps.values():
        for block in item['blocks']:
            if hasattr(block, 'model_dump'):
                block = block.model_dump()
            if block.get('type') == 'image':
                add_image(block, item['step_id'])
    report['steps'] = sorted(steps.values(), key=lambda item: item['number'])
    for index, item in enumerate(report['steps'], 1):
        item['number'] = index
    report['evidence'] = list(evidence.values())
    report['supporting_evidence'] = '\n'.join(item['caption'] for item in evidence.values())
    report['manual_guidance'] = guidance
    possible_causes = []
    for item in guidance:
        if not item.get('sources'):
            continue
        for block in item['blocks']:
            if block.get('type') == 'table':
                indices = [index for index, column in enumerate(block.get('columns', []))
                           if re.search(r'\b(?:possible |probable )?causes?\b', str(column), re.I)]
                possible_causes.extend(str(row[index]) for row in block.get('rows', [])
                                       for index in indices if len(row) > index and row[index])
    report['possible_causes'] = list(dict.fromkeys(possible_causes))
    report['diagnostic_findings'] = '\n'.join(item['observation'] for item in report['steps']
        if item['status'] in ('Completed', 'Failed') and item['observation'])
    return report


def upgrade_report(report):
    """Project older owner-checked reports onto the new layout without new queries."""
    if report.get('report_version') == 2:
        return report
    original_status = report.get('status')
    report.setdefault('images', {})
    report.setdefault('history', [])
    enrich_snapshot(report, SimpleNamespace(assets={}, repair_cache=None))
    if original_status in ISSUE_STATUSES:
        report['status'] = original_status
    elif original_status == 'Technician reported resolved':
        report['status'] = 'Resolved'
    return report


def validate_details(report):
    """Validate facts independently from resolution and operational approval."""
    now = datetime.now(timezone.utc)
    if (report.get('source_machine') and report.get('machine', '').strip().casefold() !=
            report['source_machine'].strip().casefold()):
        raise ValueError('Machine name must match this incident and its manual evidence. Use Asset ID for a plant identifier.')
    parsed = {key: parse_time(report.get(key), key.replace('_', ' ')) for key in DATE_FIELDS}
    for key in DATE_FIELDS - {'expected_resolution_at'}:
        if parsed[key] and parsed[key] > now:
            raise ValueError(f'{key.replace("_", " ")} cannot be in the future.')
    for first, last in [('service_start', 'service_end'), ('fault_started_at', 'fault_reported_at')]:
        if parsed[first] and parsed[last] and parsed[first] > parsed[last]:
            raise ValueError(f'{last.replace("_", " ")} cannot precede {first.replace("_", " ")}.')
    for key, choices in [('status', ISSUE_STATUSES), ('operational_status', OPERATIONAL_STATUSES),
                         ('functional_test', TEST_RESULTS), ('safety_test', TEST_RESULTS),
                         ('escalation_required', {'Yes', 'No'}),
                         ('return_to_service', {'Not authorized'}),
                         ('escalation_status', {'Not submitted', 'Required', 'Draft', 'Pending'})]:
        if report.get(key) and report[key] not in choices:
            raise ValueError(f'Invalid {key.replace("_", " ")}.')
    # A report is not a submission or an operational release mechanism.
    report['return_to_service'] = 'Not authorized'
    report['approval_status'] = 'Pending'
    report['fault_duration_days'] = ((now.astimezone(LOCAL_ZONE).date() -
        parsed['fault_started_at'].astimezone(LOCAL_ZONE).date()).days if parsed['fault_started_at'] else None)
    return report


def update_confirmations(report, confirmations, identity):
    steps = {item['step_id']: item for item in report.get('steps', [])}
    seen = set()
    for confirmation in confirmations:
        if confirmation.step_id not in steps or confirmation.step_id in seen:
            raise ValueError('Select a unique troubleshooting step belonging to this report.')
        seen.add(confirmation.step_id)
        instant = parse_time(confirmation.completed_at, 'Step completion time')
        if instant and instant > datetime.now(timezone.utc):
            raise ValueError('Step completion time cannot be in the future.')
        if confirmation.status in ('Pending', 'Skipped', 'Not performed') and (
                confirmation.completed_at or confirmation.action_performed or confirmation.parts_used or
                confirmation.result != 'Not tested'):
            raise ValueError('Unperformed steps cannot contain completed actions, parts or test results.')
        item = steps[confirmation.step_id]
        item.update(confirmation.model_dump(exclude={'step_id'}))
        performed = confirmation.status in ('Completed', 'Failed')
        item['performed_by'] = (report.get('technician_name') or identity.email) if performed else ''
        item['confirmation_recorded_at'] = datetime.now(timezone.utc).isoformat()
        item['confirmed_by_uid'] = identity.uid
    report['diagnostic_findings'] = '\n'.join(item['observation'] for item in steps.values()
        if item['status'] in ('Completed', 'Failed') and item.get('observation'))
    if report.get('action_taken_source') == 'guided_confirmations':
        report['action_taken'] = '\n'.join(item.get('action_performed', '') for item in steps.values()
            if item['status'] in ('Completed', 'Failed') and item.get('action_performed'))
    return report
