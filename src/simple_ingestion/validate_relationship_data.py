"""Read-only validator for extraction.json, relationships.json, and chunks.json.

Usage:
  python validate_relationship_data.py extraction.json relationships.json chunks.json
"""
import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

COLLECTIONS = (("paragraphs", "paragraph"), ("tables", "table"), ("images", "image"), ("links", "link"))
FAMILIES = ("image_relationships", "table_relationships", "link_relationships")


def load(path):
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"File not found: {path}")
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("extraction", type=Path)
    parser.add_argument("relationships", type=Path)
    parser.add_argument("chunks", type=Path)
    args = parser.parse_args()
    extraction, rels, chunks = load(args.extraction), load(args.relationships), load(args.chunks)
    if not isinstance(extraction, dict) or not isinstance(extraction.get("pages"), list):
        raise TypeError("extraction.json must be an object with a pages list")
    if not isinstance(rels, dict) or not isinstance(rels.get("pages"), list):
        raise TypeError("relationships.json must be an object with a pages list")
    if not isinstance(chunks, list):
        raise TypeError("chunks.json must contain a JSON list")

    errors, warnings = [], []
    elements = defaultdict(list)
    bad_extracted = []
    for page in extraction["pages"]:
        for collection, kind in COLLECTIONS:
            for item in page.get(collection, []) or []:
                if not isinstance(item, dict) or item.get("id") is None:
                    bad_extracted.append((page.get("page_number"), collection))
                    continue
                elements[str(item["id"])].append((page.get("page_number"), kind, item))
    if bad_extracted:
        errors.append(f"{len(bad_extracted)} extracted records have no usable id; examples: {bad_extracted[:5]}")

    sections = rels.get("sections", []) or []
    section_ids = set()
    for section in sections:
        if not isinstance(section, dict) or section.get("section_id") is None:
            errors.append("A section record is malformed or missing section_id")
            continue
        sid = str(section["section_id"])
        if sid in section_ids:
            errors.append(f"Duplicate section_id: {sid}")
        section_ids.add(sid)
        if sid not in elements:
            warnings.append(f"Section id {sid!r} does not match an extracted element id")
    valid_nodes = set(elements) | section_ids

    chunk_ids, source_to_chunks = [], defaultdict(list)
    chunks_missing_source = []
    for i, chunk in enumerate(chunks):
        if not isinstance(chunk, dict):
            errors.append(f"Chunk record {i} is not an object")
            continue
        cid = chunk.get("chunk_id")
        if cid is None or str(cid).strip() == "":
            errors.append(f"Chunk record {i} has no chunk_id")
            continue
        cid = str(cid)
        chunk_ids.append(cid)
        if not isinstance(chunk.get("content"), str):
            errors.append(f"Chunk {cid!r} content is not a string")
        metadata = chunk.get("metadata")
        if not isinstance(metadata, dict):
            errors.append(f"Chunk {cid!r} metadata is missing or invalid")
            continue
        source = next((metadata.get(k) for k in ("source_id", "element_id", "table_id", "image_id", "link_id") if metadata.get(k) is not None), None)
        if source is None:
            chunks_missing_source.append(cid)
        else:
            source_to_chunks[str(source)].append(cid)
        for related_id in metadata.get("related_chunk_ids", []) or []:
            if not isinstance(related_id, str):
                errors.append(f"Chunk {cid!r} has a non-string related_chunk_id")
            elif related_id not in {str(c.get("chunk_id")) for c in chunks if isinstance(c, dict) and c.get("chunk_id") is not None}:
                warnings.append(f"Chunk {cid!r} references unknown related_chunk_id {related_id!r}")
    duplicates = [x for x, n in Counter(chunk_ids).items() if n > 1]
    if duplicates:
        errors.append(f"Duplicate chunk IDs: {duplicates[:10]}")
    if chunks_missing_source:
        warnings.append(f"{len(chunks_missing_source)} chunks have no recognized source ID; examples: {chunks_missing_source[:10]}")

    all_edges, edge_signatures, edge_statuses = [], [], Counter()
    unresolved_sources, unresolved_targets, malformed_edges = [], [], []
    allowed_statuses = {"candidate", "confirmed", "validated", "accepted", "rejected"}
    for page in rels["pages"]:
        if not isinstance(page, dict):
            errors.append("A relationship page record is not an object")
            continue
        pnum = page.get("page_number")
        for edge in page.get("relationships", []) or []:
            all_edges.append(edge)
            if not isinstance(edge, dict) or edge.get("source_id") is None or edge.get("target_id") is None:
                malformed_edges.append((pnum, edge))
                continue
            source, target = str(edge["source_id"]), str(edge["target_id"])
            edge_statuses[str(edge.get("status"))] += 1
            if edge.get("status") not in allowed_statuses:
                warnings.append(f"Edge {source!r}->{target!r} has unexpected status {edge.get('status')!r}")
            if source not in valid_nodes:
                unresolved_sources.append((pnum, source))
            if target not in valid_nodes:
                unresolved_targets.append((pnum, target))
            edge_signatures.append((source, target, edge.get("relation_type")))
    if malformed_edges:
        errors.append(f"{len(malformed_edges)} malformed general relationship edges; examples: {malformed_edges[:5]}")
    if unresolved_sources:
        warnings.append(f"{len(unresolved_sources)} unresolved edge source endpoints; examples: {unresolved_sources[:10]}")
    if unresolved_targets:
        warnings.append(f"{len(unresolved_targets)} unresolved edge target endpoints; examples: {unresolved_targets[:10]}")
    dup_edges = [sig for sig, n in Counter(edge_signatures).items() if n > 1]
    if dup_edges:
        warnings.append(f"{len(dup_edges)} duplicate source-target-relation signatures; examples: {dup_edges[:10]}")

    family_counts = Counter()
    malformed_family, unresolved_nested, missing_urls = [], [], []
    for page in rels["pages"]:
        if not isinstance(page, dict):
            continue
        pnum = page.get("page_number")
        for family in FAMILIES:
            records = page.get(family, []) or []
            family_counts[family] += len(records)
            for i, record in enumerate(records):
                if not isinstance(record, dict) or record.get("element_id") is None:
                    malformed_family.append((pnum, family, i, "missing/invalid element_id"))
                    continue
                eid = str(record["element_id"])
                if eid not in elements:
                    warnings.append(f"{family} element_id {eid!r} is absent from extraction.json")
                if family == "link_relationships" and not record.get("url"):
                    missing_urls.append((pnum, eid))
                for j, related in enumerate(record.get("related_elements", []) or []):
                    if not isinstance(related, dict) or related.get("element_id") is None:
                        malformed_family.append((pnum, family, i, f"invalid related_elements[{j}]"))
                    elif str(related["element_id"]) not in valid_nodes:
                        unresolved_nested.append((pnum, family, eid, str(related["element_id"])))
    if malformed_family:
        errors.append(f"{len(malformed_family)} malformed category relationship records; examples: {malformed_family[:10]}")
    if unresolved_nested:
        warnings.append(f"{len(unresolved_nested)} unresolved nested related_elements endpoints; examples: {unresolved_nested[:10]}")
    if missing_urls:
        warnings.append(f"{len(missing_urls)} link relationship records have no URL; examples: {missing_urls[:10]}")

    missing_children, missing_parents = [], []
    for section in sections:
        if not isinstance(section, dict) or section.get("section_id") is None:
            continue
        sid = str(section["section_id"])
        for child in section.get("child_element_ids", []) or []:
            if str(child) not in elements:
                missing_children.append((sid, str(child)))
        parent = section.get("parent_section_id")
        if parent is not None and str(parent) not in section_ids:
            missing_parents.append((sid, str(parent)))
    if missing_children:
        warnings.append(f"{len(missing_children)} unresolved section child references; examples: {missing_children[:10]}")
    if missing_parents:
        warnings.append(f"{len(missing_parents)} unresolved parent section references; examples: {missing_parents[:10]}")

    print("\n========== RELATIONSHIP VALIDATION REPORT ==========")
    print(f"Extracted element IDs: {len(elements)}")
    print(f"Sections: {len(section_ids)}")
    print(f"Chunks loaded: {len(chunks)}")
    print(f"Unique chunk IDs: {len(set(chunk_ids))}")
    print(f"Source IDs mapped to chunks: {len(source_to_chunks)}")
    print(f"Elements mapping to multiple chunks: {sum(len(v) > 1 for v in source_to_chunks.values())}")
    print(f"General relationship edges: {len(all_edges)}")
    print(f"Relationship statuses: {dict(edge_statuses)}")
    for family in FAMILIES:
        print(f"{family}: {family_counts[family]}")
    print("\n--- ERRORS ---")
    print("\n".join(f"ERROR: {e}" for e in errors) if errors else "No structural errors found.")
    print("\n--- WARNINGS ---")
    print("\n".join(f"WARNING: {w}" for w in warnings) if warnings else "No warnings found.")
    print("\nValidation complete. Warnings require review; they are not automatically proof of bad data.")
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
