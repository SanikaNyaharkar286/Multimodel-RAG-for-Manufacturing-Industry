# this file starts the fastapi app and connects its api routes

from contextlib import asynccontextmanager
import asyncio
from fastapi import FastAPI
from fastapi import Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from app import config as cfg
from app.sessions.session_store import store
from app.api import auth_routes, chat_routes, health_routes, image_routes, session_routes
from app.observability.middleware import PerformanceMiddleware
from app.observability.sink import ApiLogSink
from auth_system.firestore import retry_pending_approval_notifications


async def _approval_email_retry_loop():
    while True:
        await asyncio.sleep(60)
        try:
            await asyncio.to_thread(retry_pending_approval_notifications)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Registration remains available even when Firestore or SMTP is unavailable.
            import logging
            logging.getLogger('uvicorn.error.approval_email').error(
                'Admin email notification retry failed error_type=%s',
                type(exc).__name__)


# close chat sessions when the app shuts down
@asynccontextmanager
async def lifespan(app):
    sink = ApiLogSink()
    sink.start()
    email_retry_task = (asyncio.create_task(_approval_email_retry_loop())
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
        await store.close()
        await asyncio.to_thread(sink.close)
        app.state.api_log_sink = None


# create the fastapi application
app = FastAPI(
    title="Manufacturing Maintenance Agent",
    lifespan=lifespan,
)

# allow the configured frontend to call the backend
app.add_middleware(
    CORSMiddleware,
    allow_origins=[cfg.ORIGIN],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type", "Authorization", "X-Client-Request-ID"],
    expose_headers=['X-Request-ID', 'Server-Timing'],
)
app.add_middleware(PerformanceMiddleware)


@app.exception_handler(Exception)
async def unexpected_error(request: Request, exc: Exception):
    # Do not expose provider exception text or request data.
    return JSONResponse(status_code=500, content={'detail': 'An unexpected server error occurred.'},
        headers={'X-Request-ID': request.scope.get('api_request_id', 'unknown'),
                 'Access-Control-Allow-Origin': cfg.ORIGIN,
                 'Access-Control-Expose-Headers': 'X-Request-ID'})


# connect the api routes to the application
app.include_router(health_routes.router)
app.include_router(session_routes.router)
app.include_router(image_routes.router)
app.include_router(chat_routes.router)
app.include_router(auth_routes.router)
