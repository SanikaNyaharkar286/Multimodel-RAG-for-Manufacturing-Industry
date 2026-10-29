# creates, stores and removes chat sessions and their data.
# also saves a backup of each session so it can come back after a restart.

import asyncio
from copy import deepcopy
import hashlib
import json
import logging
import secrets
import time
from dataclasses import dataclass, field

from fastapi import HTTPException
from google.adk.events import Event, EventActions
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.config import APP_NAME, TTL
from app import config as cfg
from . import persistence


logger = logging.getLogger('uvicorn.error.session_checkpoint')


# keeps chat sessions in memory
session_service = InMemorySessionService()


# holds all the details of one chat session
@dataclass
class Entry:
    id: str
    user_id: str
    expires_at: float
    expires_mono: float
    owner_uid: str = 'local-development'
    cases: dict = field(default_factory=dict)
    receipts: dict = field(default_factory=dict)
    progress: dict = field(default_factory=dict)
    current_progress: dict | None = None
    safety_task: asyncio.Task | None = None

    # makes requests for this chat run one at a time
    lock: asyncio.Lock = field(
        default_factory=asyncio.Lock
    )

    # stores messages and uploaded images
    history: list = field(default_factory=list)
    assets: dict = field(default_factory=dict)
    # keeps the latest upload even if its answer fails
    uploaded_photo: bytes | None = None
    request_stage: str = ""
    repair_workflow: dict | None = None
    # reuses manual evidence and images while the repair moves forward
    repair_cache: dict | None = None

    # saving to firestore happens after the reply is sent, and requests are merged
    checkpoint_task: asyncio.Task | None = None
    checkpoint_requested: bool = False
    pending_checkpoint_body: dict | None = None

    # the running request and the expiry timer
    active: asyncio.Task | None = None
    timer: asyncio.Task | None = None


# creates and manages all chat sessions
class SessionStore:
    # sets up the store with empty lists and the session time limit
    def __init__(
        self,
        service=session_service,
        ttl=TTL,
    ):
        self.service = service
        self.ttl = ttl

        # keeps track of sessions and who owns each image
        self.entries = {}
        self.asset_owners = {}
        self.restoring = {}

    # makes a new session and starts its expiry timer
    async def create(self, owner_uid='local-development'):
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
            owner_uid=owner_uid,
        )

        self.entries[entry.id] = entry

        # delete the session when it expires
        entry.timer = asyncio.create_task(
            self.expire_later(entry.id)
        )

        return entry

    # waits until the session time is over, then deletes it
    async def expire_later(self, session_id):
        entry = self.entries[session_id]

        await asyncio.sleep(
            max(
                0,
                entry.expires_mono - time.monotonic(),
            )
        )

        await self.delete(session_id)

    # finds a session by id. brings it back from the backup if needed. errors if expired
    async def get(self, session_id):
        entry = self.entries.get(session_id)
        if entry is None and cfg.CHAT_SESSION_CHECKPOINTS_ENABLED:
            task = self.restoring.get(session_id)
            if task is None:
                task = asyncio.create_task(self._restore(session_id))
                self.restoring[session_id] = task
                task.add_done_callback(lambda done: self.restoring.pop(session_id, None))
            # Progress and image requests arriving together share one restore.
            # A disconnected request must not cancel recovery for the others.
            await asyncio.shield(task)
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

    async def _restore(self, session_id):
        saved = await asyncio.to_thread(persistence.load_snapshot, session_id)
        if saved and saved['expires_at'] > time.time():
            state = saved.pop('state')
            saved['expires_mono'] = time.monotonic() + saved['expires_at'] - time.time()
            saved['assets'] = {key: tuple(value) for key, value in saved['assets'].items()}
            entry = Entry(**saved)
            if session_id not in self.entries:
                await self.service.create_session(app_name=APP_NAME, user_id=entry.user_id,
                                                  session_id=entry.id, state=state)
                self.entries[entry.id] = entry
                self.asset_owners.update({key: entry.id for key in entry.assets})
                entry.timer = asyncio.create_task(self.expire_later(entry.id))

    # gets a session only if it belongs to this user. otherwise says not found
    async def owned(self, session_id, identity):
        entry = await self.get(session_id)
        if entry.owner_uid != identity.uid:
            raise HTTPException(404, 'Conversation not found.')
        return entry

    # saves a backup of the session right now and waits for it to finish
    async def checkpoint(self, entry):
        if not cfg.CHAT_SESSION_CHECKPOINTS_ENABLED:
            return
        state = await self.state(entry)
        await asyncio.to_thread(persistence.save_snapshot, entry, state)

    # saves a backup in the background so the chat reply is not slowed down
    async def queue_checkpoint(self, entry):
        """saves the latest session without making the chat reply wait."""
        if not cfg.CHAT_SESSION_CHECKPOINTS_ENABLED:
            return
        state = await self.state(entry)
        # take one copy of the session now, while the request still holds the lock
        entry.pending_checkpoint_body = deepcopy(
            persistence._snapshot_body(entry, state))
        entry.checkpoint_requested = True
        if entry.checkpoint_task and not entry.checkpoint_task.done():
            return

        # background job that keeps saving until there is nothing new to save
        async def worker():
            try:
                while entry.checkpoint_requested and entry.id in self.entries:
                    entry.checkpoint_requested = False
                    body = entry.pending_checkpoint_body
                    entry.pending_checkpoint_body = None
                    started = time.monotonic()
                    await asyncio.to_thread(persistence.save_snapshot_body, body)
                    logger.info('Firestore checkpoint completed session=%s duration_ms=%.3f',
                                entry.id, (time.monotonic() - started) * 1000)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # the live session in memory is still correct.
                # the next request will try to save again
                logger.error('Firestore checkpoint failed session=%s error_type=%s',
                             entry.id, type(exc).__name__)
            finally:
                entry.checkpoint_task = None

        entry.checkpoint_task = asyncio.create_task(worker())

    # deletes the session and clears all its data
    async def delete(self, session_id):
        entry = self.entries.pop(session_id, None)

        if cfg.CHAT_SESSION_CHECKPOINTS_ENABLED and entry and entry.checkpoint_task:
            # a running firestore save cannot be stopped safely.
            # wait for it, so it cannot bring the session back after we delete it
            await entry.checkpoint_task
        if cfg.CHAT_SESSION_CHECKPOINTS_ENABLED:
            await asyncio.to_thread(persistence.delete_snapshot, session_id)

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
        entry.repair_workflow = None
        entry.repair_cache = None

        # delete the session from adk
        await self.service.delete_session(
            app_name=APP_NAME,
            user_id=entry.user_id,
            session_id=entry.id,
        )

    # gets the saved state of a session from adk
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

    # saves new values into the session state
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

    # saves the question and reply in the chat history when the agent was not used
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

    # saves an image in the session and gives back its id
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

    def put_media_image(self, entry, asset):
        if isinstance(asset.get('bytes'), bytes):
            return self.put_image(entry, asset['bytes'])
        from app.media.manual_assets import image_identity, validate_reference
        reference = validate_reference(asset['reference'])
        digest = image_identity(asset)
        for asset_id, existing in entry.assets.items():
            if existing[0] == digest:
                return asset_id
        asset_id = secrets.token_urlsafe(24)
        entry.assets[asset_id] = (digest, reference)
        self.asset_owners[asset_id] = entry.id
        return asset_id

    # finds an image by its id and gives back the image data
    async def image(self, asset_id):
        owner = self.asset_owners.get(asset_id)

        # report an error if the image is missing
        if not owner:
            raise HTTPException(
                404,
                "Image unavailable or session expired.",
            )

        entry = await self.get(owner)

        from app.media.manual_assets import stored_image_bytes
        value = entry.assets[asset_id][1]
        if isinstance(value, bytes):
            return value
        try:
            data = await asyncio.to_thread(stored_image_bytes, value)
        except Exception as error:
            raise HTTPException(503, 'The original manual image is temporarily unavailable. Please retry.') from error
        entry.assets[asset_id] = (hashlib.sha256(data).digest(), data)
        return data

    # saves a last backup of every session and clears the store when the app shuts down
    async def close(self):
        for session_id in list(self.entries):
            entry = self.entries[session_id]
            if cfg.CHAT_SESSION_CHECKPOINTS_ENABLED and entry.checkpoint_task:
                await entry.checkpoint_task
            # a newer copy may have been queued during the last save.
            # save that newest copy before shutdown
            if cfg.CHAT_SESSION_CHECKPOINTS_ENABLED and entry.pending_checkpoint_body is not None:
                body = entry.pending_checkpoint_body
                entry.pending_checkpoint_body = None
                entry.checkpoint_requested = False
                await asyncio.to_thread(persistence.save_snapshot_body, body)
            elif cfg.CHAT_SESSION_CHECKPOINTS_ENABLED and not entry.checkpoint_task:
                await self.checkpoint(entry)
            if entry.timer:
                entry.timer.cancel()
        self.entries.clear()
        self.asset_owners.clear()


# create the session store used by the app
store = SessionStore()
