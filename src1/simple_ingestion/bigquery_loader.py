#Reads processed PDF data and pre-generated embeddings from Google Cloud Storage (GCS), 
# validates them, and loads them into 9 BigQuery tables.
"""
GCS -> BigQuery loader for the manufacturing multimodal RAG pipeline.

Reads chunks.json, embedded_chunks.json, extraction.json, relationships.json, and
segmentation/manifest.json from one processed document folder in GCS. Embeddings
must already exist in embedded_chunks.json; this loader never calls the embedding API.

Start safely with --dry-run. The loader defaults to the dev dataset configured in
config.py / environment variables.
"""
from __future__ import annotations

import argparse
import json
import os
import uuid
from typing import Any

from google.cloud import bigquery, storage

# Use the existing project configuration where available.
try:
    import config as project_config
except ImportError:
    project_config = None

GCP_PROJECT_ID = os.getenv(
    "GCP_PROJECT_ID",
    getattr(project_config, "GCP_PROJECT_ID",
            getattr(project_config, "GOOGLE_CLOUD_PROJECT", "market-lens-506611")),
)
GCS_BUCKET_NAME = os.getenv(
    "GCS_BUCKET_NAME",
    getattr(project_config, "GCS_BUCKET_NAME", "market-lens-506611-manufacturing-rag"),
)
GCS_PROCESSED_PREFIX = os.getenv(
    "GCS_PROCESSED_PREFIX",
    getattr(project_config, "GCS_PROCESSED_PREFIX", "processed"),
)
BIGQUERY_DATASET = os.getenv(
    "BIGQUERY_DATASET",
    getattr(project_config, "BIGQUERY_DATASET", "manufacturing_rag_dev"),
)

# Embeddings are generated in a separate step and read from embedded_chunks.json.
PROJECT = os.getenv("GCP_PROJECT_ID", GCP_PROJECT_ID or "market-lens-506611")
BUCKET = os.getenv(
    "GCS_BUCKET_NAME", GCS_BUCKET_NAME or "market-lens-506611-manufacturing-rag"
)
DATASET = os.getenv("BIGQUERY_DATASET", BIGQUERY_DATASET or "manufacturing_rag_dev")
PREFIX = os.getenv("GCS_PROCESSED_PREFIX", GCS_PROCESSED_PREFIX or "processed").strip("/")

EMBEDDING_MODEL = "gemini-embedding-001"
EMBEDDING_DIMENSIONS = 768
EMBEDDABLE_TYPES = {"paragraph", "table_row"}

TABLES = (
    "document_chunks",
    "document_chunk_geometry",
    "document_images",
    "document_links",
    "document_table_rows",
    "chunk_relationships",
    "document_sections",
    "section_elements",
    "document_segments",
)

ARTIFACTS = (
    "chunks.json",
    "embedded_chunks.json",
    "extraction.json",
    "relationships.json",
    "segmentation/manifest.json",
)


def first(*values: Any, default=None):
    """Return the first non-null, non-empty value."""
    return next((v for v in values if v is not None and v != ""), default)


def as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def records(data: Any, *keys: str) -> list[dict]:
    """Extract a list of record dictionaries from common JSON shapes."""
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def nested(data: dict, *paths: str, default=None):
    for path in paths:
        value: Any = data
        for part in path.split("."):
            value = value.get(part) if isinstance(value, dict) else None
            if value is None:
                break
        if value is not None:
            return value
    return default


def get_metadata(chunk: dict) -> dict:
    return as_dict(chunk.get("metadata"))


def chunk_id_of(chunk: dict):
    md = get_metadata(chunk)
    return first(chunk.get("chunk_id"), chunk.get("id"), md.get("chunk_id"))


def chunk_uuid_of(chunk: dict):
    # UUID is generated upstream by chunk_builder.py. Do not generate a second,
    # potentially different UUID here.
    md = get_metadata(chunk)
    return first(chunk.get("chunk_uuid"), md.get("chunk_uuid"))


def get_bbox(chunk: dict):
    md = get_metadata(chunk)
    value = first(
        chunk.get("bbox"),
        chunk.get("bounding_box"),
        md.get("bbox"),
        md.get("bounding_box"),
        md.get("geometry"),
    )
    if isinstance(value, dict):
        vals = [
            first(value.get("x0"), value.get("left")),
            first(value.get("y0"), value.get("top")),
            first(value.get("x1"), value.get("right")),
            first(value.get("y1"), value.get("bottom")),
        ]
    elif isinstance(value, (list, tuple)) and len(value) >= 4:
        vals = list(value[:4])
    else:
        return None
    try:
        if any(v is None for v in vals):
            return None
        return dict(zip(("x0", "y0", "x1", "y1"), [float(v) for v in vals]))
    except (TypeError, ValueError):
        return None


def read_json_from_gcs(bucket, folder: str, relative_path: str, *, required=False):
    object_name = f"{folder.rstrip('/')}/{relative_path}"
    blob = bucket.blob(object_name)
    try:
        return json.loads(blob.download_as_text(encoding="utf-8"))
    except Exception as exc:
        if required:
            raise RuntimeError(
                f"Could not read required artifact gs://{BUCKET}/{object_name}"
            ) from exc
        return {}


class GcsReader:
    def __init__(self, storage_client=None):
        self.client = storage_client or storage.Client(project=PROJECT)
        self.bucket = self.client.bucket(BUCKET)

    def folders(self) -> list[str]:
        """Find processed document folders; return paths relative to PREFIX."""
        found = set()
        prefix = f"{PREFIX}/" if PREFIX else ""
        for blob in self.client.list_blobs(self.bucket, prefix=prefix):
            if blob.name.endswith("/chunks.json"):
                full_folder = blob.name.rsplit("/", 1)[0]
                relative_folder = (
                    full_folder[len(prefix):] if prefix and full_folder.startswith(prefix)
                    else full_folder
                )
                found.add(relative_folder + "/")
        return sorted(found)

    def read(self, folder: str) -> dict:
        relative_folder = folder.strip("/")
        full_folder = f"{PREFIX}/{relative_folder}" if PREFIX else relative_folder
        chunks_data = read_json_from_gcs(
            self.bucket, full_folder, "chunks.json", required=True
        )
        embedded_data = read_json_from_gcs(
            self.bucket, full_folder, "embedded_chunks.json", required=True
        )
        extraction = read_json_from_gcs(self.bucket, full_folder, "extraction.json")
        relationships = read_json_from_gcs(self.bucket, full_folder, "relationships.json")
        manifest = read_json_from_gcs(
            self.bucket, full_folder, "segmentation/manifest.json"
        )

        chunks = records(chunks_data, "chunks", "items", "records")
        if not chunks:
            raise ValueError(
                f"No chunks found in gs://{BUCKET}/{folder}/chunks.json"
            )

        embedded_chunks = records(embedded_data, "chunks", "items", "records")
        if not embedded_chunks:
            raise ValueError(
                f"No embedded chunks found in gs://{BUCKET}/{folder}/embedded_chunks.json"
            )
        if isinstance(embedded_data, dict):
            if embedded_data.get("embeddings_generated") is not True:
                raise ValueError(
                    f"{folder}: embedded_chunks.json does not confirm embeddings_generated=true"
                )
            artifact_model = embedded_data.get("model")
            artifact_dimensions = embedded_data.get("dimensions")
            if artifact_model and artifact_model != EMBEDDING_MODEL:
                raise ValueError(
                    f"{folder}: unexpected embedding model {artifact_model!r}; "
                    f"expected {EMBEDDING_MODEL!r}"
                )
            if artifact_dimensions and int(artifact_dimensions) != EMBEDDING_DIMENSIONS:
                raise ValueError(
                    f"{folder}: unexpected embedding dimensions {artifact_dimensions}; "
                    f"expected {EMBEDDING_DIMENSIONS}"
                )

        # Build a strict chunk_id -> precomputed record map. No API fallback is allowed.
        embedded_by_id = {}
        for embedded in embedded_chunks:
            embedded_id = first(
                embedded.get("chunk_id"),
                embedded.get("id"),
                get_metadata(embedded).get("chunk_id"),
            )
            if embedded_id is None:
                raise ValueError(f"{folder}: embedded record without chunk_id")
            embedded_id = str(embedded_id)
            if embedded_id in embedded_by_id:
                raise ValueError(f"{folder}: duplicate embedded chunk_id {embedded_id!r}")
            embedded_by_id[embedded_id] = embedded

        original_ids = []
        for chunk in chunks:
            cid = chunk_id_of(chunk)
            if cid is None:
                raise ValueError(f"{folder}: source chunk without chunk_id")
            original_ids.append(str(cid))
        if len(original_ids) != len(set(original_ids)):
            raise ValueError(f"{folder}: duplicate chunk_id in chunks.json")
        missing_ids = sorted(set(original_ids) - set(embedded_by_id))
        extra_ids = sorted(set(embedded_by_id) - set(original_ids))
        if missing_ids or extra_ids:
            raise ValueError(
                f"{folder}: embedded_chunks.json does not match chunks.json; "
                f"missing={missing_ids[:5]}, extra={extra_ids[:5]}"
            )

        # Validate every expected vector before transformation or any BigQuery write.
        for chunk in chunks:
            cid = str(chunk_id_of(chunk))
            md = get_metadata(chunk)
            content_type = str(first(
                chunk.get("content_type"), md.get("content_type"),
                md.get("element_type"), default="unknown"
            )).strip().lower()
            embedded = embedded_by_id[cid]
            vector = embedded.get("embedding")
            if content_type in EMBEDDABLE_TYPES:
                if not isinstance(vector, list) or len(vector) != EMBEDDING_DIMENSIONS:
                    actual = len(vector) if isinstance(vector, list) else type(vector).__name__
                    raise ValueError(
                        f"{folder}: chunk {cid} requires a {EMBEDDING_DIMENSIONS}-dimension "
                        f"embedding; got {actual}"
                    )
            elif vector not in (None, []):
                if not isinstance(vector, list) or len(vector) != EMBEDDING_DIMENSIONS:
                    raise ValueError(
                        f"{folder}: non-text chunk {cid} has an invalid embedding value"
                    )

        parts = folder.split("/")
        extraction_dict = as_dict(extraction)
        document_meta = as_dict(extraction_dict.get("document"))
        machine = first(
            document_meta.get("machine_name"),
            extraction_dict.get("machine_name"),
            get_metadata(chunks[0]).get("machine_name"),
            parts[-2] if len(parts) > 1 else None,
            default="unknown_machine",
        )
        document = first(
            document_meta.get("document_name"),
            document_meta.get("name"),
            extraction_dict.get("document_name"),
            get_metadata(chunks[0]).get("document_name"),
            parts[-1],
            default="unknown_document",
        )

        return {
            "folder": relative_folder + "/",
            "machine": str(machine),
            "document": str(document),
            "chunks": chunks,
            "embedded_by_id": embedded_by_id,
            "extraction": as_dict(extraction),
            "relationships": as_dict(relationships),
            "manifest": manifest,
        }


def transform(doc: dict) -> dict[str, list[dict]]:
    """Map the four JSON artifacts into rows for the nine target tables."""
    out = {table: [] for table in TABLES}
    source_to_chunk_uuid: dict[str, str] = {}
    seen_uuids: set[str] = set()
    embedded_by_id = doc.get("embedded_by_id") or {}

    for chunk in doc["chunks"]:
        md = get_metadata(chunk)
        chunk_id = chunk_id_of(chunk)
        chunk_uuid = chunk_uuid_of(chunk)
        if not chunk_id:
            raise ValueError(f"{doc['folder']}: chunk without chunk_id")
        if not chunk_uuid:
            raise ValueError(
                f"{doc['folder']}: chunk {chunk_id} has no chunk_uuid. "
                "Regenerate chunks.json with the updated chunk_builder.py first."
            )
        chunk_id = str(chunk_id)
        chunk_uuid = str(chunk_uuid)
        if chunk_uuid in seen_uuids:
            raise ValueError(f"Duplicate chunk_uuid in document: {chunk_uuid}")
        seen_uuids.add(chunk_uuid)

        content = first(chunk.get("content"), chunk.get("text"), chunk.get("page_content"), default="")
        page = first(chunk.get("page_number"), md.get("page_number"), md.get("page"))
        content_type = str(first(chunk.get("content_type"), md.get("content_type"), md.get("element_type"), default="unknown")).strip().lower()
        source_id = first(chunk.get("source_id"), md.get("source_id"), md.get("element_id"))
        if source_id is not None:
            source_to_chunk_uuid[str(source_id)] = chunk_uuid

        if page is None or content is None or not str(content).strip():
            raise ValueError(
                f"{doc['folder']}: chunk {chunk_id} missing page_number or content"
            )

        # Reuse the precomputed vector. Never call the embedding API from this loader.
        embedded_record = embedded_by_id.get(chunk_id)
        if embedded_record is None:
            raise ValueError(
                f"{doc['folder']}: no precomputed embedding record for chunk {chunk_id}"
            )
        raw_vector = embedded_record.get("embedding")
        vector = (
            [float(value) for value in raw_vector]
            if isinstance(raw_vector, list) else []
        )
        model = EMBEDDING_MODEL if content_type in EMBEDDABLE_TYPES else None
        dimensions = len(vector) if content_type in EMBEDDABLE_TYPES else None
        if content_type in EMBEDDABLE_TYPES and len(vector) != EMBEDDING_DIMENSIONS:
            raise ValueError(
                f"{chunk_id}: expected {EMBEDDING_DIMENSIONS} precomputed values, "
                f"received {len(vector)}"
            )

        metadata = dict(md)
        metadata.setdefault("machine_name", doc["machine"])
        metadata.setdefault("document_name", doc["document"])
        metadata.setdefault("page_number", int(page))
        metadata.setdefault("content_type", content_type)

        out["document_chunks"].append({
            "chunk_uuid": chunk_uuid,
            "chunk_id": chunk_id,
            "machine_name": str(first(chunk.get("machine_name"), md.get("machine_name"), doc["machine"])),
            "document_name": str(first(chunk.get("document_name"), md.get("document_name"), doc["document"])),
            "page_number": int(page),
            "content_type": content_type,
            "source_id": str(source_id) if source_id is not None else None,
            "content": str(content),
            "embedding_model": model,
            "embedding_dimensions": dimensions,
            "embedding": vector,
            "metadata": metadata,
        })

        box = get_bbox(chunk)
        if box:
            out["document_chunk_geometry"].append({
                "chunk_uuid": chunk_uuid,
                "chunk_id": chunk_id,
                **box,
            })

        image_id = first(md.get("image_id"), chunk.get("image_id"))
        image_path = first(
            md.get("image_path"), md.get("asset"), chunk.get("image_path")
        )
        if image_id is not None or image_path is not None or content_type == "image":
            # Avoid inventing required image IDs/paths. Only write complete rows.
            if image_id is not None and image_path is not None:
                out["document_images"].append({
                    "chunk_uuid": chunk_uuid,
                    "chunk_id": chunk_id,
                    "image_id": str(image_id),
                    "image_path": str(image_path),
                    "relationship_status": md.get("relationship_status"),
                    "relationship_method": md.get("relationship_method"),
                })

        table_id = first(md.get("table_id"), chunk.get("table_id"))
        row_number = first(md.get("row_number"), chunk.get("row_number"))
        if content_type == "table_row" and table_id is not None and row_number is not None:
            out["document_table_rows"].append({
                "chunk_uuid": chunk_uuid,
                "chunk_id": chunk_id,
                "table_id": str(table_id),
                "row_number": int(row_number),
                "content": str(content),
            })

        link_id = first(md.get("link_id"), chunk.get("link_id"))
        url = first(md.get("url"), chunk.get("url"))
        if link_id is not None and url:
            out["document_links"].append({
                "chunk_uuid": chunk_uuid,
                "chunk_id": chunk_id,
                "link_id": str(link_id),
                "url": str(url),
            })

    # Section hierarchy and element mappings.
    relationships = doc["relationships"]
    for section in records(relationships, "sections"):
        sid = first(section.get("section_id"), section.get("id"))
        title = section.get("title")
        level = section.get("level")
        page = first(section.get("page_number"), section.get("page"))
        if sid is not None and title is not None and level is not None and page is not None:
            out["document_sections"].append({
                "section_id": str(sid),
                "document_name": doc["document"],
                "title": str(title),
                "level": int(level),
                "page_number": int(page),
                "parent_section_id": (
                    str(section["parent_section_id"])
                    if section.get("parent_section_id") is not None else None
                ),
            })
        child_ids = first(
            section.get("child_element_ids"),
            section.get("element_ids"),
            default=[],
        )
        for element_id in child_ids or []:
            if sid is not None and element_id is not None:
                out["section_elements"].append({
                    "section_id": str(sid),
                    "element_id": str(element_id),
                })

    # Preserve every relationship family emitted by the relationship builder.
    for page_record in records(relationships, "pages"):
        for family in (
            "relationships", "image_relationships",
            "table_relationships", "link_relationships",
        ):
            for edge in records(page_record, family):
                source = first(edge.get("source_id"), edge.get("element_id"))
                target = first(edge.get("target_id"), edge.get("related_element_id"))
                if source is None or target is None:
                    continue
                relation_type = str(first(
                    edge.get("relation_type"),
                    edge.get("relationship_type"),
                    default=family.removesuffix("_relationships"),
                ))
                identity = f"{doc['document']}|{source}|{target}|{relation_type}"
                relationship_id = str(uuid.uuid5(uuid.NAMESPACE_URL, identity))
                out["chunk_relationships"].append({
                    "relationship_id": relationship_id,
                    "source_id": str(source),
                    "target_id": str(target),
                    "relation_type": relation_type,
                    "evidence": edge.get("evidence"),
                    "relationship_method": first(edge.get("relationship_method"), edge.get("method")),
                    "status": edge.get("status"),
                })

    # Segmentation manifest can be either a list or a wrapper containing segments.
    manifest = doc.get("manifest")
    segments = records(manifest, "segments", "document_segments", "items")
    if not segments and isinstance(manifest, list):
        segments = records(manifest)
    for segment in segments:
        segment_id = first(segment.get("segment_id"), segment.get("id"))
        if segment_id is None:
            continue
        out["document_segments"].append({
            "segment_id": str(segment_id),
            "document_id": (
                str(segment["document_id"])
                if segment.get("document_id") is not None else None
            ),
            "document_name": str(first(segment.get("document_name"), doc["document"])),
            "virtual_page": int(first(segment.get("virtual_page"), segment.get("virtual_page_number"), default=0)),
            "original_page_number": (
                int(segment["original_page_number"])
                if segment.get("original_page_number") is not None else None
            ),
            "segment_type": str(first(segment.get("segment_type"), segment.get("type"), default="unknown")),
            "associated_table": bool(segment.get("associated_table", False)),
            "source_bbox": segment.get("source_bbox"),
            "detected_table_bbox": segment.get("detected_table_bbox"),
            "coordinate_system": segment.get("coordinate_system"),
            "review_required": bool(segment.get("review_required", False)),
            "table_detection": segment.get("table_detection"),
            "attached_heading_lines": segment.get("attached_heading_lines"),
            "related_images": segment.get("related_images"),
        })

    # Remove duplicate relationship/section rows deterministically.
    key_columns = {
        "document_chunks": ("chunk_uuid",),
        "document_chunk_geometry": ("chunk_uuid",),
        "document_images": ("chunk_uuid", "image_id"),
        "document_links": ("chunk_uuid", "link_id"),
        "document_table_rows": ("chunk_uuid", "table_id", "row_number"),
        "chunk_relationships": ("relationship_id",),
        "document_sections": ("section_id",),
        "section_elements": ("section_id", "element_id"),
        "document_segments": ("segment_id",),
    }
    for table, keys in key_columns.items():
        deduped = {}
        for row in out[table]:
            deduped[tuple(row.get(k) for k in keys)] = row
        out[table] = list(deduped.values())

    return out


def bq_schema(table: str):
    """Schemas matching the finalized nine-table development dataset."""
    S = bigquery.SchemaField
    schemas = {
        "document_chunks": [
            S("chunk_uuid", "STRING"), S("chunk_id", "STRING", "REQUIRED"),
            S("machine_name", "STRING", "REQUIRED"),
            S("document_name", "STRING", "REQUIRED"),
            S("page_number", "INT64", "REQUIRED"),
            S("content_type", "STRING", "REQUIRED"), S("source_id", "STRING"),
            S("content", "STRING", "REQUIRED"), S("embedding_model", "STRING"),
            S("embedding_dimensions", "INT64"), S("embedding", "FLOAT64", mode="REPEATED"),
            S("metadata", "JSON"),
        ],
        "document_chunk_geometry": [
            S("chunk_uuid", "STRING"), S("chunk_id", "STRING", "REQUIRED"),
            S("x0", "FLOAT64", "REQUIRED"), S("y0", "FLOAT64", "REQUIRED"),
            S("x1", "FLOAT64", "REQUIRED"), S("y1", "FLOAT64", "REQUIRED"),
        ],
        "document_images": [
            S("chunk_uuid", "STRING"), S("chunk_id", "STRING", "REQUIRED"),
            S("image_id", "STRING", "REQUIRED"), S("image_path", "STRING"),
            S("relationship_status", "STRING"), S("relationship_method", "STRING"),
        ],
        "document_links": [
            S("chunk_uuid", "STRING"), S("chunk_id", "STRING", "REQUIRED"),
            S("link_id", "STRING", "REQUIRED"), S("url", "STRING", "REQUIRED"),
        ],
        "document_table_rows": [
            S("chunk_uuid", "STRING"), S("chunk_id", "STRING", "REQUIRED"),
            S("table_id", "STRING", "REQUIRED"), S("row_number", "INT64", "REQUIRED"),
            S("content", "STRING", "REQUIRED"),
        ],
        "chunk_relationships": [
            S("relationship_id", "STRING", "REQUIRED"), S("source_id", "STRING", "REQUIRED"),
            S("target_id", "STRING", "REQUIRED"), S("relation_type", "STRING", "REQUIRED"),
            S("evidence", "STRING"), S("relationship_method", "STRING"), S("status", "STRING"),
        ],
        "document_sections": [
            S("section_id", "STRING", "REQUIRED"), S("document_name", "STRING", "REQUIRED"),
            S("title", "STRING", "REQUIRED"), S("level", "INT64", "REQUIRED"),
            S("page_number", "INT64", "REQUIRED"), S("parent_section_id", "STRING"),
        ],
        "section_elements": [
            S("section_id", "STRING", "REQUIRED"), S("element_id", "STRING", "REQUIRED"),
        ],
        "document_segments": [
            S("segment_id", "STRING", "REQUIRED"), S("document_id", "STRING"),
            S("document_name", "STRING", "REQUIRED"), S("virtual_page", "INT64", "REQUIRED"),
            S("original_page_number", "INT64"), S("segment_type", "STRING", "REQUIRED"),
            S("associated_table", "BOOL", "REQUIRED"), S("source_bbox", "FLOAT64", mode="REPEATED"),
            S("detected_table_bbox", "FLOAT64", mode="REPEATED"), S("coordinate_system", "STRING"),
            S("review_required", "BOOL", "REQUIRED"), S("table_detection", "JSON"),
            S("attached_heading_lines", "JSON"), S("related_images", "JSON"),
        ],
    }
    return schemas[table]


def merge_rows(client, table: str, rows: list[dict]) -> None:
    """Stage rows and MERGE them idempotently into the destination table."""
    if not rows:
        return
    target = f"{PROJECT}.{DATASET}.{table}"
    temp = f"{PROJECT}.{DATASET}._tmp_{table}_{uuid.uuid4().hex[:10]}"
    schema = bq_schema(table)
    columns = [field.name for field in schema]
    keys = {
        "document_chunks": ["chunk_uuid"],
        "document_chunk_geometry": ["chunk_uuid"],
        "document_images": ["chunk_uuid", "image_id"],
        "document_links": ["chunk_uuid", "link_id"],
        "document_table_rows": ["chunk_uuid", "table_id", "row_number"],
        "chunk_relationships": ["relationship_id"],
        "document_sections": ["section_id"],
        "section_elements": ["section_id", "element_id"],
        "document_segments": ["segment_id"],
    }[table]
    # Convert nested dict/list values for BigQuery JSON columns.
    json_columns = {
        "document_chunks": {"metadata"},
        "document_segments": {"table_detection", "attached_heading_lines", "related_images"},
    }.get(table, set())
    payload = []
    for row in rows:
        item = dict(row)
        for name in json_columns:
            if item.get(name) is not None and not isinstance(item[name], str):
                item[name] = json.dumps(item[name], ensure_ascii=False)
        payload.append(item)

    job_config = bigquery.LoadJobConfig(
        schema=schema,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
    )
    client.load_table_from_json(payload, temp, job_config=job_config).result()
    try:
        on_clause = " AND ".join(f"T.`{k}` = S.`{k}`" for k in keys)
        partition_by = ", ".join(f"`{k}`" for k in keys)
        updates = [c for c in columns if c not in keys]
        update_clause = (
            "WHEN MATCHED THEN UPDATE SET "
            + ", ".join(f"T.`{c}` = S.`{c}`" for c in updates)
            if updates else ""
        )
        column_sql = ", ".join(f"`{c}`" for c in columns)
        value_sql = ", ".join(f"S.`{c}`" for c in columns)
        sql = f"""
        MERGE `{target}` T
        USING (
          SELECT * EXCEPT(rn)
          FROM (
            SELECT *, ROW_NUMBER() OVER (
              PARTITION BY {partition_by}
              ORDER BY {partition_by}
            ) AS rn
            FROM `{temp}`
          )
          WHERE rn = 1
        ) S
        ON {on_clause}
        {update_clause}
        WHEN NOT MATCHED THEN
          INSERT ({column_sql}) VALUES ({value_sql})
        """
        client.query(sql).result()
    finally:
        client.delete_table(temp, not_found_ok=True)


def load_gcs_document_to_bigquery(
    folder: str,
    tables: tuple[str, ...] | list[str] = TABLES,
    *,
    commit: bool = False,
) -> dict[str, int]:
    """
    Load one processed GCS document folder into BigQuery.

    `folder` is relative to the processed prefix, for example:
    `HAB/6 - Haas Surface Grinder - Maintenance/`.

    commit=False is a dry run: source artifacts and saved embeddings are validated,
    but no BigQuery writes are made. commit=True stages and MERGEs rows.
    """
    tables = tuple(tables)
    invalid = set(tables) - set(TABLES)
    if invalid:
        raise ValueError(f"Unsupported tables: {sorted(invalid)}")

    reader = GcsReader()
    doc = reader.read(folder)
    mapped = transform(doc)

    chunk_uuids = {row["chunk_uuid"] for row in mapped["document_chunks"]}
    for table in (
        "document_chunk_geometry", "document_images",
        "document_links", "document_table_rows",
    ):
        missing = [
            row["chunk_uuid"] for row in mapped[table]
            if row["chunk_uuid"] not in chunk_uuids
        ]
        if missing:
            raise ValueError(f"{table} references unknown chunk_uuid: {missing[:5]}")

    counts = {}
    bq_client = bigquery.Client(project=PROJECT) if commit else None
    for table in tables:
        rows = mapped[table]
        counts[table] = len(rows)
        print(f"[ROWS] {table}: {len(rows)}")
        if commit and rows:
            merge_rows(bq_client, table, rows)
            print(f"[MERGED] {table}")
        elif not commit:
            print(f"[DRY RUN] no BigQuery writes to {table}")
    return counts


def main():
    parser = argparse.ArgumentParser(description="Load processed manufacturing RAG artifacts to BigQuery.")
    parser.add_argument(
        "--folder",
        help="Folder relative to processed prefix, e.g. 'HAB/6 - Haas Surface Grinder - Maintenance/'",
    )
    parser.add_argument("--tables", default="all", help="Comma-separated table names, or 'all'.")
    parser.add_argument("--dry-run", action="store_true", help="Validate artifacts and saved embeddings without BigQuery writes (default).")
    parser.add_argument("--commit", action="store_true", help="Write rows to BigQuery.")
    args = parser.parse_args()

    if args.dry_run and args.commit:
        parser.error("Choose either --dry-run or --commit.")
    selected_tables = TABLES if args.tables.strip().lower() == "all" else tuple(
        name.strip() for name in args.tables.split(",") if name.strip()
    )
    invalid = set(selected_tables) - set(TABLES)
    if not selected_tables or invalid:
        parser.error(f"Unsupported tables: {sorted(invalid)}. Available: {', '.join(TABLES)}")

    reader = GcsReader()
    folders = [args.folder] if args.folder else reader.folders()
    if not folders:
        print(f"No processed document folders containing chunks.json under gs://{BUCKET}/{PREFIX}/")
        return

    for folder in folders:
        folder = folder.strip("/") + "/"
        print(f"\n[DOCUMENT] gs://{BUCKET}/{PREFIX}/{folder}")
        counts = load_gcs_document_to_bigquery(folder, selected_tables, commit=args.commit)
        print("[SUMMARY] " + ", ".join(f"{name}={count}" for name, count in counts.items()))

    if not args.commit:
        print("\nDry run complete. No BigQuery rows were written.")


if __name__ == "__main__":
    main()
