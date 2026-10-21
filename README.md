

<<<<<<< HEAD
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
=======
# Multimodel-RAG-for-Manufacturing-Industry

Project documentation: [Backend and Agent Guide](BACKEND_AND_AGENT_GUIDE.md) · [Frontend Guide](FRONTEND_GUIDE.md).

Performance monitoring: [Latency and Logging Guide](docs/LATENCY_AND_LOGGING_GUIDE.md), including local logs, browser inspection and BigQuery setup/reporting.

Run these commands from the repository folder in two PowerShell terminals:

```powershell
# Terminal 1: backend, using its own Python environment
.\backend\run.cmd

# Terminal 2: frontend
.\frontend\run.cmd
```

Open http://localhost:5173. The API runs at http://127.0.0.1:8000
and its health endpoint is `/health`.

For a fresh installation:

```powershell
python -m venv backend/.venv
.\backend\.venv\Scripts\python.exe -m pip install -r backend/requirements.txt
npm.cmd --prefix frontend ci
```

Configure Google Cloud and manual storage settings in
`backend/.env` and authenticate with Google Cloud application
default credentials before using chat. The provided backend launcher uses
`backend/.venv`. An additional `.venv-agent` environment is currently retained
because a running Python process was using it during cleanup.

If chat reports `iam.serviceAccounts.getAccessToken` denied, the configured
application default credentials use service-account impersonation. A Google
Cloud administrator must verify that the target service account exists and grant
the calling account `roles/iam.serviceAccountTokenCreator` on that service account.
This permission error cannot be fixed by reinstalling Uvicorn.

QR references use Document AI during manual ingestion, then BigQuery during chat.
OpenCV is no longer used. Processor and application settings are in `backend/.env`.

From `backend`, test a PDF page or a PNG/JPEG image containing a known QR code:

```powershell
.\.venv\Scripts\python.exe -m scripts.extract_qr_links "path/to/manual.pdf" --machine HSG-2550 --document "manual.pdf" --page 1 --output qr_test_result.json
```

Check the returned URLs against the QR destinations. Only after verification:

```powershell
.\.venv\Scripts\python.exe -m scripts.save_qr_links qr_test_result.json --verified --account=rajwardhanjadhav555@gmail.com
```

This saves links in `manufacturing_rag.document_qr_links`. Chat reads links only
for retrieved manual documents/pages; it does not call Document AI or write data.
Use the exact manual name and original page number. The configured OCR processor
returned no QR data in the initial Operation manual test; QR decoding remains
unverified until a known QR page returns the correct URL. Empty results are not saved.
>>>>>>> rajwardhan
