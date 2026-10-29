"""Build document-hierarchy and same-page proximity relationships.

Input: extraction.json produced by the existing PDF/segmentation extraction pipeline.
Output: a relationships JSON compatible with the current chunk_builder.py.

The builder keeps hierarchy relationships and restores proximity candidates for
images, tables, and links. It does not run PDF extraction or call any embedding API.
"""
import json
import math
import re
import sys
from pathlib import Path

MAX_DISTANCE = 500
MAX_RELATIONSHIPS = 5

_NUMBERED_HEADING = re.compile(
    r"^\s*(?:(?:chapter|section|part)\s+)?"
    r"(?P<number>\d+(?:\.\d+)*)(?:[.):])?\s+\S",
    re.IGNORECASE,
)


def _text(element):
    text = str(element.get("text") or "").strip()
    if text:
        return text if any(char.isalnum() for char in text) else ""

    rows = element.get("rows", [])
    return "\n".join(
        " | ".join(
            str(cell).strip()
            for cell in row
            if cell is not None and str(cell).strip()
        )
        for row in rows
        if any(cell is not None and str(cell).strip() for cell in row)
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
    """Collect all extractable element types while retaining IDs and bounding boxes."""
    elements = []
    for category, default_type in (
        ("paragraphs", "paragraph"),
        ("tables", "table"),
        ("images", "image"),
        ("links", "link"),
    ):
        for item in page.get(category, []) or []:
            if not isinstance(item, dict):
                continue
            text = _text(item)
            url = item.get("url")
            asset = item.get("asset", item.get("image_path"))
            if not text and default_type not in ("image", "link"):
                continue

            level = _heading_level(item, text) if default_type == "paragraph" and text else None
            element_type = "heading" if level is not None else default_type
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
        if isinstance(bbox, (list, tuple)) and len(bbox) == 4:
            try:
                return (0, float(bbox[1]), float(bbox[0]), element["_order"])
            except (TypeError, ValueError):
                pass
        return (1, 0, 0, element["_order"])

    return sorted(elements, key=reading_order)


def _valid_bbox(bbox):
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return None
    try:
        values = [float(value) for value in bbox]
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(value) for value in values):
        return None
    x1, y1, x2, y2 = values
    return (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))


def _center(bbox):
    valid = _valid_bbox(bbox)
    if not valid:
        return None
    x1, y1, x2, y2 = valid
    return ((x1 + x2) / 2, (y1 + y2) / 2)


def _bbox_gap(bbox_a, bbox_b):
    """Minimum edge-to-edge distance; unlike center distance, respects adjacent columns."""
    a = _valid_bbox(bbox_a)
    b = _valid_bbox(bbox_b)
    if not a or not b:
        return None
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    dx = max(0.0, bx1 - ax2, ax1 - bx2)
    dy = max(0.0, by1 - ay2, ay1 - by2)
    return math.hypot(dx, dy)


def _vertical_gap(bbox_a, bbox_b):
    a = _valid_bbox(bbox_a)
    b = _valid_bbox(bbox_b)
    if not a or not b:
        return None
    return max(0.0, b[1] - a[3], a[1] - b[3])


def _horizontal_overlap(bbox_a, bbox_b):
    a = _valid_bbox(bbox_a)
    b = _valid_bbox(bbox_b)
    if not a or not b:
        return 0.0
    overlap = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    return overlap / max(1.0, min(a[2] - a[0], b[2] - b[0]))


def _priority(source_type, candidate_type):
    """Prefer explanatory text/table elements over other visual elements."""
    priorities = {
        "image": {"heading": 0, "paragraph": 1, "table": 2, "image": 4, "link": 5},
        "table": {"heading": 0, "paragraph": 1, "image": 2, "link": 4, "table": 5},
        "link": {"heading": 0, "paragraph": 1, "table": 2, "image": 3, "link": 5},
    }
    return priorities.get(source_type, {}).get(candidate_type, 6)


def _find_related_elements(source, elements):
    """Rank same-page candidates by box gap and layout alignment, not center distance."""
    candidates = []
    source_box = _valid_bbox(source.get("bbox"))
    if not source_box:
        return []

    sx1, sy1, sx2, sy2 = source_box
    source_center_y = (sy1 + sy2) / 2

    for candidate in elements:
        if candidate["id"] == source["id"]:
            continue
        if candidate["type"] not in ("heading", "paragraph", "table", "image", "link"):
            continue
        candidate_box = _valid_bbox(candidate.get("bbox"))
        if not candidate_box:
            continue

        gap = _bbox_gap(source["bbox"], candidate["bbox"])
        if gap is None or gap > MAX_DISTANCE:
            continue

        cx1, cy1, cx2, cy2 = candidate_box
        candidate_center_y = (cy1 + cy2) / 2
        vertical_gap = _vertical_gap(source["bbox"], candidate["bbox"]) or 0.0
        overlap = _horizontal_overlap(source["bbox"], candidate["bbox"])

        # Penalize headings and number-only OCR fragments as direct image captions.
        text = (candidate.get("text") or "").strip()
        penalty = _priority(source["type"], candidate["type"]) * 8
        if candidate["type"] == "heading":
            penalty += 35
        if text.isdigit() or re.fullmatch(r"\s*[\d.]+\s*", text):
            penalty += 80

        # For this PDF layout, explanatory text is generally in the right column.
        # Reward a candidate to the right of an image when its vertical position aligns.
        if source["type"] == "image" and candidate["type"] in ("paragraph", "heading", "table"):
            if cx1 >= sx2:
                penalty -= 25
            elif cx2 <= sx1:
                penalty += 35

        # Vertical alignment is a stronger cue than center distance across columns.
        score = gap + min(vertical_gap, 250) * 0.35 + penalty
        if overlap > 0:
            score -= min(40, overlap * 0.15)

        candidates.append((score, gap, abs(candidate_center_y - source_center_y), candidate))

    candidates.sort(key=lambda item: (item[0], item[1], item[2], item[3]["_order"]))
    result = []
    for score, gap, _, candidate in candidates[:MAX_RELATIONSHIPS]:
        result.append({
            "element_id": candidate["id"],
            "element_type": candidate["type"],
            "text_preview": candidate["text"][:200],
            "distance": round(gap, 3),
            "ranking_score": round(score, 3),
            "relationship_method": "same_page_bbox_proximity",
            "relation_type": "nearby_element",
            "status": "candidate",
        })
    return result


def _edge(source_id, target_id, relation_type, evidence, method):
    return {
        "source_id": source_id,
        "target_id": target_id,
        "relation_type": relation_type,
        "evidence": evidence,
        "relationship_method": method,
        "status": "candidate",
    }


def build_relationships(data):
    """Build section hierarchy plus proximity-based relationships within each page."""
    output = {"document": data.get("document", ""), "sections": [], "pages": []}
    section_stack = []

    for page in data.get("pages", []) or []:
        page_number = page.get("page_number")
        elements = _page_elements(page)
        page_result = {
            "page_number": page_number,
            "relationships": [],
            "image_relationships": [],
            "table_relationships": [],
            "link_relationships": [],
        }

        # Hierarchy is maintained in reading order. Each page's proximity candidates
        # are calculated separately so relationships never cross page boundaries.
        active_section_by_element = {}
        for element in elements:
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
                    page_result["relationships"].append(_edge(
                        element["id"], parent_id, "section_parent",
                        "Heading levels and extracted reading order indicate this parent section.",
                        "document_hierarchy",
                    ))
                section_stack.append(section)
                active_section_by_element[element["id"]] = section
                continue

            if section_stack:
                section = section_stack[-1]
                section["child_element_ids"].append(element["id"])
                active_section_by_element[element["id"]] = section
                page_result["relationships"].append(_edge(
                    element["id"], section["section_id"], "belongs_to_section",
                    "Element follows this heading in extracted reading order, up to the next heading at this level or above.",
                    "document_hierarchy",
                ))

        for element in elements:
            category = {
                "image": "image_relationships",
                "table": "table_relationships",
                "link": "link_relationships",
            }.get(element["type"])
            if not category:
                continue

            related = _find_related_elements(element, elements)
            section = active_section_by_element.get(element["id"])
            # Preserve hierarchy context as a candidate too, without duplicating it.
            if section and not any(item["element_id"] == section["section_id"] for item in related):
                related.append({
                    "element_id": section["section_id"],
                    "element_type": "heading",
                    "text_preview": section["title"][:200],
                    "relationship_method": "document_hierarchy",
                    "relation_type": "belongs_to_section",
                    "status": "candidate",
                })

            page_result[category].append({
                "element_id": element["id"],
                "element_type": element["type"],
                "page_number": page_number,
                "bbox": element["bbox"],
                "url": element["url"],
                "related_elements": related,
                "status": "candidates_found" if related else "no_related_elements",
                "relationship_method": "same_page_bbox_proximity_and_hierarchy",
            })

            for candidate in related:
                # Proximity candidates are represented as graph edges as well, allowing
                # chunk_builder to resolve relationships when both elements have chunks.
                if candidate.get("relationship_method") == "same_page_bbox_proximity":
                    page_result["relationships"].append(_edge(
                        element["id"], candidate["element_id"], "nearby_element",
                        f"Bounding-box edge gap: {candidate.get('distance')} units.",
                        "same_page_bbox_proximity",
                    ))

        output["pages"].append(page_result)

    return output


def main():
    if len(sys.argv) != 2:
        print('Usage: python relationship_builder.py "path/to/extraction.json"')
        raise SystemExit(2)

    input_path = Path(sys.argv[1])
    if not input_path.is_file():
        print(f"Error: file not found: {input_path}")
        raise SystemExit(1)

    try:
        data = json.loads(input_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        print(f"Error: invalid JSON: {error}")
        raise SystemExit(1)

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
            records = page[category]
            linked = sum(bool(record["related_elements"]) for record in records)
            print(f"  {category}: {linked}/{len(records)} have candidates")


if __name__ == "__main__":
    main()
