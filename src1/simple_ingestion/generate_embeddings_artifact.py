"""Generate embedded_chunks.json from existing chunks.json without reprocessing the PDF.

Only paragraph and table_row chunks are sent to Gemini Embeddings. All chunk
records and relationship/asset metadata are retained in the output artifact.
No GCS upload or BigQuery write is performed by this script.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from embedding_generator import (
    DEFAULT_DIMENSIONS,
    DEFAULT_MODEL,
    generate_embeddings,
    enrich_chunks,
    normalize_record,
)


EMBEDDABLE_TYPES = {"paragraph", "table_row"}


def load_json(path: Path):
    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def extract_chunks(raw):
    if isinstance(raw, list):
        chunks = raw
    elif isinstance(raw, dict):
        chunks = raw.get("chunks", raw.get("items", raw.get("records")))
    else:
        chunks = None
    if not isinstance(chunks, list) or not all(isinstance(item, dict) for item in chunks):
        raise ValueError(
            "chunks.json must be a list or an object containing a 'chunks', "
            "'items', or 'records' list."
        )
    return chunks


def generate_artifact(
    chunks_path: Path,
    relationships_path: Path,
    output_path: Path,
    *,
    batch_size: int = 16,
):
    if not chunks_path.is_file():
        raise FileNotFoundError(f"Chunks file not found: {chunks_path}")
    if not relationships_path.is_file():
        raise FileNotFoundError(f"Relationships file not found: {relationships_path}")

    chunks = extract_chunks(load_json(chunks_path))
    relationships = load_json(relationships_path)
    chunks = enrich_chunks(chunks, relationships)

    ids = [str(chunk.get("chunk_id", "")).strip() for chunk in chunks]
    if any(not chunk_id for chunk_id in ids):
        raise ValueError("At least one chunk has a missing chunk_id.")
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate chunk_id values found; refusing to generate vectors.")

    records = [
        normalize_record(chunk, DEFAULT_MODEL, DEFAULT_DIMENSIONS)
        for chunk in chunks
    ]

    eligible = []
    for record in records:
        metadata = record.get("metadata") or {}
        content_type = str(
            metadata.get("content_type")
            or metadata.get("element_type")
            or record.get("content_type")
            or "unknown"
        ).strip().lower()

        metadata["content_type"] = content_type
        record["metadata"] = metadata
        record["content_type"] = content_type

        if content_type in EMBEDDABLE_TYPES:
            if not str(record.get("content", "")).strip():
                raise ValueError(
                    f"Embeddable chunk {record['chunk_id']} has empty content."
                )
            eligible.append(record)
        else:
            # Do not call the API for images, links, headings, or other non-target types.
            record["embedding"] = []

    if not eligible:
        type_counts = {}
        for record in records:
            kind = record.get("content_type", "unknown")
            type_counts[kind] = type_counts.get(kind, 0) + 1
        raise ValueError(
            "No paragraph/table_row chunks found. Observed content_type counts: "
            + json.dumps(type_counts, sort_keys=True)
        )

    print(
        f"Chunks: {len(records)} total; {len(eligible)} eligible for embeddings. "
        f"Model={DEFAULT_MODEL}, dimensions={DEFAULT_DIMENSIONS}, batch_size={batch_size}."
    )
    # This is the only API-calling step. It receives only eligible text records.
    # generate_embeddings() returns the embedded records in this project.
    embedded_records = generate_embeddings(
        eligible,
        model=DEFAULT_MODEL,
        dimensions=DEFAULT_DIMENSIONS,
        batch_size=batch_size,
    )

    # Merge returned records back by stable chunk_id, so the full output keeps
    # non-embeddable chunks and preserves the original chunk metadata.
    embedded_by_id = {
        str(record["chunk_id"]): record
        for record in embedded_records
    }
    for record in records:
        embedded = embedded_by_id.get(str(record["chunk_id"]))
        if embedded is not None:
            record["embedding"] = embedded.get("embedding", [])

    for record in eligible:
        record["embedding"] = embedded_by_id[str(record["chunk_id"])].get("embedding", [])
        vector = record.get("embedding")
        if not isinstance(vector, list) or len(vector) != DEFAULT_DIMENSIONS:
            raise ValueError(
                f"{record['chunk_id']}: expected {DEFAULT_DIMENSIONS} embedding values."
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    artifact = {
        "model": DEFAULT_MODEL,
        "dimensions": DEFAULT_DIMENSIONS,
        "count": len(records),
        "embedded_count": len(eligible),
        "skipped_non_text_count": len(records) - len(eligible),
        "embeddings_generated": True,
        "chunks": records,
    }
    with output_path.open("w", encoding="utf-8") as file:
        json.dump(artifact, file, ensure_ascii=False, indent=2)

    print(f"Saved embedding artifact: {output_path}")
    print(f"Total chunks: {len(records)}")
    print(f"Chunks with vectors: {len(eligible)}")
    print(f"Chunks without vectors by design: {len(records) - len(eligible)}")
    print("No GCS upload or BigQuery write was performed.")
    return artifact


def main():
    parser = argparse.ArgumentParser(
        description="Generate embedded_chunks.json from existing chunk artifacts."
    )
    parser.add_argument("chunks_json", type=Path, help="Path to chunks.json")
    parser.add_argument(
        "--relationships",
        type=Path,
        required=True,
        help="Path to relationships.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output path (default: embedded_chunks.json beside chunks.json)",
    )
    parser.add_argument("--batch-size", type=int, default=16)
    args = parser.parse_args()

    if args.batch_size < 1:
        parser.error("--batch-size must be at least 1")

    output_path = args.output or args.chunks_json.with_name("embedded_chunks.json")
    generate_artifact(
        args.chunks_json,
        args.relationships,
        output_path,
        batch_size=args.batch_size,
    )


if __name__ == "__main__":
    main()
