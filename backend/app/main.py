#start the FastAPI application, connect all API routes, configure frontend access,
#  manage startup/shutdown activities, and handle unexpected errors
from contextlib import asynccontextmanager
import asyncio
import os
from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from app import config as cfg
from app.sessions.session_store import store
from app.sessions import persistence
from app.api import auth_routes, chat_routes, health_routes, image_routes, session_routes, report_routes, admin_files_routes
from app.api import report_submission_routes
from app.observability.middleware import PerformanceMiddleware
from app.observability.sink import ApiLogSink
from auth_system.firestore import retry_pending_approval_notifications
from app.reports import email_worker



def _allowed_frontend_origins():

    return sorted({cfg.ORIGIN, 'http://localhost:5173', 'http://127.0.0.1:5173'})


# every 60 seconds, tries to send the admin approval emails again
async def _approval_email_retry_loop():
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.to_thread(retry_pending_approval_notifications)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # app keeps working even if email or firestore is down
            import logging
            logging.getLogger('uvicorn.error.approval_email').error(
                'Admin email notification retry failed error_type=%s',
                type(exc).__name__)


async def _report_email_worker_loop():
    """Drain submitted-report email jobs when the API is run by itself.

    The outbox remains the source of truth and the worker's Firestore lease
    prevents duplicate delivery if a separately supervised worker is also
    running.  This makes the normal local/API startup command sufficient for
    report notifications; a standalone worker can still be used in production.
    """
    import logging
    logger = logging.getLogger('uvicorn.error.report_email_worker')
    interval = max(5, int(os.getenv('REPORT_EMAIL_WORKER_INTERVAL_SECONDS', '10')))
    while True:
        try:
            handled = await asyncio.to_thread(email_worker.run_once, 50)
            if handled:
                logger.info('Report email worker processed jobs=%s', handled)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Email delivery must never take down the API. Jobs remain in the
            # durable outbox and are retried on the next poll or by a worker.
            logger.error('Report email worker poll failed error_type=%s', type(exc).__name__)
        await asyncio.sleep(interval)


# runs setup when app starts and cleanup when app stops
@asynccontextmanager
async def lifespan(app):
    await asyncio.to_thread(persistence.purge_expired_snapshots)
    sink = ApiLogSink()
    sink.start()
    email_retry_task = (asyncio.create_task(_approval_email_retry_loop())
                        if cfg.AUTH_ENABLED else None)
    report_email_task = (asyncio.create_task(_report_email_worker_loop())
                         if cfg.AUTH_ENABLED else None)
    app.state.api_log_sink = sink
    try:
        yield
    finally:
        if email_retry_task is not None:
            email_retry_task.cancel()
            try:
                await email_retry_task
            except asyncio.CancelledError:
                pass
        if report_email_task is not None:
            report_email_task.cancel()
            try:
                await report_email_task
            except asyncio.CancelledError:
                pass
        await store.close()
        await asyncio.to_thread(sink.close)
        app.state.api_log_sink = None


# create the app
app = FastAPI(
    title="Manufacturing Maintenance Agent",
    lifespan=lifespan,
)

# let the frontend call the backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_frontend_origins(),
    allow_methods=["GET", "POST", "PATCH", "DELETE"],
    allow_headers=["Content-Type", "Authorization", "X-Client-Request-ID"],
    expose_headers=['X-Request-ID', 'Server-Timing'],
)
app.add_middleware(PerformanceMiddleware)


# if any unexpected error happens, sends a safe error message without showing details
@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    # do not show the real error or request data
    origin = request.headers.get('origin')
    cors_origin = origin if origin in _allowed_frontend_origins() else cfg.ORIGIN
    return JSONResponse(status_code=500, content={'detail': 'An unexpected server error occurred.'},
        headers={'X-Request-ID': request.scope.get('api_request_id', 'unknown'),
                 'Access-Control-Allow-Origin': cors_origin,
                 'Access-Control-Expose-Headers': 'X-Request-ID'})


# add all the api routes to the app
app.include_router(health_routes.router)
app.include_router(session_routes.router)
app.include_router(image_routes.router)
app.include_router(chat_routes.router)
app.include_router(auth_routes.router)
app.include_router(report_submission_routes.router)
app.include_router(report_routes.router)
app.include_router(admin_files_routes.router)
