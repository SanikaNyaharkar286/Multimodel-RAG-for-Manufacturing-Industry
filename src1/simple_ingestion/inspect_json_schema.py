from __future__ import annotations
import argparse, json
from collections import Counter, defaultdict
from pathlib import Path

def typ(v):
    if v is None: return "null"
    if isinstance(v, bool): return "boolean"
    if isinstance(v, int): return "integer"
    if isinstance(v, float): return "number"
    if isinstance(v, str): return "string"
    if isinstance(v, list): return "array"
    if isinstance(v, dict): return "object"
    return type(v).__name__

def schema_for(path):
    data = json.loads(path.read_text(encoding="utf-8"))
    stats, presence = defaultdict(lambda: {
        "instances": 0, "nulls": 0, "types": Counter(),
        "examples": set(), "keys": set(), "lengths": [], "item_types": Counter()
    }), Counter()

    def walk(v, p):
        s = stats[p]; s["instances"] += 1; s["types"][typ(v)] += 1
        if v is None:
            s["nulls"] += 1; return
        if isinstance(v, dict):
            s["keys"].update(v.keys())
            for k, child in v.items():
                cp = f"{p}.{k}" if p else k
                presence[cp] += 1
                walk(child, cp)
        elif isinstance(v, list):
            s["lengths"].append(len(v))
            for child in v:
                cp = p + "[]"; stats[p]["item_types"][typ(child)] += 1
                walk(child, cp)
        elif isinstance(v, (str, int, float, bool)):
            s["examples"].add(repr(v)[:120])

    if isinstance(data, list):
        stats["$"]["instances"] = 1
        stats["$"]["types"]["array"] += 1
        stats["$"]["lengths"].append(len(data))
        for v in data:
            stats["$"]["item_types"][typ(v)] += 1
            walk(v, "$[]")
        root_type, root_count = "array", len(data)
    else:
        walk(data, "$")
        root_type, root_count = typ(data), None

    fields = {}
    for p, s in sorted(stats.items()):
        out = {
            "instance_count": s["instances"], "null_count": s["nulls"],
            "types": dict(s["types"]), "object_keys": sorted(s["keys"]),
            "array_item_types": dict(s["item_types"]),
            "examples": sorted(s["examples"])[:5],
        }
        if s["lengths"]:
            ls = s["lengths"]
            out["array_length"] = {
                "observations": len(ls), "min": min(ls), "max": max(ls),
                "average": round(sum(ls)/len(ls), 2)
            }
        if "." in p:
            parent = p.rsplit(".", 1)[0]
            out["parent_instance_count"] = stats[parent]["instances"]
            out["missing_count"] = max(0, stats[parent]["instances"] - presence[p])
        fields[p] = out
    return {"file": str(path), "root_type": root_type,
            "root_record_count": root_count, "fields": fields}

def element_ids(extraction):
    ids = set()
    for page in extraction.get("pages", []) if isinstance(extraction, dict) else []:
        if not isinstance(page, dict): continue
        for family in ("paragraphs", "images", "tables", "links"):
            for row in page.get(family, []) or []:
                if isinstance(row, dict) and row.get("id") is not None:
                    ids.add(str(row["id"]))
    return ids

def main():
    ap = argparse.ArgumentParser(description="Inspect JSON schemas and cross-document IDs.")
    ap.add_argument("--input", required=True, help="Root containing machine/document folders")
    ap.add_argument("--output", default="reports/json_schema_report.json")
    args = ap.parse_args()
    root = Path(args.input).resolve()
    if not root.is_dir(): raise SystemExit(f"Input directory not found: {root}")

    chunks_files = sorted(root.rglob("chunks.json"))
    docs, all_schemas = [], []
    chunk_locations, element_locations = defaultdict(list), defaultdict(list)

    for cp in chunks_files:
        folder = cp.parent
        machine, document = folder.parent.name, folder.name
        rel = str(folder.relative_to(root))
        try:
            chunks = json.loads(cp.read_text(encoding="utf-8"))
            if not isinstance(chunks, list): raise ValueError("chunks.json root must be an array")
            ep, rp = folder/"extraction.json", folder/"relationships.json"
            extraction = json.loads(ep.read_text(encoding="utf-8")) if ep.exists() else {}
            relationships = json.loads(rp.read_text(encoding="utf-8")) if rp.exists() else {}
            eids = element_ids(extraction)
            cids = [str(x["chunk_id"]) for x in chunks if isinstance(x, dict) and x.get("chunk_id") is not None]
            counts = Counter(cids)
            for cid in set(cids): chunk_locations[cid].append({"machine": machine, "document": document, "folder": rel})
            for eid in eids: element_locations[eid].append({"machine": machine, "document": document, "folder": rel})
            source_ids = {
                str(x.get("metadata", {}).get("source_id"))
                for x in chunks if isinstance(x, dict) and isinstance(x.get("metadata"), dict)
                and x["metadata"].get("source_id") is not None
            }
            missing_sources = sorted(source_ids - eids)

            # Endpoint IDs may refer to sections as well as extracted page elements.
            endpoint_ids = []
            for page in relationships.get("pages", []) if isinstance(relationships, dict) else []:
                if not isinstance(page, dict): continue
                for family in ("relationships", "image_relationships", "table_relationships", "link_relationships"):
                    for relrow in page.get(family, []) or []:
                        if not isinstance(relrow, dict): continue
                        for key in ("source_id", "target_id", "element_id"):
                            if relrow.get(key) is not None: endpoint_ids.append(str(relrow[key]))
                        for item in relrow.get("related_elements", []) or []:
                            if isinstance(item, dict) and item.get("element_id") is not None:
                                endpoint_ids.append(str(item["element_id"]))

            docs.append({
                "machine_name": machine, "document_folder": document, "folder": rel,
                "artifacts": {
                    "chunks.json": True, "extraction.json": ep.exists(),
                    "relationships.json": rp.exists(),
                    "manifest.json": (folder/"segmentation"/"manifest.json").exists() or (folder/"manifest.json").exists(),
                    "embedded_chunks.json": (folder/"embedded_chunks.json").exists()
                },
                "validation": {
                    "chunk_count": len(chunks),
                    "missing_chunk_id_count": sum(not isinstance(x, dict) or not x.get("chunk_id") for x in chunks),
                    "duplicate_chunk_ids": [{"id": k, "count": n} for k,n in counts.items() if n > 1],
                    "extracted_element_id_count": len(eids),
                    "distinct_chunk_source_id_count": len(source_ids),
                    "chunk_source_ids_not_in_extraction": missing_sources,
                    "relationship_endpoint_count": len(endpoint_ids),
                    "relationship_endpoints_not_matching_extracted_or_chunk_ids": sorted(set(endpoint_ids)-eids-set(cids))[:100]
                }
            })
        except Exception as e:
            docs.append({"machine_name": machine, "document_folder": document, "folder": rel, "error": str(e)})

    for f in sorted(root.rglob("*.json")):
        if f.name in {"chunks.json", "extraction.json", "relationships.json", "manifest.json", "embedded_chunks.json"}:
            try: all_schemas.append(schema_for(f))
            except Exception as e: all_schemas.append({"file": str(f), "error": str(e)})

    def cross(items):
        return {k: v for k,v in items.items() if len({x["folder"] for x in v}) > 1}
    chunk_collisions, element_reuse = cross(chunk_locations), cross(element_locations)
    report = {
        "scan_root": str(root), "documents_found": len(chunks_files),
        "document_validation": docs,
        "cross_document_chunk_id_collisions": {"count": len(chunk_collisions), "items": chunk_collisions},
        "cross_document_extracted_element_id_reuse": {"count": len(element_reuse), "items": element_reuse},
        "schema_reports": all_schemas,
        "notes": [
            "Nested field missing counts are calculated against instances of the immediate parent object.",
            "Element ID reuse across documents may be expected when IDs are document-scoped.",
            "Unmatched relationship endpoints may refer to sections or other entity types; review before treating them as errors.",
            "No Google Cloud services or BigQuery tables are accessed or modified."
        ]
    }
    out = Path(args.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Scan root: {root}")
    print(f"Document folders with chunks.json: {len(chunks_files)}")
    print(f"Cross-document chunk ID collisions: {len(chunk_collisions)}")
    print(f"Cross-document extracted element ID reuse: {len(element_reuse)}")
    for d in docs:
        if "error" in d: print(f"ERROR {d['folder']}: {d['error']}"); continue
        v = d["validation"]
        print(f"- {d['machine_name']} / {d['document_folder']}: {v['chunk_count']} chunks; "
              f"{len(v['duplicate_chunk_ids'])} local duplicate IDs; "
              f"{len(v['chunk_source_ids_not_in_extraction'])} source IDs not in extraction")
    print(f"Report written to: {out}")

if __name__ == "__main__":
    main()
