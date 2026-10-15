"""Read actual Document AI QR results. Processor support must be tested."""

import argparse
import json
import hashlib
import io
import re
from pathlib import Path
from urllib.parse import urlsplit

from google.api_core.client_options import ClientOptions
from google.cloud import documentai
from PIL import Image
from app import config as cfg


def valid_url(value):
    value = value.strip()

    try:
        parsed = urlsplit(value)

        if (
            parsed.scheme in ("http", "https")
            and parsed.hostname
            and not parsed.username
            and not parsed.password
        ):
            return value

    except ValueError:
        pass

    return None


def read_qr_results(document, machine_name, document_name, original_page):
    found = {}

    for page in document.pages:
        for detected in page.detected_barcodes:
            barcode = detected.barcode

            if barcode.format != "QR_CODE":
                continue

            raw = barcode.raw_value.strip()
            candidates = [raw]

            if raw.startswith("MEBKM:"):
                candidates = re.findall(
                    r"URL:(https?://[^;]+)",
                    raw,
                )

            for candidate in candidates:
                url = valid_url(candidate)

                if not url:
                    continue

                identity = (
                    f"{machine_name}\n{document_name}\n"
                    f"{original_page}\n{url}"
                )

                chunk_id = (
                    "qr_"
                    + hashlib.sha256(identity.encode()).hexdigest()
                )

                found[chunk_id] = {
                    "chunk_id": chunk_id,
                    "machine_name": machine_name,
                    "document_name": document_name,
                    "page_number": original_page,
                    "content_type": "link",
                    "source_id": chunk_id,
                    "content": url,
                    "url": url,
                    "origin": "document_ai_qr",
                    "relationship_status": "page_candidate",
                }

    return list(found.values())


def decode_manual_page(file_path, machine_name, document_name, page=1):
    if page < 1:
        raise ValueError("Page numbers start at 1")

    path = Path(file_path)

    mime_types = {
        ".pdf": "application/pdf",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
    }

    mime = mime_types.get(path.suffix.lower())

    if not mime:
        raise ValueError("Use a PDF, PNG, JPEG or WebP file")

    return decode_bytes(
        path.read_bytes(),
        mime,
        machine_name,
        document_name,
        page,
    )


def decode_bytes(data, mime, machine_name, document_name, page=1):
    if page < 1:
        raise ValueError("Page numbers start at 1")

    if mime not in (
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/webp",
    ):
        raise ValueError("Unsupported file type")

    if mime == "image/webp":
        with Image.open(io.BytesIO(data)) as image:
            output = io.BytesIO()
            image.convert("RGB").save(output, format="PNG")
            data, mime = output.getvalue(), "image/png"

    project = cfg.DOCUMENT_AI_PROJECT
    location = cfg.DOCUMENT_AI_LOCATION
    processor = cfg.DOCUMENT_AI_PROCESSOR_ID
    version = cfg.DOCUMENT_AI_PROCESSOR_VERSION

    if not processor or processor == "PASTE_REAL_PROCESSOR_ID":
        raise ValueError(
            "Set DOCUMENT_AI_PROCESSOR_ID to the actual processor ID"
        )

    name = (
        f"projects/{project}/locations/{location}"
        f"/processors/{processor}"
    )

    if version:
        name += f"/processorVersions/{version}"

    client = documentai.DocumentProcessorServiceClient(
        client_options=ClientOptions(
            api_endpoint=f"{location}-documentai.googleapis.com"
        )
    )

    options = documentai.ProcessOptions()

    if mime == "application/pdf":
        options.individual_page_selector.pages = [page]

    try:
        request = documentai.ProcessRequest(
            name=name,
            raw_document=documentai.RawDocument(
                content=data,
                mime_type=mime,
            ),
            process_options=options,
        )

        response = client.process_document(
            request=request,
            timeout=120,
            retry=None,
        )

        records = read_qr_results(
            response.document,
            machine_name,
            document_name,
            page,
        )

        return {
            "processor": name,
            "status": (
                "decoded" if records else "no_qr_url_decoded"
            ),
            "detected_barcode_count": sum(
                len(item.detected_barcodes)
                for item in response.document.pages
            ),
            "records": records,
        }

    finally:
        client.transport.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    parser.add_argument("file")
    parser.add_argument("--machine", required=True)
    parser.add_argument("--document", required=True)
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument(
        "--output",
        default="qr_test_result.json",
    )

    args = parser.parse_args()

    result = decode_manual_page(
        args.file,
        args.machine,
        args.document,
        args.page,
    )

    output = json.dumps(
        result,
        indent=2,
        ensure_ascii=False,
    )

    Path(args.output).write_text(output, encoding="utf-8")
    print(output)

    if not result["records"]:
        print(
            "No QR URL decoded by this processor/version. "
            "This does not prove that the page has no QR code."
        )
        raise SystemExit(2)
