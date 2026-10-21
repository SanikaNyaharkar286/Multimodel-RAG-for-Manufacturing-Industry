"""Offline script: save verified QR links in BigQuery."""

import argparse
import json
import subprocess
from pathlib import Path

from google.cloud import bigquery
from google.oauth2.credentials import Credentials

from scripts.extract_qr_links import valid_url
from app.retrieval.qr_link_search import TABLE_ID, PROJECT, LOCATION


FIELDS = {
    "chunk_id": "STRING",
    "machine_name": "STRING",
    "document_name": "STRING",
    "page_number": "INTEGER",
    "content_type": "STRING",
    "source_id": "STRING",
    "content": "STRING",
    "url": "STRING",
    "origin": "STRING",
    "relationship_status": "STRING",
}


def save_records(records, client=None):
    if not records:
        raise ValueError(
            "Nothing to save: the processor decoded no QR URLs"
        )

    for row in records:
        if (
            not valid_url(row.get("url", ""))
            or row.get("origin") != "document_ai_qr"
        ):
            raise ValueError(
                "Only decoded Document AI QR URLs may be saved"
            )

        if (
            not row.get("chunk_id")
            or int(row.get("page_number", 0)) < 1
            or not row.get("machine_name")
            or not row.get("document_name")
        ):
            raise ValueError(
                "Each record needs a stable ID and valid source page"
            )

    records = list({row["chunk_id"]: row for row in records}.values())
    client = client or bigquery.Client(project=PROJECT)

    client.create_table(
        bigquery.Table(
            TABLE_ID,
            schema=[
                bigquery.SchemaField(name, kind)
                for name, kind in FIELDS.items()
            ],
        ),
        exists_ok=True,
    )

    select_fields = ", ".join(
        (
            f"CAST(JSON_VALUE(item, '$.{name}') AS INT64) "
            f"AS {name}"
        )
        if kind == "INTEGER"
        else f"JSON_VALUE(item, '$.{name}') AS {name}"
        for name, kind in FIELDS.items()
    )

    columns = ", ".join(FIELDS)
    values = ", ".join(f"S.{name}" for name in FIELDS)

    updates = ", ".join(
        f"T.{name}=S.{name}"
        for name in FIELDS
        if name != "chunk_id"
    )

    sql = f"""
        MERGE `{TABLE_ID}` T
        USING (
            SELECT {select_fields}
            FROM UNNEST(JSON_QUERY_ARRAY(@payload)) item
        ) S
        ON T.chunk_id = S.chunk_id

        WHEN MATCHED THEN
            UPDATE SET {updates}

        WHEN NOT MATCHED THEN
            INSERT ({columns})
            VALUES ({values})
    """

    config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter(
                "payload",
                "STRING",
                json.dumps(records),
            )
        ]
    )

    client.query(
        sql,
        location=LOCATION,
        job_config=config,
    ).result()

    return len(records)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("file")
    parser.add_argument("--verified", action="store_true", required=True,
                        help="Confirm you checked the decoded QR destinations")
    parser.add_argument("--account", help="Google Cloud login with table-write access")
    args = parser.parse_args()
    result = json.loads(
        Path(args.file).read_text(encoding="utf-8")
    )
    if result.get("status") != "decoded":
        parser.error("No decoded Document AI QR URLs to save")
    client = None
    if args.account:
        token = subprocess.run(
            ["gcloud.cmd", "auth", "print-access-token", f"--account={args.account}"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        client = bigquery.Client(project=PROJECT, credentials=Credentials(token=token))
    count = save_records(result["records"], client)

    print(f"Saved {count} QR link records into {TABLE_ID}")
