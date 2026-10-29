# Maintenance report submission and administrator inbox

This feature submits an existing, technician-reviewed maintenance snapshot. It
does not call the agent, change findings, alter the PDF layout, or change manual
retrieval, ingestion, sign-in or approval decisions.

## Files and entry points

New backend files:

- `app/reports/schemas.py`: validated submission/review/notification response models.
- `app/reports/repository.py`: `create_submission`, `list_reports`, `review`,
  `notifications`, `mark_read`, `mark_all_read` and legacy registration adaptation.
- `app/reports/storage.py`: `persist_report`, `read_object`, `read_snapshot`.
- `app/reports/service.py`: `submit`, `detail`, configured email recipients.
- `app/reports/email_worker.py`: independent durable outbox consumer (`claim`,
  `transmitting`, `finish`, `deliver`, `run_once`).
- `app/api/report_submission_routes.py`: technician and administrator endpoints.
- `tests/test_report_submissions.py`: storage, transactions, retries, authorization.
- `firestore.report-submissions.indexes.json`, `scripts/deploy_report_indexes.py`.

New frontend files:

- `components/reports/ReportPdf.jsx`: authenticated PDF preview/download and status.
- `components/reports/reportDisplay.js`: shared date, review and email labels.
- `components/reports/MySubmittedReports.jsx`: owner-scoped persistent submission list.
- `components/reports/AdminReports.jsx`: search, filters, pagination, detail and review.
- `components/reports/AdminNotifications.jsx`: read/unread inbox and navigation.
- `services/reportSubmissionsApi.js`, `styles/submitted-reports.css` and
  `tests/reportSubmissions.test.mjs`.

Modified existing files: backend `app/api/report_routes.py`, `app/main.py`,
`app/services/email_service.py` (reuse SMTP settings for an explicit recipient),
`.env.example`; frontend `src/App.jsx`, `src/auth/AuthGate.jsx`,
`src/auth/approvalLink.js`, `src/components/AdminPortal.jsx`,
`src/components/layout/Header.jsx`,
`src/components/chat/Documentation.jsx`, `src/services/documentationApi.js`,
and `package.json` (include new tests). Earlier unrelated workspace changes are
outside this feature.

## Records, indexes and storage

The existing initialized Firebase Admin app and configured named Firestore
database are reused. No browser accesses these collections directly.

- `maintenanceReports`: unchanged editable draft snapshots (existing collection).
- `maintenance_reports/{report_id}`: submitted report metadata; its document ID
  is the original collision-resistant UUID. `report_number` is the human-facing
  TMR number. Ownership comes from Firebase, not the request body.
- `maintenance_reports/{report_id}/review_events`: append-only admin review audit.
- `admin_notifications/{sha256(kind:reference:admin_uid)}`: individual read state
  per approved admin. Report references use the canonical report document ID.
- `maintenance_report_email_jobs/{sha256(report_id:recipient)}`: durable per-recipient
  outbox records with retry time, attempt count, lease, stable Message-ID and status.
- `admin_notification_sync/{sha256(admin_uid)}`: progress while adapting existing
  `adminNotifications` registration requests. At most 200 older registrations are
  adapted per poll, then the cursor advances. Existing registration email and
  decisions remain unchanged; no duplicate registration delivery system is created.

Report submission metadata, new notifications and delivery jobs use one atomic
Firestore create batch, including real `SERVER_TIMESTAMP` fields. Concurrent or
retried submission returns the original record, without another email job. The
same PDF is used after restart, and submitted report edits are rejected with 409.

Private GCS objects use
`maintenance-reports/{sha256(owner_uid)}/{report_uuid}/revision-{revision}/` with
`report.pdf` and compressed `snapshot.json.gz`. Snapshot JSON preserves exact
report/evidence data for authorized access after restart; binary files are not
added to Firestore. Generation-zero create preconditions prevent replacement;
generation, size and SHA-256 are checked on reads. Uploads are verified before
submission is committed. If Firestore fails after upload, retries reuse the
verified objects. Do not configure bucket lifecycle deletion for committed report
objects. Failed/abandoned uploads require an operator cleanup policy that checks
Firestore references; there is no automatic deletion of submitted evidence.

Create the composite indexes in `firestore.report-submissions.indexes.json` in
the configured database before enabling the feature. Merge them into your
existing Firebase index deployment, or from `backend` run:

```powershell
.\.venv\Scripts\python.exe scripts/deploy_report_indexes.py --project YOUR_PROJECT --database YOUR_DATABASE
# After reviewing the validated definitions, deploy:
.\.venv\Scripts\python.exe scripts/deploy_report_indexes.py --project YOUR_PROJECT --database YOUR_DATABASE --apply
```

The script defaults to validation without cloud changes. Deployment skips existing
indexes and stops on other errors. Wait for indexes to become READY. Single-field indexing must remain enabled for timestamps,
review status, owner, job report ID and search tokens. Native search indexes
case-insensitive word/phrase prefixes from report IDs, technician, machine and
the issue metadata. It is not a full-text search service: the API accepts 80
characters, at most 2,000 search terms are stored per report, and searchable issue
metadata is 2,000 characters. The original report, findings and PDF are not
truncated. Pagination uses bounded page/page-size parameters (up to 50 per page).

Index builds are requested together before the deployment utility waits for
completion. `--no-wait` submits builds and returns immediately; it does not mean
the indexes are usable yet. While an index is missing or building, report and
notification APIs return 503 with `Retry-After: 30` and a clear readiness message.
The admin inbox retains its last successful data and offers Retry notifications;
it does not replace a failed read with an empty inbox or zero unread count.

## Configuration and deployment

Reuse existing settings:

- `AUTH_ENABLED=true`, `FIREBASE_PROJECT`, `FIRESTORE_DATABASE_ID`.
- `GOOGLE_CLOUD_PROJECT`, `GCS_BUCKET_NAME` (existing private bucket).
- `SMTP_HOST`, `SMTP_PORT`, `SMTP_USERNAME`, `SMTP_PASSWORD`, `SMTP_FROM_EMAIL`,
  `SMTP_FROM_NAME`, `SMTP_USE_TLS` (the existing SMTP provider).
- `APPROVAL_PORTAL_URL`: administrator-reachable frontend URL. Production should
  use HTTPS. Email report links point to `?admin=reports&report=<canonical_id>`;
  opening a link grants no access or role.

New optional settings:

- `ADMIN_NOTIFICATION_EMAILS`: comma/semicolon-separated addresses (maximum 100).
  Falls back to existing `ADMIN_NOTIFICATION_EMAIL`. Addresses do not grant admin
  privileges; actual portal recipients are approved `users` with role `admin`.
- `REPORT_EMAIL_MAX_MESSAGE_BYTES=20971520`: total MIME message budget including
  base64 overhead. Oversized attachments use the authenticated admin page link.

Keep secrets only on the backend, using existing environment/Secret Manager
injection. No API keys or bucket credentials are included in React. Missing or
invalid recipients do not lose the submitted PDF: submission succeeds with an
explicit email-configuration failure. Configure recipients before accepting real
submissions: there is no automatic resubmission or admin resend action.

The API and worker runtime identities need Firestore entity read/write/transaction
access (`roles/datastore.user` on the existing project/database). Prefer a custom
GCS role with `storage.objects.create` and `storage.objects.get`, scoped to the
report prefix on the existing bucket, alongside existing manual read permissions.
Do not grant `allUsers` access. Enforce uniform bucket-level access and public
access prevention according to the existing bucket policy. The API creates
reports; a separate worker identity can use read-only GCS access. Index deployment
needs `roles/datastore.indexAdmin`, only for the deployment identity.

Use ADC locally and Workload Identity/service identity in production. Enable
Firestore and Cloud Storage APIs in the existing projects. The worker needs
outbound TLS access to the configured SMTP provider. If using Secret Manager,
grant secret accessor only on the needed SMTP secrets.

## Durable email worker

API requests only write durable Firestore delivery records. Email is not sent by
FastAPI BackgroundTasks. Run the consumer under a process supervisor, or deploy
the same backend image as a Cloud Run Job with a one-minute Cloud Scheduler
trigger and the command `python -m app.reports.email_worker --once`. A continuously
running worker polls every 20 seconds. Set worker task timeout above the maximum
batch runtime (up to 50 sequential SMTP attempts; e.g. 60 minutes), or run the
supervised process. Leases prevent two workers sending the same job concurrently.
Provide the same project/database/bucket configuration and SMTP secret injection
to the worker. Alert on worker failures, growing pending queues, failed deliveries
and expired leases. No extra SDK dependency or second database is needed.

Clear transient failures retry exponentially (30 seconds up to one hour, eight
attempts). Known permanent SMTP rejection is recorded as failed. A crash during
preparation can be reclaimed; a crash/disconnect after transmission may have
started is flagged `delivery_uncertain` and held for operator investigation.
SMTP cannot guarantee exactly-once delivery. The stable Message-ID is not a
provider idempotency key. Do not blindly reset uncertain jobs: inspect provider
logs first. A definite unsent/configuration failure can be requeued by a trusted
operator after correcting configuration; there is no public resend endpoint.

`email_status=sent` means the SMTP server accepted the message; `email_delivery`
is `accepted_by_smtp`, never `delivered`. The UI explicitly explains this. SMTP
does not supply a standardized provider message ID: `provider_message_id` remains
empty rather than inventing one. Each job has a real stable RFC Message-ID.
Email failure never deletes the report or its admin notification.

## Run locally

From `backend` in one PowerShell terminal:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m uvicorn app.main:app --host 127.0.0.1 --port 8001 --reload
```

In a second terminal in `backend` (this sends real queued email):

```powershell
.\.venv\Scripts\python.exe -X utf8 -m app.reports.email_worker
```

From `frontend`:

```powershell
npm.cmd run dev
```

Use approved technician and administrator accounts from the existing auth flow.
Match the API port to the frontend's existing `VITE_API_URL` (default port 8001).
The new submission/admin APIs reject auth-disabled mode. Existing local draft
and troubleshooting behavior is unchanged.

## Verify the complete workflow

1. Deploy indexes, configure SMTP/recipients and run the worker. Sign in as an
   approved technician; generate/review a maintenance report. Leave any
   unperformed steps Pending and check the existing PDF layout.
2. Select Send to Admin. Edits are saved first, then an immutable snapshot is
   submitted. During submission the button is disabled. Check the submitted
   timestamp and separate email state. Double clicks/retries must not create
   additional report documents, notification documents or delivery jobs.
3. Refresh and open Reports → My Submitted Reports. View/download the same PDF;
   restart the API and verify it is still available. The record is server-backed,
   not a localStorage substitute. A lost HTTP response is reconciled before retry.
4. Sign in as an admin. The bell updates within 20 seconds, with separate unread
   records for report submissions and existing registration requests. Check mark
   one/read all, refresh persistence, and navigation to each relevant record.
5. Open Maintenance Reports; search a word/phrase prefix, filter, sort and page.
   Open the actual report detail and PDF, record Under Review, add a comment,
   then mark Reviewed. Check the technician's list updates. The PDF must remain
   identical; administrative review is not return-to-service authorization.
6. Use a second technician: access to the first technician's report and PDF must
   return 404. Technician calls to admin APIs return 403; unauthenticated calls
   return 401. Unapproved users cannot submit.
7. With mocked services, test upload/Firestore errors and retry recovery, SMTP
   rejection, expired leases, duplicate worker claims and attachment-size fallback.
   Do not test destructive failures against production data.

Automated backend tests: `python -m unittest discover -s tests -p test_report_submissions.py`.
Frontend: `npm.cmd test` and `npm.cmd run build`. The suite mocks GCP/SMTP and
does not send real emails or provision cloud resources.

Cloud indexes, IAM, public-access policy, SMTP credentials/provider limits, worker
deployment/supervision, and an external end-to-end mail-delivery check require
the deployment administrator. They are not silently changed by running tests.

References: [GCS create preconditions](https://docs.cloud.google.com/storage/docs/request-preconditions),
[Firestore query cursors](https://docs.cloud.google.com/firestore/native/docs/query-data/query-cursors).

## Validation recorded for this implementation

- Backend submission tests: 20/20 passed, with mocked GCP/SMTP boundaries;
  index deployment utility tests: 3/3 passed.
- Initial full backend suite: 222/223 passed. The existing
  `test_auth_profile.CurrentProfileTests.test_me_returns_only_safe_fields_for_verified_identity`
  fails because the existing profile response contains `decisionReason` while
  its assertion excludes that field. This feature does not change that behavior.
- Frontend tests: 70/70 passed, including seven new component/API workflow tests.
- Frontend production build passed; the existing large-bundle warning remains.
- Repository lint still reports the existing hook naming error in
  `frontend/tests/useChat.test.mjs:120`; it was not changed for this feature.
- All 14 index definitions validate with the installed Firestore Admin SDK.
  The validation command itself makes no cloud changes. During the subsequent
  notification fix, these 14 indexes were deployed to the existing
  `market-lens-506611/manufacturing-agent` database and verified READY.
- Existing Firebase auth, recipient settings and SMTP configuration validate
  locally. No live email was sent; the independent email worker was not deployed.
- Live notification fix verification: the approved-admin inbox and its unread
  count loaded and passed response-schema validation. Admin report listing,
  owner-scoped listing, status/search filters and ascending sort also succeeded
  against Firestore. Existing registration notifications were adapted through
  the same idempotent per-admin mechanism used by the API.

Additional safeguards beyond the basic submission UI include SHA-256/generation
checks, a compressed immutable snapshot beside the PDF, review audit events,
optimistic review conflicts, stale-request protection, bounded browser request
timeouts, lost-response reconciliation, and held ambiguous SMTP outcomes.
