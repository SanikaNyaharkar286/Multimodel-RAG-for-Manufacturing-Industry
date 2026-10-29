"""Pure projection of recorded technician work for maintenance report summaries.

Only normalized steps and guided-session completion records are consulted. This
module does not load history, generate text, query storage, or modify snapshots.
"""

from copy import deepcopy


def _text(value):
    """Collapse whitespace without summarizing or rewriting recorded text."""
    return ' '.join(str(value).split()) if value is not None else ''


def _confirmed(step):
    review = (_text(step.get('confirmed_by_uid'))
              and _text(step.get('confirmation_recorded_at')))
    legacy = (_text(step.get('performed_by')) and _text(step.get('completed_at')))
    return bool(review or legacy)


def performed_steps(report) -> list[dict]:
    """Return performed actions backed by recorded execution confirmations.

    Normalized statuses are authoritative, including retractions of old session
    completions. A Completed/Failed label alone is not evidence. Evidence is a
    matching workflow completion, a server-recorded review confirmation, or a
    legacy performer plus an actual recorded completion time. Boolean completion
    flags are not evidence.

    When normalized steps are absent/None, workflow completion membership itself
    records completion, even without a timestamp. An explicit empty normalized
    list remains empty. Input order and recorded numbers are retained; missing
    numbers and timestamps are never invented. Returned containers are detached
    from the snapshot, including parts and precautions.
    """
    completed = (report.get('workflow') or {}).get('completed_steps') or []
    steps = report.get('steps')
    legacy_only = steps is None
    if legacy_only:
        steps = completed
    completed_ids = {step.get('step_id') for step in completed if step.get('step_id')}

    result = []
    for step in steps:
        status = _text(step.get('status'))
        if legacy_only and not status:
            status = 'Completed'
        if status not in ('Completed', 'Failed'):
            continue
        if not (legacy_only or step.get('step_id') in completed_ids or _confirmed(step)):
            continue

        title = _text(step.get('title'))
        action = next((text for value in (
            step.get('action_performed'), step.get('instruction'), title
        ) if (text := _text(value))), '')
        result.append({
            'step_id': deepcopy(step.get('step_id', '')),
            'number': deepcopy(step.get('number', '')),
            'title': title,
            'action': action,
            'observation': _text(step.get('observation')) or 'Not provided',
            'status': status,
            'result': _text(step.get('result')) or 'Not tested',
            'precautions': deepcopy(step.get('precautions') or []),
            'parts_used': deepcopy(step.get('parts_used') or ''),
        })
    return result
