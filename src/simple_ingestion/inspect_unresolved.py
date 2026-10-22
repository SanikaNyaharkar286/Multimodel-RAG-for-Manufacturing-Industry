
import json
from pathlib import Path

CHUNKS_PATH = Path(r"C:\Users\Sonakshi\OneDrive\Desktop\Multimodel-RAG-for-Manufacturing-Industry\data\processed\HSG-2550\5 - Haas Surface Grinder - Operation\chunks.json")
RELATIONSHIPS_PATH = Path(r"C:\Users\Sonakshi\OneDrive\Desktop\Multimodel-RAG-for-Manufacturing-Industry\data\processed\HSG-2550\5 - Haas Surface Grinder - Operation\relationships.json")

with CHUNKS_PATH.open("r", encoding="utf-8") as f:
    chunks = json.load(f)

with RELATIONSHIPS_PATH.open("r", encoding="utf-8") as f:
    data = json.load(f)

target_id = "p1_text_1"

print("\n--- Matching chunk IDs ---")
for chunk in chunks:
    if target_id in str(chunk.get("chunk_id", "")):
        print(json.dumps(chunk, indent=2))

print("\n--- Matching source IDs ---")
for chunk in chunks:
    metadata = chunk.get("metadata") or {}
    if metadata.get("source_id") == target_id:
        print(json.dumps(chunk, indent=2))

print("\n--- Edges involving this ID ---")
for page in data.get("pages", []):
    for edge in page.get("relationships", []) or []:
        if (
            edge.get("source_id") == target_id
            or edge.get("target_id") == target_id
        ):
            print(json.dumps(edge, indent=2))


EXTRACTION_PATH = Path(r"C:\Users\Sonakshi\OneDrive\Desktop\Multimodel-RAG-for-Manufacturing-Industry\data\processed\HSG-2550\5 - Haas Surface Grinder - Operation\extraction.json")

with EXTRACTION_PATH.open("r", encoding="utf-8") as f:
    extraction = json.load(f)

print("\n--- Search extraction for both IDs ---")

def search_ids(obj, target_ids, path="$"):
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in ("element_id", "source_id", "chunk_id", "id"):
                if value in target_ids:
                    print(f"Found {value!r} at {path}.{key}")
                    print(json.dumps(obj, indent=2))
            search_ids(value, target_ids, f"{path}.{key}")

    elif isinstance(obj, list):
        for index, item in enumerate(obj):
            search_ids(item, target_ids, f"{path}[{index}]")

search_ids(extraction, {"p1_text_1", "p1_text_12"})



"""
    
CREATE TABLE IF NOT EXISTS
  `market-lens-506611.manufacturing_rag.document_chunks` (
    chunk_id STRING NOT NULL,
    machine_name STRING NOT NULL,
    document_name STRING NOT NULL,
    page_number INT64 NOT NULL,
    content_type STRING NOT NULL,
    source_id STRING,
    content STRING NOT NULL,
    embedding_model STRING NOT NULL,
    embedding_dimensions INT64 NOT NULL,
    embedding ARRAY<FLOAT64>
  );

CREATE TABLE IF NOT EXISTS
  `market-lens-506611.manufacturing_rag.document_chunk_geometry` (
    chunk_id STRING NOT NULL,
    x0 FLOAT64 NOT NULL,
    y0 FLOAT64 NOT NULL,
    x1 FLOAT64 NOT NULL,
    y1 FLOAT64 NOT NULL
  );

"""