"""Invalidate manual-only caches after ingestion without changing its schema.

Run ``python -m app.retrieval.cache_revision`` after replacing indexed manuals.
Running workers observe the marker on their next request; no restart is needed.
"""
from uuid import uuid4
from app import config as cfg


def corpus_revision():
    try:
        marker = (cfg.BACKEND_ROOT / '.cache' / 'manual-corpus.version').read_text(encoding='utf-8').strip()
    except OSError:
        marker = ''
    return (cfg.MANUAL_CORPUS_VERSION, marker)


def invalidate_manual_caches():
    target = cfg.BACKEND_ROOT / '.cache' / 'manual-corpus.version'
    target.parent.mkdir(parents=True, exist_ok=True)
    revision = uuid4().hex
    temporary = target.with_name(f'manual-corpus.{revision}.tmp')
    temporary.write_text(revision, encoding='utf-8')
    temporary.replace(target)
    return revision


if __name__ == '__main__':
    print('Manual cache revision:', invalidate_manual_caches())
