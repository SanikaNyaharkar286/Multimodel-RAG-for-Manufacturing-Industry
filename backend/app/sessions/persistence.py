"""Local durable snapshots; contains no credentials and adds no cloud service."""
import base64
import json
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from app import config as cfg


def encode(value):
    if isinstance(value, bytes):
        return {'__bytes__': base64.b64encode(value).decode()}
    if hasattr(value, 'model_dump'):
        return {'__answer__': value.model_dump()}
    if isinstance(value, datetime):
        return {'__datetime__': value.isoformat()}
    if isinstance(value, date):
        return {'__date__': value.isoformat()}
    if isinstance(value, Decimal):
        return {'__decimal__': str(value)}
    raise TypeError(type(value).__name__)


def decode(value):
    if '__bytes__' in value:
        return base64.b64decode(value['__bytes__'])
    if '__answer__' in value:
        from app.responses.schemas import AgentAnswer
        return AgentAnswer.model_validate(value['__answer__'])
    if '__datetime__' in value:
        return datetime.fromisoformat(value['__datetime__'])
    if '__date__' in value:
        return date.fromisoformat(value['__date__'])
    if '__decimal__' in value:
        return Decimal(value['__decimal__'])
    return value


@contextmanager
def connection():
    path = Path(cfg.BACKEND_ROOT) / 'data' / 'workspace.sqlite3'
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=15)
    db.execute('CREATE TABLE IF NOT EXISTS snapshots (id TEXT PRIMARY KEY, body TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS reports (id TEXT PRIMARY KEY, owner TEXT NOT NULL, body TEXT NOT NULL)')
    try:
        with db:
            yield db
    finally:
        db.close()


def save_snapshot(entry, state):
    body = {name: getattr(entry, name) for name in (
        'id', 'user_id', 'owner_uid', 'expires_at', 'history', 'assets', 'uploaded_photo',
        'repair_workflow', 'repair_cache', 'cases', 'receipts')}
    body['state'] = state
    with connection() as db:
        db.execute('INSERT OR REPLACE INTO snapshots VALUES (?, ?)',
                   (entry.id, json.dumps(body, default=encode)))


def load_snapshot(session_id):
    with connection() as db:
        row = db.execute('SELECT body FROM snapshots WHERE id=?', (session_id,)).fetchone()
    return json.loads(row[0], object_hook=decode) if row else None


def delete_snapshot(session_id):
    with connection() as db:
        db.execute('DELETE FROM snapshots WHERE id=?', (session_id,))


def purge_expired_snapshots():
    with connection() as db:
        db.execute('DELETE FROM snapshots WHERE json_extract(body, "$.expires_at") <= ?', (time.time(),))


def save_report(report, owner, expected_revision=None):
    with connection() as db:
        if expected_revision is None:
            db.execute('INSERT OR REPLACE INTO reports VALUES (?, ?, ?)',
                       (report['report_id'], owner, json.dumps(report)))
        else:
            updated = db.execute('UPDATE reports SET body=? WHERE id=? AND owner=? AND json_extract(body, "$.revision")=?',
                                 (json.dumps(report), report['report_id'], owner, expected_revision))
            if updated.rowcount != 1:
                raise ValueError('This report has changed. Reopen it before saving.')


def load_report(report_id, owner):
    with connection() as db:
        row = db.execute('SELECT body FROM reports WHERE id=? AND owner=?', (report_id, owner)).fetchone()
    return json.loads(row[0]) if row else None
