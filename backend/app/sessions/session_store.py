import asyncio
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field

from fastapi import HTTPException
from google.adk.events import Event, EventActions
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.config import APP_NAME, TTL


session_service = InMemorySessionService()


@dataclass
class Entry:
    id: str
    user_id: str
    expires_at: float
    expires_mono: float

    lock: asyncio.Lock = field(
        default_factory=asyncio.Lock
    )

    history: list = field(default_factory=list)
    assets: dict = field(default_factory=dict)

    active: asyncio.Task | None = None
    timer: asyncio.Task | None = None


class SessionStore:
    def __init__(
        self,
        service=session_service,
        ttl=TTL,
    ):
        self.service = service
        self.ttl = ttl

        self.entries = {}
        self.asset_owners = {}

    async def create(self):
        session = await self.service.create_session(
            app_name=APP_NAME,
            user_id=secrets.token_urlsafe(18),
        )

        entry = Entry(
            id=session.id,
            user_id=session.user_id,
            expires_at=time.time() + self.ttl,
            expires_mono=time.monotonic() + self.ttl,
        )

        self.entries[entry.id] = entry

        entry.timer = asyncio.create_task(
            self.expire_later(entry.id)
        )

        return entry

    async def expire_later(self, session_id):
        entry = self.entries[session_id]

        await asyncio.sleep(
            max(
                0,
                entry.expires_mono - time.monotonic(),
            )
        )

        await self.delete(session_id)

    async def get(self, session_id):
        entry = self.entries.get(session_id)

        if entry is None:
            raise HTTPException(
                410,
                "Session expired or server restarted. "
                "Start a new chat.",
            )

        if time.monotonic() >= entry.expires_mono:
            await self.delete(session_id)

            raise HTTPException(
                410,
                "The one-hour session has expired. "
                "Start a new chat.",
            )

        return entry

    async def delete(self, session_id):
        entry = self.entries.pop(session_id, None)

        if entry is None:
            return

        current = asyncio.current_task()

        if entry.active and entry.active is not current:
            entry.active.cancel()

        if entry.timer and entry.timer is not current:
            entry.timer.cancel()

        for asset_id in entry.assets:
            self.asset_owners.pop(asset_id, None)

        entry.assets.clear()
        entry.history.clear()

        await self.service.delete_session(
            app_name=APP_NAME,
            user_id=entry.user_id,
            session_id=entry.id,
        )

    async def state(self, entry):
        session = await self.service.get_session(
            app_name=APP_NAME,
            user_id=entry.user_id,
            session_id=entry.id,
        )

        if session is None:
            raise HTTPException(
                410,
                "Session is no longer available.",
            )

        return session.state

    async def update_state(self, entry, values):
        await self.get(entry.id)

        session = await self.service.get_session(
            app_name=APP_NAME,
            user_id=entry.user_id,
            session_id=entry.id,
        )

        await self.service.append_event(
            session=session,
            event=Event(
                author=APP_NAME,
                actions=EventActions(
                    state_delta=values
                ),
            ),
        )

    async def record_direct_reply(
        self,
        entry,
        question,
        photo,
        response,
    ):
        """
        Store clarification/unavailable replies in ADK too,
        even when no answer-agent execution was needed.
        """

        await self.get(entry.id)

        session = await self.service.get_session(
            app_name=APP_NAME,
            user_id=entry.user_id,
            session_id=entry.id,
        )

        parts = []

        if question:
            parts.append(
                types.Part(text=question)
            )

        if photo:
            parts.append(
                types.Part.from_bytes(
                    data=photo,
                    mime_type="image/png",
                )
            )

        await self.service.append_event(
            session=session,
            event=Event(
                author="user",
                content=types.Content(
                    role="user",
                    parts=parts,
                ),
            ),
        )

        await self.service.append_event(
            session=session,
            event=Event(
                author=APP_NAME,
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            text=json.dumps(response)
                        )
                    ],
                ),
            ),
        )

    def put_image(self, entry, data):
        digest = hashlib.sha256(data).digest()

        for asset_id, existing in entry.assets.items():
            if existing[0] == digest:
                return asset_id

        asset_id = secrets.token_urlsafe(24)

        entry.assets[asset_id] = (
            digest,
            data,
        )

        self.asset_owners[asset_id] = entry.id

        return asset_id

    async def image(self, asset_id):
        owner = self.asset_owners.get(asset_id)

        if not owner:
            raise HTTPException(
                404,
                "Image unavailable or session expired.",
            )

        entry = await self.get(owner)

        return entry.assets[asset_id][1]

    async def close(self):
        for session_id in list(self.entries):
            await self.delete(session_id)


store = SessionStore()
