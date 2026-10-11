
import json
from pathlib import Path

from google import genai
from google.genai.types import EmbedContentConfig


def create_embedding_client():
    """Create a Vertex AI client using Google Cloud credentials."""
    return genai.Client(
        vertexai=True,
        project="market-lens-506611",
        location="us-central1",
    )


def generate_embedding(client, text):
    """Generate one text embedding."""
    response = client.models.embed_content(
        model="gemini-embedding-001",
        contents=text,
        config=EmbedContentConfig(
            output_dimensionality=768,
        ),
    )

    return response.embeddings[0].values
#For gemini-embedding-001, Google recommends 768, 1536, and 3072 dimensions. 
# A 768-dimensional embedding uses about 75% less raw vector storage than 3072 dimensions, 
# because it contains one-quarter as many numbers. Google indicates that reducing 
# dimensionality can improve storage efficiency and computational efficiency while 
# sacrificing little quality.

def process_chunks(chunks_path):
    """Embed text and table chunks; preserve image chunks unchanged."""
    chunks_path = Path(chunks_path)
    chunks = json.loads(chunks_path.read_text(encoding="utf-8"))

    client = create_embedding_client()
    embedded_chunks = []

    for chunk in chunks:
        content_type = chunk.get("metadata", {}).get("content_type")

        if content_type in {"paragraph", "table_row"}:
            chunk["embedding"] = generate_embedding(
                client,
                chunk["content"],
            )
        else:
            chunk["embedding"] = None

        embedded_chunks.append(chunk)

    output_path = chunks_path.parent / "embedded_chunks.json"
    output_path.write_text(
        json.dumps(embedded_chunks, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"Processed chunks: {len(embedded_chunks)}")
    print(f"Saved to: {output_path}")


if __name__ == "__main__":
    chunks_file = (
        Path(__file__).resolve().parents[2]
        / "data"
        / "processed"
        / "HSG-2550"
        / "7 - Haas Surface Grinder - Troubleshooting"
        / "chunks.json"
    )

    process_chunks(chunks_file)
