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
# Normalized manual figures are immutable between ingestion runs. Retaining a
# larger working set avoids repeating GCS downloads during normal use without
# changing which figures are returned to the answer or report.
_IMAGE_CACHE_MAX = 256


# loads images and links related to the search results
def prepare_media(
    bundle,
    wants_visual=False,
    wants_video=False,
    include_images=None,
):
    # collect prepared images and any loading warnings
    assets = []
    warnings = list(bundle.get("warnings", []))
    print(
        f"[MEDIA DEBUG] wants_visual={wants_visual}, "
        f"wants_video={wants_video}, "
        f"chunks={len(bundle.get('chunks', []))}, "
        f"images={len(bundle.get('images', []))}, "
        f"links={len(bundle.get('links', []))}"
    )

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
    # The retrieval bundle already selects relevant figure-bearing rows. Keep
    # every selected figure, including figures whose source is after chunk 8.
    # Text-only answers do not need Cloud Storage or PDF media calls.
    # Keep direct utility/script calls backward compatible; the chat pipeline
    # passes an explicit flag so normal text answers can skip media entirely.
    if include_images is None:
        include_images = True
    records = bundle["images"] if include_images else []
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
                    "machine_name": record.get("machine_name", ""),
                    "page": record["page_number"],
                    "kind": "original_manual_image",
                })
            except Exception as error:
                warnings.append(
                    "An original image could not be loaded "
                    f"({type(error).__name__})."
                )

    # Render a selected page only when there is no usable original figure for
    # it. Previously every visual request added two full PDF pages even when
    # the exact figures had already been downloaded, duplicating model input.
    if wants_visual:
        covered_pages = {
            (asset.get("machine_name", ""), asset["document"], asset["page"])
            for asset in assets
        }
        pages = list(
            dict.fromkeys(
                (
                    row.get("machine_name", ""),
                    row["document_name"],
                    row["page_number"],
                )
                for row in bundle["chunks"]
            )
        )[:2]

        page_sources = []
        for machine, document, number in pages:
            if (machine, document, number) in covered_pages:
                continue
            source = next(
                row
                for row in bundle["chunks"]
                if row["document_name"] == document
                and row["page_number"] == number
                and row.get("machine_name", "") == machine
            )

            page_sources.append(source)

        def load_page(source):
            cache_key = ("page", source["document_name"], source["page_number"],
                         source.get("machine_name", ""))
            with _IMAGE_CACHE_LOCK:
                cached = _IMAGE_CACHE.get(cache_key)
            if cached is not None:
                increment('media_cache_hits')
                return cached
            result = normalize_image(render_page(
                source["document_name"], source["page_number"],
                machine=source.get("machine_name", ""),
            ))
            with _IMAGE_CACHE_LOCK:
                if len(_IMAGE_CACHE) >= _IMAGE_CACHE_MAX:
                    _IMAGE_CACHE.pop(next(iter(_IMAGE_CACHE)))
                _IMAGE_CACHE[cache_key] = result
            return result

        with ThreadPoolExecutor(max_workers=2) as pool:
            pending_pages = [submit_with_context(pool, load_page, source)
                             for source in page_sources]
            for source, future in zip(page_sources, pending_pages):
                try:
                    assets.append({
                        "id": f"image-{len(assets) + 1}",
                        "bytes": future.result(),
                        "chunk_id": source["chunk_id"],
                        "document": source["document_name"],
                        "machine_name": source.get("machine_name", ""),
                        "page": source["page_number"],
                        "kind": "original_manual_page",
                    })
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
