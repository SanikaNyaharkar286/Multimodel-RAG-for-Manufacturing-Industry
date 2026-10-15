import json
from google.cloud import storage
from config import GCP_PROJECT_ID, GCS_BUCKET_NAME

DOCS = [
    "processed/HSG-2550/5 - Haas Surface Grinder - Operation/",
    "processed/HSG-2550/7 - Haas Surface Grinder - Troubleshooting/",
]

ARTIFACTS = (
    "embedded_chunks.json",
    "relationships.json",
    "extraction.json",
)

client = storage.Client(project=GCP_PROJECT_ID)
bucket = client.bucket(GCS_BUCKET_NAME)


def read_json(folder, name, required=False):
    blob = bucket.blob(folder + name)
    if not blob.exists():
        if required:
            raise FileNotFoundError(f"Missing gs://{GCS_BUCKET_NAME}/{folder}{name}")
        return {}
    return json.loads(blob.download_as_text(encoding="utf-8"))


def as_dict(value):
    return value if isinstance(value, dict) else {}


def records(data, *keys):
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def collect_ids(value, result):
    """Collect explicit ID-like fields from extracted JSON."""
    if isinstance(value, dict):
        for key, item in value.items():
            if key.lower() in {
                "id", "chunk_id", "source_id", "element_id",
                "image_id", "link_id", "table_id", "section_id",
                "target_id",
            } and isinstance(item, (str, int)):
                result.add(str(item))
            collect_ids(item, result)
    elif isinstance(value, list):
        for item in value:
            collect_ids(item, result)


def main():
    for folder in DOCS:
        print("\n" + "=" * 78)
        print("DOCUMENT:", folder)
        print("=" * 78)

        embedded = read_json(folder, "embedded_chunks.json", required=True)
        relationships = read_json(folder, "relationships.json")
        extraction = read_json(folder, "extraction.json")

        chunks = records(embedded, "chunks", "embedded_chunks", "items")
        if not chunks:
            print("[FAIL] No chunks found in embedded_chunks.json")
            continue

        chunk_ids = set()
        known_ids = set()
        image_records = []

        for chunk in chunks:
            md = as_dict(chunk.get("metadata"))
            chunk_id = chunk.get("chunk_id") or chunk.get("id") or md.get("chunk_id")
            if chunk_id is not None:
                chunk_ids.add(str(chunk_id))
                known_ids.add(str(chunk_id))

            for key in ("source_id", "element_id", "image_id", "link_id", "table_id"):
                value = chunk.get(key) or md.get(key)
                if value is not None:
                    known_ids.add(str(value))

            image_id = chunk.get("image_id") or md.get("image_id")
            image_path = (
                chunk.get("image_path")
                or md.get("image_path")
                or md.get("asset")
            )
            content_type = (
                chunk.get("content_type")
                or md.get("content_type")
                or md.get("element_type")
            )
            if image_id or image_path or content_type == "image":
                image_records.append({
                    "chunk_id": str(chunk_id) if chunk_id is not None else None,
                    "image_id": str(image_id) if image_id is not None else None,
                    "path": str(image_path) if image_path else None,
                })

        collect_ids(extraction, known_ids)
        collect_ids(relationships, known_ids)

        print(f"Chunks found: {len(chunks)}")
        print(f"Unique chunk IDs: {len(chunk_ids)}")
        if len(chunk_ids) != len(chunks):
            print("[WARN] Some chunks have missing or duplicate IDs")
        else:
            print("[PASS] Every chunk has a unique ID")

        # Validate geometry/image/table/link references as present in chunk data.
        missing_chunk_refs = []
        for rec in image_records:
            if rec["chunk_id"] is None or rec["chunk_id"] not in chunk_ids:
                missing_chunk_refs.append(rec)

        if missing_chunk_refs:
            print(f"[WARN] Image-like records with missing chunk references: {len(missing_chunk_refs)}")
            for item in missing_chunk_refs[:10]:
                print(" ", item)
        else:
            print("[PASS] All image-like records refer to known chunk IDs")

        # Check every relationship edge in each relationship family.
        edges_checked = 0
        unresolved = []
        rel_pages = records(relationships, "pages")

        for page in rel_pages:
            for family in (
                "relationships",
                "image_relationships",
                "table_relationships",
                "link_relationships",
            ):
                for edge in records(page, family):
                    source = edge.get("source_id")
                    target = edge.get("target_id")
                    if source is None or target is None:
                        unresolved.append((family, source, target, "missing endpoint"))
                        continue
                    edges_checked += 1
                    source, target = str(source), str(target)
                    if source not in known_ids or target not in known_ids:
                        unresolved.append((
                            family, source, target,
                            "source unknown" if source not in known_ids else "target unknown"
                        ))

        print(f"Relationship edges checked: {edges_checked}")
        if unresolved:
            print(f"[WARN] Relationship endpoints not found in collected IDs: {len(unresolved)}")
            for item in unresolved[:15]:
                print(" ", item)
            print("Note: some endpoints may be valid IDs stored in a different artifact.")
        else:
            print("[PASS] All checked relationship endpoints resolve to collected IDs")

        # List existing objects and test plausible paths for each image.
        objects = {
            blob.name
            for blob in client.list_blobs(bucket, prefix=folder)
        }
        image_objects = [
            name for name in objects
            if "/images/" in name.lower()
            or name.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"))
        ]
        print(f"Objects under document prefix: {len(objects)}")
        print(f"Image-like GCS objects under prefix: {len(image_objects)}")
        print(f"Image-like chunk records: {len(image_records)}")

        unresolved_images = []
        no_path = 0
        for rec in image_records:
            path = rec["path"]
            if not path:
                no_path += 1
                unresolved_images.append((rec, "missing image path"))
                continue

            path = path.replace("\\", "/")
            candidates = set()

            if path.startswith("gs://"):
                remainder = path[5:]
                if "/" in remainder:
                    path_bucket, object_name = remainder.split("/", 1)
                    if path_bucket == GCS_BUCKET_NAME:
                        candidates.add(object_name)
            else:
                candidates.add(path.lstrip("/"))
                candidates.add(folder + path.lstrip("/"))
                candidates.add(folder + "assets/images/" + path.split("/")[-1])
                candidates.add(folder + "images/" + path.split("/")[-1])

            if not candidates.intersection(objects):
                unresolved_images.append((rec, "no matching object under document prefix"))

        if unresolved_images:
            print(f"[WARN] Image paths not resolved by candidate matching: {len(unresolved_images)}")
            print(f"[INFO] Image records without paths: {no_path}")
            for item in unresolved_images[:15]:
                print(" ", item)
        else:
            print("[PASS] Every image-like record matched a candidate GCS object")

        if image_records and not image_objects:
            print("[WARN] Image records exist, but no image-like GCS objects were found under this prefix.")

    print("\nRead-only audit finished. No BigQuery writes were performed.")


if __name__ == "__main__":
    main()
