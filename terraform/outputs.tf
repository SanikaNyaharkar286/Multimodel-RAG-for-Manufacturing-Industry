# Terraform outputs will go here.

output "bucket_name" {
  description = "Name of the GCS bucket"
  value       = google_storage_bucket.manufacturing_rag.name
}

output "raw_prefix" {
  description = "Upload PDFs under this prefix"
  value       = "raw/"
}

output "processed_prefix" {
  description = "Processed artifacts will use this prefix"
  value       = "processed/"
}

output "bigquery_dataset" {
  description = "BigQuery dataset ID"
  value       = google_bigquery_dataset.manufacturing_rag.dataset_id
}

output "document_chunks_table" {
  value = google_bigquery_table.document_chunks.table_id
}

output "ingestion_jobs_table" {
  value = google_bigquery_table.ingestion_jobs.table_id
}
