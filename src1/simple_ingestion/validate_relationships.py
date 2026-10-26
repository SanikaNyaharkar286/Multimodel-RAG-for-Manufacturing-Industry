
import json
import csv
from pathlib import Path

work = Path.home() / "AppData" / "Local" / "Temp" / "hab3232_relationship_rebuild"

input_file = work / "extraction_relationships.json"
output_file = work / "relationship_validation.csv"

data = json.loads(input_file.read_text(encoding="utf-8"))

rows = []

for page in data.get("pages", []):
    for image in page.get("image_relationships", []):
        candidates = [
            item for item in image.get("related_elements", [])
            if item.get("element_type") in ("paragraph", "heading", "table")
        ]

        for rank, candidate in enumerate(
            sorted(candidates, key=lambda x: x.get("distance", float("inf")))[:3],
            start=1,
        ):
            rows.append({
                "page": page.get("page_number"),
                "image_id": image.get("element_id"),
                "candidate_rank": rank,
                "related_text_id": candidate.get("element_id"),
                "related_text": candidate.get("text_preview", ""),
                "distance": candidate.get("distance", ""),
                "is_correct": "",
            })

with output_file.open("w", newline="", encoding="utf-8-sig") as file:
    writer = csv.DictWriter(file, fieldnames=[
        "page", "image_id", "candidate_rank",
        "related_text_id", "related_text", "distance", "is_correct"
    ])
    writer.writeheader()
    writer.writerows(rows)

print(f"Report created: {output_file}")
print(f"Candidate rows: {len(rows)}")
