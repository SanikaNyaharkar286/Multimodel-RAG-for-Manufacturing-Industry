
# Multimodel-RAG-for-Manufacturing-Industry

Project documentation: [Backend and Agent Guide](BACKEND_AND_AGENT_GUIDE.md) · [Frontend Guide](FRONTEND_GUIDE.md).

Only actual issue and repair requests use interactive guidance. Information
requests receive the full relevant answer, manual references, selected figures,
and an editable **Create PDF** documentation preview. An information follow-up
does not advance or hide the active repair controls.

For troubleshooting, Flash prepares the private plan while an independent
Flash-Lite task selects preliminary safety precautions. Safety is delivered
through authenticated progress updates; there is no artificial display delay.
Only one step is revealed. **Done — Next Step** records completion and reveals
the saved next action without a model or retrieval call. Conditional steps
require an observed result. **Add Details / Upload Image** keeps evidence in the
same case and can revise the plan version without losing completed history.
Related follow-ups and repeated questions stay with the current case. A clearly
different repair issue starts a new case automatically in the same conversation,
with the earlier case archived; no case-switch confirmation is required.

**Issue Resolved** and **Still Not Resolved — Create Report** open the editable
case documentation. Resolution is technician-reported, never independently
verified by the model. Report changes persist, and PDF export includes only the
selected case or information answer, including its images and references.

Conversation snapshots and reports use local SQLite in `backend/data/` (ignored
by Git). Snapshots retain the configured session expiry; saved reports retain
their own embedded image copies. Run one backend worker with a persistent data
directory. Authentication and approval remain required, including ownership
checks for conversations, progress, images and reports.

Implementation and validation details: [Response Flow Guide](docs/RESPONSE_FLOW.md).

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
>>>>>>> rajwardhan
