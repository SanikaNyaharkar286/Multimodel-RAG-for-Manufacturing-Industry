"""Validated manual references and a generation-aware, manual-only PNG cache.

No user uploads are shared here. Model image references never become public
URLs; session/report authorization remains in the existing API routes.
"""
import hashlib
import json
import mimetypes
import threading
from collections import OrderedDict
from pathlib import PurePosixPath
from urllib.parse import urlparse
from uuid import uuid4

from app import config as cfg
from app.media.manual_images import doc_stem, normalize_image, storage_client, MAX_SOURCE_BYTES
from app.observability.timing import increment, span
from app.retrieval.cache_revision import corpus_revision
from app.services.singleflight import SingleFlight

_MEMORY = OrderedDict()
_LOCK = threading.Lock()
_FLIGHTS = SingleFlight()
_PNG = b'\x89PNG\r\n\x1a\n'


def manual_reference(path, document, machine=''):
    """Resolve only this machine/manual's assets folder, without network I/O."""
    value = str(path).replace('\\', '/')
    machine = str(machine)
    if '..' in PurePosixPath(machine).parts or machine.startswith('/'):
        raise ValueError('Invalid manual machine path')
    prefix = f'{cfg.PROCESSED_PREFIX}/{machine}/' if machine else f'{cfg.PROCESSED_PREFIX}/'
    allowed = f'{prefix}{doc_stem(document)}/assets/'
    parsed = urlparse(value)
    if parsed.query or parsed.fragment or '..' in PurePosixPath(parsed.path).parts:
        raise ValueError('Invalid manual image reference')
    if parsed.scheme == 'gs':
        name = parsed.path.lstrip('/')
        if parsed.netloc != cfg.BUCKET or not name.startswith(allowed):
            raise ValueError('Image reference is outside the approved manual assets folder')
    elif parsed.scheme or value.startswith('/'):
        raise ValueError('External image references are not permitted')
    else:
        name = f'{prefix}{doc_stem(document)}/{value}' if value.startswith('assets/') else allowed + PurePosixPath(value).name
    if not name.startswith(allowed) or name == allowed:
        raise ValueError('Invalid manual image reference')
    mime = mimetypes.guess_type(name)[0] or ''
    return {'manual_image': True, 'uri': f'gs://{cfg.BUCKET}/{name}',
            'path': value, 'document': document, 'machine': machine,
            'mime_type': mime, 'revision': list(corpus_revision())}


def validate_reference(reference):
    expected = manual_reference(reference['path'], reference['document'], reference.get('machine', ''))
    if reference.get('uri') != expected['uri'] or reference.get('manual_image') is not True:
        raise ValueError('Invalid approved manual reference')
    if reference.get('revision') != expected['revision']:
        raise ValueError('Manual sources changed; refresh this conversation before loading its image')
    return expected


def image_identity(asset):
    data = asset.get('bytes')
    if isinstance(data, bytes):
        return hashlib.sha256(data).digest()
    reference = validate_reference(asset['reference'])
    return hashlib.sha256(json.dumps(reference, sort_keys=True).encode()).digest()


def _disk_read(path):
    try:
        packed = path.read_bytes()
        data = packed[32:]
        if data.startswith(_PNG) and hashlib.sha256(data).digest() == packed[:32]:
            return data
    except OSError:
        pass
    return None


def _disk_write(path, data):
    if not cfg.MANUAL_IMAGE_CACHE_BYTES or len(data) > cfg.MANUAL_IMAGE_CACHE_BYTES:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f'.{uuid4().hex}.tmp')
        temporary.write_bytes(hashlib.sha256(data).digest() + data)
        temporary.replace(path)
        # Only cache files in this dedicated directory are eligible for eviction.
        files = sorted(path.parent.glob('*.image'), key=lambda item: item.stat().st_mtime)
        size = sum(item.stat().st_size for item in files)
        for item in files:
            if size <= cfg.MANUAL_IMAGE_CACHE_BYTES:
                break
            item_size = item.stat().st_size
            item.unlink()
            size -= item_size
    except OSError:
        # A read-only/full cache must not suppress an otherwise valid diagram.
        increment('manual_disk_cache_write_errors')


def load_manual_reference(reference):
    """Materialize a selected image, sharing concurrent reads and pinning its generation."""
    reference = validate_reference(reference)
    flight_key = (reference['uri'], tuple(reference['revision']))
    def read():
        parsed = urlparse(reference['uri'])
        blob = storage_client().bucket(parsed.netloc).blob(parsed.path.lstrip('/'))
        # Revalidate generation even after restart; a replaced object cannot
        # reuse cached bytes from its earlier version.
        with span('manual_image_metadata'):
            blob.reload(timeout=4, retry=None)
        generation = str(blob.generation or '')
        if not generation or not 0 < int(blob.size or 0) <= MAX_SOURCE_BYTES:
            raise ValueError('Manual image metadata is invalid or exceeds the size budget')
        key = hashlib.sha256(json.dumps([*flight_key, generation, 'png-1800-v1']).encode()).hexdigest()
        with _LOCK:
            if key in _MEMORY:
                _MEMORY.move_to_end(key)
                increment('media_cache_hits')
                return _MEMORY[key]
        path = cfg.BACKEND_ROOT / '.cache' / 'manual-images' / f'{key}.image'
        data = _disk_read(path) if cfg.MANUAL_IMAGE_CACHE_BYTES else None
        if data is not None:
            increment('manual_disk_cache_hits')
        else:
            with span('gcs_bytes_download'):
                original = blob.download_as_bytes(start=0, end=MAX_SOURCE_BYTES,
                    if_generation_match=int(generation), timeout=4, retry=None)
            if not original or len(original) > MAX_SOURCE_BYTES:
                raise ValueError('Manual image exceeds the size budget')
            data = normalize_image(original)
            _disk_write(path, data)
        with _LOCK:
            _MEMORY[key] = data
            while len(_MEMORY) > 256:
                _MEMORY.popitem(last=False)
        return data
    return _FLIGHTS.run(flight_key, read)


def stored_image_bytes(value):
    return value if isinstance(value, bytes) else load_manual_reference(value)
