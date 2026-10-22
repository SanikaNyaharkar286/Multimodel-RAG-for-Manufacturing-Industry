r"""Inspect local request timings without printing prompts, identities or provider messages.

From backend: .\.venv\Scripts\python.exe .\scripts\inspect_latency.py --last 12
Times are seconds and timestamps are UTC. Nested/parallel spans must not be added.
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import statistics


def seconds(row, *names):
    stages = row.get('timings_json', {}).get('stages_ms', {})
    return sum(stages.get(name + '_ms', 0) for name in names) / 1000


def distribution(rows):
    values = sorted(row['total_ms'] / 1000 for row in rows)
    if not values:
        return 'none'
    p90 = values[int((len(values) - 1) * .9)]
    return (f'n={len(values)} median={statistics.median(values):.2f}s '
            f'p90={p90:.2f}s max={max(values):.2f}s')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--last', type=int, default=12, help='Number of recent chat requests to display')
    parser.add_argument('--since', default='', help='UTC date/time prefix lower bound, e.g. 2026-10-21T12:00')
    parser.add_argument('--logs', type=Path, default=Path(__file__).resolve().parents[1] / 'logs',
                        help='Directory containing api.log and rotated api.log.* files')
    args = parser.parse_args()
    if args.last < 1:
        parser.error('--last must be positive')

    rows, skipped = [], 0
    for path in args.logs.glob('api.log*'):
        with path.open(encoding='utf-8-sig') as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        skipped += 1
                        continue
                    if (row.get('event') == 'api_request' and row.get('endpoint') == '/chat'
                            and row.get('timestamp', '') >= args.since
                            and isinstance(row.get('total_ms'), (int, float))):
                        rows.append(row)
                except (ValueError, TypeError):
                    skipped += 1
    rows.sort(key=lambda row: row.get('timestamp', ''))
    if not rows:
        print('No matching /chat requests found.')
        return

    active = [row for row in rows if row.get('model_calls', 0)]
    print('All chat requests (includes validation, direct replies and test requests): ' + distribution(rows))
    print('Requests with model or embedding API calls: ' + distribution(active))
    print('Outcomes: ' + ', '.join(f'{key}={value}' for key, value in Counter(
        row.get('outcome', 'unknown') for row in rows).items()))
    print('Nested/parallel spans overlap; retrieval already includes embedding and BigQuery.')
    headers = ('UTC', 'HTTP', 'Outcome', 'Total', 'Auth', 'Plan', 'Embed', 'BQ', 'Media', 'Agent', 'Calls', 'Cache', 'Provider')
    table = []
    for row in rows[-args.last:]:
        details = row.get('timings_json', {}).get('metadata', {})
        provider = details.get('provider_error_code')
        if provider is None:
            provider = next((event.get('provider_error_code') for event in
                             row.get('timings_json', {}).get('events', [])
                             if event.get('provider_error_code') is not None), '-')
        cache = details.get('repair_plan_cache_hit', row.get('cache_hits', '-'))
        table.append((row['timestamp'][:19], str(row.get('status_code', '-')), row.get('outcome', '-'),
                      f"{row['total_ms'] / 1000:.2f}",
                      f"{seconds(row, 'auth_token_verify', 'auth_profile_read'):.2f}",
                      f"{seconds(row, 'planner'):.2f}", f"{seconds(row, 'embedding'):.2f}",
                      f"{seconds(row, 'bq_seed_query', 'bq_context_query', 'qr_query'):.2f}",
                      f"{seconds(row, 'media'):.2f}", f"{seconds(row, 'agent'):.2f}",
                      str(row.get('model_calls', '-')), str(cache), str(provider)))
    widths = [max(len(str(value)) for value in column) for column in zip(headers, *table)]
    print('  '.join(value.ljust(width) for value, width in zip(headers, widths)))
    for values in table:
        print('  '.join(value.ljust(width) for value, width in zip(values, widths)))
    if skipped:
        print(f'Skipped {skipped} malformed log lines.')
    print('Auth=0 on older logs means unmeasured; missing API usage is unknown, not proof of no provider activity.')


if __name__ == '__main__':
    main()
