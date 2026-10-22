# Multimodel-RAG-for-Manufacturing-Industry

Project documentation: [Backend and Agent Guide](BACKEND_AND_AGENT_GUIDE.md) · [Frontend Guide](FRONTEND_GUIDE.md).

Only actual issue and repair requests use interactive guidance. Information
requests receive a full answer without repair controls or a maintenance report,
including informational questions asked during a repair conversation.
A safety warning appears before the
current manual-supported step. Complete it and click **Done** to request the
next step; explanations and typed next-step requests do not advance the repair.
When the issue is fixed, choose **Issue resolved**, confirm the machine, issue,
actual action taken and any confirmed cause, then submit. A maintenance report
with status **Resolved** is generated automatically in the conversation.
Unknown causes remain **Not confirmed**. Proposed actions are not recorded as
completed until the technician confirms them. Reports and repair progress use
the existing temporary session storage and expire with the chat; they are not
saved as files or permanent maintenance records.

Run backend checks with `.\.venv\Scripts\python.exe -m unittest discover -s tests`
from `backend`, and frontend checks with
`npm.cmd --prefix frontend test`, `npm.cmd --prefix frontend run lint` and
`npm.cmd --prefix frontend run build` from the repository root.

Performance monitoring: [Latency and Logging Guide](docs/LATENCY_AND_LOGGING_GUIDE.md), including local logs, browser inspection and BigQuery setup/reporting.

Run these commands from the repository folder in two PowerShell terminals:

```powershell
# Terminal 1: backend, using its own Python environment
.\backend\run.cmd

# Terminal 2: frontend
.\frontend\run.cmd
```

Open http://localhost:5173. The API runs at http://127.0.0.1:8001
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
