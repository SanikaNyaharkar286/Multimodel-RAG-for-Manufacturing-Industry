import re
from google.cloud import bigquery
from app import config as cfg
from app.retrieval.bigquery_client import bq_client, query
from app.retrieval.embeddings import get_embedding
from app.retrieval.qr_link_search import get_saved_qr_links_for_pages

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


def table(name):
    if name not in TABLES:
        raise ValueError("Unapproved table")

    return f"`{cfg.PROJECT}.{cfg.DATASET}.{name}`"


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


def search_manual(
    question,
    wants_visual=False,
    wants_video=False,
):
    """
    Retrieve relevant evidence, then enrich it from all eight tables.

    Only fixed SELECT SQL is used.
    Users and Gemini cannot supply executable SQL.
    """

    vector = get_embedding(question)

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

    mentioned_machines = list(dict.fromkeys(re.findall(r"\b[A-Z]{2,}[A-Z0-9]*-\d{3,}\b", question)))

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
        parameters,
    )

    if not seeds:
        return {
            "chunks": [],
            "images": [],
            "links": [],
            "warnings": [],
        }

    seed_ids = [
        row["chunk_id"]
        for row in seeds
    ]

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
        table_rows AS (
            SELECT r.chunk_id,
                ARRAY_AGG(STRUCT(r.table_id, r.row_number, r.content)) AS table_rows
            FROM {table('document_table_rows')} r
            JOIN context c USING (chunk_id)
            GROUP BY r.chunk_id
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
            IFNULL(t.table_rows, []) AS table_rows,
            IFNULL(l.links, []) AS links,
            IFNULL(r.relationships, []) AS relationships,
            IFNULL(s.sections, []) AS sections
        FROM context c
        LEFT JOIN geometry g USING (chunk_id)
        LEFT JOIN images i USING (chunk_id)
        LEFT JOIN table_rows t USING (chunk_id)
        LEFT JOIN links l USING (chunk_id)
        LEFT JOIN relationships r USING (chunk_id)
        LEFT JOIN sections s USING (chunk_id)
        ORDER BY
            IF(c.chunk_id IN UNNEST(@ids), 0, 1),
            c.page_number,
            c.chunk_id
        LIMIT 100
        """,
        enrich_parameters,
    )

    seed_map = {
        row["chunk_id"]: row
        for row in seeds
    }

    def score(row):
        if row["chunk_id"] in seed_map:
            return (
                0,
                seed_map[row["chunk_id"]]["distance"],
            )

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

        content_terms = set(
            re.findall(
                r"[a-z0-9-]+",
                row["content"].lower(),
            )
        )

        overlap = len(
            content_terms.intersection(terms)
        )

        source_ids = {
            seed.get("source_id")
            for seed in nearby_seeds
        }

        linked = any(
            relation.get("source_id") in source_ids
            or relation.get("target_id") in source_ids
            for relation in row.get("relationships", [])
        )

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

    rows.sort(key=score)

    selected = []
    used_characters = 0

    for row in rows:
        if len(selected) >= 16:
            continue

        if used_characters + len(row["content"]) > 16000:
            continue

        selected.append(row)
        used_characters += len(row["content"])

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

    chunks = {
        row["chunk_id"]: row
        for row in selected
    }

    # Every supplied image/link must retain its source chunk.
    for row in image_rows + link_rows:
        chunks.setdefault(row["chunk_id"], row)

    images = []
    links = []

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

    # QR extraction is offline; chat only reads links for retrieved manual pages.
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

    return {
        "chunks": list(chunks.values()),
        "images": images,
        "links": links,
        "warnings": [],
    }


def check_tables():
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

        if missing:
            raise RuntimeError(
                f"{name}: missing columns {sorted(missing)}"
            )

        report[name] = {
            "rows": item.num_rows,
        }

    return report
