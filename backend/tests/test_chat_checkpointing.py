import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from google.adk.sessions import InMemorySessionService

from app import config
from app.sessions import persistence
from app.sessions.session_store import SessionStore


class CheckpointTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.disabled = patch.object(config, 'CHAT_SESSION_CHECKPOINTS_ENABLED', False)
        self.disabled.start()
        self.addCleanup(self.disabled.stop)
        self.store = SessionStore(service=InMemorySessionService())

    async def asyncTearDown(self):
        await self.store.close()

    async def test_chat_and_step_cache_remain_live_without_snapshot_work(self):
        entry = await self.store.create('owner')
        entry.repair_cache = {'prepared_results': {'next': {'blocks': [{'type':'text', 'content':'Next step'}]}}}
        image = self.store.put_image(entry, b'original diagram')
        with (patch.object(self.store, 'state', new=AsyncMock(side_effect=AssertionError('No snapshot assembly'))) as state,
              patch('app.sessions.persistence.save_snapshot') as save,
              patch('app.sessions.persistence.save_snapshot_body') as save_body):
            await self.store.checkpoint(entry)
            await self.store.queue_checkpoint(entry)
            await self.store.queue_checkpoint(entry)
            self.assertIs(await self.store.get(entry.id), entry)
            self.assertEqual(await self.store.image(image), b'original diagram')
            self.assertIn('next', entry.repair_cache['prepared_results'])
            self.assertIsNone(entry.checkpoint_task)
            self.assertIsNone(entry.pending_checkpoint_body)
            state.assert_not_awaited()
            save.assert_not_called()
            save_body.assert_not_called()

    async def test_shutdown_does_not_save_chat_or_read_session_state(self):
        await self.store.create('owner')
        with (patch.object(self.store, 'state', new=AsyncMock(side_effect=AssertionError('No shutdown snapshot'))),
              patch('app.sessions.persistence.save_snapshot') as save,
              patch('app.sessions.persistence.save_snapshot_body') as save_body):
            await self.store.close()
            save.assert_not_called()
            save_body.assert_not_called()
        self.assertEqual(self.store.entries, {})

    async def test_missing_session_returns_expired_without_firestore_restore(self):
        with patch('app.sessions.persistence.load_snapshot', side_effect=AssertionError('No cloud restore')) as load:
            with self.assertRaises(HTTPException) as error:
                await self.store.get('session-from-before-restart')
        self.assertEqual(error.exception.status_code, 410)
        load.assert_not_called()

    async def test_ending_a_chat_does_not_delete_existing_firestore_backups(self):
        entry = await self.store.create('owner')
        with patch('app.sessions.persistence.delete_snapshot') as delete:
            await self.store.delete(entry.id)
        delete.assert_not_called()

    async def test_persistence_helpers_cannot_bypass_the_disabled_setting(self):
        with patch('app.sessions.persistence._client', side_effect=AssertionError('No Firestore chat IO')):
            persistence.save_snapshot_body({'id':'chat'})
            self.assertIsNone(persistence.load_snapshot('chat'))
            persistence.delete_snapshot('chat')

    async def test_maintenance_report_writes_remain_enabled(self):
        with (patch.object(config, 'AUTH_ENABLED', True),
              patch('app.sessions.persistence._client') as client,
              patch('app.sessions.persistence._write') as write):
            report = {'report_id':'checkpoint-regression-report', 'revision':1}
            persistence.save_report(report, 'owner')
        client.return_value.collection.assert_called_once_with(persistence.REPORT_COLLECTION)
        self.assertEqual(write.call_args.args[1:], (report, 'owner'))
