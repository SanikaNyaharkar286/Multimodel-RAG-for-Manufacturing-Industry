"""Administrator-only access to the manual intake area in GCS.

The ingestion pipeline remains the owner of processing and BigQuery writes.
This router only lists raw machine prefixes and places approved PDF files in
the exact ``raw/<machine>/<file>.pdf`` location watched by Eventarc.
"""
import asyncio
import re
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from auth_system.security import require_admin
from app import config as cfg
from app.media.manual_images import storage_client

router = APIRouter()
_MACHINE_PATTERN = re.compile(r"[^/\\\x00-\x1f\x7f]+")
_MAX_UPLOAD_BYTES = 100 * 1024 * 1024


def _machine_name(value: str) -> str:
    name = str(value or "").strip()
    if not name or name in {".", ".."} or not _MACHINE_PATTERN.fullmatch(name):
        raise HTTPException(
            status_code=422,
            detail="Machine name must be a non-empty folder name without slashes or control characters.",
        )
    return name


def _filename(value: str) -> str:
    # UploadFile.filename is client supplied; only its final safe component is used.
    name = PurePosixPath(str(value or "").replace("\\", "/")).name.strip()
    if not name or name in {".", ".."} or not name.lower().endswith(".pdf"):
        raise HTTPException(status_code=422, detail="Only PDF manual files can be uploaded.")
    if any(ord(char) < 32 for char in name):
        raise HTTPException(status_code=422, detail="The document filename contains an invalid character.")
    return name


def _list_machines() -> list[str]:
    prefix = f"{cfg.RAW_PREFIX}/"
    bucket = storage_client().bucket(cfg.BUCKET)
    iterator = bucket.list_blobs(prefix=prefix, delimiter="/")
    # GCS exposes first-level folders through iterator.prefixes only after the
    # iterator has been consumed. The fallback also supports compatible clients.
    objects = list(iterator)
    prefixes = set(getattr(iterator, "prefixes", ()) or ())
    machines = {item[len(prefix):].rstrip("/") for item in prefixes if item.startswith(prefix)}
    for blob in objects:
        object_name = str(getattr(blob, "name", ""))
        if object_name.startswith(prefix):
            remainder = object_name[len(prefix):]
            if "/" in remainder:
                machines.add(remainder.split("/", 1)[0])
    return sorted(machine for machine in machines if machine and machine not in {".", ".."} and not machine.startswith("_"))


def _upload(machine: str, upload: UploadFile) -> dict:
    filename = _filename(upload.filename)
    object_name = f"{cfg.RAW_PREFIX}/{machine}/{filename}"
    stream = upload.file
    stream.seek(0)
    header = stream.read(5)
    if header != b"%PDF-":
        raise HTTPException(status_code=422, detail=f"{filename} is not a valid PDF manual.")
    stream.seek(0)
    stream.seek(0, 2)
    size = stream.tell()
    if size <= 0 or size > _MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail=f"{filename} must be between 1 byte and 100 MB.")
    stream.seek(0)
    blob = storage_client().bucket(cfg.BUCKET).blob(object_name)
    try:
        blob.upload_from_file(
            stream,
            content_type="application/pdf",
            if_generation_match=0,
            timeout=120,
        )
    except Exception as exc:
        if "precondition" in str(exc).lower() or "412" in str(exc):
            raise HTTPException(status_code=409, detail=f"A document named {filename} already exists for this machine.") from exc
        raise
    return {"filename": filename, "object": object_name, "uri": f"gs://{cfg.BUCKET}/{object_name}"}


@router.get("/admin/files/machines")
async def machines(identity=Depends(require_admin)):
    return {"raw_prefix": cfg.RAW_PREFIX, "machines": await asyncio.to_thread(_list_machines)}


@router.post("/admin/files/upload")
async def upload_files(
    machine_name: str = Form(..., min_length=1, max_length=200),
    files: list[UploadFile] = File(...),
    identity=Depends(require_admin),
):
    machine = _machine_name(machine_name)
    if not files:
        raise HTTPException(status_code=422, detail="Select at least one PDF manual.")
    if len(files) > 20:
        raise HTTPException(status_code=422, detail="Upload at most 20 PDF manuals at a time.")
    results = []
    for upload in files:
        results.append(await asyncio.to_thread(_upload, machine, upload))
    # A GCS folder is an object-name prefix; uploading the first file creates
    # raw/<machine>/ automatically. No empty marker is created because the
    # Eventarc trigger intentionally accepts only raw/<machine>/<file>.pdf.
    return {"machine": machine, "uploaded": results, "message": "Files uploaded to the raw intake folder."}
