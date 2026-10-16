# this file reads, renders, and resizes images from equipment manuals

import io
from functools import lru_cache
from pathlib import PurePosixPath
from urllib.parse import urlparse
import pymupdf
from google.api_core.exceptions import NotFound
from google.cloud import storage
from PIL import Image
from app import config as cfg

# limit the size of source files read by this file
MAX_SOURCE_BYTES = 25 * 1024 * 1024


# creates and reuses the cloud storage client
@lru_cache(maxsize=1)
def storage_client():
    return storage.Client(project=cfg.PROJECT)


# checks and cleans the manual's file name
def doc_stem(document):
    name = str(document).replace("\\", "/")

    if "/" in name or name in {"", ".", ".."}:
        raise ValueError("Invalid source document name")

    if name.lower().endswith(".pdf"):
        return name[:-4]

    return name


# downloads a file from the approved cloud storage bucket
def blob_bytes(name):
    blob = (
        storage_client()
        .bucket(cfg.BUCKET)
        .blob(name)
    )

    # check the file details before downloading
    blob.reload(timeout=10)

    # reject missing, empty, or oversized files
    if not blob.size or blob.size > MAX_SOURCE_BYTES:
        raise ValueError(
            "Manual asset exceeds the configured size budget"
        )

    return blob.download_as_bytes(timeout=15)


# finds and reads an image referenced by the database
@lru_cache(maxsize=128)
def read_image(path, document, machine=""):
    """
    Resolve a database-referenced image in the approved bucket.

    Handles gs:// references and the relative asset paths produced
    by the uploaded ingestion pipeline.
    """

    # prepare the document name and image path
    stem = doc_stem(document)
    value = path.replace("\\", "/")
    parsed = urlparse(value)

    # limit image lookups to the machine's processed folder
    machine_prefix = f"{cfg.PROCESSED_PREFIX}/{machine}/" if machine else f"{cfg.PROCESSED_PREFIX}/"

    # check and read a cloud storage image reference
    if parsed.scheme == "gs":
        object_name = parsed.path.lstrip("/")

        # reject images outside the approved bucket folder
        if (
            parsed.netloc != cfg.BUCKET
            or not object_name.startswith(machine_prefix)
        ):
            raise ValueError(
                "Image reference is outside the approved prefix"
            )

        return blob_bytes(object_name)

    # reject references to external locations
    if parsed.scheme and len(parsed.scheme) > 1:
        raise ValueError(
            "External image references are not permitted"
        )

    # check the image file name
    basename = PurePosixPath(value).name

    if not basename or basename in {".", ".."}:
        raise ValueError("Invalid image reference")

    # keep safe relative paths; otherwise use only the file name
    if (
        not parsed.scheme
        and not value.startswith("/")
        and ".." not in value.split("/")
    ):
        relative_path = value
    else:
        relative_path = basename

    # prepare possible locations for the image
    candidates = []

    if relative_path.startswith(machine_prefix):
        candidates.append(relative_path)

    # check common folders used for manual images
    for folder in (stem, str(document)):
        prefix = f"{machine_prefix}{folder}/"

        candidates.extend(
            [
                prefix + relative_path,
                prefix + "assets/images/" + basename,
                prefix + "images/" + basename,
            ]
        )

    # try each possible image location
    for candidate in dict.fromkeys(candidates):
        try:
            return blob_bytes(candidate)
        except NotFound:
            continue

    # look for the image by name inside the machine's folder
    matches = []

    for blob in storage_client().list_blobs(
        cfg.BUCKET,
        prefix=machine_prefix,
        max_results=400,
    ):
        remainder = blob.name[len(machine_prefix):]
        folder = remainder.split("/", 1)[0]

        if (
            doc_stem(folder).lower() == stem.lower()
            and PurePosixPath(blob.name).name == basename
        ):
            matches.append(blob.name)

    # use the image if exactly one match was found
    if len(matches) == 1:
        return blob_bytes(matches[0])

    # report an error if no unique image was found
    raise FileNotFoundError(
        "Referenced image could not be resolved uniquely"
    )


# reads a manual pdf from local storage or cloud storage
@lru_cache(maxsize=12)
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

    # read the pdf from cloud storage if it is not local
    return blob_bytes(
        f"{cfg.RAW_PREFIX}/{machine}/{stem}.pdf"
    )


# turns a page from the original pdf into a png image
def render_page(document, page_number, geometry=None, machine=""):
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

        # use the selected area if valid image coordinates were provided
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

        # render the page or selected area as a png
        pixmap = page.get_pixmap(
            matrix=pymupdf.Matrix(1.5, 1.5),
            clip=clip,
            alpha=False,
        )

        return pixmap.tobytes("png")


# resizes an image and saves it as a png
def normalize_image(data, max_side=1800):
    """Resize actual image pixels; do not generate an image."""

    with Image.open(io.BytesIO(data)) as image:
        # reject images with too many pixels
        if image.width * image.height > 40_000_000:
            raise ValueError("Image has too many pixels")

        # convert the image and limit its size
        image = image.convert("RGB")
        image.thumbnail((max_side, max_side))

        # save the resized image as png data
        output = io.BytesIO()
        image.save(output, format="PNG")

        return output.getvalue()