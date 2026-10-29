# sets up the bigquery table that stores api logs, and adds log rows to it.
# used by the setup command and by the log writer (sink.py).
import json
from google.api_core.exceptions import NotFound
from google.cloud import bigquery
from app import config as cfg
from .timing import STAGE_COLUMNS

# full name of the log table: project.dataset.table
TABLE_ID = f'{cfg.PROJECT}.{cfg.API_LOG_DATASET}.{cfg.API_LOG_TABLE}'
# the columns of the log table and their types
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


# makes a bigquery client just for logging.
# it uses a separate service account file if one is set
def log_client():
    # kept separate from the manual search client
    if cfg.API_LOG_CREDENTIALS_FILE:
        return bigquery.Client.from_service_account_json(cfg.API_LOG_CREDENTIALS_FILE,
                                                        project=cfg.PROJECT)
    return bigquery.Client(project=cfg.PROJECT)


# makes sure the log table exists. creates it if missing.
# if it already exists, it checks that the columns and settings match and stops with an error if not.
# it never changes an existing table
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


# gives the sql text to create the log table by hand, if someone prefers that
def create_table_sql():
    types = {'FLOAT': 'FLOAT64', 'INTEGER': 'INT64', 'BOOLEAN': 'BOOL'}
    columns = ',\n'.join('  '+field.name+' '+types.get(field.field_type, field.field_type)
                         + (' NOT NULL' if field.mode == 'REQUIRED' else '') for field in SCHEMA)
    return (f'CREATE TABLE IF NOT EXISTS `{TABLE_ID}` (\n{columns}\n)\n'
            'PARTITION BY DATE(timestamp)\nCLUSTER BY endpoint, status_code\n'
            f'OPTIONS (partition_expiration_days={cfg.API_LOG_RETENTION_DAYS}, require_partition_filter=TRUE);\n')


# adds a batch of log rows to the table.
# the request id is used so the same row is not saved twice. raises an error if bigquery rejects any row
def insert_records(client, rows):
    encoded = [{**row, 'timings_json': json.dumps(row['timings_json'], allow_nan=False)} for row in rows]
    errors = client.insert_rows_json(TABLE_ID, encoded,
        row_ids=[row['request_id'] for row in rows], timeout=10, retry=None)
    if errors:
        # do not log bigquery's error text, it may contain row values
        raise RuntimeError('BigQuery rejected one or more API log rows')