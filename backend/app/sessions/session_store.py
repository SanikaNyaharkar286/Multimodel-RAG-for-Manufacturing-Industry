# creates, stores, and removes chat sessions and their data

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


# keeps chat sessions in memory
session_service = InMemorySessionService()


# holds the details for one chat session
@dataclass
class Entry:
    id: str
    user_id: str
    expires_at: float
    expires_mono: float

    # keeps requests for this chat in order
    lock: asyncio.Lock = field(
        default_factory=asyncio.Lock
    )

    # stores messages and uploaded images
    history: list = field(default_factory=list)
    assets: dict = field(default_factory=dict)

    # tracks the running request and expiry timer
    active: asyncio.Task | None = None
    timer: asyncio.Task | None = None


# creates and manages chat sessions
class SessionStore:
    def __init__(
        self,
        service=session_service,
        ttl=TTL,
    ):
        self.service = service
        self.ttl = ttl

        # keeps track of sessions and image owners
        self.entries = {}
        self.asset_owners = {}

    # creates a session and starts its expiry timer
    async def create(self):
        session = await self.service.create_session(
            app_name=APP_NAME,
            user_id=secrets.token_urlsafe(18),
        )

        # save the session details and expiry time
        entry = Entry(
            id=session.id,
            user_id=session.user_id,
            expires_at=time.time() + self.ttl,
            expires_mono=time.monotonic() + self.ttl,
        )

        self.entries[entry.id] = entry

        # delete the session when it expires
        entry.timer = asyncio.create_task(
            self.expire_later(entry.id)
        )

        return entry

    # wait until the session expires, then delete it
    async def expire_later(self, session_id):
        entry = self.entries[session_id]

        await asyncio.sleep(
            max(
                0,
                entry.expires_mono - time.monotonic(),
            )
        )

        await self.delete(session_id)

    # find a session and check if it has expired
    async def get(self, session_id):
        entry = self.entries.get(session_id)

        # ask the user to start a new chat if the session is missing
        if entry is None:
            raise HTTPException(
                410,
                "Session expired or server restarted. "
                "Start a new chat.",
            )

        # delete the session if it has expired
        if time.monotonic() >= entry.expires_mono:
            await self.delete(session_id)

            raise HTTPException(
                410,
                "The one-hour session has expired. "
                "Start a new chat.",
            )

        return entry

    # delete the session and clear its data
    async def delete(self, session_id):
        entry = self.entries.pop(session_id, None)

        if entry is None:
            return

        current = asyncio.current_task()

        # stop the request if it is still running
        if entry.active and entry.active is not current:
            entry.active.cancel()

        # stop the expiry timer
        if entry.timer and entry.timer is not current:
            entry.timer.cancel()

        # remove the session's image links
        for asset_id in entry.assets:
            self.asset_owners.pop(asset_id, None)

        # clear the saved images and messages
        entry.assets.clear()
        entry.history.clear()

        # delete the session from adk
        await self.service.delete_session(
            app_name=APP_NAME,
            user_id=entry.user_id,
            session_id=entry.id,
        )

    # get the saved state for a session
    async def state(self, entry):
        session = await self.service.get_session(
            app_name=APP_NAME,
            user_id=entry.user_id,
            session_id=entry.id,
        )

        # report an error if adk cannot find the session
        if session is None:
            raise HTTPException(
                410,
                "Session is no longer available.",
            )

        return session.state

    # save new values in the session
    async def update_state(self, entry, values):
        await self.get(entry.id)

        session = await self.service.get_session(
            app_name=APP_NAME,
            user_id=entry.user_id,
            session_id=entry.id,
        )

        # add the values to the session
        await self.service.append_event(
            session=session,
            event=Event(
                author=APP_NAME,
                actions=EventActions(
                    state_delta=values
                ),
            ),
        )

    # save a reply when the agent did not need to run
    async def record_direct_reply(
        self,
        entry,
        question,
        photo,
        response,
    ):
        """
        saves a simple reply in adk when the agent was not needed
        """

        await self.get(entry.id)

        session = await self.service.get_session(
            app_name=APP_NAME,
            user_id=entry.user_id,
            session_id=entry.id,
        )

        parts = []

        # add the question to the user's message
        if question:
            parts.append(
                types.Part(text=question)
            )

        # add the image to the user's message
        if photo:
            parts.append(
                types.Part.from_bytes(
                    data=photo,
                    mime_type="image/png",
                )
            )

        # save the user's message in adk
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

        # save the reply in adk
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

    # save an image and return its id
    def put_image(self, entry, data):
        digest = hashlib.sha256(data).digest()

        # reuse the id if this image is already saved
        for asset_id, existing in entry.assets.items():
            if existing[0] == digest:
                return asset_id

        # create an id and save the image
        asset_id = secrets.token_urlsafe(24)

        entry.assets[asset_id] = (
            digest,
            data,
        )

        self.asset_owners[asset_id] = entry.id

        return asset_id

    # find an image and return its data
    async def image(self, asset_id):
        owner = self.asset_owners.get(asset_id)

        # report an error if the image is missing
        if not owner:
            raise HTTPException(
                404,
                "Image unavailable or session expired.",
            )

        entry = await self.get(owner)

        return entry.assets[asset_id][1]

    # delete all sessions when the store closes
    async def close(self):
        for session_id in list(self.entries):
            await self.delete(session_id)


# create the session store used by the app
store = SessionStore()