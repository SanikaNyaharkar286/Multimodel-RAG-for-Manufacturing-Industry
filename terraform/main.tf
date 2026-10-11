# Terraform resources and provider configuration will go here.

terraform {
  required_version = ">= 1.5.0"

  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 7.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

resource "google_project_service" "required_apis" {
  for_each = toset([
    "storage.googleapis.com",
    "bigquery.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "aiplatform.googleapis.com"
  ])

  project            = var.project_id
  service            = each.value
  disable_on_destroy = false
}

resource "google_storage_bucket" "manufacturing_rag" {
  name                        = var.bucket_name
  project                     = var.project_id
  location                    = var.region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  force_destroy               = false

  versioning {
    enabled = true
  }

  labels = {
    application = "manufacturing-rag"
    environment = "dev"
  }

  depends_on = [
    google_project_service.required_apis
  ]
}

resource "google_bigquery_dataset" "manufacturing_rag" {
  project     = var.project_id
  dataset_id  = var.dataset_id
  location    = var.region
  description = "Manufacturing maintenance RAG data"

  delete_contents_on_destroy = false

  labels = {
    application = "manufacturing-rag"
    environment = "dev"
  }

  depends_on = [
    google_project_service.required_apis
  ]
}

resource "google_bigquery_table" "document_chunks" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.manufacturing_rag.dataset_id
  table_id            = "document_chunks"
  deletion_protection = true

  description = "Extracted document chunks and their embedding vectors"

  schema = jsonencode([
    {
      name = "chunk_id"
      type = "STRING"
      mode = "REQUIRED"
    },
    {
      name = "machine_name"
      type = "STRING"
      mode = "NULLABLE"
    },
    {
      name = "document_name"
      type = "STRING"
      mode = "NULLABLE"
    },
    {
      name = "page_number"
      type = "INT64"
      mode = "NULLABLE"
    },
    {
      name = "content_type"
      type = "STRING"
      mode = "NULLABLE"
    },
    {
      name = "content"
      type = "STRING"
      mode = "NULLABLE"
    },
    {
      name = "asset_uri"
      type = "STRING"
      mode = "NULLABLE"
    },
    {
      name = "embedding"
      type = "FLOAT64"
      mode = "REPEATED"
    },
    {
      name = "source_generation"
      type = "STRING"
      mode = "NULLABLE"
    },
    {
      name = "created_at"
      type = "TIMESTAMP"
      mode = "NULLABLE"
    }
  ])
}

resource "google_bigquery_table" "ingestion_jobs" {
  project             = var.project_id
  dataset_id          = google_bigquery_dataset.manufacturing_rag.dataset_id
  table_id            = "ingestion_jobs"
  deletion_protection = true

  description = "Status and errors for document ingestion jobs"

  schema = jsonencode([
    {
      name = "job_id"
      type = "STRING"
      mode = "REQUIRED"
    },
    {
      name = "bucket_name"
      type = "STRING"
      mode = "NULLABLE"
    },
    {
      name = "object_name"
      type = "STRING"
      mode = "NULLABLE"
    },
    {
      name = "machine_name"
      type = "STRING"
      mode = "NULLABLE"
    },
    {
      name = "status"
      type = "STRING"
      mode = "NULLABLE"
    },
    {
      name = "error_message"
      type = "STRING"
      mode = "NULLABLE"
    },
    {
      name = "started_at"
      type = "TIMESTAMP"
      mode = "NULLABLE"
    },
    {
      name = "completed_at"
      type = "TIMESTAMP"
      mode = "NULLABLE"
    }
  ])
}
