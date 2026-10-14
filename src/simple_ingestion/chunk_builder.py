import json
import re
from pathlib import Path


def clean_text(text):
    """Normalize whitespace without removing meaningful line breaks."""
    text = str(text or "").replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


def _page_relationship_map(relationships_data):
    """Index candidate relationships by (page_number, source element id)."""
    indexed = {}
    if not relationships_data:
        return indexed
    for page in relationships_data.get("pages", []):
        page_number = page.get("page_number")
        for key in ("image_relationships", "table_relationships", "link_relationships"):
            for record in page.get(key, []):
                indexed[(page_number, record.get("element_id"))] = record
    return indexed


def build_table_chunks(table, machine_name, document_name, page_number):
    """Convert a table into row-level chunks while preserving column headers."""
    rows = table.get("rows", [])
    if len(rows) < 2:
        return []
    headers = [clean_text(v) if v is not None else "" for v in rows[0]]
    chunks = []
    table_id = table.get("id", table.get("table_id", "table"))
    for row_number, row in enumerate(rows[1:], start=1):
        values = [clean_text(v) if v is not None else "" for v in row]
        if not any(values):
            continue
        fields = []
        if table.get("title"):
            fields.append(f"Table: {clean_text(table['title'])}")
        for index, value in enumerate(values):
            if value:
                header = headers[index] if index < len(headers) and headers[index] else f"Column {index + 1}"
                fields.append(f"{header}: {value}")
        chunks.append({
            "chunk_id": f"{document_name}_p{page_number}_{table_id}_r{row_number}",
            "content": "\n".join(fields),
            "metadata": {
                "machine_name": machine_name,
                "document_name": document_name,
                "page_number": page_number,
                "content_type": "table_row",
                "source_id": table_id,
                "table_id": table_id,
                "row_number": row_number,
                "related_chunk_ids": [],
                "associated_images": [],
            },
        })
    return chunks


def build_chunks_from_extraction(extracted_data, machine_name, document_name):
    chunks = []
    for page in extracted_data.get("pages", []):
        for table in page.get("tables", []):
            chunks.extend(build_table_chunks(table, machine_name, document_name, page.get("page_number")))
    return chunks


def build_paragraph_chunks(extracted_data, machine_name, document_name):
    chunks = []
    for page in extracted_data.get("pages", []):
        page_number = page.get("page_number")
        for paragraph in page.get("paragraphs", []):
            content = clean_text(paragraph.get("text", ""))
            if not content or content in {".", "-", "•"}:
                continue
            source_id = paragraph.get("id", paragraph.get("block_id", "paragraph"))
            chunks.append({
                "chunk_id": f"{document_name}_p{page_number}_{source_id}",
                "content": content,
                "metadata": {
                    "machine_name": machine_name,
                    "document_name": document_name,
                    "page_number": page_number,
                    "content_type": "paragraph",
                    "source_id": source_id,
                    "bbox": paragraph.get("bbox"),
                    "related_chunk_ids": [],
                    "associated_images": [],
                },
            })
    return chunks


def build_image_chunks(extracted_data, machine_name, document_name, relationships_data=None):
    """Build image chunks and preserve all proximity-based candidate links."""
    chunks = []
    relationship_map = _page_relationship_map(relationships_data)
    for page in extracted_data.get("pages", []):
        page_number = page.get("page_number")
        paragraphs = page.get("paragraphs", [])
        tables = page.get("tables", [])
        for image_data in page.get("images", []):
            image_id = image_data.get("id", image_data.get("image_id", "unknown_image"))
            image_path = image_data.get("asset", image_data.get("image_path", ""))
            image_bbox = image_data.get("bbox", [])
            relation = relationship_map.get((page_number, image_id), {})
            related_elements = relation.get("related_elements", [])

            # Keep every paragraph/table candidate, not just the nearest paragraph.
            related_ids = []
            related_paragraphs = []
            related_tables = []
            for candidate in related_elements:
                item = {
                    "element_id": candidate.get("element_id"),
                    "element_type": candidate.get("element_type"),
                    "distance": candidate.get("distance"),
                    "relationship_method": candidate.get("relationship_method", "same_page_proximity"),
                    "status": "candidate",
                }
                if candidate.get("element_type") in ("paragraph", "heading"):
                    related_paragraphs.append(item)
                    related_ids.append(candidate.get("element_id"))
                elif candidate.get("element_type") == "table":
                    related_tables.append(item)
                    related_ids.append(candidate.get("element_id"))

            related_text = []
            for paragraph in paragraphs:
                if paragraph.get("id", paragraph.get("block_id")) in related_ids:
                    text = clean_text(paragraph.get("text", ""))
                    if text:
                        related_text.append(text)
            nearby_text = "\n".join(related_text)
            if not nearby_text:
                # Relationship previews can still provide useful context if IDs do not match.
                nearby_text = "\n".join(c.get("text_preview", "") for c in related_elements if c.get("text_preview"))

            content = (
                f"Machine: {machine_name}\nDocument: {document_name}\nPage: {page_number}\n"
                f"Image ID: {image_id}\nRelated text candidates: {nearby_text or 'No nearby text available'}"
            )
            chunks.append({
                "chunk_id": f"{document_name}_p{page_number}_{image_id}",
                "content": content,
                "metadata": {
                    "machine_name": machine_name,
                    "document_name": document_name,
                    "page_number": page_number,
                    "content_type": "image",
                    "image_id": image_id,
                    "source_id": image_id,
                    "image_path": image_path,
                    "bbox": image_bbox,
                    "related_element_ids": related_ids,
                    "related_elements": related_paragraphs + related_tables,
                    "relationship_status": relation.get("status", "no_section_relationship"),
                    "relationship_method": relation.get("relationship_method", "document_hierarchy"),
                    "related_chunk_ids": [],
                    "associated_images": [],
                },
            })
    return chunks


def build_link_chunks(extracted_data, machine_name, document_name):
    chunks = []
    for page in extracted_data.get("pages", []):
        page_number = page.get("page_number")
        for link in page.get("links", []):
            link_id = link.get("id", link.get("link_id", "unknown_link"))
            url = str(link.get("url", link.get("target_uri", ""))).strip()
            if not url:
                continue
            chunks.append({
                "chunk_id": f"{document_name}_p{page_number}_{link_id}",
                "content": url,
                "metadata": {
                    "machine_name": machine_name,
                    "document_name": document_name,
                    "page_number": page_number,
                    "content_type": "link",
                    "source_id": link_id,
                    "link_id": link_id,
                    "url": url,
                    "bbox": link.get("bbox"),
                    "related_chunk_ids": [],
                    "associated_images": [],
                },
            })
    return chunks


def integrate_chunk_relationships(chunks, relationships_data=None):
    """Copy relationship edges into chunks and resolve their target chunk IDs."""
    source_to_chunks = {}
    for chunk in chunks:
        metadata = chunk.setdefault("metadata", {})
        source_id = metadata.get("source_id") or metadata.get("table_id") or metadata.get("image_id") or metadata.get("link_id")
        if source_id:
            source_to_chunks.setdefault((metadata.get("page_number"), source_id), []).append(chunk["chunk_id"])

    chunk_by_id = {chunk["chunk_id"]: chunk for chunk in chunks}
    for page in (relationships_data or {}).get("pages", []):
        page_number = page.get("page_number")
        for edge in page.get("relationships", []):
            source_id = edge.get("source_id")
            target_id = edge.get("target_id")
            if not source_id or not target_id:
                continue
            source_chunks = source_to_chunks.get((page_number, source_id), [])
            target_chunks = source_to_chunks.get((page_number, target_id), [])
            relation = {
                "source_id": source_id,
                "target_id": target_id,
                "relation_type": edge.get("relation_type"),
                "evidence": edge.get("evidence"),
                "status": edge.get("status"),
                "relationship_method": edge.get("relationship_method"),
            }
            for chunk_id in source_chunks:
                metadata = chunk_by_id[chunk_id]["metadata"]
                outgoing = {**relation, "direction": "outgoing"}
                if outgoing not in metadata.setdefault("relationships", []):
                    metadata["relationships"].append(outgoing)
                for related_id in target_chunks:
                    if related_id != chunk_id and related_id not in metadata["related_chunk_ids"]:
                        metadata["related_chunk_ids"].append(related_id)

            for chunk_id in target_chunks:
                metadata = chunk_by_id[chunk_id]["metadata"]
                incoming = {**relation, "direction": "incoming"}
                if incoming not in metadata.setdefault("relationships", []):
                    metadata["relationships"].append(incoming)
                for related_id in source_chunks:
                    if related_id != chunk_id and related_id not in metadata["related_chunk_ids"]:
                        metadata["related_chunk_ids"].append(related_id)

    for image in chunks:
        image_meta = image["metadata"]
        if image_meta.get("content_type") != "image":
            continue
        image_ref = {
            "image_id": image_meta.get("image_id"),
            "chunk_id": image["chunk_id"],
            "image_path": image_meta.get("image_path", ""),
            "page_number": image_meta.get("page_number"),
            "relationship_method": image_meta.get("relationship_method"),
            "status": image_meta.get("relationship_status", "candidate"),
        }
        for related_id in image_meta.get("related_chunk_ids", []):
            target = chunk_by_id[related_id]
            associated = target["metadata"].setdefault("associated_images", [])
            if not any(item.get("chunk_id") == image["chunk_id"] for item in associated):
                associated.append(image_ref)
    return chunks


def build_all_chunks(extracted_data, machine_name, document_name, relationships_data=None):
    """Build text, table-row and image chunks, then integrate candidate links."""
    chunks = []
    chunks.extend(build_paragraph_chunks(extracted_data, machine_name, document_name))
    chunks.extend(build_chunks_from_extraction(extracted_data, machine_name, document_name))
    chunks.extend(build_image_chunks(extracted_data, machine_name, document_name, relationships_data))
    chunks.extend(build_link_chunks(extracted_data, machine_name, document_name))
    return integrate_chunk_relationships(chunks, relationships_data)


def save_chunks(chunks, output_path):
    """Save chunks as UTF-8 JSON with portable asset paths."""
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    for chunk in chunks:
        metadata = chunk.get("metadata", {})
        for path_key in ("image_path",):
            if metadata.get(path_key):
                metadata[path_key] = metadata[path_key].replace("\\", "/")
        for image in metadata.get("associated_images", []):
            if image.get("image_path"):
                image["image_path"] = image["image_path"].replace("\\", "/")
    output_path.write_text(json.dumps(chunks, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Saved {len(chunks)} chunks to: {output_path}")
