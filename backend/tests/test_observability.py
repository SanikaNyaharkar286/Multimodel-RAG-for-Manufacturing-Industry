import asyncio
import json
import queue
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
from app import config as cfg
from app.main import app
from app.observability.timing import Trace, current_trace, metadata, span, submit_with_context
from app.observability.sink import ApiLogSink
from app.observability.storage import insert_records, provision, TABLE_ID
from app.observability.middleware import PerformanceMiddleware


class TimingTests(unittest.TestCase):
    def test_query_statistics_are_recorded_without_sql_or_rows(self):
        from app.retrieval.bigquery_client import query
        class Rows(list):
            job_id = 'bq-job-123'
            total_bytes_processed = 12345
            cache_hit = False
        trace = Trace('POST')
        token = current_trace.set(trace)
        client = SimpleNamespace(query_and_wait=lambda *args, **kwargs: Rows([{'content': 'SECRET ROW'}]))
        try:
            with patch('app.retrieval.bigquery_client.bq_client', return_value=client):
                rows = query('SECRET SQL', [], operation='bq_seed_query')
            row = trace.snapshot('/chat', 200, close=True)
        finally:
            current_trace.reset(token)
        event = row['timings_json']['events'][0]
        self.assertEqual(rows[0]['content'], 'SECRET ROW')
        self.assertEqual(event['job_id'], 'bq-job-123')
        self.assertEqual(event['bytes_processed'], 12345)
        self.assertNotIn('SECRET', json.dumps(row))

    def test_parallel_workers_share_only_their_request_and_freeze_after_completion(self):
        trace = Trace('POST')
        token = current_trace.set(trace)
        def worker():
            with span('image_normalization'):
                metadata(response_image_count=1, question='SECRET', image=b'SECRET')
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [submit_with_context(pool, worker) for _ in range(4)]
                for future in futures:
                    future.result()
            row = trace.snapshot('/chat', 200, close=True)
            metadata(outcome='changed_late')
        finally:
            current_trace.reset(token)
        self.assertEqual(len(row['timings_json']['events']), 4)
        self.assertNotIn('SECRET', json.dumps(row))
        self.assertNotIn('question', row)
        self.assertNotIn('changed_late', json.dumps(trace.snapshot('/chat', 200)))

    def test_failures_and_nested_timings_are_not_combined_into_total(self):
        trace = Trace('POST')
        token = current_trace.set(trace)
        try:
            with self.assertRaises(ValueError):
                with span('media'):
                    with span('image_normalization'):
                        raise ValueError('secret provider message')
            metadata(outcome='answered')
            row = trace.snapshot('/chat', 500, close=True)
        finally:
            current_trace.reset(token)
        self.assertEqual(row['outcome'], 'error')
        self.assertEqual(row['timings_json']['events'][1]['error_type'], 'ValueError')
        self.assertNotIn('secret provider message', json.dumps(row))

    def test_concurrent_async_requests_have_distinct_contexts(self):
        async def work():
            trace = Trace('GET')
            token = current_trace.set(trace)
            try:
                await asyncio.to_thread(metadata, question_length=7)
                await asyncio.sleep(0)
                return trace.request_id, current_trace.get().request_id, trace.metadata
            finally:
                current_trace.reset(token)
        async def run():
            return await asyncio.gather(work(), work())
        rows = asyncio.run(run())
        self.assertNotEqual(rows[0][0], rows[1][0])
        self.assertTrue(all(a == b and m['question_length'] == 7 for a,b,m in rows))


class SinkTests(unittest.TestCase):
    def test_unwritable_local_log_does_not_break_startup(self):
        with tempfile.TemporaryDirectory() as directory:
            invalid = Path(directory)/'is-a-file'
            invalid.write_text('test', encoding='utf-8')
            with patch.object(cfg, 'API_LOG_DIRECTORY', invalid), \
                 patch.object(cfg, 'BQ_API_LOGGING_ENABLED', False):
                sink = ApiLogSink()
                sink.start()
                sink.emit(Trace('GET').snapshot('/health', 200))
                self.assertFalse(sink.status()['local_enabled'])
                self.assertEqual(sink.status()['last_error_type'], 'FileExistsError')
                sink.close()

    def test_background_cloud_failure_does_not_block_emit_and_is_logged_locally(self):
        entered, release = threading.Event(), threading.Event()
        def blocked_insert(_client, _batch):
            entered.set()
            release.wait(2)
            raise RuntimeError('SECRET IMAGE OR PROMPT')
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = temporary.name
        with (
            patch.object(cfg, 'API_LOG_DIRECTORY', Path(directory)),
            patch.object(cfg, 'BQ_API_LOGGING_ENABLED', True),
            patch.object(cfg, 'API_LOG_FLUSH_SECONDS', 0.01),
            patch('app.observability.sink.log_client', return_value=object()),
            patch('app.observability.sink.insert_records', side_effect=blocked_insert),
        ):
            sink = ApiLogSink()
            sink.start()
            row = Trace('GET').snapshot('/health', 200, close=True)
            sink.emit(row)
            self.assertTrue(entered.wait(2))
            # Worker remains blocked; the request-side emit already completed.
            release.set()
            sink.close()
            self.assertEqual(sink.export_failed, 1)
            logged = (Path(directory)/'api.log').read_text(encoding='utf-8')
            self.assertIn(row['request_id'], logged)
            self.assertIn('bq_export_failed', logged)
            self.assertNotIn('SECRET', logged)

    def test_bounded_queue_overflow_keeps_local_summary(self):
        sink = ApiLogSink()
        sink.accepting = True
        sink.bq_queue = queue.Queue(maxsize=1)
        with patch.object(cfg, 'BQ_API_LOGGING_ENABLED', True), patch.object(sink, 'local') as local:
            sink.emit(Trace('GET').snapshot('/health', 200))
            sink.emit(Trace('GET').snapshot('/health', 200))
        self.assertEqual(sink.bq_dropped, 1)
        self.assertEqual(local.call_count, 3)

    def test_table_and_insert_use_partition_retention_ids_and_no_prompt(self):
        from unittest.mock import Mock
        from google.api_core.exceptions import NotFound
        client = Mock()
        client.get_table.side_effect = NotFound('not found')
        client.create_table.side_effect = lambda table, **kwargs: table
        table = provision(client)
        self.assertEqual(table.time_partitioning.field, 'timestamp')
        self.assertEqual(table.time_partitioning.expiration_ms, 30*86400000)
        self.assertTrue(table.require_partition_filter)
        client.get_table.side_effect = None
        client.get_table.return_value = table
        self.assertIs(provision(client), table)
        self.assertEqual(client.create_table.call_count, 1)
        client.insert_rows_json.return_value = []
        row = Trace('GET').snapshot('/health', 200)
        insert_records(client, [row])
        args, kwargs = client.insert_rows_json.call_args
        self.assertEqual(args[0], TABLE_ID)
        self.assertEqual(kwargs['row_ids'], [row['request_id']])
        self.assertIsNone(kwargs['retry'])
        self.assertIsInstance(args[1][0]['timings_json'], str)
        client.insert_rows_json.return_value = [{'errors': [{'message': 'SECRET'}]}]
        with self.assertRaisesRegex(RuntimeError, 'rejected'):
            insert_records(client, [row])


class MiddlewareTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory_patch = patch.object(cfg, 'API_LOG_DIRECTORY', Path(temporary.name))
        self.directory_patch.start()
        self.addCleanup(self.directory_patch.stop)

    def test_routes_errors_and_body_parsing_have_request_ids_and_safe_paths(self):
        with patch.object(cfg, 'BQ_API_LOGGING_ENABLED', False), patch.object(cfg, 'API_TIMING_DEBUG', True):
            with TestClient(app) as client:
                sink = app.state.api_log_sink
                with patch.object(sink, 'emit') as emit:
                    response = client.get('/health', headers={'Origin': cfg.ORIGIN})
                    self.assertIn('server-timing', response.headers)
                    self.assertIn('x-request-id', response.headers)
                    self.assertIn('X-Request-ID', response.headers['access-control-expose-headers'])
                    self.assertEqual(emit.call_args.args[0]['request_id'], response.headers['x-request-id'])
                    created = client.post('/sessions').json()
                    session_id = created['session_id']
                    response = client.get('/sessions/'+session_id)
                    row = emit.call_args.args[0]
                    self.assertEqual(row['endpoint'], '/sessions/{session_id}')
                    self.assertNotIn(session_id, json.dumps(row))
                    response = client.post('/chat', data={'session_id': session_id})
                    self.assertEqual(response.status_code, 400)
                    self.assertIn('x-request-id', response.headers)
                    self.assertEqual(emit.call_args.args[0]['error_type'], 'HTTP400')
                    response = client.get('/secret-arbitrary-path')
                    self.assertEqual(emit.call_args.args[0]['endpoint'], '/unmatched')
                    self.assertNotIn('secret-arbitrary-path', json.dumps(emit.call_args.args[0]))
                    client_id = 'a678cda1-1a41-40b6-81ec-19a60843c97e'
                    response = client.get('/health', headers={'X-Client-Request-ID': client_id})
                    self.assertEqual(emit.call_args.args[0]['client_request_id'], client_id)
                    self.assertNotEqual(response.headers['x-request-id'], client_id)
                    response = client.options('/chat', headers={
                        'Origin': cfg.ORIGIN, 'Access-Control-Request-Method': 'POST',
                        'Access-Control-Request-Headers': 'X-Client-Request-ID',
                    })
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(emit.call_args.args[0]['endpoint'], '/cors-preflight')

    def test_unhandled_error_has_same_id_as_log(self):
        with patch.object(cfg, 'BQ_API_LOGGING_ENABLED', False), TestClient(app, raise_server_exceptions=False) as client:
            with patch.object(app.state.api_log_sink, 'emit') as emit, \
                 patch('app.api.session_routes.store.create', new=AsyncMock(side_effect=RuntimeError('SECRET'))):
                response = client.post('/sessions')
            self.assertEqual(response.status_code, 500)
            self.assertEqual(response.headers['x-request-id'], emit.call_args.args[0]['request_id'])
            self.assertNotIn('SECRET', response.text)

    def test_chat_logs_outcome_stages_and_correlates_json_with_header(self):
        async def answer(_entry, _question, _photo):
            metadata(outcome='unsupported')
            with span('agent'):
                await asyncio.sleep(0)
            return {'blocks': [{'type': 'text', 'content': cfg.UNAVAILABLE}]}, False
        with patch.object(cfg, 'BQ_API_LOGGING_ENABLED', False), TestClient(app) as client:
            session_id = client.post('/sessions').json()['session_id']
            with patch.object(app.state.api_log_sink, 'emit') as emit, \
                 patch('app.api.chat_routes.answer_question', side_effect=answer):
                response = client.post('/chat', data={'session_id': session_id, 'message': 'SECRET QUESTION'})
            row = emit.call_args.args[0]
            self.assertEqual(response.json()['request_id'], row['request_id'])
            self.assertEqual(response.headers['x-request-id'], row['request_id'])
            self.assertEqual(row['outcome'], 'unsupported')
            self.assertIn('session_lock_wait_ms', row['timings_json']['stages_ms'])
            self.assertIn('agent_ms', row['timings_json']['stages_ms'])
            self.assertNotIn('SECRET QUESTION', json.dumps(row))
