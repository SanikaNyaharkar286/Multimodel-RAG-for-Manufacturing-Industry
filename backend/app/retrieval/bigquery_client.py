# this file connects to bigquery and runs database queries

from functools import lru_cache
from google.cloud import bigquery
from app import config as cfg
from app.observability.timing import span


# creates and reuses the bigquery client
@lru_cache(maxsize=1)
def bq_client():
    return bigquery.Client(project=cfg.PROJECT)


# converts bigquery rows and nested values into regular python data
def plain(value):
    if isinstance(value, bigquery.Row):
        return {
            key: plain(value[key])
            for key in value.keys()
        }

    if isinstance(value, list):
        return [plain(item) for item in value]

    if isinstance(value, dict):
        return {
            key: plain(item)
            for key, item in value.items()
        }

    return value


# runs a bigquery query and returns its results
def query(sql, parameters, operation='bq_query'):
    # set query parameters and limit the amount billed
    job_config = bigquery.QueryJobConfig(
        query_parameters=parameters,
        maximum_bytes_billed=100 * 1024 * 1024,
    )

    # run the query with location and time limits
    with span(operation) as measurements:
        rows = bq_client().query_and_wait(sql, job_config=job_config,
            location=cfg.BQ_LOCATION, api_timeout=20, wait_timeout=25)
        result = [plain(row) for row in rows]
        for field, key in (('job_id', 'job_id'), ('total_bytes_processed', 'bytes_processed'),
                           ('total_bytes_billed', 'bytes_billed'), ('cache_hit', 'cache_hit')):
            value = getattr(rows, field, None)
            if isinstance(value, (str, int, bool)):
                measurements[key] = value
        return result
