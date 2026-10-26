"""Pure ASGI middleware: covers body parsing, failures, and response transmission."""
import asyncio
from uuid import UUID
from .timing import Trace, current_trace, session_metadata
from app import config as cfg


class PerformanceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        trace = Trace(scope['method'])
        client_id = dict(scope.get('headers', [])).get(b'x-client-request-id', b'')
        try:
            if client_id:
                trace.put(client_request_id=str(UUID(client_id.decode('ascii'))))
        except (ValueError, UnicodeDecodeError):
            pass
        scope['api_request_id'] = trace.request_id
        token = current_trace.set(trace)
        status_code, disconnected = 500, False

        async def observed_receive():
            nonlocal disconnected
            message = await receive()
            if message['type'] == 'http.disconnect':
                disconnected = True
            return message

        async def observed_send(message):
            nonlocal status_code
            if message['type'] == 'http.response.start':
                status_code = message['status']
                headers = list(message.get('headers', []))
                headers.append((b'x-request-id', trace.request_id.encode()))
                if cfg.API_TIMING_DEBUG:
                    snapshot = trace.snapshot('', status_code)
                    stages = snapshot['timings_json']['stages_ms']
                    selected = ['auth_token_verify', 'auth_profile_read', 'planner',
                                'retrieval', 'media', 'agent', 'response_build']
                    timing = [f'response_ready;dur={snapshot["total_ms"]:.2f}']
                    timing += [f'{name};dur={stages[name+"_ms"]:.2f}' for name in selected if name+'_ms' in stages]
                    headers.append((b'server-timing', ', '.join(timing).encode()))
                message = {**message, 'headers': headers}
            await send(message)

        try:
            await self.app(scope, observed_receive, observed_send)
        except BaseException as exc:
            trace.put(error_type=type(exc).__name__)
            if isinstance(exc, asyncio.CancelledError):
                status_code = 499
            raise
        finally:
            route = scope.get('route')
            endpoint = getattr(route, 'path', None)
            # Never store arbitrary paths: URLs can contain session IDs and user data.
            endpoint = endpoint or ('/cors-preflight' if scope['method'] == 'OPTIONS' else '/unmatched')
            params = scope.get('path_params', {})
            if params.get('session_id'):
                session_metadata(str(params['session_id']))
            if disconnected and status_code == 500:
                status_code = 499
            if status_code >= 400 and 'error_type' not in trace.metadata:
                trace.put(error_type=f'HTTP{status_code}')
            row = trace.snapshot(endpoint, status_code, close=True)
            sink = getattr(getattr(scope.get('app'), 'state', None), 'api_log_sink', None)
            try:
                if sink:
                    sink.emit(row)
            except Exception:
                # Telemetry is never allowed to turn a completed answer into an error.
                pass
            finally:
                current_trace.reset(token)
