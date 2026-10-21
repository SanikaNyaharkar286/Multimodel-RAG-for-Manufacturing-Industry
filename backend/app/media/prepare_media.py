# this file loads manual images and prepares related video and qr links

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
import requests
from app.media.manual_images import read_image, render_page, normalize_image
from app.media.video_links import safe_url, youtube_url, manual_video_targets
from app.observability.timing import increment, submit_with_context, timed

_IMAGE_CACHE = {}
_IMAGE_CACHE_LOCK = threading.Lock()
_IMAGE_CACHE_MAX = 64


# loads images and links related to the search results
def prepare_media(
    bundle,
    wants_visual=False,
    wants_video=False,
):
    # collect prepared images and any loading warnings
    assets = []
    warnings = list(bundle.get("warnings", []))

    # load and normalize image bytes once for each distinct source in this answer
    @timed('manual_image_load')
    def load_image_bytes(record):
        cache_key = (record["image_path"], record["document_name"],
                     record.get("machine_name", ""), record["page_number"],
                     json.dumps(record.get("geometry", []), sort_keys=True, default=str))
        with _IMAGE_CACHE_LOCK:
            cached = _IMAGE_CACHE.get(cache_key)
        if cached is not None:
            increment('media_cache_hits')
            return cached
        try:
            # Image references point to separately stored manual assets.
            # Load that asset directly; render the PDF page only when the
            # referenced asset is actually missing.
            data = read_image(
                record["image_path"], record["document_name"],
                record.get("machine_name", ""),
            )
        except FileNotFoundError:
            data = render_page(
                record["document_name"], record["page_number"],
                record.get("geometry"), record.get("machine_name", ""),
            )
        result = normalize_image(data)
        with _IMAGE_CACHE_LOCK:
            if len(_IMAGE_CACHE) >= _IMAGE_CACHE_MAX:
                _IMAGE_CACHE.pop(next(iter(_IMAGE_CACHE)))
            _IMAGE_CACHE[cache_key] = result
        return result

    # Multiple evidence chunks can point at the same original image. Share one
    # future so concurrent workers do not download and normalize it repeatedly.
    # Retrieval may return many image references. Load only images attached to
    # the highest-ranked evidence that can reach the answer model; loading the
    # complete bundle caused avoidable GCS latency before payload capping.
    has_ranked_chunks = bool(bundle["chunks"])
    allowed_chunks = {row["chunk_id"] for row in bundle["chunks"][:8]}
    if not has_ranked_chunks:
        # Keep the small unit-test/helper bundle behavior unchanged.
        records = bundle["images"]
    else:
        records = []
        seen_sources = set()
        for record in bundle["images"]:
            key = (record["image_path"], record["document_name"], record.get("page_number"))
            if record["chunk_id"] not in allowed_chunks or key in seen_sources:
                continue
            seen_sources.add(key)
            records.append(record)
            if len(records) >= 6:
                break
    with ThreadPoolExecutor(max_workers=6) as pool:
        pending = {}
        keys = []
        for record in records:
            key = (
                record["image_path"], record["document_name"],
                record.get("machine_name", ""), record["page_number"],
                json.dumps(record.get("geometry", []), sort_keys=True, default=str),
            )
            keys.append(key)
            if key not in pending:
                pending[key] = submit_with_context(pool, load_image_bytes, record)
            else:
                increment('cache_hits')
        for record, key in zip(records, keys):
            try:
                assets.append({
                    "id": f"image-{len(assets) + 1}",
                    "bytes": pending[key].result(),
                    "chunk_id": record["chunk_id"],
                    "document": record["document_name"],
                    "page": record["page_number"],
                    "kind": "original_manual_image",
                })
            except Exception as error:
                warnings.append(
                    "An original image could not be loaded "
                    f"({type(error).__name__})."
                )

    # render selected original pages when a visual answer is needed
    if wants_visual:
        pages = list(
            dict.fromkeys(
                (
                    row["document_name"],
                    row["page_number"],
                )
                for row in bundle["chunks"]
            )
        )[:2]

        # find each page's source information
        for document, number in pages:
            source = next(
                row
                for row in bundle["chunks"]
                if row["document_name"] == document
                and row["page_number"] == number
            )

            # render the page and add it as an image
            try:
                data = render_page(
                    document, number, machine=source.get("machine_name", "")
                )

                assets.append(
                    {
                        "id": f"image-{len(assets) + 1}",
                        "bytes": normalize_image(data),
                        "chunk_id": source["chunk_id"],
                        "document": document,
                        "page": number,
                        "kind": "original_manual_page",
                    }
                )

            # record a warning if the page could not be rendered
            except Exception as error:
                warnings.append(
                    "A selected PDF page could not be rendered "
                    f"({type(error).__name__})."
                )

    # keep valid links and remove duplicates
    links = {}

    for record in bundle["links"]:
        url = record["url"]

        if not safe_url(url):
            continue

        links.setdefault(
            url,
            {
                **record,
                "origin": record.get("origin", "pdf_link"),
            },
        )

    # The original manual links are always returned. Inspect linked websites
    # for extra videos only when the user asked for video material.
    if wants_video:
        records = list(links.values())
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = [submit_with_context(pool, manual_video_targets, record["url"]) for record in records]
            for record, future in zip(records, pending):
                try:
                    for target in future.result():
                        links.setdefault(target, {
                            **record,
                            "url": target,
                            "origin": "manual_linked_video",
                        })
                except (requests.RequestException, ValueError):
                    warnings.append(
                        "A manual-linked video page could not be "
                        "checked; its original link is still available."
                    )

    # prepare the final link details
    result_links = []

    for url, record in links.items():
        identifier = hashlib.sha256(
            url.encode()
        ).hexdigest()[:16]

        result_links.append(
            {
                **record,
                "id": f"link-{identifier}",
                "is_video": youtube_url(url),
            }
        )

    # return the images, links, and unique warnings
    return {
        "images": assets,
        "links": result_links,
        "warnings": list(dict.fromkeys(warnings)),
    }
