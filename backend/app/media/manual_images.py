# Reads images and PDF manuals from Google Cloud Storage 
# (GCS) or local files, converts PDF pages into images, and resizes images.

import io
import threading
import time
from collections import OrderedDict
from functools import lru_cache
from pathlib import PurePosixPath
from urllib.parse import urlparse
import pymupdf
from google.api_core.exceptions import NotFound
from google.cloud import storage
from PIL import Image
from app import config as cfg
from app.observability.timing import increment, timed, span

# biggest file size we are willing to read (25 mb)
MAX_SOURCE_BYTES = 25 * 1024 * 1024
# short memory of files that were not found, so we do not look for them again and again
_MISSING_CACHE = OrderedDict()
_MISSING_CACHE_LOCK = threading.Lock()
_MISSING_CACHE_TTL_SECONDS = 60
_MISSING_CACHE_MAX = 256


# checks if this file was recently not found. old entries are removed after 60 seconds
def _known_missing(key):
    now = time.monotonic()
    with _MISSING_CACHE_LOCK:
        timestamp = _MISSING_CACHE.get(key)
        if timestamp is None:
            return False
        if now - timestamp >= _MISSING_CACHE_TTL_SECONDS:
            _MISSING_CACHE.pop(key, None)
            return False
        _MISSING_CACHE.move_to_end(key)
    increment('missing_asset_cache_hits')
    return True


# remembers that a file was not found. keeps only the latest 256
def _remember_missing(key):
    with _MISSING_CACHE_LOCK:
        _MISSING_CACHE[key] = time.monotonic()
        _MISSING_CACHE.move_to_end(key)
        while len(_MISSING_CACHE) > _MISSING_CACHE_MAX:
            _MISSING_CACHE.popitem(last=False)


# makes the cloud storage client once and reuses it
@lru_cache(maxsize=1)
def storage_client():
    return storage.Client(project=cfg.PROJECT)


# checks the manual's file name is safe and removes the .pdf from the end
def doc_stem(document):
    name = str(document).replace("\\", "/")

    if "/" in name or name in {"", ".", ".."}:
        raise ValueError("Invalid source document name")

    if name.lower().endswith(".pdf"):
        return name[:-4]

    return name


# downloads a file from the approved cloud storage bucket.
# it rejects empty files and files over the size limit
@timed('gcs_download')
def blob_bytes(name):
    missing_key = ('blob', cfg.BUCKET, name)
    if _known_missing(missing_key):
        raise NotFound('Manual asset was not found in approved storage')
    blob = (
        storage_client()
        .bucket(cfg.BUCKET)
        .blob(name)
    )

    # read at most one byte over the limit.
    # this avoids a separate size check for each file but still rejects big files
    with span('gcs_bytes_download'):
        try:
            # only one try with a short timeout, so a failure does not make the chat wait long
            data = blob.download_as_bytes(
                start=0, end=MAX_SOURCE_BYTES, timeout=4, retry=None,
            )
        except NotFound:
            _remember_missing(missing_key)
            raise

    if not data or len(data) > MAX_SOURCE_BYTES:
        raise ValueError("Manual asset exceeds the configured size budget")

    return data


# finds an image referenced by document_images and gives back its bytes.
# Images are permitted only below the current manual's assets/ folder.
# No machine-folder fallback or filename search is allowed.
@timed('image_read')
@lru_cache(maxsize=128)
@timed('image_cache_miss')
def read_image(path, document, machine=""):
    """
    finds an image from the database in the approved bucket.

    works with gs:// links and the relative paths made by the upload pipeline.
    """

    # prepare the document name and image path
    stem = doc_stem(document)
    value = path.replace("\\", "/")
    parsed = urlparse(value)
    missing_key = ('reference', cfg.BUCKET, cfg.PROCESSED_PREFIX, value, document, machine)
    if _known_missing(missing_key):
        raise FileNotFoundError('Referenced image could not be resolved uniquely')

    # only look inside this machine's processed folder
    machine_prefix = f"{cfg.PROCESSED_PREFIX}/{machine}/" if machine else f"{cfg.PROCESSED_PREFIX}/"

    assets_prefix = f"{machine_prefix}{stem}/assets/"

    # a gs:// link: check it and read it
    if parsed.scheme == "gs":
        object_name = parsed.path.lstrip("/")

        # reject images outside this manual's assets folder
        if (
            parsed.netloc != cfg.BUCKET
            or not object_name.startswith(assets_prefix)
        ):
            raise ValueError(
                "Image reference is outside the approved manual assets folder"
            )

        try:
            return blob_bytes(object_name)
        except NotFound as error:
            _remember_missing(missing_key)
            raise FileNotFoundError('Referenced manual image is missing') from error

    # reject links to outside websites
    if parsed.scheme and len(parsed.scheme) > 1:
        raise ValueError(
            "External image references are not permitted"
        )

    # check the image file name
    basename = PurePosixPath(value).name

    if not basename or basename in {".", ".."}:
        raise ValueError("Invalid image reference")

    # Relative paths from document_images may be stored as assets/... or as a
    # filename. Normalize both forms into this manual's assets/ prefix only.
    if value.startswith("/") or ".." in value.split("/"):
        raise ValueError("Invalid image path")
    relative_path = value.lstrip("/")
    if relative_path.startswith("assets/"):
        object_name = f"{machine_prefix}{stem}/{relative_path}"
    else:
        object_name = f"{assets_prefix}{basename}"
    if not object_name.startswith(assets_prefix):
        raise ValueError("Image reference is outside the approved manual assets folder")
    try:
        return blob_bytes(object_name)
    except NotFound:
        pass

    # report an error if no single image was found
    _remember_missing(missing_key)
    raise FileNotFoundError(
        "Referenced image could not be resolved uniquely"
    )


# reads a manual pdf. checks the local data folder first, then cloud storage.
# results are cached
@timed('pdf_read')
@lru_cache(maxsize=12)
@timed('pdf_cache_miss')
def read_pdf(document, machine=""):
    stem = doc_stem(document)

    # check the local raw manuals folder first
    local_path = (
        cfg.PROJECT_ROOT
        / "data"
        / "raw"
        / machine
        / f"{stem}.pdf"
    )

    if local_path.is_file():
        # check the pdf size before reading it
        if local_path.stat().st_size > MAX_SOURCE_BYTES:
            raise ValueError(
                "Source PDF exceeds the size budget"
            )

        return local_path.read_bytes()

    # not found locally, so read it from cloud storage
    return blob_bytes(
        f"{cfg.RAW_PREFIX}/{machine}/{stem}.pdf"
    )


# turns a page of the original pdf into a png image.
# if an area is given, it cuts only that area
@timed('pdf_render')
def render_page(
    document,
    page_number,
    geometry=None,
    machine="",
    reject_stitched_page=False,
):
    with pymupdf.open(
        stream=read_pdf(document, machine),
        filetype="pdf",
    ) as pdf:
        # make sure the page number is valid
        if not 1 <= page_number <= len(pdf):
            raise ValueError(
                "Page number is outside the original PDF"
            )

        page = pdf[page_number - 1]
        clip = None

        # use the given area only if it is big enough and inside the page
        if geometry:
            box = geometry[0]

            proposed = (
                pymupdf.Rect(
                    box["x0"],
                    box["y0"],
                    box["x1"],
                    box["y1"],
                )
                & page.rect
            )

            if (
                proposed.width > 10
                and proposed.height > 10
            ):
                clip = proposed

        # Some ingested manuals are stored as one very tall continuous PDF
        # page. Rendering that whole page produces an unreadable contact-sheet
        # strip in the UI. Callers that are looking for a relevant fallback
        # figure can require a valid evidence crop instead.
        if (
            reject_stitched_page
            and clip is None
            and page.rect.height > page.rect.width * 4
        ):
            raise ValueError(
                "A stitched manual page needs evidence geometry before rendering"
            )

        # draw the page (or the area) as a png
        pixmap = page.get_pixmap(
            matrix=pymupdf.Matrix(1.5, 1.5),
            clip=clip,
            alpha=False,
        )

        return pixmap.tobytes("png")


# makes an image smaller if it is big, and saves it as a png
@timed('image_normalization')
def normalize_image(data, max_side=1800):
    """makes the real image smaller. it does not make a new image."""

    with Image.open(io.BytesIO(data)) as image:
        # reject images with too many pixels
        if image.width * image.height > 40_000_000:
            raise ValueError("Image has too many pixels")

        # change to rgb and make it fit the size limit
        image = image.convert("RGB")
        image.thumbnail((max_side, max_side))

        # save the smaller image as png data
        output = io.BytesIO()
        image.save(output, format="PNG")

        return output.getvalue()
