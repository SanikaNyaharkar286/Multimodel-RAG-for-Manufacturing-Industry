"""Share concurrent reads without caching failures or request-specific state."""
from concurrent.futures import Future
from threading import Lock


class SingleFlight:
    def __init__(self):
        self._lock = Lock()
        self._pending = {}

    def run(self, key, read):
        with self._lock:
            future = self._pending.get(key)
            owner = future is None
            if owner:
                future = self._pending[key] = Future()
        if not owner:
            return future.result()
        try:
            value = read()
            future.set_result(value)
            return value
        except BaseException as error:
            future.set_exception(error)
            raise
        finally:
            with self._lock:
                if self._pending.get(key) is future:
                    del self._pending[key]
