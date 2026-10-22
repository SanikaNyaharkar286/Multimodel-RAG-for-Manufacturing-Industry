import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.sessions.session_store import store
from app.sessions import persistence
from app.responses.documentation import (EDITABLE, create_document, public_document,
                                         render_pdf, save_document)
from auth_system.security import require_approved, user_profile

router = APIRouter()


class CreateReport(BaseModel):
    case_id: str = Field(default='', max_length=80)
    message_id: str = Field(default='', max_length=80)


class EditReport(BaseModel):
    revision: int
    fields: dict[str, str]


async def get_report(report_id, identity):
    report = await asyncio.to_thread(persistence.load_report, report_id, identity.uid)
    if not report:
        raise HTTPException(404, 'Report not found.')
    return report


@router.post('/sessions/{session_id}/reports')
async def create_report(session_id: str, selection: CreateReport, identity=Depends(require_approved)):
    entry = await store.owned(session_id, identity)
    async with entry.lock:
        try:
            report = create_document(entry, identity, selection.case_id, selection.message_id)
        except ValueError as error:
            raise HTTPException(404, str(error)) from error
        profile = await asyncio.to_thread(user_profile, identity.uid) or {}
        report['technician_name'] = profile.get('fullName', '')
        await asyncio.to_thread(save_document, report, identity.uid)
        return public_document(report)


@router.get('/reports/{report_id}')
async def read_report(report_id: str, identity=Depends(require_approved)):
    return public_document(await get_report(report_id, identity))


@router.post('/reports/{report_id}')
async def edit_report(report_id: str, edit: EditReport, identity=Depends(require_approved)):
    report = await get_report(report_id, identity)
    if edit.revision != report['revision']:
        raise HTTPException(409, 'This report has changed. Reopen it before saving.')
    if set(edit.fields) - EDITABLE or any(len(value) > 20000 for value in edit.fields.values()):
        raise HTTPException(400, 'Invalid report fields.')
    report.update(edit.fields)
    report['revision'] += 1
    report['updated_at'] = datetime.now(timezone.utc).isoformat()
    report['edits'].append({'revision': report['revision'], 'updated_at': report['updated_at'],
                            'fields': sorted(edit.fields)})
    try:
        await asyncio.to_thread(persistence.save_report, report, identity.uid, edit.revision)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return public_document(report)


@router.get('/reports/{report_id}/pdf')
async def download_pdf(report_id: str, identity=Depends(require_approved)):
    report = await get_report(report_id, identity)
    data = await asyncio.to_thread(render_pdf, report)
    return Response(data, media_type='application/pdf', headers={
        'Content-Disposition': f'attachment; filename="{report["report_number"]}.pdf"', 'Cache-Control': 'no-store'})


@router.get('/reports/{report_id}/images/{image_id}')
async def report_image(report_id: str, image_id: str, identity=Depends(require_approved)):
    import base64
    report = await get_report(report_id, identity)
    data = report['images'].get(image_id)
    if not data:
        raise HTTPException(404, 'Report image not found.')
    return Response(base64.b64decode(data.split(',', 1)[1]), media_type='image/png', headers={'Cache-Control':'no-store'})
