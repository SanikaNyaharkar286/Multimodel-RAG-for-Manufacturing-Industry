#Converts extracted PDF content into searchable chunks 
# (text, tables, images, and links) and connects related information.

import json
import re
import uuid
from pathlib import Path


def make_chunk_uuid(machine_name, document_name, chunk_id):
    """Return a stable UUID for this machine/document/chunk identity."""
    identity = f"manufacturing-rag|{machine_name}|{document_name}|{chunk_id}"
    return str(uuid.uuid5(uuid.NAMESPACE_URL, identity))


def clean_text(text):
    """Normalize whitespace without removing meaningful line breaks."""
    text = str(text or "").replace("\u00a0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


def _page_relationship_map(relationships_data):
    """Index image/table/link relationship records by page and source element ID."""
    indexed = {}
    if not relationships_data:
        return indexed
    for page in relationships_data.get("pages", []):
        page_number = page.get("page_number")
        for key in ("image_relationships", "table_relationships", "link_relationships"):
            for record in page.get(key, []) or []:
                indexed[(page_number, record.get("element_id"))] = record
    return indexed


def _element_map(extracted_data):
    indexed = {}
    for page in extracted_data.get("pages", []):
        page_number = page.get("page_number")
        for collection in ("paragraphs", "tables", "images", "links"):
            for item in page.get(collection, []) or []:
                element_id = item.get("id", item.get("block_id", item.get("image_id", item.get("link_id"))))
                if element_id:
                    indexed[(page_number, element_id)] = item
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
            "chunk_uuid": make_chunk_uuid(
                machine_name, document_name,
                f"{document_name}_p{page_number}_{table_id}_r{row_number}",
            ),
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
                "relationships": [],
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
                "chunk_uuid": make_chunk_uuid(
                    machine_name, document_name,
                    f"{document_name}_p{page_number}_{source_id}",
                ),
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
                    "relationships": [],
                    "section_id": None,
                    "section_title": None,
                },
            })
    return chunks


def build_image_chunks(extracted_data, machine_name, document_name, relationships_data=None):
    """Create image chunks and include all proximity/hierarchy candidate metadata."""
    chunks = []
    relationship_map = _page_relationship_map(relationships_data)
    element_map = _element_map(extracted_data)
    for page in extracted_data.get("pages", []):
        page_number = page.get("page_number")
        for image_data in page.get("images", []):
            image_id = image_data.get("id", image_data.get("image_id", "unknown_image"))
            image_path = image_data.get("asset", image_data.get("image_path", ""))
            relation = relationship_map.get((page_number, image_id), {})
            related_elements = relation.get("related_elements", [])
            related_ids = []
            related_records = []
            related_text = []
            for candidate in related_elements:
                element_id = candidate.get("element_id")
                element_type = candidate.get("element_type")
                if element_id:
                    related_ids.append(element_id)
                original = element_map.get((page_number, element_id), {})
                text = clean_text(original.get("text", "")) or clean_text(candidate.get("text_preview", ""))
                related_records.append({
                    "element_id": element_id,
                    "element_type": element_type,
                    "distance": candidate.get("distance"),
                    "relation_type": candidate.get("relation_type"),
                    "relationship_method": candidate.get("relationship_method", relation.get("relationship_method")),
                    "status": candidate.get("status", "candidate"),
                    "text_preview": text[:200],
                })
                if text:
                    related_text.append(text)

            content = (
                f"Machine: {machine_name}\nDocument: {document_name}\nPage: {page_number}\n"
                f"Image ID: {image_id}\nRelated text candidates: "
                f"{' | '.join(dict.fromkeys(related_text)) or 'No nearby text available'}"
            )
            chunks.append({
                "chunk_id": f"{document_name}_p{page_number}_{image_id}",
                "chunk_uuid": make_chunk_uuid(
                    machine_name, document_name,
                    f"{document_name}_p{page_number}_{image_id}",
                ),
                "content": content,
                "metadata": {
                    "machine_name": machine_name,
                    "document_name": document_name,
                    "page_number": page_number,
                    "content_type": "image",
                    "image_id": image_id,
                    "source_id": image_id,
                    "image_path": image_path,
                    "bbox": image_data.get("bbox"),
                    "related_element_ids": list(dict.fromkeys(related_ids)),
                    "related_elements": related_records,
                    "relationship_status": relation.get("status", "no_nearby_elements"),
                    "relationship_method": relation.get("relationship_method", "same_page_proximity"),
                    "related_chunk_ids": [],
                    "associated_images": [],
                    "relationships": [],
                },
            })
    return chunks


def build_link_chunks(extracted_data, machine_name, document_name, relationships_data=None):
    """Create link chunks with URL plus surrounding text and relationship metadata."""
    chunks = []
    relationship_map = _page_relationship_map(relationships_data)
    element_map = _element_map(extracted_data)
    for page in extracted_data.get("pages", []):
        page_number = page.get("page_number")
        for link in page.get("links", []):
            link_id = link.get("id", link.get("link_id", "unknown_link"))
            url = str(link.get("url", link.get("target_uri", ""))).strip()
            if not url:
                continue
            relation = relationship_map.get((page_number, link_id), {})
            related_elements = relation.get("related_elements", [])
            related_ids = []
            related_records = []
            related_text = []
            for candidate in related_elements:
                element_id = candidate.get("element_id")
                if element_id:
                    related_ids.append(element_id)
                original = element_map.get((page_number, element_id), {})
                text = clean_text(original.get("text", "")) or clean_text(candidate.get("text_preview", ""))
                related_records.append({
                    "element_id": element_id,
                    "element_type": candidate.get("element_type"),
                    "distance": candidate.get("distance"),
                    "relation_type": candidate.get("relation_type"),
                    "relationship_method": candidate.get("relationship_method", relation.get("relationship_method")),
                    "status": candidate.get("status", "candidate"),
                    "text_preview": text[:200],
                })
                if text:
                    related_text.append(text)

            context = " | ".join(dict.fromkeys(related_text))
            content = f"Link: {url}"
            if context:
                content += f"\nRelated text candidates: {context}"
            chunks.append({
                "chunk_id": f"{document_name}_p{page_number}_{link_id}",
                "chunk_uuid": make_chunk_uuid(
                    machine_name, document_name,
                    f"{document_name}_p{page_number}_{link_id}",
                ),
                "content": content,
                "metadata": {
                    "machine_name": machine_name,
                    "document_name": document_name,
                    "page_number": page_number,
                    "content_type": "link",
                    "source_id": link_id,
                    "link_id": link_id,
                    "url": url,
                    "bbox": link.get("bbox"),
                    "related_element_ids": list(dict.fromkeys(related_ids)),
                    "related_elements": related_records,
                    "relationship_status": relation.get("status", "no_nearby_elements"),
                    "relationship_method": relation.get("relationship_method", "same_page_proximity"),
                    "related_chunk_ids": [],
                    "associated_images": [],
                    "relationships": [],
                },
            })
    return chunks


def integrate_chunk_relationships(chunks, relationships_data=None):
    """Copy graph edges into chunks and resolve element IDs to chunk IDs."""
    source_to_chunks = {}
    chunk_by_id = {chunk["chunk_id"]: chunk for chunk in chunks}

    for chunk in chunks:
        metadata = chunk.setdefault("metadata", {})
        source_id = (
            metadata.get("source_id") or metadata.get("table_id")
            or metadata.get("image_id") or metadata.get("link_id")
        )
        if source_id:
            source_to_chunks.setdefault((metadata.get("page_number"), source_id), []).append(chunk["chunk_id"])

    # Resolve nearby candidates attached to image/link/table records.
    for chunk in chunks:
        metadata = chunk["metadata"]
        page_number = metadata.get("page_number")
        for candidate in metadata.get("related_elements", []):
            target_id = candidate.get("element_id")
            if not target_id:
                continue
            target_chunks = source_to_chunks.get((page_number, target_id), [])
            for target_chunk_id in target_chunks:
                if target_chunk_id != chunk["chunk_id"] and target_chunk_id not in metadata["related_chunk_ids"]:
                    metadata["related_chunk_ids"].append(target_chunk_id)
                target_meta = chunk_by_id[target_chunk_id]["metadata"]
                relation = {
                    "source_id": metadata.get("source_id"),
                    "target_id": target_id,
                    "relation_type": candidate.get("relation_type", "nearby_context"),
                    "distance": candidate.get("distance"),
                    "relationship_method": candidate.get("relationship_method", "same_page_proximity"),
                    "status": candidate.get("status", "candidate"),
                }
                outgoing = {**relation, "direction": "outgoing"}
                if outgoing not in metadata.setdefault("relationships", []):
                    metadata["relationships"].append(outgoing)
                incoming = {**relation, "direction": "incoming"}
                if incoming not in target_meta.setdefault("relationships", []):
                    target_meta["relationships"].append(incoming)
                if chunk["metadata"].get("content_type") == "image":
                    image_ref = {
                        "image_id": metadata.get("image_id"),
                        "chunk_id": chunk["chunk_id"],
                        "image_path": metadata.get("image_path", ""),
                        "page_number": page_number,
                        "relationship_method": candidate.get("relationship_method", "same_page_proximity"),
                        "status": candidate.get("status", "candidate"),
                    }
                    associated = target_meta.setdefault("associated_images", [])
                    if not any(item.get("chunk_id") == chunk["chunk_id"] for item in associated):
                        associated.append(image_ref)

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
                "distance": edge.get("distance"),
                "status": edge.get("status"),
                "relationship_method": edge.get("relationship_method"),
            }
            for chunk_id in source_chunks:
                metadata = chunk_by_id[chunk_id]["metadata"]
                outgoing = {**relation, "direction": "outgoing"}
                if outgoing not in metadata.setdefault("relationships", []):
                    metadata["relationships"].append(outgoing)
                for target_chunk_id in target_chunks:
                    if target_chunk_id != chunk_id and target_chunk_id not in metadata["related_chunk_ids"]:
                        metadata["related_chunk_ids"].append(target_chunk_id)
            for chunk_id in target_chunks:
                metadata = chunk_by_id[chunk_id]["metadata"]
                incoming = {**relation, "direction": "incoming"}
                if incoming not in metadata.setdefault("relationships", []):
                    metadata["relationships"].append(incoming)
                for source_chunk_id in source_chunks:
                    if source_chunk_id != chunk_id and source_chunk_id not in metadata["related_chunk_ids"]:
                        metadata["related_chunk_ids"].append(source_chunk_id)

    # Attach section metadata to chunks when a source element belongs to a section.
    section_by_id = {s.get("section_id"): s for s in (relationships_data or {}).get("sections", [])}
    for page in (relationships_data or {}).get("pages", []):
        page_number = page.get("page_number")
        for edge in page.get("relationships", []):
            if edge.get("relation_type") != "belongs_to_section":
                continue
            section = section_by_id.get(edge.get("target_id"))
            if not section:
                continue
            for chunk_id in source_to_chunks.get((page_number, edge.get("source_id")), []):
                metadata = chunk_by_id[chunk_id]["metadata"]
                metadata["section_id"] = section.get("section_id")
                metadata["section_title"] = section.get("title")

    return chunks


def build_all_chunks(extracted_data, machine_name, document_name, relationships_data=None):
    """Build paragraph, table-row, image and link chunks, then integrate relationships."""
    chunks = []
    chunks.extend(build_paragraph_chunks(extracted_data, machine_name, document_name))
    chunks.extend(build_chunks_from_extraction(extracted_data, machine_name, document_name))
    chunks.extend(build_image_chunks(extracted_data, machine_name, document_name, relationships_data))
    chunks.extend(build_link_chunks(extracted_data, machine_name, document_name, relationships_data))
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
