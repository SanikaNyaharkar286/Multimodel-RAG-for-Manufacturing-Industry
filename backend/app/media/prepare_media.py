# loads the manual images for an answer and prepares the related video and qr links.

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
import requests
from app import config as cfg
from app.media.video_links import safe_url, youtube_url, manual_video_targets
from app.media.manual_assets import manual_reference, load_manual_reference, _MEMORY as _IMAGE_CACHE
from app.observability.timing import increment, submit_with_context, timed

# loads the images and links for the search results.
# it gives back the images, the links and any warnings
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

    # loads one image and makes it smaller. reuses it from the cache if we already have it
    @timed('manual_image_load')
    def prepare_image(record):
        reference = manual_reference(record['image_path'], record['document_name'], record.get('machine_name', ''))
        if cfg.MANUAL_IMAGE_TRANSPORT == 'gcs':
            if reference['mime_type'] not in {'image/png', 'image/jpeg', 'image/webp'}:
                raise ValueError('Original manual image type is unsupported by reference transport')
            return {'reference': reference}
        return {'bytes': load_manual_reference(reference)}

    # many text parts can point to the same image, so we load each image only once.
    # we keep every figure that was selected by the search.
    # text-only answers skip the cloud storage and pdf calls.
    # if nobody says otherwise (like in a direct script call), images are loaded
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
                pending[key] = submit_with_context(pool, prepare_image, record)
            else:
                increment('cache_hits')
        for record, key in zip(records, keys):
            try:
                assets.append({
                    "id": f"image-{len(assets) + 1}",
                    **pending[key].result(),
                    "chunk_id": record["chunk_id"],
                    "source_chunk_ids": list(record.get("source_chunk_ids", [record["chunk_id"]])),
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

    # the original manual links are always kept.
    # we only open the linked websites to look for more videos if the user asked for video
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
