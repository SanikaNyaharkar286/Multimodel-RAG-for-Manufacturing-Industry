"""Approved technician submissions and server-authorized administrator inbox."""
import asyncio
import logging
from typing import Annotated, Literal
from fastapi import APIRouter, Depends, HTTPException, Query, Path
from fastapi.responses import Response
from pydantic import BaseModel
from google.api_core.exceptions import FailedPrecondition
from auth_system.security import require_approved, require_admin
from app import config as cfg
from app.reports import repository, service, storage
from app.reports.schemas import (SubmitReport, ReviewReport, Submission, ReportList,
                                 NotificationList)

router = APIRouter()
RecordId = Annotated[str, Path(min_length=1, max_length=100, pattern=r'^[A-Za-z0-9_-]+$')]
logger = logging.getLogger('uvicorn.error.report_submission')


async def report_call(operation, *args, **kwargs):
    """Keep deployment/index readiness failures distinct from application bugs."""
    try:
        return await asyncio.to_thread(operation, *args, **kwargs)
    except FailedPrecondition as error:
        if 'index' not in str(error).casefold():
            raise
        logger.warning('Report storage index is not ready operation=%s',
                       getattr(operation, '__name__', 'report_operation'))
        raise HTTPException(503,
            'Maintenance report and notification indexes are being prepared. Please retry shortly.',
            headers={'Retry-After': '30'}) from error


def approved_identity(identity=Depends(require_approved)):
    if not cfg.AUTH_ENABLED:
        raise HTTPException(503, 'Report submission requires Firebase authentication.')
    return identity


def admin_identity(identity=Depends(require_admin)):
    if not cfg.AUTH_ENABLED:
        raise HTTPException(503, 'Report administration requires Firebase authentication.')
    return identity


class ReportDetail(BaseModel):
    submission: Submission
    report: dict


class CountResponse(BaseModel):
    unread_count: int


class ReadResponse(BaseModel):
    marked_read: int


def pdf_response(record, data):
    # Safe server-generated filename; identity/path inputs never become headers.
    filename = ''.join(c for c in record['report_number'] if c.isalnum() or c in '-_')[:150]
    return Response(data, media_type='application/pdf', headers={
        'Content-Disposition': f'attachment; filename="{filename}.pdf"',
        'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})


@router.post('/reports/{report_id}/submit', response_model=Submission)
async def submit(report_id: RecordId, selection: SubmitReport, identity=Depends(approved_identity)):
    if not cfg.AUTH_ENABLED:
        raise HTTPException(503, 'Report submission requires Firebase authentication.')
    return await report_call(service.submit, report_id, identity, selection.revision)


@router.get('/reports/my', response_model=ReportList)
async def my_reports(page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=50),
                     identity=Depends(approved_identity)):
    return await report_call(repository.list_reports, owner_uid=identity.uid, page=page, page_size=page_size)


@router.get('/admin/reports', response_model=ReportList)
async def reports(status: Literal['', 'new', 'under_review', 'reviewed'] = '',
                  search: str = Query('', max_length=80), page: int = Query(1, ge=1, le=1000),
                  page_size: int = Query(20, ge=1, le=50), sort: Literal['asc', 'desc'] = 'desc',
                  identity=Depends(admin_identity)):
    return await report_call(repository.list_reports, status=status, search=search,
                                  page=page, page_size=page_size, sort=sort)


async def admin_record(report_id):
    record = await report_call(repository.get_submission, report_id)
    if not record:
        raise HTTPException(404, 'Submitted report not found. It may have been removed.')
    return record


@router.get('/admin/reports/{report_id}', response_model=ReportDetail)
async def report(report_id: RecordId, identity=Depends(admin_identity)):
    return await report_call(service.detail, await admin_record(report_id))


@router.get('/admin/reports/{report_id}/pdf')
async def pdf(report_id: RecordId, identity=Depends(admin_identity)):
    record = await admin_record(report_id)
    return pdf_response(record, await report_call(storage.read_object, record))


@router.patch('/admin/reports/{report_id}/review', response_model=Submission)
async def review(report_id: RecordId, edit: ReviewReport, identity=Depends(admin_identity)):
    return await report_call(repository.review, report_id, identity.uid, edit)


@router.get('/admin/notifications', response_model=NotificationList)
async def notifications(page: int = Query(1, ge=1, le=1000), page_size: int = Query(20, ge=1, le=50),
                        identity=Depends(admin_identity)):
    return await report_call(repository.notifications, identity.uid, page, page_size)


@router.get('/admin/notifications/unread-count', response_model=CountResponse)
async def unread_count(identity=Depends(admin_identity)):
    value = await report_call(repository.notifications, identity.uid, 1, 1)
    return {'unread_count': value['unread_count']}


@router.patch('/admin/notifications/{notification_id}/read', response_model=ReadResponse)
async def read(notification_id: RecordId, identity=Depends(admin_identity)):
    await report_call(repository.mark_read, identity.uid, notification_id)
    return {'marked_read': 1}


@router.post('/admin/notifications/mark-all-read', response_model=ReadResponse)
async def read_all(identity=Depends(admin_identity)):
    return {'marked_read': await report_call(repository.mark_all_read, identity.uid)}
