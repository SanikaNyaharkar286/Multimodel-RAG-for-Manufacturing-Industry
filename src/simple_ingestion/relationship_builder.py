
import json
import sys
from pathlib import Path


MAX_DISTANCE = 500
MAX_RELATIONSHIPS = 5


def get_center(bbox):
    x0, y0, x1, y1 = bbox
    return (x0 + x1) / 2, (y0 + y1) / 2


def get_distance(bbox1, bbox2):
    x1, y1 = get_center(bbox1)
    x2, y2 = get_center(bbox2)

    return ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5


def get_text(element):
    """Extract searchable text from a paragraph or table."""
    text = element.get("text", "")

    if text:
        return str(text).strip()

    rows = element.get("rows", [])
    lines = []

    for row in rows:
        values = [
            str(cell).strip()
            for cell in row
            if cell is not None and str(cell).strip()
        ]

        if values:
            lines.append(" | ".join(values))

    return "\n".join(lines)


def identify_type(element, default_type):
    """Use available metadata to identify headings and other elements."""
    value = str(
        element.get("type", element.get("block_type", ""))
    ).lower()

    if "heading" in value or value == "title":
        return "heading"

    return default_type


def add_elements(collection, default_type, elements):
    """Convert extracted elements into a common internal format."""
    for element in elements:
        bbox = element.get("bbox")

        if not bbox or len(bbox) != 4:
            continue

        element_type = identify_type(element, default_type)

        collection.append({
            "id": element.get("id", "unknown"),
            "type": element_type,
            "bbox": bbox,
            "text": get_text(element),
            "url": element.get("url"),
        })


def build_page_elements(page):
    """Collect paragraphs, tables, images and links on a page."""
    elements = []

    add_elements(
        elements,
        "paragraph",
        page.get("paragraphs", []),
    )

    add_elements(
        elements,
        "table",
        page.get("tables", []),
    )

    add_elements(
        elements,
        "image",
        page.get("images", []),
    )

    add_elements(
        elements,
        "link",
        page.get("links", []),
    )

    return elements


def get_priority(source_type, target_type):
    """Prefer useful text relationships over unrelated visual elements."""
    priorities = {
        "heading": 0,
        "paragraph": 1,
        "table": 2,
        "image": 3,
        "link": 4,
    }

    # Images and links should primarily connect to explanatory text.
    if source_type in ("image", "link"):
        return {
            "heading": 0,
            "paragraph": 1,
            "table": 2,
            "image": 3,
            "link": 4,
        }.get(target_type, 5)

    # Tables should primarily connect to their section and explanation.
    if source_type == "table":
        return {
            "heading": 0,
            "paragraph": 1,
            "image": 2,
            "link": 3,
            "table": 4,
        }.get(target_type, 5)

    return priorities.get(target_type, 5)


def find_related_elements(source, elements):
    """Find nearby elements that may be related to the source."""
    candidates = []

    for target in elements:
        if target["id"] == source["id"]:
            continue

        # Avoid connecting an image or link to another link unnecessarily.
        if (
            source["type"] == "link"
            and target["type"] in ("image", "link")
        ):
            continue

        if (
            source["type"] == "image"
            and target["type"] in ("image", "link")
        ):
            continue

        distance = get_distance(source["bbox"], target["bbox"])

        if distance > MAX_DISTANCE:
            continue

        priority = get_priority(source["type"], target["type"])

        candidates.append({
            "element_id": target["id"],
            "element_type": target["type"],
            "distance": round(distance, 2),
            "relationship_method": "same_page_proximity",
            "text_preview": target["text"][:200],
            "url": target["url"],
            "sort_score": distance + priority * 30,
        })

    candidates.sort(key=lambda item: item["sort_score"])

    results = []

    for candidate in candidates[:MAX_RELATIONSHIPS]:
        candidate.pop("sort_score")
        results.append(candidate)

    return results


def build_relationships(data):
    """Build image, table and link relationships for every PDF page."""
    output = {
        "document": data.get("document", ""),
        "pages": [],
    }

    for page in data.get("pages", []):
        elements = build_page_elements(page)

        results = {
            "images": [],
            "tables": [],
            "links": [],
        }

        for element in elements:
            category = {
                "image": "images",
                "table": "tables",
                "link": "links",
            }.get(element["type"])

            if category is None:
                continue

            related = find_related_elements(element, elements)

            record = {
                "element_id": element["id"],
                "page_number": page.get("page_number"),
                "bbox": element["bbox"],
                "related_elements": related,
                "status": (
                    "candidates_found"
                    if related
                    else "no_nearby_elements"
                ),
            }

            if element["type"] == "link":
                record["url"] = element["url"]

            results[category].append(record)

        output["pages"].append({
            "page_number": page.get("page_number"),
            "image_relationships": results["images"],
            "table_relationships": results["tables"],
            "link_relationships": results["links"],
        })

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

    output_path = input_path.with_name(
        f"{input_path.stem}_relationships.json"
    )

    output_path.write_text(
        json.dumps(relationships, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"Saved relationships: {output_path}")

    for page in relationships["pages"]:
        print(f"\nPage {page['page_number']}:")

        for category in (
            "image_relationships",
            "table_relationships",
            "link_relationships",
        ):
            for item in page[category]:
                print(
                    f"  {category}: {item['element_id']} -> "
                    f"{len(item['related_elements'])} candidates"
                )


if __name__ == "__main__":
    main()
