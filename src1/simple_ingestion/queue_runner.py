"""
Sequential local -> GCS raw -> processed artifacts -> BigQuery queue.

Edit MACHINE_FOLDERS to point to local folders containing machine PDFs.
Each PDF is uploaded with gcs_uploader.py, then main.py processes that exact
GCS raw URI through embeddings, processed-artifact upload, and BigQuery commit.
The queue advances only after both child commands return success.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from collections import deque
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
UPLOADER_SCRIPT = PROJECT_ROOT / "gcs_uploader.py"
PIPELINE_SCRIPT = PROJECT_ROOT / "main.py"

# EDIT THIS LIST: local folder per machine. The script discovers *.pdf in each
# folder (non-recursive) and queues every PDF found there.
MACHINE_FOLDERS = [
    {"machine": "HAB-3232", "folder": r"C:\Users\Sonakshi\OneDrive\Desktop\main\Multimodel-RAG-for-Manufacturing-Industry\data\raw\HAB-3232"},
    {"machine": "HSG-2550", "folder": r"C:\Users\Sonakshi\OneDrive\Desktop\main\Multimodel-RAG-for-Manufacturing-Industry\data\raw\HSG-2550"},
]

# Bucket is prefilled for this project; environment variable can override it.
DEFAULT_BUCKET = "market-lens-506611-manufacturing-rag"
DELAY_BETWEEN_FILES_SECONDS = 10
RAW_PREFIX = os.getenv("GCS_RAW_PREFIX", "raw").strip("/")


def discover_local_pdfs() -> deque[dict[str, str]]:
    queue: deque[dict[str, str]] = deque()
    for config in MACHINE_FOLDERS:
        machine = str(config["machine"]).strip()
        folder = Path(config["folder"])
        if not machine or "/" in machine or "\\" in machine:
            raise ValueError(f"Invalid machine name: {machine!r}")
        if not folder.is_absolute():
            folder = PROJECT_ROOT / folder
        if not folder.is_dir():
            raise FileNotFoundError(
                f"Machine folder not found: {folder}. Update MACHINE_FOLDERS in queue_runner.py."
            )
        for pdf in sorted(folder.glob("*.pdf"), key=lambda p: p.name.casefold()):
            queue.append({"machine": machine, "file": str(pdf.resolve())})
    return queue


def run_command(command: list[str], description: str) -> None:
    print(f"\n{'=' * 78}\n{description}\n{'=' * 78}", flush=True)
    print("Command:", subprocess.list2cmdline(command), flush=True)
    result = subprocess.run(command, cwd=PROJECT_ROOT, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"{description} failed with exit code {result.returncode}; queue stopped."
        )


def raw_uri(bucket: str, machine: str, filename: str) -> str:
    # GCS object names contain literal spaces. Do not URL-encode gs:// URIs:
    # google-cloud-storage expects the literal object name and handles HTTP
    # URL encoding internally.
    return f"gs://{bucket}/{RAW_PREFIX}/{machine}/{filename}"


def main() -> int:
    bucket = os.getenv("GCS_BUCKET_NAME", DEFAULT_BUCKET).strip()
    if not bucket:
        print("[ERROR] GCS_BUCKET_NAME is empty.", file=sys.stderr)
        return 2

    for script in (UPLOADER_SCRIPT, PIPELINE_SCRIPT):
        if not script.is_file():
            print(f"[ERROR] Required script not found: {script}", file=sys.stderr)
            return 2

    try:
        queue = discover_local_pdfs()
    except (OSError, ValueError) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 2

    if not queue:
        print(
            "No local PDFs found. Check MACHINE_FOLDERS at the top of queue_runner.py."
        )
        return 0

    # Validate all entries before uploading the first file.
    print(f"Queued {len(queue)} local PDF(s):", flush=True)
    for i, item in enumerate(queue, start=1):
        print(f"  {i}. {item['machine']}: {item['file']}", flush=True)

    print(
        "\nFor each PDF: local -> GCS raw -> process/embeddings -> GCS processed "
        "-> BigQuery commit. Queue stops at the first error.",
        flush=True,
    )

    completed = 0
    total = len(queue)
    while queue:
        item = queue[0]  # Peek; do not pop until both commands succeed.
        pdf = Path(item["file"])
        machine = item["machine"]
        uri = raw_uri(bucket, machine, pdf.name)
        index = completed + 1

        print(f"\n[{index}/{total}] Starting {pdf.name} for machine {machine}", flush=True)
        try:
            # Command 1: local PDF -> raw/<machine>/<filename>.pdf in GCS.
            run_command(
                [
                    sys.executable, str(UPLOADER_SCRIPT),
                    "--file", str(pdf),
                    "--machine", machine,
                ],
                f"[{index}/{total}] Command 1: upload local PDF to GCS raw",
            )

            # Command 2: raw GCS URI -> process -> upload processed -> BigQuery commit.
            run_command(
                [
                    sys.executable, str(PIPELINE_SCRIPT),
                    "--gcs-uri", uri,
                    "--load-to-bigquery",
                    "--commit-bigquery",
                    "--tables", "all",
                ],
                f"[{index}/{total}] Command 2: process raw PDF through BigQuery",
            )

        except (RuntimeError, OSError) as exc:
            print(f"\n[STOPPED] {exc}", file=sys.stderr, flush=True)
            print(
                f"Current queue item was NOT popped: {pdf}\n"
                "Fix the issue before rerunning. If raw upload succeeded but "
                "processing failed, the create-only uploader may reject re-upload "
                "of that same raw object. Resume/retry that item deliberately; "
                "do not enable overwrite blindly.",
                file=sys.stderr,
                flush=True,
            )
            return 1

        queue.popleft()
        completed += 1
        print(
            f"[SUCCESS] {pdf.name} fully completed. Queue remaining: {len(queue)}",
            flush=True,
        )

        if queue:
            print(f"Waiting {DELAY_BETWEEN_FILES_SECONDS} seconds...", flush=True)
            time.sleep(DELAY_BETWEEN_FILES_SECONDS)

    print(f"\n[COMPLETE] All {completed} PDF(s) completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
