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


# close chat sessions when the app shuts down
@asynccontextmanager
async def lifespan(app):
    sink = ApiLogSink()
    sink.start()
    app.state.api_log_sink = sink
    try:
        yield
    finally:
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
