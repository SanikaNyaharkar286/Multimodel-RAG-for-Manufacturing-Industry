from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from app import config as cfg
from app.sessions.session_store import store
from app.api import chat_routes, health_routes, image_routes, session_routes

@asynccontextmanager
async def lifespan(app):
    yield
    await store.close()


app = FastAPI(
    title="Manufacturing Maintenance Agent",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[cfg.ORIGIN],
    allow_methods=["GET", "POST", "DELETE"],
    allow_headers=["Content-Type"],
)



app.include_router(health_routes.router)
app.include_router(session_routes.router)
app.include_router(image_routes.router)
app.include_router(chat_routes.router)
