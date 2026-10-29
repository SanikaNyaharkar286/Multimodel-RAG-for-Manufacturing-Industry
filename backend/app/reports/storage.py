"""Immutable, private submitted PDFs and their exact report snapshots."""
import gzip
import hashlib
import json
from google.api_core.exceptions import PreconditionFailed
from fastapi import HTTPException
from app import config as cfg
from app.media.manual_images import storage_client
from app.responses.documentation import render_pdf
from app.sessions.persistence import encode, decode

MAX_PDF_BYTES = 100 * 1024 * 1024


def _write_once(path, data, content_type, snapshot_hash):
    blob = storage_client().bucket(cfg.BUCKET).blob(path)
    blob.metadata = {'snapshot_sha256': snapshot_hash, 'sha256': hashlib.sha256(data).hexdigest()}
    try:
        blob.upload_from_string(data, content_type=content_type, if_generation_match=0, timeout=60)
    except PreconditionFailed:
        # A concurrent request or a retry after a DB failure may already have
        # stored this revision. Reuse it, never overwrite another snapshot.
        pass
    blob.reload(timeout=15)
    if (blob.metadata or {}).get('snapshot_sha256') != snapshot_hash or not blob.generation or not blob.size:
        raise HTTPException(409, 'This report revision changed. Reopen it before submitting.')
    return {'path': path, 'generation': int(blob.generation), 'size': int(blob.size),
            'sha256': (blob.metadata or {}).get('sha256')}


def persist_report(report, owner_uid):
    raw = json.dumps(report, default=encode, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
    if len(raw) > MAX_PDF_BYTES:
        raise HTTPException(413, 'Report snapshot exceeds the storage limit.')
    digest = hashlib.sha256(raw).hexdigest()
    owner_key = hashlib.sha256(owner_uid.encode()).hexdigest()
    prefix = f'maintenance-reports/{owner_key}/{report["report_id"]}/revision-{report["revision"]}'
    pdf = render_pdf(report)
    if not pdf.startswith(b'%PDF-') or len(pdf) > MAX_PDF_BYTES:
        raise HTTPException(413, 'Report PDF is invalid or exceeds the storage limit.')
    packed = gzip.compress(raw)
    if len(packed) > MAX_PDF_BYTES:
        raise HTTPException(413, 'Report snapshot exceeds the storage limit.')
    snapshot = _write_once(f'{prefix}/snapshot.json.gz', packed, 'application/gzip', digest)
    stored_pdf = _write_once(f'{prefix}/report.pdf', pdf, 'application/pdf', digest)
    # Both objects were verified before Firestore can advertise a submission.
    return {'pdf_storage': stored_pdf, 'snapshot_storage': snapshot, 'snapshot_sha256': digest,
            'pdf_gcs_path': stored_pdf['path']}


def read_object(record, kind='pdf_storage'):
    value = record[kind]
    expected_prefix = f'maintenance-reports/{hashlib.sha256(record["owner_uid"].encode()).hexdigest()}/{record["report_id"]}/'
    if not value['path'].startswith(expected_prefix) or '..' in value['path'].split('/'):
        raise HTTPException(503, 'Stored report reference is invalid.')
    if not 0 < value['size'] <= MAX_PDF_BYTES:
        raise HTTPException(503, 'Stored report exceeds its size limit.')
    blob = storage_client().bucket(cfg.BUCKET).blob(value['path'], generation=value['generation'])
    data = blob.download_as_bytes(if_generation_match=value['generation'], timeout=60)
    if len(data) != value['size'] or hashlib.sha256(data).hexdigest() != value['sha256']:
        raise HTTPException(503, 'Stored report integrity check failed.')
    return data


def read_snapshot(record):
    raw = gzip.decompress(read_object(record, 'snapshot_storage'))
    if hashlib.sha256(raw).hexdigest() != record['snapshot_sha256']:
        raise HTTPException(503, 'Stored report integrity check failed.')
    return json.loads(raw, object_hook=decode)
