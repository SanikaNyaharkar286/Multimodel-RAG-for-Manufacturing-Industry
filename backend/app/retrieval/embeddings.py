# this file creates a vector for a question so matching manual text can be found

from functools import lru_cache
from google import genai
from google.genai import types
from app import config as cfg
from app.observability.timing import span, timed, increment


# creates and reuses the gemini embedding client
@lru_cache(maxsize=1)
def embedding_client():
    return genai.Client(
        vertexai=True,
        project=cfg.PROJECT,
        location=cfg.EMBEDDING_LOCATION,
    )


# turns the question into a vector for manual search
@lru_cache(maxsize=256)
def _cached_embedding(question):
    increment('model_calls')
    response = embedding_client().models.embed_content(
        model=cfg.EMBEDDING_MODEL,
        contents=question,
        config=types.EmbedContentConfig(
            task_type="RETRIEVAL_QUERY",
            output_dimensionality=cfg.DIMENSIONS,
            http_options=types.HttpOptions(
                timeout=15000,
            retry_options=types.HttpRetryOptions(attempts=1, max_delay=1),
            ),
        ),
    )

    # get the vector values from the response
    vector = list(response.embeddings[0].values)

    # check that the vector has the expected number of values
    with span('embedding_validation'):
        if len(vector) != cfg.DIMENSIONS:
            raise RuntimeError('Query embedding dimensions do not match ingestion settings')

    return vector


@timed('embedding')
def get_embedding(question):
    """Return a reusable query vector without repeating provider calls."""
    return _cached_embedding(question.strip())
