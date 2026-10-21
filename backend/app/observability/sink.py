"""Local logging and bounded, batched BigQuery export never run in the request path."""
import json
import logging
import logging.handlers
import queue
import threading
import time
from app import config as cfg
from .storage import log_client, insert_records


class JsonFormatter(logging.Formatter):
    def format(self, record):
        return json.dumps(record.event, ensure_ascii=False, allow_nan=False)


class SafeQueueHandler(logging.handlers.QueueHandler):
    def __init__(self, log_queue, owner):
        super().__init__(log_queue)
        self.owner = owner

    def enqueue(self, record):
        try:
            self.queue.put_nowait(record)
        except queue.Full:
            self.owner.local_dropped += 1


class DrainingQueueListener(logging.handlers.QueueListener):
    def enqueue_sentinel(self):
        self.queue.put(self._sentinel, timeout=5)


class ApiLogSink:
    def __init__(self):
        self.bq_queue = queue.Queue(maxsize=cfg.API_LOG_QUEUE_SIZE)
        self.local_queue = queue.Queue(maxsize=10000)
        self.stop_event = threading.Event()
        self.listener = self.worker = self.file_handler = None
        self.accepting = False
        self.exported = self.export_failed = self.bq_dropped = self.local_dropped = 0
        self.last_error = None
        self.logger = logging.getLogger(f'api.performance.{id(self)}')
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False

    def start(self):
        try:
            cfg.API_LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)
            self.file_handler = logging.handlers.RotatingFileHandler(
                cfg.API_LOG_DIRECTORY / 'api.log', maxBytes=10*1024*1024, backupCount=5,
                encoding='utf-8')
            self.file_handler.setFormatter(JsonFormatter())
            self.listener = DrainingQueueListener(self.local_queue, self.file_handler)
            self.logger.addHandler(SafeQueueHandler(self.local_queue, self))
            self.listener.start()
        except OSError as exc:
            self.last_error = type(exc).__name__
            logging.getLogger('uvicorn.error').warning(
                'Local API logging is unavailable (%s); request handling continues', self.last_error)
        self.accepting = True
        if cfg.BQ_API_LOGGING_ENABLED:
            self.worker = threading.Thread(target=self._export_loop, daemon=True, name='api-log-export')
            self.worker.start()

    def local(self, event):
        if self.accepting:
            self.logger.info('api_event', extra={'event': event})

    def emit(self, row):
        self.local({'event': 'api_request', **row})
        if self.accepting and cfg.BQ_API_LOGGING_ENABLED:
            try:
                self.bq_queue.put_nowait(row)
            except queue.Full:
                self.bq_dropped += 1
                self.local({'event': 'bq_queue_full', 'request_id': row['request_id']})

    def _export_loop(self):
        client = None
        while not self.stop_event.is_set() or not self.bq_queue.empty():
            batch = []
            try:
                batch.append(self.bq_queue.get(timeout=0.2))
            except queue.Empty:
                continue
            deadline = time.monotonic() + cfg.API_LOG_FLUSH_SECONDS
            while len(batch) < cfg.API_LOG_BATCH_SIZE:
                try:
                    batch.append(self.bq_queue.get_nowait())
                except queue.Empty:
                    if self.stop_event.is_set() or time.monotonic() >= deadline:
                        break
                    self.stop_event.wait(0.05)
            for attempt in range(3):
                try:
                    client = client or log_client()
                    insert_records(client, batch)
                    self.exported += len(batch)
                    self.last_error = None
                    break
                except Exception as exc:
                    self.last_error = type(exc).__name__
                    if attempt < 2:
                        self.stop_event.wait(min(attempt+1, 2))
                    else:
                        self.export_failed += len(batch)
                        self.local({'event': 'bq_export_failed', 'error_type': self.last_error,
                                    'request_ids': [row['request_id'] for row in batch], 'attempts': 3})
            for _ in batch:
                self.bq_queue.task_done()

    def status(self):
        return {'local_enabled': self.listener is not None,
                'bq_enabled': cfg.BQ_API_LOGGING_ENABLED, 'queue_pending': self.bq_queue.qsize(),
                'exported': self.exported, 'export_failed': self.export_failed,
                'bq_dropped': self.bq_dropped, 'local_dropped': self.local_dropped,
                'last_error_type': self.last_error}

    def close(self):
        self.stop_event.set()
        if self.worker:
            self.worker.join(timeout=35)
        if self.worker and self.worker.is_alive():
            # Bound shutdown; local summaries already contain the affected records.
            self.logger.info('export_shutdown_pending', extra={'event': {
                'event': 'export_shutdown_pending', 'pending': self.bq_queue.qsize()}})
        self.accepting = False
        if self.listener:
            self.listener.stop()
        self.logger.handlers.clear()
        if self.file_handler:
            self.file_handler.close()
