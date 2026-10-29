Manufacturing RAG pipeline integration
========================================

Files
-----
- main.py: CLI/orchestration entry point.
- pipeline.py: existing PDF processing plus a processed-artifact path that can
  rebuild chunks from extraction.json and relationships.json without rerunning
  segmentation or extraction.
- bigquery_loader.py: the generated GCS -> BigQuery loader. Review its mappings
  against your actual BigQuery table schemas before committing.

Install
-------
Copy these three files into:
src1/simple_ingestion/

Keep backups of your existing main.py and pipeline.py first.

Safety notes
------------
- The processed-artifact mode validates UUIDs before saving chunks.json.
- BigQuery loading is optional and defaults to dry-run (no writes to the target
  RAG tables). The current loader's dry-run still generates embeddings, so it
  can incur Vertex AI embedding charges.
- Uploading to GCS also runs the existing GCS audit transfer, which may write
  audit rows to BigQuery.
- Do not use --commit-bigquery until the loader mappings and destination table
  schemas have been verified.
- This integration has been syntax-checked, but it has not been executed against
  your Google Cloud project or validated against live table schemas.
