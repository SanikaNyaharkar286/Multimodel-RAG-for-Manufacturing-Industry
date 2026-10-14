# Manufacturing RAG Pipeline

This project turns PDF machine manuals into searchable knowledge.

In simple words: we read a manual, pull out text, tables, images, and links, group the content by section, make chunks, and create embeddings so the data can be searched and used in a RAG system.

## How this project works

```text
PDF files in data/raw/
   ↓
main.py / pipeline.py
   ↓
pdf_extractor.py
   ├─ reads text from each page
   ├─ finds tables
   ├─ saves images
   └─ keeps links
   ↓
relationship_builder.py
   ↓
chunk_builder.py
   ↓
embedding_generator.py
   ↓
gcs_ingestor.py + bigquery_loader.py
   ↓
processed files in data/processed/
```

## Main parts of the codebase

- `src/simple_ingestion/main.py` - start point for the app
- `src/simple_ingestion/pipeline.py` - runs the main workflow for one file or all files
- `src/simple_ingestion/pdf_extractor.py` - extracts data from PDF pages
- `src/simple_ingestion/table_extractor.py` - handles tables and merged cells
- `src/simple_ingestion/relationship_builder.py` - groups content into sections
- `src/simple_ingestion/chunk_builder.py` - creates chunks for search
- `src/simple_ingestion/embedding_generator.py` - creates vector embeddings
- `src/simple_ingestion/gcs_ingestor.py` - uploads outputs to Google Cloud Storage
- `src/simple_ingestion/bigquery_loader.py` - loads processed data into BigQuery
- `src/simple_ingestion/config.py` - project settings and environment config

## Input and output folders

- `data/raw/` = source manuals
- `data/processed/` = cleaned outputs for each PDF

Example:

```text
data/
├── raw/
│   └── HSG-2550/
│       └── file.pdf
└── processed/
    └── HSG-2550/
        └── file/
            ├── extraction.json
            ├── relationships.json
            ├── chunks.json
            ├── embedded_chunks.json
            └── assets/
```

## Run locally

Run all PDFs from the repo root:

```powershell
.\.venv\Scripts\python.exe .\src\simple_ingestion\main.py --all
```

Run one PDF:

```powershell
.\.venv\Scripts\python.exe .\src\simple_ingestion\main.py ".\data\raw\HSG-2550\7 - Haas Surface Grinder - Troubleshooting.pdf" "HSG-2550"
```

## Cloud setup

Create a `.env` file with values like:

```env
GCP_PROJECT_ID=
GCS_BUCKET_NAME=
BIGQUERY_DATASET=
GCP_REGION=us-central1
```

This project is built around one simple flow:

PDF input -> extract data -> build structure -> make chunks -> create embeddings -> store in cloud for retrieval.
