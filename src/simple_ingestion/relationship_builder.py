"""Build document hierarchy relationships from extracted PDF elements."""
import json
import re
import sys
from pathlib import Path


_NUMBERED_HEADING = re.compile(
    r"^\s*(?:(?:chapter|section|part)\s+)?"
    r"(?P<number>\d+(?:\.\d+)*)(?:[.):])?\s+\S",
    re.IGNORECASE,
)



def _text(element):
    text = str(element.get("text") or "").strip()

    # Return meaningful text only; skip punctuation-only text.
    if text:
        if any(char.isalnum() for char in text):
            return text
        return ""

    # Fallback for table-like elements without regular text.
    rows = element.get("rows", [])

    return "\n".join(
        " | ".join(
            str(cell).strip()
            for cell in row
            if cell is not None and str(cell).strip()
        )
        for row in rows
        if any(
            cell is not None and str(cell).strip()
            for cell in row
        )
    )

def _heading_level(element, text):
    kind = str(element.get("type", element.get("block_type", ""))).lower()
    if "heading" in kind or kind == "title":
        return 1

    numbered = _NUMBERED_HEADING.match(text)
    if numbered and len(text) <= 120 and len(text.split()) <= 16:
        label = text.lstrip().split(maxsplit=1)[0].lower()
        if numbered.group("number").count(".") or label in {"chapter", "section", "part"}:
            return numbered.group("number").count(".") + 1

    letters = [char for char in text if char.isalpha()]
    if (
        letters
        and len(text) <= 100
        and len(text.split()) <= 12
        and sum(char.isupper() for char in letters) / len(letters) >= 0.9
    ):
        return 1
    return None



def _page_elements(page):
    """Collect text, tables, images, and links without dropping visual-only elements."""
    elements = []

    categories = (
        ("paragraphs", "paragraph"),
        ("tables", "table"),
        ("images", "image"),
        ("links", "link"),
    )

    for category, default_type in categories:
        for item in page.get(category, []) or []:
            if not isinstance(item, dict):
                continue

            text = _text(item)
            url = item.get("url")
            asset = item.get("asset", item.get("image_path"))

            # Preserve images and links even when their text is empty.
            if not text and default_type not in ("image", "link"):
                continue

            element_type = default_type
            level = (
                _heading_level(item, text)
                if default_type == "paragraph" and text
                else None
            )

            if level is not None:
                element_type = "heading"

            elements.append({
                "id": str(item.get("id") or f"{element_type}_{len(elements) + 1}"),
                "type": element_type,
                "text": text,
                "bbox": item.get("bbox"),
                "url": url,
                "asset": asset,
                "level": level,
                "_order": len(elements),
            })

    def reading_order(element):
        bbox = element["bbox"]

        if (
            isinstance(bbox, (list, tuple))
            and len(bbox) == 4
        ):
            return (0, bbox[1], bbox[0], element["_order"])

        return (1, 0, 0, element["_order"])

    return sorted(elements, key=reading_order)


def _edge(source_id, target_id, relation_type, evidence):
    return {
        "source_id": source_id,
        "target_id": target_id,
        "relation_type": relation_type,
        "evidence": evidence,
        "relationship_method": "document_hierarchy",
        "status": "candidate",
    }


def build_relationships(data):
    """Assign elements to the active heading; never infer links by distance."""
    output = {"document": data.get("document", ""), "sections": [], "pages": []}
    section_stack = []

    for page in data.get("pages", []):
        page_number = page.get("page_number")
        page_result = {
            "page_number": page_number,
            "relationships": [],
            "image_relationships": [],
            "table_relationships": [],
            "link_relationships": [],
        }

        for element in _page_elements(page):
            if element["type"] == "heading":
                level = element["level"]
                while section_stack and section_stack[-1]["level"] >= level:
                    section_stack.pop()

                parent_id = section_stack[-1]["section_id"] if section_stack else None
                section = {
                    "section_id": element["id"],
                    "title": element["text"],
                    "level": level,
                    "page_number": page_number,
                    "parent_section_id": parent_id,
                    "child_element_ids": [],
                }
                output["sections"].append(section)
                if parent_id:
                    page_result["relationships"].append(
                        _edge(element["id"], parent_id, "section_parent",
                              "Heading levels and extracted reading order indicate this parent section.")
                    )
                section_stack.append(section)
                continue

            category = {
                "image": "image_relationships",
                "table": "table_relationships",
                "link": "link_relationships",
            }.get(element["type"])
            if section_stack:
                section = section_stack[-1]
                section["child_element_ids"].append(element["id"])
                page_result["relationships"].append(_edge(
                    element["id"],
                    section["section_id"],
                    "belongs_to_section",
                    "Element follows this heading in extracted reading order, up to the next heading at this level or above.",
                ))
                related = [{
                    "element_id": section["section_id"],
                    "element_type": "heading",
                    "text_preview": section["title"][:200],
                    "relationship_method": "document_hierarchy",
                    "relation_type": "belongs_to_section",
                    "status": "candidate",
                }]
            else:
                related = []

            if category:
                page_result[category].append({
                    "element_id": element["id"],
                    "element_type": element["type"],
                    "page_number": page_number,
                    "bbox": element["bbox"],
                    "url": element["url"],
                    "related_elements": related,
                    "status": "candidates_found" if related else "no_section_heading",
                    "relationship_method": "document_hierarchy",
                })

        output["pages"].append(page_result)

    return output


def main():
    if len(sys.argv) != 2:
        print('Usage: python relationship_builder.py "path/to/file.json"')
        return

    input_path = Path(sys.argv[1])
    if not input_path.is_file():
        print(f"Error: file not found: {input_path}")
        return

    try:
        data = json.loads(input_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        print(f"Error: invalid JSON: {error}")
        return

    relationships = build_relationships(data)
    output_path = input_path.with_name(f"{input_path.stem}_relationships.json")
    output_path.write_text(
        json.dumps(relationships, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"Saved relationships: {output_path}")
    for page in relationships["pages"]:
        print(f"\nPage {page['page_number']}:")
        for category in ("image_relationships", "table_relationships", "link_relationships"):
            for item in page[category]:
                print(f"  {category}: {item['element_id']} -> {len(item['related_elements'])} section")


if __name__ == "__main__":
    main()
