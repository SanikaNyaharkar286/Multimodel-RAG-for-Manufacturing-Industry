
import json
from pathlib import Path
import uuid
from config import PROCESSED_DATA_DIR
from chunk_builder import build_all_chunks, save_chunks


# Change this to the exact machine folder name printed in Step 1.
MACHINE_NAME = "HAB"

machine_dir = PROCESSED_DATA_DIR / MACHINE_NAME

if not machine_dir.is_dir():
    raise FileNotFoundError(f"Machine directory not found: {machine_dir}")

extraction_files = sorted(machine_dir.rglob("extraction.json"))

if not extraction_files:
    raise FileNotFoundError(
        f"No extraction.json files found under {machine_dir}"
    )

print(f"Machine: {MACHINE_NAME}")
print(f"Extraction files found: {len(extraction_files)}")

for extraction_path in extraction_files:
    document_dir = extraction_path.parent
    relationships_path = document_dir / "relationships.json"
    chunks_path = document_dir / "chunks.json"

    if not relationships_path.is_file():
        raise FileNotFoundError(
            f"Missing relationships file: {relationships_path}"
        )

    with extraction_path.open("r", encoding="utf-8") as f:
        extracted = json.load(f)

    with relationships_path.open("r", encoding="utf-8") as f:
        relationships = json.load(f)

    document_name = extracted.get("document", document_dir.name)

    chunks = build_all_chunks(
        extracted,
        MACHINE_NAME,
        document_name,
        relationships_data=relationships,
    )

    # Validate UUIDs before replacing the existing file.
    missing_uuid = [
        chunk.get("chunk_id", "<missing chunk_id>")
        for chunk in chunks
        if not chunk.get("chunk_uuid")
    ]

    uuids = [chunk["chunk_uuid"] for chunk in chunks if chunk.get("chunk_uuid")]
    duplicate_uuids = len(uuids) != len(set(uuids))
    for chunk_uuid in uuids:
        uuid.UUID(chunk_uuid)

    if missing_uuid:
        raise ValueError(
            f"{document_dir}: {len(missing_uuid)} chunks lack chunk_uuid"
        )

    if duplicate_uuids:
        raise ValueError(f"Duplicate chunk_uuid values in {document_dir}")

    # Save only after the UUID checks pass.
    save_chunks(chunks, chunks_path)

    print(
        f"Saved {len(chunks)} chunks | "
        f"{len(uuids)} UUIDs validated | {chunks_path}"
    )

print("Chunk regeneration completed.")
