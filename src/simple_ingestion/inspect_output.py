
import json
from pathlib import Path

file_path = (
    Path(__file__).resolve().parents[2]
    / "data"
    / "processed"
    / "HSG-2550"
    / "7 - Haas Surface Grinder - Troubleshooting.json"
)

with file_path.open("r", encoding="utf-8") as file:
    data = json.load(file)

for page in data.get("pages", []):
    print(f"\n{'=' * 50}")
    print(f"PAGE {page.get('page_number')}")
    
    print("\n--- PARAGRAPHS ---")
    for paragraph in page.get("paragraphs", []):
        print("-", paragraph.get("text", "")[:180])

    print("\n--- TABLES ---")
    for table in page.get("tables", []):
        print(json.dumps(table.get("rows", []), ensure_ascii=False, indent=2)[:2000])

    print("\n--- IMAGES ---")
    for image in page.get("images", []):
        print(image)

    print("\n--- LINKS ---")
    for link in page.get("links", []):
        print(link)
