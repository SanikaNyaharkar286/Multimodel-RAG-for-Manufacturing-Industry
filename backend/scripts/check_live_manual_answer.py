"""Read-only end-to-end diagnostic using an isolated, in-memory chat.

Uses the configured manual dataset and Gemini. It does not change BigQuery,
create reports, or record technician actions in a production conversation.
Run from backend: python -m scripts.check_live_manual_answer --question "..."
"""
import argparse
import asyncio
import json
import time

from app import config
from app.agent.chat_service import answer_question, cached_step_previews
from app.sessions.session_store import store


async def check(question, summary=False):
    config.CHAT_SESSION_CHECKPOINTS_ENABLED = False
    entry = await store.create('isolated-manual-diagnostic')
    started = time.monotonic()
    try:
        response, _ = await answer_question(entry, question, None)
        cache = entry.repair_cache or {}
        future = []
        for value in cache.get('remaining_answers', []):
            if isinstance(value, dict):
                future.append(value.get('instruction'))
            elif value.repair_step:
                future.append(value.repair_step.instruction)
        preview_started = time.perf_counter()
        previews = cached_step_previews(entry)
        preview_ms = round((time.perf_counter() - preview_started) * 1000, 2)
        visible = ({'blocks':response.get('blocks'), 'sources':response.get('sources'),
                    'current_step':(response.get('repair_workflow') or {}).get('current_step')}
                   if summary else response)
        print(json.dumps({'elapsed_seconds':round(time.monotonic() - started, 2),
                          'response':visible, 'prepared_remaining_steps':future,
                          'cached_preview_count':len(previews), 'cached_preview_ms':preview_ms,
                          'cache_ready':cache.get('plan_ready'),
                          'coverage_verified':response.get('coverage_verified')}, ensure_ascii=False, default=str), flush=True)
    finally:
        if entry.safety_task and not entry.safety_task.done():
            entry.safety_task.cancel()
        await store.delete(entry.id)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--question', required=True)
    parser.add_argument('--summary', action='store_true')
    args = parser.parse_args()
    asyncio.run(check(args.question, args.summary))
