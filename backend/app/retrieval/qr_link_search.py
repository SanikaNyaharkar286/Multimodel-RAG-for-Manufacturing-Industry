# this file reads saved document ai qr links from bigquery

"""Read Document AI QR links saved during manual ingestion."""

import re

from google.api_core.exceptions import NotFound
from google.cloud import bigquery

from app import config as cfg
from app.observability.timing import timed

# get the bigquery project, location, and qr links table
PROJECT = cfg.PROJECT
LOCATION = cfg.BQ_LOCATION
TABLE_ID = f"{PROJECT}.{cfg.DATASET}.{cfg.QR_LINKS_TABLE}"


# gets saved qr links for selected pages in one document
def get_saved_qr_links(machine_name, document_name, pages, client=None):
    return get_saved_qr_links_for_pages(
        machine_name, [(document_name, page) for page in pages], client,
    )


# gets saved qr links for selected pages
@timed('qr_query')
def get_saved_qr_links_for_pages(machine_name, pages, client=None):
    # return an empty list if no pages were provided
    if not pages:
        return []

    # create page keys to match documents and page numbers
    sources = sorted({
        re.sub(r"\.pdf$", "", document, flags=re.I).lower() + f":{page}"
        for document, page in pages
    })

    # search for qr links from document ai
    sql = rf"""
        SELECT * FROM `{TABLE_ID}`
        WHERE machine_name = @machine
          AND CONCAT(REGEXP_REPLACE(LOWER(document_name), r'\.pdf$', ''),
                     ':', CAST(page_number AS STRING)) IN UNNEST(@sources)
          AND origin = 'document_ai_qr'
        ORDER BY page_number, chunk_id
    """

    # pass query values safely and set a billing limit
    config = bigquery.QueryJobConfig(
        maximum_bytes_billed=100_000_000,
        query_parameters=[
            bigquery.ScalarQueryParameter("machine", "STRING", machine_name),
            bigquery.ArrayQueryParameter("sources", "STRING", sources),
        ],
    )

    # use the provided client or create a bigquery client
    client = client or bigquery.Client(project=PROJECT)

    try:
        # run the query and return its rows as dictionaries
        rows = client.query_and_wait(
            sql,
            job_config=config,
            location=LOCATION,
            api_timeout=20,
            wait_timeout=25,
        )
        return [dict(row.items()) for row in rows]

    except NotFound:
        # return no qr links if the table does not exist yet
        return []
