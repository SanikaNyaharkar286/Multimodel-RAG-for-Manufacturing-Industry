# makes maintenance reports for a chat or repair case, lets the technician edit them,
# and turns them into a pdf file.
import base64
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4
import asyncio
import logging

from app.observability.timing import span
from app.sessions import persistence
from app.media.manual_assets import stored_image_bytes
from app.responses.maintenance_report import EXTRA_FIELDS, enrich_snapshot

logger = logging.getLogger('uvicorn.error.documentation')
_SAVE_TASKS = set()

# the report fields a technician is allowed to edit
EDITABLE = {'machine', 'model', 'asset_id', 'issue', 'cause', 'action_taken', 'verification',
            'remaining_issues', 'escalation_notes', 'notes', 'title', 'content', 'status'} | EXTRA_FIELDS


# builds a new report from the chosen answer or repair case.
# it collects the chat messages, the repair steps and the images shown in that case
def create_document(entry, identity, case_id='', message_id=''):
    workflow = entry.repair_workflow if entry.repair_workflow and entry.repair_workflow['workflow_id'] == case_id else entry.cases.get(case_id)
    if case_id and not workflow:
        raise ValueError('Case not found in this conversation.')
    history = [deepcopy(item) for item in entry.history if
               (case_id and item.get('case_id') == case_id) or
               (not case_id and item.get('id') == message_id)]
    if not case_id and history:
        index = next(i for i, item in enumerate(entry.history) if item.get('id') == message_id)
        if index and entry.history[index - 1]['role'] == 'user':
            history.insert(0, deepcopy(entry.history[index - 1]))
    if not history and not workflow:
        raise ValueError('Select an answer or troubleshooting case.')
    for item in history:
        if item.get('response'):
            item['response'].pop('documentation', None)
            item['response'].pop('maintenance_report', None)
    now = datetime.now(timezone.utc).isoformat()
    report_id = str(uuid4())
    report = {
        'report_id': report_id, 'report_number': f"TMR-{now[:10].replace('-', '')}-{report_id.replace('-', '').upper()}",
        'session_id': entry.id, 'case_id': case_id, 'message_id': message_id, 'created_at': now, 'updated_at': now,
        'timezone': 'Asia/Calcutta', 'technician_email': identity.email,
        'title': 'Technical Service & Maintenance Report' if workflow else 'Machine information',
        'machine': (workflow or {}).get('machine', ''), 'model': '', 'asset_id': '',
        'issue': (workflow or {}).get('issue', ''), 'cause': 'Not confirmed',
        'action_taken': '\n'.join(step['instruction'] for step in (workflow or {}).get('completed_steps', [])),
        'verification': '', 'remaining_issues': '', 'escalation_notes': '', 'notes': '',
        'status': 'Technician reported resolved' if (workflow or {}).get('status') == 'resolved' else ('Unresolved' if workflow else 'Information'),
        'content': '', 'history': history, 'workflow': deepcopy(workflow),
        'revision': 1, 'edits': [], 'images': {},
    }
    if workflow and workflow.get('report'):
        for key in ('machine', 'issue', 'cause', 'action_taken'):
            report[key] = workflow['report'].get(key, report[key])
    # only keep images that were shown in this case.
    # this way the pdf and saved report still work after the session expires,
    # and no other case's images get in
    for message in history:
        if message.get('image', '').startswith('data:image/'):
            report['images'][message['id']] = message['image']
        for block in message.get('response', {}).get('blocks', []):
            if block.get('type') == 'image':
                key = block['src'].rsplit('/', 1)[-1]
                if key in entry.assets:
                    report['images'][key] = 'data:image/png;base64,' + base64.b64encode(stored_image_bytes(entry.assets[key][1])).decode()
    return enrich_snapshot(report, entry)


# saves the report for its owner
def save_document(report, uid):
    persistence.save_report(report, uid)


# saves the report in the background so the chat reply is not slowed down
def queue_document_save(report, uid):
    """saves a report in the background, away from the chat reply."""
    task = asyncio.create_task(asyncio.to_thread(save_document, report, uid))
    _SAVE_TASKS.add(task)

    # runs when the save ends. removes the task and logs an error if it failed
    def finished(completed):
        _SAVE_TASKS.discard(completed)
        if not completed.cancelled() and completed.exception():
            logger.error('Background report save failed report_id=%s error_type=%s',
                         report.get('report_id'), type(completed.exception()).__name__)

    task.add_done_callback(finished)
    return task


# gives the report without its images, so it is light to send to the frontend
def public_document(report):
    return {key: value for key, value in report.items() if key != 'images'}


# Renders the immutable snapshot using the shared A4 report layout.
def render_pdf(report):
    from app.responses.maintenance_pdf import render_maintenance_pdf
    with span('pdf_generation'):
        return render_maintenance_pdf(deepcopy(report))
