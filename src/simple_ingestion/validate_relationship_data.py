
import json
from pathlib import Path
from collections import Counter

# Update these two paths to point to your actual JSON files.
CHUNKS_PATH = Path(r"C:\Users\Sonakshi\OneDrive\Desktop\Multimodel-RAG-for-Manufacturing-Industry\data\processed\HSG-2550\5 - Haas Surface Grinder - Operation\chunks.json")
RELATIONSHIPS_PATH = Path(r"C:\Users\Sonakshi\OneDrive\Desktop\Multimodel-RAG-for-Manufacturing-Industry\data\processed\HSG-2550\5 - Haas Surface Grinder - Operation\relationships.json")

def load_json(path):
    if not path.exists():
        raise FileNotFoundError(
            f"File not found: {path.resolve()}\n"
            "Update the paths at the top of this script."
        )

    with path.open("r", encoding="utf-8") as file:
        return json.load(file)


def as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def validate():
    chunks = load_json(CHUNKS_PATH)
    relationship_data = load_json(RELATIONSHIPS_PATH)

    errors = []
    warnings = []

    if not isinstance(chunks, list):
        raise TypeError("chunks.json must contain a JSON list.")

    # --------------------------------------------------
    # 1. Validate chunk IDs and metadata
    # --------------------------------------------------
    chunk_ids = [
        str(c["chunk_id"])
        for c in chunks
        if isinstance(c, dict) and c.get("chunk_id") is not None
    ]

    duplicate_ids = [
        chunk_id
        for chunk_id, count in Counter(chunk_ids).items()
        if count > 1
    ]

    if duplicate_ids:
        errors.append(f"Duplicate chunk IDs: {duplicate_ids[:10]}")

    chunk_id_set = set(chunk_ids)
    source_to_chunks = {}

    for chunk in chunks:
        if not isinstance(chunk, dict):
            errors.append("A chunk record is not a dictionary.")
            continue

        chunk_id = chunk.get("chunk_id")
        metadata = chunk.get("metadata")

        if not chunk_id:
            errors.append("A chunk has no chunk_id.")
            continue

        if not isinstance(chunk.get("content"), str):
            errors.append(f"{chunk_id}: content is not a string.")

        if not isinstance(metadata, dict):
            errors.append(f"{chunk_id}: metadata is missing or invalid.")
            continue

        source_id = metadata.get("source_id")
        if source_id is not None:
            source_to_chunks.setdefault(str(source_id), []).append(
                str(chunk_id)
            )

    # --------------------------------------------------
    # 2. Validate related_chunk_ids in metadata
    # --------------------------------------------------
    missing_related = []

    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue

        metadata = chunk.get("metadata") or {}
        if not isinstance(metadata, dict):
            continue

        related_ids = as_list(metadata.get("related_chunk_ids"))

        for related_id in related_ids:
            if not isinstance(related_id, str):
                errors.append(
                    f"{chunk.get('chunk_id')}: related chunk ID "
                    "is not a string."
                )
            elif related_id not in chunk_id_set:
                missing_related.append(
                    (chunk.get("chunk_id"), related_id)
                )

    if missing_related:
        warnings.append(
            f"{len(missing_related)} related_chunk_ids reference "
            "unknown chunk IDs. Examples: {missing_related[:5]}"
        )

    # --------------------------------------------------
    # 3. Validate general relationship edges
    # --------------------------------------------------
    pages = relationship_data.get("pages", [])
    sections = relationship_data.get("sections", [])

    section_ids = {
        str(section["section_id"])
        for section in sections
        if isinstance(section, dict)
        and section.get("section_id") is not None
    }

    known_source_ids = set(source_to_chunks) | section_ids

    all_edges = []
    for page in pages:
        all_edges.extend(page.get("relationships", []) or [])

    edge_statuses = Counter()
    unresolved_sources = []
    unresolved_targets = []
    malformed_edges = []
    edge_signatures = []

    allowed_statuses = {
        "candidate",
        "confirmed",
        "validated",
        "accepted",
        "rejected",
    }

    for index, edge in enumerate(all_edges):
        if not isinstance(edge, dict):
            malformed_edges.append(index)
            continue

        source = edge.get("source_id")
        target = edge.get("target_id")
        status = edge.get("status")

        if not source or not target:
            malformed_edges.append(index)
            continue

        edge_statuses[str(status)] += 1

        if status not in allowed_statuses:
            warnings.append(
                f"Edge {index} has unexpected status: {status!r}"
            )

        if str(source) not in known_source_ids:
            unresolved_sources.append(str(source))

        if str(target) not in known_source_ids:
            unresolved_targets.append(str(target))

        edge_signatures.append(
            (str(source), str(target), edge.get("relation_type"))
        )

    if malformed_edges:
        errors.append(
            f"{len(malformed_edges)} malformed relationship edges."
        )

    if unresolved_sources:
        warnings.append(
            "Unresolved edge source IDs: "
            f"{Counter(unresolved_sources).most_common(10)}"
        )

    if unresolved_targets:
        warnings.append(
            "Unresolved edge target IDs: "
            f"{Counter(unresolved_targets).most_common(10)}"
        )

    duplicate_edges = [
        edge
        for edge, count in Counter(edge_signatures).items()
        if count > 1
    ]

    if duplicate_edges:
        warnings.append(
            f"{len(duplicate_edges)} duplicate relationship signatures."
        )

    # --------------------------------------------------
    # 4. Validate image, table and link relationship records
    # --------------------------------------------------
    type_counts = Counter()
    missing_link_urls = []

    for page in pages:
        for key in (
            "image_relationships",
            "table_relationships",
            "link_relationships",
        ):
            records = page.get(key, []) or []
            type_counts[key] += len(records)

            for record in records:
                if not isinstance(record, dict):
                    errors.append(f"Invalid record in {key}.")
                    continue

                if key == "link_relationships" and not record.get("url"):
                    missing_link_urls.append(record.get("element_id"))

    if missing_link_urls:
        warnings.append(
            f"{len(missing_link_urls)} link relationship records "
            f"have no URL. Examples: {missing_link_urls[:5]}"
        )

    # --------------------------------------------------
    # 5. Report
    # --------------------------------------------------
    print("\n========== DATA VALIDATION REPORT ==========")
    print(f"Chunks loaded: {len(chunks)}")
    print(f"Unique chunk IDs: {len(chunk_id_set)}")
    print(f"Source IDs mapped to chunks: {len(source_to_chunks)}")
    print(f"Sections: {len(section_ids)}")
    print(f"General relationship edges: {len(all_edges)}")
    print(f"Relationship statuses: {dict(edge_statuses)}")
    print(f"Image relationship records: {type_counts['image_relationships']}")
    print(f"Table relationship records: {type_counts['table_relationships']}")
    print(f"Link relationship records: {type_counts['link_relationships']}")

    print("\n--- ERRORS ---")
    if errors:
        for item in errors:
            print(f"ERROR: {item}")
    else:
        print("No structural errors found.")

    print("\n--- WARNINGS ---")
    if warnings:
        for item in warnings:
            print(f"WARNING: {item}")
    else:
        print("No warnings found.")

    print("\nValidation complete.")
    print("Warnings require review; they do not automatically mean the data is wrong.")


if __name__ == "__main__":
    validate()
