import json

from app.config import (
    BUCKET,
    MACHINE,
    PROCESSED_PREFIX,
)
from app.media.prepare_media import prepare_media
from app.media.manual_images import storage_client
from app.retrieval.manual_search import (
    check_tables,
    search_manual,
)


def main():
    print("Checking the eight BigQuery schemas...")

    print(
        json.dumps(
            check_tables(),
            indent=2,
        )
    )

    print("Checking the configured bucket...")

    # Uses object listing permission, not bucket-admin permission.
    list(
        storage_client().list_blobs(
            BUCKET,
            prefix=f"{PROCESSED_PREFIX}/{MACHINE}/",
            max_results=1,
            timeout=15,
        )
    )

    print("Bucket accessible:", BUCKET)

    print("Retrieving control-panel evidence and media...")

    bundle = search_manual(
        "HSG-2550 control panel switches diagram",
        wants_visual=True,
        wants_video=True,
    )

    media = prepare_media(
        bundle,
        wants_visual=True,
        wants_video=True,
    )

    print(
        json.dumps(
            {
                "evidence_chunks": len(bundle["chunks"]),
                "database_image_references": len(
                    bundle["images"]
                ),
                "loaded_original_assets": len(
                    media["images"]
                ),
                "links": media["links"],
                "warnings": media["warnings"],
            },
            indent=2,
        )
    )

    if not bundle["chunks"]:
        raise SystemExit(
            "No compatible embedded chunks found. "
            "Verify the machine, embedding model "
            "and dimensions."
        )

    if not media["images"]:
        raise SystemExit(
            "Text retrieval works, but no original image loaded. "
            "Check bucket paths, permissions and PDF availability."
        )

    print(
        "Read-only connection and media check passed."
    )


if __name__ == "__main__":
    main()
