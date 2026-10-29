# api routes for maintenance reports. a user can create, open, edit and download
# a report as pdf, and get the images saved in a report. only the report's owner can use them.

import asyncio
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.sessions.session_store import store
from app.sessions import persistence
from app.responses.documentation import (EDITABLE, create_document, public_document,
                                         render_pdf, save_document)
from app.responses.maintenance_report import (StepConfirmation, apply_profile,
                                             update_confirmations, validate_details, upgrade_report)
from auth_system.security import require_approved, user_profile
from app.reports import repository as submissions, storage as submitted_storage, service as submission_service

router = APIRouter()


# says which case or message the new report should be made from
class CreateReport(BaseModel):
    case_id: str = Field(default='', max_length=80)
    message_id: str = Field(default='', max_length=80)


# the report edit: the revision number the user has and the fields they changed
class EditReport(BaseModel):
    revision: int = Field(ge=1)
    fields: dict[str, str]
    step_confirmations: list[StepConfirmation] = Field(default_factory=list, max_length=1000)


# loads a report that belongs to this user. gives a 404 error if it is not found
async def get_report(report_id, identity):
    report = await asyncio.to_thread(persistence.load_report, report_id, identity.uid)
    if not report:
        raise HTTPException(404, 'Report not found.')
    return upgrade_report(report)


# makes a new report from a case or a message in a chat session, saves it and gives it back
@router.post('/sessions/{session_id}/reports')
async def create_report(session_id: str, selection: CreateReport, identity=Depends(require_approved)):
    entry = await store.owned(session_id, identity)
    async with entry.lock:
        try:
            report = await asyncio.to_thread(create_document, entry, identity, selection.case_id, selection.message_id)
        except ValueError as error:
            raise HTTPException(404, str(error)) from error
        profile = await asyncio.to_thread(user_profile, identity.uid) or {}
        apply_profile(report, identity, profile)
        await asyncio.to_thread(save_document, report, identity.uid)
        return public_document(report)


# opens a saved report (without its images)
@router.get('/reports/{report_id}')
async def read_report(report_id: str, identity=Depends(require_approved)):
    submitted = await asyncio.to_thread(submissions.get_submission, report_id, identity.uid)
    if submitted:
        report = await asyncio.to_thread(submitted_storage.read_snapshot, submitted)
        return {**public_document(report), 'submission': submission_service.public(submitted)}
    return public_document(await get_report(report_id, identity))


# saves the user's changes to a report.
# it stops with an error if the report changed since the user opened it,
# or if a field is not allowed or too long
@router.post('/reports/{report_id}')
async def edit_report(report_id: str, edit: EditReport, identity=Depends(require_approved)):
    if await asyncio.to_thread(submissions.get_submission, report_id, identity.uid):
        raise HTTPException(409, 'Submitted reports are immutable. Download the submitted snapshot or create a new report.')
    report = await get_report(report_id, identity)
    if edit.revision != report['revision']:
        raise HTTPException(409, 'This report has changed. Reopen it before saving.')
    if set(edit.fields) - EDITABLE or any(len(value) > 20000 for value in edit.fields.values()):
        raise HTTPException(400, 'Invalid report fields.')
    report.update(edit.fields)
    if 'action_taken' in edit.fields:
        report['action_taken_source'] = 'technician_recorded'
    try:
        update_confirmations(report, edit.step_confirmations, identity)
        validate_details(report)
    except ValueError as error:
        raise HTTPException(400, str(error)) from error
    report['preparation_status'] = 'Technician reviewed'
    report['revision'] += 1
    report['updated_at'] = datetime.now(timezone.utc).isoformat()
    report['edits'].append({'revision': report['revision'], 'updated_at': report['updated_at'],
                            'fields': sorted(edit.fields), 'confirmed_by_uid': identity.uid,
                            'step_ids': [item.step_id for item in edit.step_confirmations]})
    try:
        await asyncio.to_thread(persistence.save_report, report, identity.uid, edit.revision)
    except ValueError as error:
        raise HTTPException(409, str(error)) from error
    return public_document(report)


# makes the report into a pdf and sends it as a download
@router.get('/reports/{report_id}/pdf')
async def download_pdf(report_id: str, identity=Depends(require_approved)):
    submitted = await asyncio.to_thread(submissions.get_submission, report_id, identity.uid)
    if submitted:
        from app.api.report_submission_routes import pdf_response
        return pdf_response(submitted, await asyncio.to_thread(submitted_storage.read_object, submitted))
    report = await get_report(report_id, identity)
    data = await asyncio.to_thread(render_pdf, report)
    return Response(data, media_type='application/pdf', headers={
        'Content-Disposition': f'attachment; filename="{report["report_number"]}.pdf"', 'Cache-Control': 'no-store'})


# sends one image that is saved inside a report
@router.get('/reports/{report_id}/images/{image_id}')
async def report_image(report_id: str, image_id: str, identity=Depends(require_approved)):
    import base64
    submitted = await asyncio.to_thread(submissions.get_submission, report_id, identity.uid)
    report = (await asyncio.to_thread(submitted_storage.read_snapshot, submitted) if submitted
              else await get_report(report_id, identity))
    data = report['images'].get(image_id)
    if not data:
        raise HTTPException(404, 'Report image not found.')
    return Response(base64.b64decode(data.split(',', 1)[1]), media_type='image/png', headers={'Cache-Control':'no-store'})
