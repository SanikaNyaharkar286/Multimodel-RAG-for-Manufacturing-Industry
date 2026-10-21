"""BigQuery schema/provisioning shared by setup CLI and the separate log writer."""
import json
from google.api_core.exceptions import NotFound
from google.cloud import bigquery
from app import config as cfg
from .timing import STAGE_COLUMNS

TABLE_ID = f'{cfg.PROJECT}.{cfg.API_LOG_DATASET}.{cfg.API_LOG_TABLE}'
SCHEMA = [
    bigquery.SchemaField('timestamp', 'TIMESTAMP', mode='REQUIRED'),
    bigquery.SchemaField('request_id', 'STRING', mode='REQUIRED'),
    *[bigquery.SchemaField(name, 'STRING') for name in
      ('method', 'endpoint', 'outcome', 'session_hash', 'client_request_id', 'error_type')],
    bigquery.SchemaField('status_code', 'INTEGER'),
    bigquery.SchemaField('success', 'BOOLEAN'),
    bigquery.SchemaField('total_ms', 'FLOAT'),
    *[bigquery.SchemaField(name, 'FLOAT') for name in STAGE_COLUMNS],
    *[bigquery.SchemaField(name, 'BOOLEAN') for name in
      ('has_image', 'reused_image', 'planner_fallback', 'events_truncated')],
    *[bigquery.SchemaField(name, 'INTEGER') for name in
      ('question_length', 'image_size_bytes', 'normalized_image_bytes', 'chunk_count',
       'response_image_count', 'response_link_count', 'asset_warning_count',
       'model_image_bytes', 'payload_characters', 'input_tokens', 'output_tokens',
       'thinking_tokens', 'model_calls', 'cache_hits', 'cache_misses')],
    bigquery.SchemaField('timings_json', 'JSON'),
]


def log_client():
    # Separate client from manual retrieval, optionally using a dedicated writer identity.
    if cfg.API_LOG_CREDENTIALS_FILE:
        return bigquery.Client.from_service_account_json(cfg.API_LOG_CREDENTIALS_FILE,
                                                        project=cfg.PROJECT)
    return bigquery.Client(project=cfg.PROJECT)


def provision(client):
    table = bigquery.Table(TABLE_ID, schema=SCHEMA)
    table.time_partitioning = bigquery.TimePartitioning(
        field='timestamp', expiration_ms=cfg.API_LOG_RETENTION_DAYS*86400000)
    table.clustering_fields = ['endpoint', 'status_code']
    table.require_partition_filter = True
    table.description = 'API request summaries. No prompts, image bytes or raw session IDs.'
    try:
        created = client.get_table(TABLE_ID, timeout=15)
    except NotFound:
        created = client.create_table(table, exists_ok=True, timeout=15)
    expected = {(field.name, field.field_type) for field in SCHEMA}
    if not expected.issubset({(field.name, field.field_type) for field in created.schema}):
        raise RuntimeError('Existing API log table schema does not match; no schema was changed')
    if not created.time_partitioning or created.time_partitioning.field != 'timestamp':
        raise RuntimeError('Existing API log table must be partitioned on timestamp')
    if (created.time_partitioning.expiration_ms != cfg.API_LOG_RETENTION_DAYS*86400000
            or created.clustering_fields != ['endpoint', 'status_code']
            or not created.require_partition_filter):
        raise RuntimeError('Existing API log table retention/clustering/filter policy differs; no policy was changed')
    return created


def create_table_sql():
    types = {'FLOAT': 'FLOAT64', 'INTEGER': 'INT64', 'BOOLEAN': 'BOOL'}
    columns = ',\n'.join('  '+field.name+' '+types.get(field.field_type, field.field_type)
                         + (' NOT NULL' if field.mode == 'REQUIRED' else '') for field in SCHEMA)
    return (f'CREATE TABLE IF NOT EXISTS `{TABLE_ID}` (\n{columns}\n)\n'
            'PARTITION BY DATE(timestamp)\nCLUSTER BY endpoint, status_code\n'
            f'OPTIONS (partition_expiration_days={cfg.API_LOG_RETENTION_DAYS}, require_partition_filter=TRUE);\n')


def insert_records(client, rows):
    encoded = [{**row, 'timings_json': json.dumps(row['timings_json'], allow_nan=False)} for row in rows]
    errors = client.insert_rows_json(TABLE_ID, encoded,
        row_ids=[row['request_id'] for row in rows], timeout=10, retry=None)
    if errors:
        # Do not log provider error messages, which may contain row values.
        raise RuntimeError('BigQuery rejected one or more API log rows')
