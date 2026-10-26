"""Generate embeddings while preserving chunk relationships and assets.

Path handling:
- Uses the paths passed by main.py, which are built from config.py.
- Does not hardcode local machine paths.
- Does not upload to GCS or write to BigQuery.

Install:
    pip install google-genai

Use from main.py:
    embeddings = process_chunks(
        chunks_path,
        relationships_path=relationship_path,
        output_path=embedded_chunks_path,
        generate=True,
    )

The generate flag is explicit because generating embeddings calls the API and may incur charges.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "gemini-embedding-001"
DEFAULT_DIMENSIONS = 768
DEFAULT_BATCH_SIZE = 16


def load_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as file:
        return json.load(file)


def write_json(path: str | Path, data: Any) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as file:
        json.dump(data, file, ensure_ascii=False, indent=2)


def first(obj: dict, keys: tuple[str, ...], default=None):
    for key in keys:
        if obj.get(key) is not None:
            return obj[key]
    return default


def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def get_source_id(chunk: dict):
    metadata = chunk.get("metadata") or {}
    return first(
        chunk,
        ("source_id", "element_id", "extraction_id"),
        first(metadata, ("source_id", "element_id", "extraction_id")),
    )


def normalize_link(link):
    if isinstance(link, str):
        return {"url": link, "type": "link"}
    if not isinstance(link, dict):
        return None

    url = first(link, ("url", "target_uri", "uri", "href"))
    if not url:
        return None

    result = {"url": str(url), "type": first(link, ("type", "link_type"), "link")}
    for key in (
        "relationship", "relation_type", "title", "status", "evidence",
        "confidence", "source_id", "target_id",
    ):
        if link.get(key) is not None:
            result[key] = link[key]
    return result


def get_edges(data):
    if isinstance(data, list):
        return [item for item in data if isinstance(item, dict)]
    if isinstance(data, dict):
        for key in ("relationships", "edges", "items", "relations"):
            if isinstance(data.get(key), list):
                return [item for item in data[key] if isinstance(item, dict)]
    return []


def extract_chunks(raw):
    """Accept either a chunk list or a wrapper containing chunks/items/records."""
    if isinstance(raw, list):
        chunks = raw
    elif isinstance(raw, dict):
        chunks = first(raw, ("chunks", "items", "records"))
    else:
        chunks = None

    if not isinstance(chunks, list) or not all(isinstance(item, dict) for item in chunks):
        raise ValueError(
            "Chunks input must be a list or a JSON object containing "
            "a 'chunks', 'items', or 'records' list."
        )
    return chunks


def normalize_chunk_metadata(chunk):
    """Merge nested metadata into top-level fields and remove duplicates."""
    metadata = chunk.get("metadata") or {}

    if not isinstance(metadata, dict):
        raise TypeError(
            f"Expected metadata to be a dictionary, got {type(metadata).__name__}"
        )

    fields = (
        "related_chunk_ids",
        "related_links",
        "asset_uris",
        "relationship_candidates",
        "associated_images",
        "relationships",
    )

    for field in fields:
        top_level = as_list(chunk.get(field))
        nested = as_list(metadata.get(field))

        # Merge while preserving order and removing duplicates,
        # including duplicates already present in the top-level list.
        merged = []
        for item in top_level + nested:
            if item not in merged:
                merged.append(item)

        chunk[field] = merged

    return chunk

def enrich_chunks(chunks, relationships_data=None):

    for chunk in chunks:
        normalize_chunk_metadata(chunk)
    """Attach confirmed links/edges and retain uncertain edges as candidates."""
    by_chunk = {
        str(chunk["chunk_id"]): chunk
        for chunk in chunks
        if chunk.get("chunk_id") is not None
    }

    # A source element can map to more than one chunk; retain all matches.
    by_source = {}
    for chunk in chunks:
        source_id = get_source_id(chunk)
        if source_id is not None:
            by_source.setdefault(str(source_id), []).append(chunk)

        chunk.setdefault("related_chunk_ids", [])
        chunk.setdefault("related_links", [])
        chunk.setdefault("asset_uris", [])
        chunk.setdefault("relationship_candidates", [])

        # Keep existing asset references from common chunk-builder field names.
        for key in ("asset_uri", "gcs_uri", "image_uri", "image_path"):
            value = chunk.get(key)
            if value:
                for uri in as_list(value):
                    if uri not in chunk["asset_uris"]:
                        chunk["asset_uris"].append(uri)

        for key in ("url", "links", "related_links"):
            value = chunk.get(key)
            for item in as_list(value):
                normalized = normalize_link(item)
                if normalized and normalized not in chunk["related_links"]:
                    chunk["related_links"].append(normalized)

        # Preserve association metadata already created by chunk_builder.
        for key in ("associated_images", "relationships"):
            value = chunk.get(key)
            if value and key not in chunk:
                chunk[key] = value

    edges = get_edges(relationships_data) if relationships_data is not None else []
    for edge in edges:
        source = first(edge, ("source_chunk_id", "source_id", "from_id"))
        target = first(edge, ("target_chunk_id", "target_id", "to_id"))
        if source is None:
            continue

        sources = []
        if str(source) in by_chunk:
            sources = [by_chunk[str(source)]]
        else:
            sources = by_source.get(str(source), [])
        if not sources:
            continue

        targets = []
        if target is not None:
            if str(target) in by_chunk:
                targets = [by_chunk[str(target)]]
            else:
                targets = by_source.get(str(target), [])

        status = str(edge.get("status", "candidate")).lower()
        confirmed = status in ("confirmed", "validated", "accepted")
        relation = first(edge, ("relation_type", "type"), "related_to")

        for source_chunk in sources:
            if targets:
                for target_chunk in targets:
                    target_id = str(target_chunk.get("chunk_id", "")).strip()
                    if not target_id or target_id == str(source_chunk.get("chunk_id", "")):
                        continue
                    if confirmed:
                        if target_id not in source_chunk["related_chunk_ids"]:
                            source_chunk["related_chunk_ids"].append(target_id)
                    else:
                        candidate = {
                            "target_chunk_id": target_id,
                            "relation_type": relation,
                            "status": status,
                        }
                        for key in ("evidence", "confidence"):
                            if edge.get(key) is not None:
                                candidate[key] = edge[key]
                        if candidate not in source_chunk["relationship_candidates"]:
                            source_chunk["relationship_candidates"].append(candidate)

            url = first(edge, ("url", "target_uri", "uri", "href"))
            if url:
                link_data = dict(edge)
                link_data["url"] = url
                link_data["type"] = first(edge, ("link_type", "asset_type"), relation)
                link_data["relationship"] = relation
                link = normalize_link(link_data)
                if link:
                    field = "related_links" if confirmed else "relationship_candidates"
                    if link not in source_chunk[field]:
                        source_chunk[field].append(link)

            uri = first(edge, ("asset_uri", "gcs_uri", "image_uri", "asset_path", "image_path"))
            if uri:
                if confirmed:
                    if uri not in source_chunk["asset_uris"]:
                        source_chunk["asset_uris"].append(uri)
                else:
                    candidate = {
                        "asset_uri": uri,
                        "relation_type": relation,
                        "status": status,
                    }
                    if candidate not in source_chunk["relationship_candidates"]:
                        source_chunk["relationship_candidates"].append(candidate)

    for chunk in chunks:
        chunk["related_chunk_ids"] = list(
            dict.fromkeys(str(value) for value in as_list(chunk["related_chunk_ids"]) if value)
        )
        chunk["asset_uris"] = list(
            dict.fromkeys(str(value) for value in as_list(chunk["asset_uris"]) if value)
        )

        links = []
        for link in as_list(chunk["related_links"]):
            normalized = normalize_link(link)
            if normalized and normalized not in links:
                links.append(normalized)
        chunk["related_links"] = links
        chunk["relationship_candidates"] = as_list(chunk["relationship_candidates"])

    return chunks


def normalize_record(chunk, model, dimensions):
    chunk_id = str(chunk.get("chunk_id", "")).strip()
    content = str(chunk.get("content", chunk.get("text", ""))).strip()

    if not chunk_id:
        raise ValueError("Every chunk must have a non-empty chunk_id.")
    if not content:
        raise ValueError(f"Chunk {chunk_id} has no content to embed.")

    metadata = dict(chunk.get("metadata") or {})
    metadata_keys = (
        "machine_name", "document_id", "document_name", "source_filename",
        "source_path", "relative_source_path", "page_number", "page_index",
        "printed_page_number", "section", "section_path", "section_context",
        "content_type", "element_type", "parent_id", "source_id", "bbox",
        "reading_order", "asset_ids", "associated_images", "table_id",
        "row_index", "column_names",
    )
    for key in metadata_keys:
        if key in chunk and key not in metadata:
            metadata[key] = chunk[key]

    # Preserve existing vectors if present; otherwise leave empty until generation.
    embedding = chunk.get("embedding") or []
    if not isinstance(embedding, list):
        embedding = list(embedding)

    record = {
        "chunk_id": chunk_id,
        "model": model,
        "dimensions": dimensions,
        "embedding": embedding,
        "content": content,
        "metadata": metadata,
        "related_chunk_ids": as_list(chunk.get("related_chunk_ids")),
        "related_links": as_list(chunk.get("related_links")),
        "asset_uris": as_list(chunk.get("asset_uris")),
        "relationship_candidates": as_list(chunk.get("relationship_candidates")),
    }

    # Preserve any additional source fields that carry image/link/relationship
    # associations rather than silently dropping them during normalization.
    for key in (
        "image_path", "image_uri", "asset_path", "asset_id", "asset_ids",
        "associated_images", "relationships", "links", "table_data",
        "source_element_ids", "element_ids",
    ):
        if key in chunk and key not in record:
            record[key] = chunk[key]

    return record


def generate_embeddings(records, model, dimensions, batch_size=16):
    from google import genai
    from google.genai import types
    import time

    from config import GOOGLE_CLOUD_PROJECT

    project = (
        os.getenv("GCP_PROJECT_ID")
        or os.getenv("GOOGLE_CLOUD_PROJECT")
        or GOOGLE_CLOUD_PROJECT
    )

    region = (
        os.getenv("GCP_REGION")
        or os.getenv("GOOGLE_CLOUD_LOCATION")
        or "us-central1"
    )

    if not project:
        raise ValueError(
            "Google Cloud project is missing. "
            "Check GOOGLE_CLOUD_PROJECT in config.py or .env."
        )

    client = genai.Client(
        vertexai=True,
        project=project,
        location=region,
    )

    for start in range(0, len(records), batch_size):
        batch = records[start:start + batch_size]

        response = client.models.embed_content(
            model=model,
            contents=[record["content"] for record in batch],
            config=types.EmbedContentConfig(
                output_dimensionality=dimensions,
                task_type="RETRIEVAL_DOCUMENT",
            ),
        )

        vectors = response.embeddings or []

        if len(vectors) != len(batch):
            raise RuntimeError(
                f"Expected {len(batch)} embeddings; got {len(vectors)}."
            )

        for record, item in zip(batch, vectors):
            vector = list(item.values)
            import math

            norm = math.sqrt(sum(value * value for value in vector))

            if norm == 0:
                raise ValueError(
                    f"{record['chunk_id']}: embedding has zero magnitude."
                )

            vector = [value / norm for value in vector]

            if len(vector) != dimensions:
                raise RuntimeError(
                    f"{record['chunk_id']}: expected {dimensions} dimensions, "
                    f"got {len(vector)}."
                )

            record["embedding"] = vector

        if start + batch_size < len(records):
            time.sleep(0.2)

    return records
def process_chunks(
    chunks_path: str | Path,
    relationships_path: str | Path | None = None,
    output_path: str | Path | None = None,
    *,
    generate: bool = False,
    model: str = DEFAULT_MODEL,
    dimensions: int = DEFAULT_DIMENSIONS,
    batch_size: int = DEFAULT_BATCH_SIZE,
):
    """Load, enrich, normalize and save chunk records.

    Paths are supplied by main.py, based on PROCESSED_DATA_DIR. Set generate=True
    explicitly to call the embedding API.
    Returns the list of output chunk records.
    """
    chunks_path = Path(chunks_path)
    if not chunks_path.is_file():
        raise FileNotFoundError(f"Chunks file not found: {chunks_path}")

    raw = load_json(chunks_path)
    chunks = extract_chunks(raw)

    relationship_data = None
    if relationships_path is not None:
        relationship_path = Path(relationships_path)
        if not relationship_path.is_file():
            raise FileNotFoundError(f"Relationships file not found: {relationship_path}")
        relationship_data = load_json(relationship_path)

    chunks = enrich_chunks(chunks, relationship_data)
    records = [normalize_record(chunk, model, dimensions) for chunk in chunks]

    chunk_ids = [record["chunk_id"] for record in records]
    if len(chunk_ids) != len(set(chunk_ids)):
        raise ValueError("Duplicate chunk_id values found.")

    if generate:
        records = generate_embeddings(records, model, dimensions, batch_size)

    destination = (
        Path(output_path)
        if output_path is not None
        else chunks_path.with_name(f"{chunks_path.stem}.embedded.json")
    )
    write_json(
        destination,
        {
            "model": model,
            "dimensions": dimensions,
            "count": len(records),
            "embeddings_generated": bool(generate),
            "chunks": records,
        },
    )
    print(
        f"Processed {len(records)} chunks. "
        f"{'Embeddings generated' if generate else 'No API calls made'}. "
        f"Output: {destination}"
    )
    return records


def main():
    parser = argparse.ArgumentParser(
        description="Create embedding records while preserving links, assets and relationships."
    )
    parser.add_argument("chunks_json", type=Path)
    parser.add_argument("--relationships", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--dimensions", type=int, default=DEFAULT_DIMENSIONS)
    parser.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--generate", action="store_true", help="Call the embedding API; may incur charges.")
    mode.add_argument("--dry-run", action="store_true", help="Write records without API calls.")

    args = parser.parse_args()
    process_chunks(
        args.chunks_json,
        relationships_path=args.relationships,
        output_path=args.output,
        generate=args.generate,
        model=args.model,
        dimensions=args.dimensions,
        batch_size=args.batch_size,
    )
    print("No GCS upload or BigQuery write was performed.")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
