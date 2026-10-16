# this file creates a vector for a question so matching manual text can be found

from functools import lru_cache
from google import genai
from google.genai import types
from app import config as cfg


# creates and reuses the gemini embedding client
@lru_cache(maxsize=1)
def embedding_client():
    return genai.Client(
        vertexai=True,
        project=cfg.PROJECT,
        location=cfg.EMBEDDING_LOCATION,
    )


# turns the question into a vector for manual search
def get_embedding(question):
    response = embedding_client().models.embed_content(
        model=cfg.EMBEDDING_MODEL,
        contents=question,
        config=types.EmbedContentConfig(
            task_type="RETRIEVAL_QUERY",
            output_dimensionality=cfg.DIMENSIONS,
            http_options=types.HttpOptions(
                timeout=15000,
                retry_options=types.HttpRetryOptions(attempts=2, max_delay=2),
            ),
        ),
    )

    # get the vector values from the response
    vector = list(response.embeddings[0].values)

    # check that the vector has the expected number of values
    if len(vector) != cfg.DIMENSIONS:
        raise RuntimeError(
            "Query embedding dimensions do not "
            "match ingestion settings"
        )

    return vector