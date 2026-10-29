# saves and loads chat sessions and reports so they are not lost.
# Reports use Firestore when login is on. Chat checkpoints are separately opt-in.
import base64, gzip, json, threading
from copy import deepcopy
from datetime import date, datetime, timezone
from decimal import Decimal
from uuid import uuid4
from app import config as cfg

SESSION_COLLECTION='chatSessions'; REPORT_COLLECTION='maintenanceReports'; PAYLOAD_COLLECTION='payload'; CHUNK_BYTES=700*1024
_LOCK=threading.Lock(); _MEMORY_SNAPSHOTS={}; _MEMORY_REPORTS={}; _REPORT_CACHE={}

# turns bytes, dates and decimals into a form json can save
def encode(v):
    if isinstance(v,bytes): return {'__bytes__':base64.b64encode(v).decode()}
    if hasattr(v,'model_dump'): return {'__answer__':v.model_dump()}
    if isinstance(v,datetime): return {'__datetime__':v.isoformat()}
    if isinstance(v,date): return {'__date__':v.isoformat()}
    if isinstance(v,Decimal): return {'__decimal__':str(v)}
    raise TypeError(type(v).__name__)
# turns the saved json back into bytes, dates and decimals
def decode(v):
    if '__bytes__' in v:return base64.b64decode(v['__bytes__'])
    if '__answer__' in v:
        from app.responses.schemas import AgentAnswer
        return AgentAnswer.model_validate(v['__answer__'])
    if '__datetime__' in v:return datetime.fromisoformat(v['__datetime__'])
    if '__date__' in v:return date.fromisoformat(v['__date__'])
    if '__decimal__' in v:return Decimal(v['__decimal__'])
    return v
# true when login is off, so data stays in memory
def _memory(): return not cfg.AUTH_ENABLED
# connects to firestore
def _client():
    from firebase_admin import firestore
    from auth_system.security import firebase_app
    return firestore.client(app=firebase_app(),database_id=cfg.FIRESTORE_DATABASE_ID)
# packs the session details into one dict so they can be saved
def _body(e,state): return {**{n:getattr(e,n) for n in ('id','user_id','owner_uid','expires_at','history','assets','uploaded_photo','repair_workflow','repair_cache','cases','receipts')},'state':state}
# same as _body, used for session snapshots
def _snapshot_body(e,state): return _body(e,state)
# zips the data, splits it into small parts and saves it in firestore
def _write(ref,value,owner=''):
    raw=gzip.compress(json.dumps(value,default=encode,ensure_ascii=False,separators=(',',':')).encode()); generation=uuid4().hex
    pieces=[raw[i:i+CHUNK_BYTES] for i in range(0,len(raw),CHUNK_BYTES)] or [b'']
    for i,piece in enumerate(pieces): ref.collection(PAYLOAD_COLLECTION).document(f'{generation}-{i:05d}').set({'data':piece})
    ref.set({'generation':generation,'chunkCount':len(pieces),'ownerUid':owner or value.get('owner_uid',''),'updatedAt':datetime.now(timezone.utc)})
# reads the saved parts, joins them and unzips them back into data
def _read(ref,meta):
    generation=meta.get('generation'); count=int(meta.get('chunkCount',0) or 0)
    if not generation or not count:return None
    raw=b''.join(ref.collection(PAYLOAD_COLLECTION).document(f'{generation}-{i:05d}').get().to_dict()['data'] for i in range(count))
    return json.loads(gzip.decompress(raw).decode(),object_hook=decode)
# saves a chat session
def save_snapshot(e,state):
    if cfg.CHAT_SESSION_CHECKPOINTS_ENABLED: save_snapshot_body(_body(e,state))
# saves the session in memory or in firestore
def save_snapshot_body(body):
    if not cfg.CHAT_SESSION_CHECKPOINTS_ENABLED: return
    if _memory():
        with _LOCK:_MEMORY_SNAPSHOTS[body['id']]=deepcopy(body)
    else:_write(_client().collection(SESSION_COLLECTION).document(body['id']),body)
# gets a saved session by id, or None if not found
def load_snapshot(sid):
    if not cfg.CHAT_SESSION_CHECKPOINTS_ENABLED: return None
    if _memory():
        with _LOCK:return deepcopy(_MEMORY_SNAPSHOTS.get(sid))
    ref=_client().collection(SESSION_COLLECTION).document(sid); snap=ref.get(); return _read(ref,snap.to_dict()) if snap.exists else None
# deletes a saved session
def delete_snapshot(sid):
    if not cfg.CHAT_SESSION_CHECKPOINTS_ENABLED: return
    if _memory():
        with _LOCK:_MEMORY_SNAPSHOTS.pop(sid,None)
    else:_client().collection(SESSION_COLLECTION).document(sid).delete()
# does nothing right now. meant for removing old sessions
def purge_expired_snapshots(): pass
# keeps a quick copy of the report. stops with an error if the report was changed by someone else
def prime_report_cache(report,owner,expected_revision=None):
    with _LOCK:
        old=_REPORT_CACHE.get(report['report_id'])
        if old and expected_revision is not None and (old['owner']!=owner or old['body'].get('revision')!=expected_revision):raise ValueError('This report has changed. Reopen it before saving.')
        _REPORT_CACHE[report['report_id']]={'owner':owner,'body':deepcopy(report)}
# saves a report for its owner
def save_report(report,owner,expected_revision=None):
    prime_report_cache(report,owner,expected_revision)
    if _memory():
        with _LOCK:_MEMORY_REPORTS[report['report_id']]={'owner':owner,'body':deepcopy(report)}
    else:_write(_client().collection(REPORT_COLLECTION).document(report['report_id']),report,owner)
# gets a report by id, only if it belongs to this owner
def load_report(rid,owner):
    with _LOCK:
        cached=_REPORT_CACHE.get(rid)
        if cached and cached['owner']==owner:return deepcopy(cached['body'])
    if _memory():
        with _LOCK:
            x=_MEMORY_REPORTS.get(rid); return deepcopy(x['body']) if x and x['owner']==owner else None
    ref=_client().collection(REPORT_COLLECTION).document(rid); snap=ref.get(); meta=snap.to_dict() if snap.exists else None
    return _read(ref,meta) if meta and meta.get('ownerUid') == owner else None
