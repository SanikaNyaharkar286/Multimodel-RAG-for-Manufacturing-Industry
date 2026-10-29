r"""Read-only live smoke test: one indexed image, no local image-byte download.

From backend: .\.venv\Scripts\python.exe -m scripts.check_image_reference_transport
This makes one small Vertex inference request and does not change indexed data.
"""
import sys
from google.api_core.exceptions import BadRequest
from google.cloud import bigquery
from google.genai import types
from app import config as cfg
from app.agent.planner import model_client
from app.media.manual_assets import manual_reference


def main():
    stage = 'indexed image lookup'
    try:
        client = bigquery.Client(project=cfg.PROJECT)
        try:
            if '--validate-image-query' in sys.argv:
                from unittest.mock import patch
                from app.retrieval.manual_search import _search_image_seeds
                base = f'SELECT c.* EXCEPT(embedding), 0.1 AS distance, 1 AS keyword_hits FROM `{cfg.PROJECT}.{cfg.DATASET}.document_chunks` c'
                with patch('app.retrieval.manual_search.query', return_value=[]) as query:
                    _search_image_seeds(base, [], [{'chunk_id': 'probe', 'machine_name': 'probe', 'distance': .2}])
                sql, parameters = query.call_args.args
                client.query(sql, job_config=bigquery.QueryJobConfig(dry_run=True,
                    use_query_cache=False, query_parameters=parameters), location=cfg.BQ_LOCATION, timeout=10)
                print('Image-reference SQL dry run: OK.', flush=True)
                return 0
            if '--diagnose-index' in sys.argv:
                sql = f'''SELECT c.content_type, c.embedding_model, c.embedding_dimensions,
                    COUNT(*) AS image_records, COUNTIF(ARRAY_LENGTH(c.embedding) > 0) AS embedded_records
                    FROM `{cfg.PROJECT}.{cfg.DATASET}.document_images` i
                    JOIN `{cfg.PROJECT}.{cfg.DATASET}.document_chunks` c USING (chunk_id)
                    GROUP BY c.content_type, c.embedding_model, c.embedding_dimensions'''
                rows = list(client.query_and_wait(sql, api_timeout=5, wait_timeout=10, location=cfg.BQ_LOCATION))
                for row in rows:
                    print('Image index:', dict(row), flush=True)
                return 0
            sql = f'''SELECT c.machine_name, c.document_name, c.content, i.image_path
                FROM `{cfg.PROJECT}.{cfg.DATASET}.document_images` i
                JOIN `{cfg.PROJECT}.{cfg.DATASET}.document_chunks` c USING (chunk_id)
                WHERE REGEXP_CONTAINS(LOWER(i.image_path), r'[.](png|jpg|jpeg|webp)$')
                LIMIT 1'''
            rows = list(client.query_and_wait(sql, api_timeout=5, wait_timeout=10, location=cfg.BQ_LOCATION))
        finally:
            client.close()
        if not rows:
            print('No indexed image available for the transport smoke test.', flush=True)
            return 2
        row = rows[0]
        reference = manual_reference(row.image_path, row.document_name, row.machine_name)
        if '--retrieval-only' in sys.argv:
            from time import monotonic
            from unittest.mock import patch
            from app.retrieval.manual_search import search_manual
            from app.media.prepare_media import prepare_media
            stage = 'embedding and image-reference retrieval'
            started = monotonic()
            bundle = search_manual(str(row.content)[:1500], wants_visual=True, machine_scope=(row.machine_name,))
            with patch('app.media.prepare_media.load_manual_reference', side_effect=AssertionError('No chat image download')):
                media = prepare_media(bundle, wants_visual=True)
            if not media['images'] or any('bytes' in image for image in media['images']):
                print('Retrieval did not return valid direct image references.', flush=True)
                return 1
            print(f'Live retrieval: OK; chunks={len(bundle["chunks"])}; image references={len(media["images"])}; local image bytes=0; elapsed={monotonic()-started:.2f}s', flush=True)
            return 0
        stage = 'Vertex image-reference access'
        model = model_client()
        try:
            response = model.models.generate_content(model=cfg.MODEL,
                contents=types.Content(role='user', parts=[
                    types.Part(text='Image transport test only. Reply with OK if you can inspect the attached image. Do not provide maintenance advice.'),
                    types.Part.from_uri(file_uri=reference['uri'], mime_type=reference['mime_type'])]),
                config=types.GenerateContentConfig(temperature=0, max_output_tokens=64,
                    thinking_config=types.ThinkingConfig(thinking_level=types.ThinkingLevel.MINIMAL),
                    http_options=types.HttpOptions(timeout=30000, retry_options=types.HttpRetryOptions(attempts=1))))
        finally:
            model.close()
        if 'OK' not in (response.text or '').upper():
            print('Vertex responded but did not confirm image access.', flush=True)
            return 1
        print('Vertex direct-reference image access: OK (no local image-byte download).', flush=True)
        return 0
    except Exception as error:
        # Do not print credentials, source paths, image descriptions or prompts.
        print(f'{stage} failed: {type(error).__name__}; status={getattr(error, "code", None)}', flush=True)
        if isinstance(error, BadRequest):
            # Query parameters keep manual text out of SQL diagnostics.
            for item in (getattr(error, 'errors', None) or [])[:1]:
                print('Query diagnostic:', item.get('message', '').split('\n')[0], flush=True)
        return 1


if __name__ == '__main__':
    sys.exit(main())
