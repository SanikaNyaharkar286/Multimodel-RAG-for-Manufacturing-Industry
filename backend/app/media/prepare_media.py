import hashlib
from concurrent.futures import ThreadPoolExecutor
import requests
from app.media.manual_images import read_image, render_page, normalize_image
from app.media.video_links import safe_url, youtube_url, manual_video_targets


def prepare_media(
    bundle,
    wants_visual=False,
    wants_video=False,
):
    assets = []
    warnings = list(bundle.get("warnings", []))

    def load_image(record):
        try:
            data = read_image(
                record["image_path"], record["document_name"],
                record.get("machine_name", ""),
            )
        except FileNotFoundError:
            data = render_page(
                record["document_name"], record["page_number"],
                record.get("geometry"), record.get("machine_name", ""),
            )
        return {
            "bytes": normalize_image(data),
            "chunk_id": record["chunk_id"],
            "document": record["document_name"],
            "page": record["page_number"],
            "kind": "original_manual_image",
        }

    records = bundle["images"]
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = [pool.submit(load_image, record) for record in records]
        for future in pending:
            try:
                assets.append({"id": f"image-{len(assets) + 1}", **future.result()})
            except Exception as error:
                warnings.append(
                    "An original image could not be loaded "
                    f"({type(error).__name__})."
                )

    # Render selected original pages for diagrams.
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

        for document, number in pages:
            source = next(
                row
                for row in bundle["chunks"]
                if row["document_name"] == document
                and row["page_number"] == number
            )

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

            except Exception as error:
                warnings.append(
                    "A selected PDF page could not be rendered "
                    f"({type(error).__name__})."
                )

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

    # Resolve selected manual-linked video pages concurrently.
    records = list(links.values())
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = [pool.submit(manual_video_targets, record["url"]) for record in records]
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

    return {
        "images": assets,
        "links": result_links,
        "warnings": list(dict.fromkeys(warnings)),
    }
