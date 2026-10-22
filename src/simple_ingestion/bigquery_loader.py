"""Idempotent GCS -> BigQuery loader for all manufacturing RAG tables.

Reads each prepared JSON artifact once per document per invocation and reuses
its parsed content for every table transformation. Dry-run is the default.

CLI:
  python bigquery_loader.py --dry-run --tables all
  python bigquery_loader.py --commit --tables all
  python bigquery_loader.py --commit --folder "processed/HSG-2550/<document>/" --tables document_images,document_table_rows

Callable integration after a successful GCS upload:
  from bigquery_loader import load_gcs_document_to_bigquery
  load_gcs_document_to_bigquery("processed/<machine>/<document>/")
"""
from __future__ import annotations

import argparse
import json
import os
import uuid
from typing import Any

from google.cloud import bigquery, storage
from config import (
    GCP_PROJECT_ID,
    GCS_BUCKET_NAME,
    BIGQUERY_DATASET,
    GCS_PROCESSED_PREFIX,
)
PROJECT = GCP_PROJECT_ID
BUCKET = GCS_BUCKET_NAME
DATASET = BIGQUERY_DATASET
PREFIX = GCS_PROCESSED_PREFIX.strip("/") + "/"
ARTIFACTS = ("extraction.json", "relationships.json", "chunks.json", "embedded_chunks.json")
TABLES = (
    "document_chunks",
    "document_chunk_geometry",
    "document_images",
    "document_table_rows",
    "document_links",
    "chunk_relationships",
    "document_sections",
    "section_elements",
)
def first(*values: Any, default=None):
    return next((v for v in values if v is not None and v != ""), default)


def as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def records(data: Any, *keys: str) -> list[dict]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def get_nested(data: dict, *paths: str, default=None):
    for path in paths:
        value: Any = data
        for part in path.split("."):
            value = value.get(part) if isinstance(value, dict) else None
            if value is None:
                break
        if value is not None:
            return value
    return default


class GcsReader:
    def __init__(self):
        self.client = storage.Client(project=PROJECT)
        self.bucket = self.client.bucket(BUCKET)

    def folders(self) -> list[str]:
        found = set()
        for blob in self.client.list_blobs(self.bucket, prefix=PREFIX):
            if blob.name.endswith("/embedded_chunks.json"):
                found.add(blob.name.rsplit("/", 1)[0] + "/")
        return sorted(found)

    def read(self, folder: str) -> dict:
        folder = folder.strip("/") + "/"
        files = {}
        for name in ARTIFACTS:
            blob = self.bucket.blob(folder + name)
            try:
                files[name] = json.loads(blob.download_as_text(encoding="utf-8"))
            except Exception as exc:
                if name == "embedded_chunks.json":
                    raise RuntimeError(f"Required artifact missing/unreadable: gs://{BUCKET}/{folder}{name}") from exc
                files[name] = {}
        parts = folder.strip("/").split("/")
        extraction = as_dict(files["extraction.json"])
        doc_meta = as_dict(extraction.get("document"))
        machine = str(first(doc_meta.get("machine_name"), extraction.get("machine_name"), parts[-2] if len(parts) > 1 else None, default="unknown_machine"))
        document = str(first(doc_meta.get("document_name"), doc_meta.get("name"), extraction.get("document_name"), parts[-1], default="unknown_document"))
        embedded_root = as_dict(files["embedded_chunks.json"])
        chunks = records(files["embedded_chunks.json"], "chunks", "embedded_chunks", "items")
        if not chunks:
            raise ValueError(f"No embedded chunks found in gs://{BUCKET}/{folder}embedded_chunks.json")
        return {"folder": folder, "machine": machine, "document": document, "files": files, "embedded_root": embedded_root, "chunks": chunks}


def get_metadata(chunk: dict) -> dict:
    return as_dict(chunk.get("metadata"))


def cid(chunk: dict):
    md = get_metadata(chunk)
    return first(chunk.get("chunk_id"), chunk.get("id"), md.get("chunk_id"))


def bbox(chunk: dict):
    md = get_metadata(chunk)
    box = first(chunk.get("bbox"), chunk.get("bounding_box"), md.get("bbox"), md.get("bounding_box"), md.get("geometry"))
    if isinstance(box, dict):
        vals = [first(box.get("x0"), box.get("left")), first(box.get("y0"), box.get("top")), first(box.get("x1"), box.get("right")), first(box.get("y1"), box.get("bottom"))]
    elif isinstance(box, (list, tuple)) and len(box) >= 4:
        vals = list(box[:4])
    else:
        return None
    try:
        if any(v is None for v in vals): return None
        return dict(zip(("x0", "y0", "x1", "y1"), [float(v) for v in vals]))
    except (ValueError, TypeError):
        return None


def unique(rows: list[dict], keys: tuple[str, ...]) -> list[dict]:
    out = {}
    for row in rows:
        out[tuple(row.get(k) for k in keys)] = row
    if len(out) < len(rows):
        print(f"[WARN] Removed {len(rows)-len(out)} duplicate source row(s), key={keys}")
    return list(out.values())


def transform(doc: dict) -> dict[str, list[dict]]:
    files = doc["files"]
    root = doc["embedded_root"]
    model = str(first(root.get("model"), root.get("embedding_model"), root.get("model_name"), default="unknown"))
    root_dims = first(root.get("dimensions"), root.get("embedding_dimensions"), default=768)
    out = {table: [] for table in TABLES}
    source_to_chunk = {}

    for chunk in doc["chunks"]:
        md = get_metadata(chunk)
        chunk_id = cid(chunk)
        if not chunk_id:
            print("[WARN] Skipping chunk without chunk_id")
            continue
        content = first(chunk.get("content"), chunk.get("text"), chunk.get("page_content"), md.get("content"), default="")
        page = first(chunk.get("page_number"), md.get("page_number"), md.get("page"))
        ctype = str(first(chunk.get("content_type"), md.get("content_type"), md.get("element_type"), default="unknown"))
        vector = first(chunk.get("embedding"), chunk.get("values"), chunk.get("embedding_vector"), md.get("embedding"))
        if not isinstance(vector, list):
            raise ValueError(f"{doc['folder']}: chunk {chunk_id} has no embedding list")
        try: vector = [float(v) for v in vector]
        except (ValueError, TypeError) as exc: raise ValueError(f"Invalid embedding for {chunk_id}") from exc
        dims = int(first(chunk.get("embedding_dimensions"), md.get("embedding_dimensions"), root_dims, len(vector)))
        if len(vector) != dims:
            raise ValueError(f"{chunk_id}: declared dimensions {dims}, actual vector length {len(vector)}")
        if page is None or not content:
            raise ValueError(f"{doc['folder']}: chunk {chunk_id} missing page_number or content")
        source_id = first(chunk.get("source_id"), md.get("source_id"), md.get("element_id"))
        if source_id is not None: source_to_chunk[str(source_id)] = str(chunk_id)
        out["document_chunks"].append({
            "chunk_id": str(chunk_id), "machine_name": str(first(chunk.get("machine_name"), md.get("machine_name"), doc["machine"])),
            "document_name": str(first(chunk.get("document_name"), md.get("document_name"), doc["document"])),
            "page_number": int(page), "content_type": ctype, "source_id": str(source_id) if source_id is not None else None,
            "content": str(content), "embedding_model": str(first(chunk.get("embedding_model"), md.get("embedding_model"), model, default="unknown")),
            "embedding_dimensions": dims, "embedding": vector,
        })
        box = bbox(chunk)
        if box: out["document_chunk_geometry"].append({"chunk_id": str(chunk_id), **box})

        image_id = first(md.get("image_id"), chunk.get("image_id"))
        image_path = first(md.get("image_path"), md.get("asset"), chunk.get("image_path"))
        if (ctype == "image" or image_id or image_path) and image_id and image_path:
            out["document_images"].append({"chunk_id": str(chunk_id), "image_id": str(image_id), "image_path": str(image_path),
                "relationship_status": md.get("relationship_status"), "relationship_method": md.get("relationship_method")})
        table_id = first(md.get("table_id"), chunk.get("table_id"))
        row_number = first(md.get("row_number"), chunk.get("row_number"))
        if ctype == "table_row" and table_id is not None and row_number is not None:
            out["document_table_rows"].append({"chunk_id": str(chunk_id), "table_id": str(table_id), "row_number": int(row_number), "content": str(content)})
        link_id = first(md.get("link_id"), chunk.get("link_id"))
        url = first(md.get("url"), chunk.get("url"))
        if ctype == "link" and link_id is not None and url:
            out["document_links"].append({"chunk_id": str(chunk_id), "link_id": str(link_id), "url": str(url)})

    rel = as_dict(files["relationships.json"])
    for section in records(rel, "sections"):
        sid, title = section.get("section_id"), section.get("title")
        level, page = section.get("level"), section.get("page_number")
        if sid is not None and title is not None and level is not None and page is not None:
            out["document_sections"].append({"section_id": str(sid), "document_name": doc["document"], "title": str(title), "level": int(level), "page_number": int(page),
                "parent_section_id": str(section["parent_section_id"]) if section.get("parent_section_id") is not None else None})
        for element_id in section.get("child_element_ids", []) or []:
            if sid is not None and element_id is not None:
                out["section_elements"].append({"section_id": str(sid), "element_id": str(element_id)})

    
    # Flatten direct and nested relationship records.
    for page in records(rel, "pages"):
        for family in (
            "relationships",
            "image_relationships",
            "table_relationships",
            "link_relationships",
        ):
            for edge in records(page, family):
                # Support direct source/target records.
                source = first(
                    edge.get("source_id"),
                    edge.get("source_element_id"),
                )
                target = first(
                    edge.get("target_id"),
                    edge.get("target_element_id"),
                )

                relation_type = first(
                    edge.get("relation_type"),
                    edge.get("relationship_type"),
                    default=family.removesuffix("_relationships"),
                )
                method = first(
                    edge.get("relationship_method"),
                    edge.get("method"),
                )
                status = edge.get("status")
                evidence = edge.get("evidence")

                # Nested records: element -> related_elements[].
                nested_relations = edge.get("related_elements") or []

                candidates = []
                if source is not None and target is not None:
                    candidates.append(
                        (source, target, relation_type, method, status, evidence)
                    )

                for related in nested_relations:
                    if not isinstance(related, dict):
                        continue

                    nested_target = related.get("element_id")
                    if nested_target is None:
                        continue

                    candidates.append(
                        (
                            edge.get("element_id"),
                            nested_target,
                            first(
                                related.get("relation_type"),
                                relation_type,
                            ),
                            first(
                                related.get("relationship_method"),
                                method,
                            ),
                            related.get("status", status),
                            first(
                                related.get("evidence"),
                                related.get("text_preview"),
                                evidence,
                            ),
                        )
                    )

                for source, target, rel_type, rel_method, rel_status, rel_evidence in candidates:
                    if source is None or target is None:
                        continue

                    identity = f"{source}|{target}|{rel_type}"
                    relationship_id = identity[:256]

                    out["chunk_relationships"].append({
                        "relationship_id": relationship_id,
                        "source_id": str(source),
                        "target_id": str(target),
                        "relation_type": str(rel_type),
                        "evidence": (
                            str(rel_evidence)
                            if rel_evidence is not None
                            else None
                        ),
                        "relationship_method": (
                            str(rel_method)
                            if rel_method is not None
                            else None
                        ),
                        "status": (
                            str(rel_status)
                            if rel_status is not None
                            else None
                        ),
                    })

    key_map = {
        "document_chunks": ("chunk_id", "machine_name", "document_name"),
        "document_chunk_geometry": ("chunk_id",),
        "document_images": ("chunk_id", "image_id"),
        "document_table_rows": ("chunk_id", "table_id", "row_number"),
        "document_links": ("chunk_id", "link_id"),
        "chunk_relationships": ("relationship_id",),
        "document_sections": ("section_id",),
        "section_elements": ("section_id", "element_id"),
    }
    for table, keys in key_map.items():
        out[table] = unique(out[table], keys)
    return out


def schema_for(table: str):
    schemas = {
        "document_chunks": [bigquery.SchemaField("chunk_id","STRING","REQUIRED"), bigquery.SchemaField("machine_name","STRING","REQUIRED"), bigquery.SchemaField("document_name","STRING","REQUIRED"), bigquery.SchemaField("page_number","INT64","REQUIRED"), bigquery.SchemaField("content_type","STRING","REQUIRED"), bigquery.SchemaField("source_id","STRING"), bigquery.SchemaField("content","STRING","REQUIRED"), bigquery.SchemaField("embedding_model","STRING","REQUIRED"), bigquery.SchemaField("embedding_dimensions","INT64","REQUIRED"), bigquery.SchemaField("embedding","FLOAT64",mode="REPEATED")],
        "document_chunk_geometry": [bigquery.SchemaField("chunk_id","STRING","REQUIRED"), bigquery.SchemaField("x0","FLOAT64","REQUIRED"), bigquery.SchemaField("y0","FLOAT64","REQUIRED"), bigquery.SchemaField("x1","FLOAT64","REQUIRED"), bigquery.SchemaField("y1","FLOAT64","REQUIRED")],
        "document_images": [bigquery.SchemaField("chunk_id","STRING","REQUIRED"), bigquery.SchemaField("image_id","STRING","REQUIRED"), bigquery.SchemaField("image_path","STRING","REQUIRED"), bigquery.SchemaField("relationship_status","STRING"), bigquery.SchemaField("relationship_method","STRING")],
        "document_table_rows": [bigquery.SchemaField("chunk_id","STRING","REQUIRED"), bigquery.SchemaField("table_id","STRING","REQUIRED"), bigquery.SchemaField("row_number","INT64","REQUIRED"), bigquery.SchemaField("content","STRING","REQUIRED")],
        "document_links": [bigquery.SchemaField("chunk_id","STRING","REQUIRED"), bigquery.SchemaField("link_id","STRING","REQUIRED"), bigquery.SchemaField("url","STRING","REQUIRED")],
        "chunk_relationships": [bigquery.SchemaField("relationship_id","STRING","REQUIRED"), bigquery.SchemaField("source_id","STRING","REQUIRED"), bigquery.SchemaField("target_id","STRING","REQUIRED"), bigquery.SchemaField("relation_type","STRING","REQUIRED"), bigquery.SchemaField("evidence","STRING"), bigquery.SchemaField("relationship_method","STRING"), bigquery.SchemaField("status","STRING")],
        "document_sections": [bigquery.SchemaField("section_id","STRING","REQUIRED"), bigquery.SchemaField("document_name","STRING","REQUIRED"), bigquery.SchemaField("title","STRING","REQUIRED"), bigquery.SchemaField("level","INT64","REQUIRED"), bigquery.SchemaField("page_number","INT64","REQUIRED"), bigquery.SchemaField("parent_section_id","STRING")],
        "section_elements": [bigquery.SchemaField("section_id","STRING","REQUIRED"), bigquery.SchemaField("element_id","STRING","REQUIRED")],
    }
    return schemas[table]


def merge_rows(client, table: str, rows: list[dict]) -> None:
    target = f"{PROJECT}.{DATASET}.{table}"
    temp = f"{PROJECT}.{DATASET}._tmp_{table}_{uuid.uuid4().hex[:10]}"
    fields = schema_for(table)
    columns = [field.name for field in fields]
    key_map = {
        "document_chunks": ["chunk_id", "machine_name", "document_name"],
        "document_chunk_geometry": ["chunk_id"], "document_images": ["chunk_id", "image_id"],
        "document_table_rows": ["chunk_id", "table_id", "row_number"], "document_links": ["chunk_id", "link_id"],
        "chunk_relationships": ["relationship_id"], "document_sections": ["section_id"],
        "section_elements": ["section_id", "element_id"],
    }
    keys = key_map[table]
    update_cols = [c for c in columns if c not in keys]
    client.load_table_from_json(rows, temp, job_config=bigquery.LoadJobConfig(schema=fields, write_disposition="WRITE_TRUNCATE")).result()
    try:
        on = " AND ".join(f"T.`{k}` = S.`{k}`" for k in keys)
        partition = ", ".join(f"`{k}`" for k in keys)
        updates = "WHEN MATCHED THEN UPDATE SET " + ", ".join(f"T.`{c}` = S.`{c}`" for c in update_cols) if update_cols else ""
        cols = ", ".join(f"`{c}`" for c in columns)
        vals = ", ".join(f"S.`{c}`" for c in columns)
        sql = f"""MERGE `{target}` T
USING (SELECT * EXCEPT(rn) FROM (SELECT *, ROW_NUMBER() OVER (PARTITION BY {partition} ORDER BY {partition}) AS rn FROM `{temp}`) WHERE rn=1) S
ON {on}
{updates}
WHEN NOT MATCHED THEN INSERT ({cols}) VALUES ({vals})"""
        client.query(sql).result()
    finally:
        client.delete_table(temp, not_found_ok=True)


def load_gcs_document_to_bigquery(folder: str, tables: tuple[str, ...] | list[str] = TABLES, *, commit: bool = True) -> dict[str, int]:
    """Load one uploaded document folder; call after all artifacts are in GCS."""
    invalid = set(tables) - set(TABLES)
    if invalid:
        raise ValueError(f"Unsupported tables: {sorted(invalid)}")
    reader = GcsReader()
    doc = reader.read(folder)
    mapped = transform(doc)
    chunk_ids = {row["chunk_id"] for row in mapped["document_chunks"]}
    for table in ("document_chunk_geometry", "document_images", "document_table_rows", "document_links"):
        missing = [row["chunk_id"] for row in mapped[table] if row["chunk_id"] not in chunk_ids]
        if missing:
            raise ValueError(f"{table} contains chunk IDs not present in document_chunks: {missing[:5]}")
    bq = bigquery.Client(project=PROJECT)
    counts = {}
    for table in tables:
        rows = mapped[table]
        counts[table] = len(rows)
        print(f"[TABLE] {table}: {len(rows)} row(s)")
        if commit and rows:
            merge_rows(bq, table, rows)
            print(f"[MERGED] {table}")
        elif commit:
            print(f"[SKIP] {table}: no rows")
        else:
            print(f"[DRY RUN] No writes to {table}")
    return counts


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tables", default="all", help="Comma-separated table names, or 'all'.")
    parser.add_argument("--folder", help="Specific GCS folder, e.g. processed/HSG-2550/<document>/")
    parser.add_argument("--dry-run", action="store_true", help="Preview mappings only (default).")
    parser.add_argument("--commit", action="store_true", help="Write rows to BigQuery.")
    args = parser.parse_args()
    if args.dry_run and args.commit:
        parser.error("Choose either --dry-run or --commit.")
    tables = TABLES if args.tables.strip().lower() == "all" else tuple(t.strip() for t in args.tables.split(",") if t.strip())
    invalid = set(tables) - set(TABLES)
    if not tables or invalid:
        parser.error(f"Unsupported tables: {sorted(invalid)}. Available: {', '.join(TABLES)}")
    reader = GcsReader()
    folders = [args.folder] if args.folder else reader.folders()
    if not folders:
        print("No processed folders with embedded_chunks.json found.")
        return
    for folder in folders:
        folder = folder.strip("/") + "/"
        print(f"\n[DOCUMENT] gs://{BUCKET}/{folder}")
        counts = load_gcs_document_to_bigquery(folder, tables, commit=args.commit)
        if not args.commit:
            for table, count in counts.items():
                print(f"[DRY RUN] {table}: {count} row(s); no writes made")
    if not args.commit:
        print("\nDry run complete. Re-run with --commit after reviewing mappings.")


if __name__ == "__main__":
    main()
