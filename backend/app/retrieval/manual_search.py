# this file searches manuals in bigquery and gathers related text, images, and links

import re
import threading
import time
from collections import OrderedDict
from copy import deepcopy
from google.cloud import bigquery
from app import config as cfg
from app.retrieval.bigquery_client import bq_client, query
from app.retrieval.embeddings import get_embedding
from app.retrieval.qr_link_search import get_saved_qr_links_for_pages
from app.observability.timing import increment, timed


# Manuals change through ingestion, not through chat. Reuse identical searches
# briefly while keeping the result isolated from per-session modifications.
_SEARCH_CACHE = OrderedDict()
_SEARCH_CACHE_LOCK = threading.Lock()
_SEARCH_CACHE_TTL_SECONDS = cfg.RETRIEVAL_CACHE_TTL_SECONDS
_SEARCH_CACHE_MAX = 128
_REFERENCE_CACHE = OrderedDict()
_REFERENCE_CACHE_LOCK = threading.Lock()
_REFERENCE_CACHE_TTL_SECONDS = cfg.RETRIEVAL_CACHE_TTL_SECONDS
_REFERENCE_CACHE_MAX = 256

# allowed tables used by manual search
TABLES = (
    "document_chunks",
    "document_chunk_geometry",
    "document_images",
    "document_table_rows",
    "document_links",
    "chunk_relationships",
    "document_sections",
    "section_elements",
)


def manual_references(chunks):
    """Extract explicit section/chapter navigation so retrieval can resolve it."""
    references = []
    pattern = re.compile(
        r'(?i)\b(?:refer to|see|consult)\s+(?:the\s+)?(?:section\s+)?'
        r'(?:(\d+(?:\.\d+)*)\s+)?([^.;\n]{2,180})'
    )
    for chunk in chunks:
        for number, body in pattern.findall(str(chunk.get('content', ''))):
            body = re.split(r'(?i)\s+(?:for|to)\s+(?:more\s+information|details|adjusting|checking|setting)\b',
                            body, maxsplit=1)[0].strip(' ,:-')
            title, chapter = body, ''
            parts = re.split(r'(?i)\s+of\s+the\s+', body, maxsplit=1)
            if len(parts) == 2:
                title, chapter = parts
                chapter = re.sub(r'(?i)\s+chapter$', '', chapter).strip()
            title = re.sub(r'(?i)^(?:section|chapter)\s+', '', title).strip()
            if title and title.casefold() not in {'manual', 'approved manual'}:
                references.append({
                    'number': number.strip(), 'title': title.casefold(),
                    'chapter': chapter.casefold(),
                })
    return list({(item['number'], item['title'], item['chapter']): item
                 for item in references}.values())[:8]


# returns a safe table name
def table(name):
    if name not in TABLES:
        raise ValueError("Unapproved table")

    return f"`{cfg.PROJECT}.{cfg.DATASET}.{name}`"


# lists the machines and manuals stored in bigquery
@timed('inventory_query')
def list_available_documents():
    """Return the indexed machines and documents without vector ranking."""
    return query(
        f"""
        SELECT machine_name, document_name
        FROM {table('document_chunks')}
        GROUP BY machine_name, document_name
        ORDER BY machine_name, document_name
        """,
        [],
    )


# searches the manuals and gathers related page information
def search_manual(
    question,
    wants_visual=False,
    wants_video=False,
):
    normalized_question = re.sub(r'\s+', ' ', question.casefold()).strip()
    cache_key = (
        cfg.PROJECT, cfg.DATASET, cfg.BQ_LOCATION, cfg.EMBEDDING_MODEL,
        cfg.EMBEDDING_LOCATION, cfg.DIMENSIONS, cfg.QR_LINKS_TABLE,
        normalized_question, bool(wants_visual), bool(wants_video),
    )
    now = time.monotonic()
    with _SEARCH_CACHE_LOCK:
        cached = _SEARCH_CACHE.get(cache_key)
        if cached is not None and now - cached[0] < _SEARCH_CACHE_TTL_SECONDS:
            _SEARCH_CACHE.move_to_end(cache_key)
            increment('cache_hits')
            increment('retrieval_cache_hits')
            return deepcopy(cached[1])
        _SEARCH_CACHE.pop(cache_key, None)

    result = _search_manual_uncached(question, wants_visual, wants_video)
    with _SEARCH_CACHE_LOCK:
        _SEARCH_CACHE[cache_key] = (time.monotonic(), deepcopy(result))
        _SEARCH_CACHE.move_to_end(cache_key)
        while len(_SEARCH_CACHE) > _SEARCH_CACHE_MAX:
            _SEARCH_CACHE.popitem(last=False)
    return result


def _search_manual_uncached(question, wants_visual=False, wants_video=False):
    """
    Retrieve relevant evidence, then enrich the fields used by the answer.

    Only fixed SELECT SQL is used.
    Users and Gemini cannot supply executable SQL.
    """

    # turn the question into a vector for semantic search
    vector = get_embedding(question)

    # find useful words from the question for keyword matching
    stop_words = {
        "what", "why", "how", "the", "is", "my",
        "are", "of", "to", "and", "a", "for",
    }

    terms = list(
        dict.fromkeys(
            token
            for token in re.findall(
                r"[a-z0-9-]+",
                question.lower(),
            )
            if len(token) > 2
            and token not in stop_words
        )
    )[:12]

    # find machine model names written in the question
    mentioned_machines = list(dict.fromkeys(re.findall(r"\b[A-Z]{2,}[A-Z0-9]*-\d{3,}\b", question)))

    # pass search values safely to bigquery
    parameters = [
        bigquery.ScalarQueryParameter(
            "model", "STRING", cfg.EMBEDDING_MODEL
        ),
        bigquery.ScalarQueryParameter(
            "dims", "INT64", cfg.DIMENSIONS
        ),
        bigquery.ArrayQueryParameter(
            "vector", "FLOAT64", vector
        ),
        bigquery.ArrayQueryParameter(
            "terms", "STRING", terms
        ),
        bigquery.ArrayQueryParameter(
            "mentioned_machines", "STRING", mentioned_machines
        ),
        bigquery.ScalarQueryParameter(
            "include_media", "BOOL", wants_visual or wants_video
        ),
    ]

    # prepare the query that ranks manual text by meaning and keywords
    base_sql = f"""
        SELECT
            c.* EXCEPT(embedding),
            ML.DISTANCE(
                c.embedding,
                @vector,
                'COSINE'
            ) AS distance,
            (
                SELECT COUNT(*)
                FROM UNNEST(@terms) term
                WHERE STRPOS(
                    LOWER(c.content),
                    term
                ) > 0
            ) AS keyword_hits
        FROM {table('document_chunks')} c
        WHERE REGEXP_REPLACE(
              c.embedding_model,
              r'^.*/',
              ''
          ) = @model
          AND c.embedding_dimensions = @dims
          AND ARRAY_LENGTH(c.embedding) = @dims
    """

    # get the best matching manual sections
    seeds = query(
        f"""
        WITH ranked AS (
            {base_sql}
        )
        SELECT *
        FROM ranked
        QUALIFY ROW_NUMBER() OVER (
            ORDER BY distance - LEAST(keyword_hits, 3) * 0.025
        ) <= 10
        OR (
            ARRAY_LENGTH(@mentioned_machines) > 0
            AND machine_name IN UNNEST(@mentioned_machines)
            AND ROW_NUMBER() OVER (
                PARTITION BY machine_name
                ORDER BY distance - LEAST(keyword_hits, 3) * 0.025
            ) <= 10
        )
        OR (
            @include_media AND content_type IN ('image', 'link')
            AND ROW_NUMBER() OVER (
                PARTITION BY content_type IN ('image', 'link')
                ORDER BY distance - LEAST(keyword_hits, 3) * 0.025
            ) <= 4
        )
        ORDER BY
            distance - LEAST(keyword_hits, 3) * 0.025
        """,
        parameters, operation='bq_seed_query',
    )

    # return empty results if no manual sections matched
    if not seeds:
        return {
            "chunks": [],
            "images": [],
            "links": [],
            "warnings": [],
        }

    # collect the ids of the matching sections
    seed_ids = [
        row["chunk_id"]
        for row in seeds
    ]
    reference_seed_ids = set()

    # Troubleshooting tables sometimes contain navigation instead of the
    # actual procedure (for example, "Refer to section 3.2 Blade
    # Installation"). Resolve those references now so Gemini receives the
    # procedure text and figures, rather than repeating the navigation to the
    # technician. Referenced rows are enriched below just like semantic seeds.
    references = manual_references(seeds)
    if references:
        machines = list(dict.fromkeys(row['machine_name'] for row in seeds))
        reference_key = (
            cfg.PROJECT, cfg.DATASET,
            tuple(sorted(str(machine).casefold() for machine in machines)),
            tuple(sorted((item['number'], item['title'], item['chapter'])
                         for item in references)),
        )
        now = time.monotonic()
        with _REFERENCE_CACHE_LOCK:
            cached_references = _REFERENCE_CACHE.get(reference_key)
            if (cached_references is not None
                    and now - cached_references[0] < _REFERENCE_CACHE_TTL_SECONDS):
                _REFERENCE_CACHE.move_to_end(reference_key)
                increment('cache_hits')
                increment('reference_cache_hits')
                reference_rows = deepcopy(cached_references[1])
            else:
                _REFERENCE_CACHE.pop(reference_key, None)
                reference_rows = None
        if reference_rows is None:
            reference_rows = query(
                f"""
            WITH candidates AS (
                SELECT c.chunk_id, c.page_number,
                    (SELECT COUNT(*) FROM UNNEST(@reference_titles) title
                     WHERE title != '' AND STRPOS(LOWER(c.content), title) > 0) AS title_hits,
                    (SELECT COUNT(*) FROM UNNEST(@reference_numbers) number
                     WHERE number != '' AND REGEXP_CONTAINS(
                         LOWER(c.content), CONCAT(r'(^|[^0-9])', REPLACE(number, '.', r'[.]'), r'([^0-9]|$)')
                     )) AS number_hits,
                    (SELECT COUNT(*) FROM UNNEST(@reference_chapters) chapter
                     WHERE chapter != '' AND STRPOS(LOWER(c.document_name), chapter) > 0) AS chapter_hits,
                    IF(LOWER(c.content_type) IN ('heading', 'title'), 1, 0) AS heading_hit
                FROM {table('document_chunks')} c
                WHERE c.machine_name IN UNNEST(@reference_machines)
            )
            SELECT chunk_id
            FROM candidates
            WHERE title_hits > 0 OR number_hits > 0 OR chapter_hits > 0
            ORDER BY title_hits DESC, number_hits DESC, heading_hit DESC,
                     chapter_hits DESC, page_number
            LIMIT 10
            """,
                [
                    bigquery.ArrayQueryParameter('reference_titles', 'STRING',
                                                 [item['title'] for item in references]),
                    bigquery.ArrayQueryParameter('reference_numbers', 'STRING',
                                                 [item['number'] for item in references]),
                    bigquery.ArrayQueryParameter('reference_chapters', 'STRING',
                                                 [item['chapter'] for item in references]),
                    bigquery.ArrayQueryParameter('reference_machines', 'STRING', machines),
                ],
                operation='bq_reference_query',
            )
            with _REFERENCE_CACHE_LOCK:
                _REFERENCE_CACHE[reference_key] = (now, deepcopy(reference_rows))
                _REFERENCE_CACHE.move_to_end(reference_key)
                while len(_REFERENCE_CACHE) > _REFERENCE_CACHE_MAX:
                    _REFERENCE_CACHE.popitem(last=False)
        reference_seed_ids = {row['chunk_id'] for row in reference_rows}
        seed_ids.extend(identifier for identifier in reference_seed_ids
                        if identifier not in seed_ids)

    # pass the matching ids to the enrichment query
    enrich_parameters = [
        bigquery.ArrayQueryParameter(
            "ids", "STRING", seed_ids
        ),
    ]

    # The uploaded pipeline may use element IDs local to a document.
    # Context expansion therefore stays inside selected documents/pages.
    # Relationships remain candidate associations, not technical proof.
    rows = query(
        rf"""
        WITH seeds AS (
            SELECT * EXCEPT(embedding)
            FROM {table('document_chunks')}
            WHERE chunk_id IN UNNEST(@ids)
        ),
        context AS (
            SELECT DISTINCT c.* EXCEPT(embedding)
            FROM {table('document_chunks')} c
            JOIN seeds seed
              ON c.machine_name = seed.machine_name
             AND REGEXP_REPLACE(
                    c.document_name,
                    r'(?i)\.pdf$',
                    ''
                 ) = REGEXP_REPLACE(
                    seed.document_name,
                    r'(?i)\.pdf$',
                    ''
                 )
             AND c.page_number = seed.page_number
        ),
        geometry AS (
            SELECT g.chunk_id,
                ARRAY_AGG(STRUCT(g.x0, g.y0, g.x1, g.y1) LIMIT 1) AS geometry
            FROM {table('document_chunk_geometry')} g
            JOIN context c USING (chunk_id)
            GROUP BY g.chunk_id
        ),
        images AS (
            SELECT image.chunk_id,
                ARRAY_AGG(STRUCT(image.image_id, image.image_path,
                    image.relationship_status, image.relationship_method)) AS images
            FROM {table('document_images')} image
            JOIN context c USING (chunk_id)
            GROUP BY image.chunk_id
        ),
        links AS (
            SELECT link.chunk_id,
                ARRAY_AGG(STRUCT(link.link_id, link.url)) AS links
            FROM {table('document_links')} link
            JOIN context c USING (chunk_id)
            GROUP BY link.chunk_id
        ),
        relationships AS (
            SELECT c.chunk_id,
                ARRAY_AGG(STRUCT(relation.source_id, relation.target_id,
                    relation.relation_type, relation.evidence,
                    relation.relationship_method, relation.status) LIMIT 8) AS relationships
            FROM context c
            JOIN {table('chunk_relationships')} relation
              ON relation.source_id IN (c.source_id, c.chunk_id)
              OR relation.target_id IN (c.source_id, c.chunk_id)
            GROUP BY c.chunk_id
        ),
        section_elements AS (
            SELECT section_id, ARRAY_AGG(element_id LIMIT 40) AS elements
            FROM {table('section_elements')}
            GROUP BY section_id
        ),
        section_matches AS (
            SELECT DISTINCT c.chunk_id,
                STRUCT(section.section_id, section.title, section.level,
                    section.page_number, section.parent_section_id,
                    IFNULL(elements.elements, []) AS elements) AS section
            FROM context c
            JOIN {table('document_sections')} section
              ON REGEXP_REPLACE(section.document_name, r'(?i)\.pdf$', '')
               = REGEXP_REPLACE(c.document_name, r'(?i)\.pdf$', '')
            LEFT JOIN {table('section_elements')} element
              ON element.section_id = section.section_id
             AND element.element_id IN (c.source_id, c.chunk_id)
            LEFT JOIN section_elements elements
              ON elements.section_id = section.section_id
            WHERE section.page_number = c.page_number
               OR element.element_id IS NOT NULL
        ),
        sections AS (
            SELECT chunk_id, ARRAY_AGG(section LIMIT 6) AS sections
            FROM section_matches
            GROUP BY chunk_id
        )
        SELECT c.*,
            IFNULL(g.geometry, []) AS geometry,
            IFNULL(i.images, []) AS images,
            IFNULL(l.links, []) AS links,
            IFNULL(r.relationships, []) AS relationships,
            IFNULL(s.sections, []) AS sections
        FROM context c
        LEFT JOIN geometry g USING (chunk_id)
        LEFT JOIN images i USING (chunk_id)
        LEFT JOIN links l USING (chunk_id)
        LEFT JOIN relationships r USING (chunk_id)
        LEFT JOIN sections s USING (chunk_id)
        ORDER BY
            IF(c.chunk_id IN UNNEST(@ids), 0, 1),
            c.page_number,
            c.chunk_id
        LIMIT 100
        """,
        enrich_parameters, operation='bq_context_query',
    )

    # keep the original ranking for the matching sections
    seed_map = {
        row["chunk_id"]: row
        for row in seeds
    }

    # rank related sections using text, links, and page position
    def score(row):
        if row["chunk_id"] in seed_map:
            return (
                0,
                seed_map[row["chunk_id"]]["distance"],
            )
        if row['chunk_id'] in reference_seed_ids:
            # Keep resolved section content in the answer context after the
            # semantic seeds that led us to the cross-reference.
            return (0, 0.5)

        document = str(
            row["document_name"]
        ).removesuffix(".pdf")

        nearby_seeds = [
            seed
            for seed in seeds
            if str(
                seed["document_name"]
            ).removesuffix(".pdf") == document
            and seed["page_number"] == row["page_number"]
        ]

        # count words shared with the question
        content_terms = set(
            re.findall(
                r"[a-z0-9-]+",
                row["content"].lower(),
            )
        )

        overlap = len(
            content_terms.intersection(terms)
        )

        # check whether the section is linked to a matching section
        source_ids = {
            seed.get("source_id")
            for seed in nearby_seeds
        }

        linked = any(
            relation.get("source_id") in source_ids
            or relation.get("target_id") in source_ids
            for relation in row.get("relationships", [])
        )

        # measure how close the section is to a matching section
        geometry_distance = 99999

        if row.get("geometry"):
            box = row["geometry"][0]

            for other in rows:
                if (
                    other["chunk_id"] in seed_ids
                    and other.get("geometry")
                    and other["document_name"]
                    == row["document_name"]
                ):
                    other_box = other["geometry"][0]

                    geometry_distance = min(
                        geometry_distance,
                        abs(box["y0"] - other_box["y0"]),
                    )

        return (
            1,
            -overlap,
            -int(linked),
            geometry_distance,
        )

    # sort sections so the most useful ones come first
    rows.sort(key=score)

    # choose related text while keeping the result size limited
    selected = []
    used_characters = 0

    for row in rows:
        if len(selected) >= 16:
            continue

        if used_characters + len(row["content"]) > 16000:
            continue

        selected.append(row)
        used_characters += len(row["content"])

    # choose rows that contain images and links
    image_rows = sorted(
        (
            row
            for row in rows
            if row.get("images")
        ),
        key=score,
    )[:6]

    link_rows = sorted(
        (
            row
            for row in rows
            if row.get("links")
        ),
        key=score,
    )[:8]

    # store selected text sections by id
    chunks = {
        row["chunk_id"]: row
        for row in selected
    }

    # keep the source section for every supplied image and link
    for row in image_rows + link_rows:
        chunks.setdefault(row["chunk_id"], row)

    images = []
    links = []

    # attach page details to each image
    for row in image_rows:
        for image in row["images"]:
            images.append(
                {
                    **image,
                    "chunk_id": row["chunk_id"],
                    "document_name": row["document_name"],
                    "machine_name": row["machine_name"],
                    "page_number": row["page_number"],
                    "geometry": row.get("geometry", []),
                }
            )

    # attach page details to each link
    for row in link_rows:
        for link in row["links"]:
            links.append(
                {
                    **link,
                    "chunk_id": row["chunk_id"],
                    "document_name": row["document_name"],
                    "page_number": row["page_number"],
                }
            )

    # QR lookups require additional BigQuery calls. They are only useful when
    # the technician asks for video, QR, or demonstration material.
    if wants_video:
        pages_by_machine = {}
        for row in chunks.values():
            pages_by_machine.setdefault(row["machine_name"], set()).add(
                (row["document_name"], row["page_number"])
            )
        for machine, machine_pages in pages_by_machine.items():
            for record in get_saved_qr_links_for_pages(
                machine, machine_pages, bq_client()
            ):
                chunks.setdefault(record["chunk_id"], record)
                links.append({**record, "link_id": record["chunk_id"]})

    # return the matching text, images, links, and warnings
    return {
        "chunks": list(chunks.values()),
        "images": images,
        "links": links,
        "warnings": [],
    }


# checks that the bigquery tables contain the required columns
def check_tables():
    # lists the required columns for each table
    required_columns = {
        "document_chunks": {
            "chunk_id",
            "machine_name",
            "document_name",
            "page_number",
            "source_id",
            "content",
            "content_type",
            "embedding",
            "embedding_model",
            "embedding_dimensions",
        },
        "document_chunk_geometry": {
            "chunk_id", "x0", "y0", "x1", "y1",
        },
        "document_images": {
            "chunk_id", "image_id", "image_path",
            "relationship_status", "relationship_method",
        },
        "document_table_rows": {
            "chunk_id", "table_id", "row_number", "content",
        },
        "document_links": {
            "chunk_id", "link_id", "url",
        },
        "chunk_relationships": {
            "source_id",
            "target_id",
            "relation_type",
            "evidence",
            "relationship_method",
            "status",
        },
        "document_sections": {
            "section_id",
            "document_name",
            "title",
            "level",
            "page_number",
            "parent_section_id",
        },
        "section_elements": {
            "section_id", "element_id",
        },
    }

    # check each table and report its row count
    report = {}

    for name, required in required_columns.items():
        item = bq_client().get_table(
            f"{cfg.PROJECT}.{cfg.DATASET}.{name}"
        )

        actual = {
            field.name
            for field in item.schema
        }

        missing = required - actual

        # report an error if any required columns are missing
        if missing:
            raise RuntimeError(
                f"{name}: missing columns {sorted(missing)}"
            )

        report[name] = {
            "rows": item.num_rows,
        }

    return report
