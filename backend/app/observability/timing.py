"""Thread-safe request measurements. Never collect question, prompt or image data."""
import contextvars
import functools
import hashlib
import hmac
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4

current_trace = contextvars.ContextVar('api_trace', default=None)
# Extra diagnostics live inside the existing JSON column, so adding a
# measurement does not invalidate a deployed BigQuery log-table schema.
_DETAIL_META = {'provider_error_code', 'provider_error_status', 'plan_cache_hit',
                'procedure_cache_hit', 'procedure_step_count', 'procedure_step_index',
                'repair_plan_cache_hit', 'repair_plan_step_count'}
_PROVIDER_STATUSES = {'CANCELLED', 'UNKNOWN', 'INVALID_ARGUMENT', 'DEADLINE_EXCEEDED',
                      'NOT_FOUND', 'ALREADY_EXISTS', 'PERMISSION_DENIED',
                      'RESOURCE_EXHAUSTED', 'FAILED_PRECONDITION', 'ABORTED',
                      'OUT_OF_RANGE', 'UNIMPLEMENTED', 'INTERNAL', 'UNAVAILABLE',
                      'DATA_LOSS', 'UNAUTHENTICATED'}
_DEGRADED_OUTCOMES = {'model_fallback', 'text_only_fallback'}
STAGE_COLUMNS = ('planner_ms', 'embedding_ms', 'bq_seed_query_ms', 'bq_context_query_ms',
                 'qr_query_ms', 'retrieval_ms', 'media_ms', 'gcs_download_ms', 'pdf_render_ms',
                 'image_normalization_ms', 'video_check_ms', 'agent_ms', 'answer_validation_ms',
                 'response_build_ms', 'session_lock_wait_ms', 'upload_normalization_ms',
                 'payload_build_ms', 'history_save_ms')
_META = {'session_hash', 'client_request_id', 'question_length', 'has_image', 'reused_image', 'image_size_bytes',
         'normalized_image_bytes', 'chunk_count', 'response_image_count', 'response_link_count',
         'asset_warning_count', 'model_image_bytes', 'payload_characters', 'outcome', 'error_type',
         'planner_fallback', 'input_tokens', 'output_tokens', 'thinking_tokens', 'model_calls',
         'cache_hits', 'cache_misses', 'events_truncated'} | _DETAIL_META
_SPAN = {'model', 'job_id', 'bytes_processed', 'bytes_billed', 'cache_hit',
         'input_tokens', 'output_tokens', 'thinking_tokens',
         'provider_error_code', 'provider_error_status'}


def _safe_values(values, allowed):
    result = {}
    for key, value in values.items():
        if key not in allowed or not isinstance(value, (str, int, float, bool, type(None))):
            continue
        if key == 'provider_error_code' and not (type(value) is int and 100 <= value <= 599):
            continue
        if key == 'provider_error_status' and value not in _PROVIDER_STATUSES:
            continue
        result[key] = value
    return result


def provider_error_metadata(error):
    """Keep numeric HTTP codes and known status names, never provider messages."""
    return _safe_values({'provider_error_code': getattr(error, 'code', None),
                         'provider_error_status': getattr(error, 'status', None)}, _DETAIL_META)


class Trace:
    def __init__(self, method):
        self.request_id = str(uuid4())
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.started = time.perf_counter()
        self.method = method
        self.metadata = {}
        self.events = []
        self.lock = threading.Lock()
        self.closed = False

    def put(self, **values):
        with self.lock:
            if not self.closed:
                self.metadata.update(_safe_values(values, _META))

    def increment(self, key, amount=1):
        with self.lock:
            if not self.closed and key in _META:
                self.metadata[key] = self.metadata.get(key, 0) + amount

    def start(self, name, started=None):
        with self.lock:
            if self.closed:
                return None
            if len(self.events) >= 500:
                self.metadata['events_truncated'] = True
                return None
            event = {'name': name, 'start_ms': round(((started or time.perf_counter())-self.started)*1000, 3),
                     'status': 'running'}
            self.events.append(event)
            return event

    def finish(self, event, started, error, attributes):
        if event is None:
            return
        with self.lock:
            if not self.closed:
                event.update(duration_ms=round((time.perf_counter()-started)*1000, 3),
                             status='error' if error else 'ok')
                if error:
                    event['error_type'] = type(error).__name__
                    provider = provider_error_metadata(error)
                    event.update(provider)
                    self.metadata.update(provider)
                event.update(_safe_values(attributes, _SPAN))

    def snapshot(self, endpoint, status_code, close=False):
        with self.lock:
            now_ms = round((time.perf_counter()-self.started)*1000, 3)
            events = [dict(event) for event in self.events]
            for event in events:
                if event['status'] == 'running':
                    event.update(duration_ms=round(now_ms-event['start_ms'], 3), status='unfinished')
            timings = {}
            for event in events:
                key = event['name'] + '_ms'
                timings[key] = round(timings.get(key, 0) + event['duration_ms'], 3)
            if close:
                self.closed = True
            metadata = dict(self.metadata)
            reported_outcome = metadata.pop('outcome', 'completed')
            degraded = reported_outcome in _DEGRADED_OUTCOMES
            outcome = (reported_outcome if status_code < 400 and
                       (degraded or not metadata.get('error_type')) else 'error')
            diagnostics = {key: metadata.pop(key) for key in _DETAIL_META if key in metadata}
            timings_json = {'stages_ms': timings, 'events': events}
            if diagnostics:
                timings_json['metadata'] = diagnostics
            return {'timestamp': self.timestamp, 'request_id': self.request_id,
                    'method': self.method, 'endpoint': endpoint, 'status_code': status_code,
                    'success': status_code < 400 and not metadata.get('error_type') and not degraded,
                    'outcome': outcome, 'total_ms': now_ms,
                    'timings_json': timings_json,
                    **{key: timings[key] for key in STAGE_COLUMNS if key in timings}, **metadata}


@contextmanager
def span(name, **attributes):
    trace = current_trace.get()
    event = trace.start(name) if trace else None
    started = time.perf_counter()
    error = None
    try:
        yield attributes
    except BaseException as exc:
        error = exc
        raise
    finally:
        if trace:
            trace.finish(event, started, error, attributes)


def timed(name):
    def decorate(function):
        @functools.wraps(function)
        def measured(*args, **kwargs):
            with span(name):
                return function(*args, **kwargs)
        return measured
    return decorate


def metadata(**values):
    trace = current_trace.get()
    if trace:
        trace.put(**values)


def increment(key, amount=1):
    trace = current_trace.get()
    if trace:
        trace.increment(key, amount)


def session_metadata(session_id):
    from app import config as cfg
    metadata(session_hash=hmac.new(cfg.API_LOG_HASH_KEY.encode(), session_id.encode(),
                                  hashlib.sha256).hexdigest()[:24])


def usage_metadata(usage):
    """SDK versions/providers do not always expose token usage; missing is unknown."""
    if usage is None:
        return {}
    values = {}
    for field, key in (('prompt_token_count', 'input_tokens'),
                       ('candidates_token_count', 'output_tokens'),
                       ('thoughts_token_count', 'thinking_tokens')):
        value = getattr(usage, field, None)
        if isinstance(value, int):
            values[key] = value
            increment(key, value)
    return values


def submit_with_context(pool, function, *args):
    # asyncio.to_thread copies ContextVars; ThreadPoolExecutor.submit does not.
    return pool.submit(contextvars.copy_context().run, function, *args)
