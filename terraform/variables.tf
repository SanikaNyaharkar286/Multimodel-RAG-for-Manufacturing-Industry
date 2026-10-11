# Terraform input variables will go here.

variable "project_id" {
  description = "Google Cloud project ID"
  type        = string
  default     = "market-lens-506611"
}

variable "region" {
  description = "Google Cloud region"
  type        = string
  default     = "us-central1"
}

variable "bucket_name" {
  description = "Globally unique GCS bucket name"
  type        = string
  default     = "market-lens-506611-manufacturing-rag"
}

variable "dataset_id" {
  description = "BigQuery dataset ID"
  type        = string
  default     = "manufacturing_rag"
}
