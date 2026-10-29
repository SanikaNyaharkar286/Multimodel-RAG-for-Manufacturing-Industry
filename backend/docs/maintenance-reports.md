# Technical Service & Maintenance Reports

The existing report routes now create an owner-scoped, immutable snapshot for a
selected troubleshooting case or information answer. The report uses current
structured session records, authenticated profile details, existing manual
guidance, technician uploads and report-review confirmations. Report generation
does not call an LLM, run retrieval, alter BigQuery, or modify ingestion.

## Three-page maintenance PDF

Maintenance PDFs now use exactly three controlled A4 pages:

1. Report ID/date/status, machine details, authenticated technician details and
   the reported issue. Optional unavailable equipment metadata is omitted.
2. Only confirmed performed troubleshooting actions, their actual observations
   and statuses, plus a short findings/corrective-action/safety summary. Pending,
   skipped and unperformed recommendations are excluded from the printed table.
   Relevant saved evidence can appear here when space permits.
3. Final status, recorded follow-up recommendations and a fixed signature table
   at the bottom. Signature/date cells stay blank for handwriting; preparation,
   review and operational authorization are distinct.

No transcript, full assistant response, repeated per-step explanation, attachment
listing or separate diagram page is printed in a maintenance report. These
records remain in the existing snapshot/review screen. Long fields are marked
with an ellipsis and an abbreviation notice; large histories use a chronological
selection (including failed attempts where possible) plus an explicit omitted
count. They never silently become complete transcripts or overflow to page 4.
The body font is not shrunk to fit. Information-answer PDF exports retain their
existing selected-answer behavior.

The same `render_pdf` entry point is used by authenticated downloads and the
existing admin/email submission path. Newly generated/submitted maintenance
PDFs use this layout; already submitted immutable files are not rewritten.

Files changed for this redesign: `app/responses/maintenance_pdf.py`, new
`app/responses/maintenance_summary.py`, `tests/test_maintenance_pdf.py`, new
`tests/test_maintenance_summary.py`, `tests/test_maintenance_reports.py`,
`tests/test_case_flow.py`, `tests/test_report_submissions.py`, and this guide.

## Use

1. Start the backend as usual. The dependency list now includes `reportlab==5.1.0`;
   install `backend/requirements.txt` into the existing virtual environment if
   needed. Start the React frontend using its existing `npm run dev` command.
2. In an active case, select **Generate Maintenance Report**. This is also
   available while the investigation is ongoing. Finished cases open their
   existing documentation. Information-answer PDF downloads are retained.
3. Review the grouped machine, service, fault, repair, verification and status
   fields. Authenticated identity fields are read-only. Machine name stays bound
   to the incident when already known; a plant asset ID is a separate field.
4. Expand the troubleshooting log to record each step's actual status,
   observations, result, work performed, parts and completion date/time. Pending
   recommendations are never automatically completed. Review supporting images
   and the recorded manual guidance.
5. Select **Save draft** or **Download PDF**. Download saves changed fields first.
   Conflicting revisions require reopening the latest version. Incomplete and
   unresolved cases can be downloaded without asserting a repair or approval.

## Accuracy and persistence

- A Done confirmation records performed work, not a successful test. Tests
  remain **Not tested** until explicitly recorded.
- Report-review edits do not advance or change the live troubleshooting case.
  Retracting a confirmation also updates a summary derived from confirmations.
- Unknown asset IDs, serials, dates, measurements and parts remain unrecorded.
  Machine name is not automatically copied into the asset-ID or model field.
- Issue duration uses a confirmed onset date and calendar days in Asia/Calcutta.
  Actual downtime is separate. The chat creation date is not a fault-start date.
- Only existing images directly associated with performed steps can appear in
  the compact maintenance PDF, when there is room. Other saved images remain
  available in the report review. Existing ownership checks remain in force.
- The existing report persistence service remains in use. With authentication
  enabled it stores reports in the existing Firestore collection; local mode
  uses memory. No new database, collection, session TTL or chat checkpoint is
  introduced. A saved report snapshot is distinct from temporary ADK sessions.
- Preparation, technical review, approval, issue resolution and operational
  condition are separate. Administrative submission/review uses the existing
  workflow; it does not authorize operation. Return-to-service stays **Not
  authorized** without an authorized release record. Generating a PDF does not
  submit it to an OEM or fabricate signatures.
- Customer/company data and previous asset maintenance history remain disabled
  and documented as future sections in the source. Neither is rendered.

## Modules and validation

- `app/responses/maintenance_report.py`: snapshot normalization, authenticated
  profile binding, legacy report projection, dates and step confirmations.
- `app/responses/maintenance_pdf.py`: light-theme A4 ReportLab layout, numbered
  sections, fixed three-page maintenance frames and bottom signature placement.
- `app/responses/maintenance_summary.py`: pure, non-mutating performed-step
  projection using session completions and recorded report confirmations.
- `app/responses/documentation.py`: existing snapshot/storage interface and PDF
  delegation; `app/api/report_routes.py`: authenticated create/edit/download.
- `app/api/chat_routes.py`: reuses profile binding when a closed case creates a
  report; the troubleshooting behavior is unchanged.
- Frontend `Documentation.jsx`, `reportReview.js`, `ReportEvidence.jsx` and
  `report-review.css`: grouped review, per-step confirmation and image preview.
- `ResponseRenderer.jsx`: exposes the draft report action during an active case.
- `documentationApi.js`: authenticated downloads, token refresh and conflicts.

Run the report checks from `backend` with the existing virtual environment:

```powershell
.\.venv-agent\Scripts\python.exe -X utf8 -m unittest tests.test_maintenance_reports tests.test_maintenance_pdf tests.test_maintenance_summary tests.test_report_submissions -q
```

Run `npm test` and `npm run build` from `frontend`. Layout test fixtures use
synthetic data; they are not production service records.
