"""Case-scoped report snapshots, editable fields and isolated PDF rendering."""
import base64
from copy import deepcopy
from datetime import datetime, timezone, timedelta
from html import escape
from io import BytesIO
from uuid import uuid4
import re

import pymupdf

from app.observability.timing import span
from app.sessions import persistence

EDITABLE = {'machine', 'model', 'asset_id', 'issue', 'cause', 'action_taken', 'verification',
            'remaining_issues', 'escalation_notes', 'notes', 'title', 'content', 'status'}


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
        'report_id': report_id, 'report_number': f"MR-{now[:10].replace('-', '')}-{report_id[:8].upper()}",
        'session_id': entry.id, 'case_id': case_id, 'created_at': now, 'updated_at': now,
        'timezone': 'Asia/Calcutta', 'technician_email': identity.email,
        'title': 'Maintenance work report' if workflow else 'Machine information',
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
    # Embed only images actually presented in the selected case, so PDFs and
    # saved reports survive session expiry without exposing another case.
    for message in history:
        if message.get('image', '').startswith('data:image/'):
            report['images'][message['id']] = message['image']
        for block in message.get('response', {}).get('blocks', []):
            if block.get('type') == 'image':
                key = block['src'].rsplit('/', 1)[-1]
                if key in entry.assets:
                    report['images'][key] = 'data:image/png;base64,' + base64.b64encode(entry.assets[key][1]).decode()
    return report


def save_document(report, uid):
    persistence.save_report(report, uid)


def public_document(report):
    return {key: value for key, value in report.items() if key != 'images'}


def report_html(report):
    def text(value): return escape(str(value or 'Not recorded'))
    def para(value): return '<p>' + text(value).replace('\n', '<br>') + '</p>'
    def markdown(value):
        lines = []
        for line in str(value or '').splitlines():
            safe = escape(line)
            safe = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', safe)
            safe = re.sub(r'`([^`]+)`', r'<code>\1</code>', safe)
            heading = re.match(r'^(#{1,6})\s+(.+)', safe)
            if heading:
                level = min(4, len(heading[1]) + 1)
                lines.append(f'<h{level}>{heading[2]}</h{level}>')
            elif re.match(r'^[-*]\s', safe):
                lines.append('<p>• ' + safe[2:] + '</p>')
            elif safe.strip() not in ('---', '```'):
                lines.append('<p>' + safe + '</p>')
        return ''.join(lines)
    def image(key, caption=''):
        data = report['images'].get(key)
        return f'<figure><img src="{data}"/><figcaption>{text(caption)}</figcaption></figure>' if data else ''
    body = f"<h1>{text(report['title'])}</h1>"
    body += para(f"{report['report_number']} | {report['status']}")
    body += '<table>' + ''.join(f'<tr><th>{text(label)}</th><td>{text(value)}</td></tr>' for label, value in [
        ('Conversation', report['session_id']), ('Case', report['case_id']),
        ('Created (Asia/Calcutta)', datetime.fromisoformat(report['created_at']).astimezone(timezone(timedelta(hours=5, minutes=30))).strftime('%d %b %Y, %I:%M %p')),
        ('Display timezone', report['timezone']), ('Technician', report.get('technician_name') or report['technician_email']),
        ('Machine', report['machine']), ('Model', report['model']), ('Asset ID', report['asset_id'])]) + '</table>'
    for label, key in [('Reported problem','issue'), ('Confirmed cause','cause'), ('Work reported','action_taken'),
                       ('Verification results','verification'), ('Remaining issues','remaining_issues'),
                       ('Escalation notes','escalation_notes'), ('Notes','notes'), ('Editable documentation','content')]:
        body += f'<h2>{label}</h2>' + para(report[key])
    workflow = report.get('workflow') or {}
    if workflow.get('safety'):
        body += '<h2>Safety presented</h2>' + ''.join(para(bullet) for bullet in workflow['safety'].get('bullets', []))
    body += '<h2>Recorded confirmations</h2>'
    body += ''.join(para(f"Step {item['step_id']}: {item['statement']}") for item in workflow.get('safety_confirmations', [])) or para('Not recorded')
    body += '<h2>Completed actions</h2>' + (''.join(para(step['instruction']) for step in workflow.get('completed_steps', [])) or para('Not recorded'))
    body += '<h2>Selected conversation evidence</h2>'
    for message in report['history']:
        if message['role'] == 'user':
            body += '<h3>Technician observation / question</h3>' + para(message.get('text')) + image(message['id'], 'Technician upload')
        else:
            for block in message.get('response', {}).get('blocks', []):
                kind = block.get('type')
                if kind == 'text': body += markdown(block.get('content'))
                elif kind == 'image':
                    body += image(block['src'].rsplit('/', 1)[-1], block.get('caption')) + para(block.get('description'))
                elif kind == 'table':
                    body += '<table><tr>' + ''.join('<th>'+text(c)+'</th>' for c in block.get('columns', [])) + '</tr>'
                    body += ''.join('<tr>'+''.join('<td>'+text(c)+'</td>' for c in row)+'</tr>' for row in block.get('rows', [])) + '</table>'
            for source in message.get('response', {}).get('sources', []):
                body += para(f"{source['document_name']}, PDF page {source['page_number']}")
    return body


def render_pdf(report):
    with span('pdf_generation'):
        output = BytesIO()
        writer = pymupdf.DocumentWriter(output)
        story = pymupdf.Story(html=report_html(report), user_css='''
            body { font-family: sans-serif; font-size: 10pt; color: #173052; }
            h1 { font-size: 22pt; } h2 { font-size: 13pt; margin-top: 16pt; }
            p { line-height: 1.4; } table { border-collapse: collapse; width: 100%; }
            td,th { border: 1px solid #ccd6e3; padding: 6pt; text-align: left; }
            img { max-width: 440pt; max-height: 400pt; } figure { margin: 14pt 0; }
            figcaption { font-size: 9pt; }''')
        page = pymupdf.paper_rect('a4')
        story.write(writer, lambda number, filled: (page, page + (40, 40, -40, -45), None))
        writer.close()
        doc = pymupdf.open(stream=output.getvalue(), filetype='pdf')
        for index, pdf_page in enumerate(doc):
            pdf_page.insert_text((40, page.height - 22),
                                 f"{report['report_number']} | Page {index + 1} of {len(doc)}", fontsize=8)
        return doc.tobytes(garbage=4, deflate=True)
