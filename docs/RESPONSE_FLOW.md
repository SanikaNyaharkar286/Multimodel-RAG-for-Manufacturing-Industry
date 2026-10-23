# Response flow and documentation

The current configured Flash / Flash-Lite versions and manual ingestion,
BigQuery, Cloud Storage and Firebase integrations are retained. No cloud
service or new dependency was added.

## Request processing

1. The existing intent planner classifies text, uploads and context.
2. An obvious repair request starts the independent safety task at submission;
   other repair intents start it when classified. Flash-Lite selects from the
   reviewed prerequisite catalog. Its call is bounded at 3 seconds and the
   async wait at 3.5 seconds, then reviewed task-relevant fallback is used.
3. Retrieval and final generation proceed without waiting for that task.
   Flash handles repairs, images and large evidence bundles. Flash-Lite handles
   smaller text-only information answers. Separate runners avoid shared model
   mutation between concurrent conversations.
4. Authenticated progress polling supplies actual stage changes and safety
   independently. The final repair response also includes prerequisites before
   the action. Five seconds is a target; provider and authentication latency
   are not guaranteed. Generic precautions are labeled as reviewed general
   guidance; machine-specific precautions come from retrieved step evidence.
5. All returned plan actions, assets, evidence IDs and branch targets are
   validated. Only the current step, visible sources and previously presented
   history are public; remaining answers are private in the session store.

## Step state

- Every case has a stable workflow ID, plan version and current step ID.
- Done validates those fields and the required observation choice, if any.
  It records completion and releases a prepared answer from cache. It never
  calls the model just to display that answer. Branches target later prepared
  step keys or END; invalid/backward targets are rejected.
- Conditional progress does not claim a known total until the path permits it.
- Additional text/images remain attached to the current step. Clarification
  does not complete a step. Reported new evidence can invalidate the old future
  plan, increment its version and replace the remaining procedure.
- The planner automatically routes related follow-ups and repeated questions
  to the current case. A different repair issue starts a new case immediately
  in the same conversation, preserving the earlier case history. No case-switch
  confirmation is required. Stale controls and responses are rejected.
- Technician safety attestations are separate from displayed warnings.
- A request ID prevents duplicate transitions on retries. A separate step
  ID/version check protects against double clicks with different request IDs.
- Resolved means technician-reported resolution. End of a plan does not prove
  resolution. Unresolved reports remain available.

## Persistence, authorization and PDFs

`backend/data/workspace.sqlite3` stores conversation snapshots and report
documents. The existing in-memory ADK/session machinery still executes active
requests. A snapshot restores that machinery, private plans and image bytes
after restart until the original session expiry. Refresh can reconnect to a
running request through its progress ID. SQLite connections are closed after
each transaction; report revisions use atomic compare-and-update.

Session creation binds Firebase UID ownership. Chat, session read/delete,
progress, images, reports and report PDFs require the existing approved-account
dependency and owner checks. Reports embed only the selected case's presented
images, so PDF exports remain usable after conversation assets expire.

The documentation editor saves editable fields separately from the immutable
conversation/action snapshot. Missing facts are not fabricated. PDF generation
uses the already-installed PyMuPDF engine and embeds approved/technician images,
tables, manual/page references, local creation time and page numbers. It exports
the selected report rather than printing the entire application.

## Main implementation files

- `backend/app/agent/chat_service.py`: orchestration, isolated model routing,
  prepared plans, branch selection, visible-step filtering and follow-ups.
- `backend/app/agent/safety.py`: independent bounded safety generation.
- `backend/app/agent/repair_workflow.py`, `responses/schemas.py`: versioned
  actions, observations, confirmation records and structured step metadata.
- `backend/app/api/chat_routes.py`, `session_routes.py`, `image_routes.py`:
  ownership, progress, receipts and conversation snapshots.
- `backend/app/sessions/persistence.py`, `session_store.py`: SQLite snapshots
  and report storage; ADK restoration.
- `backend/app/api/report_routes.py`, `responses/documentation.py`: case-scoped
  report creation, editing, embedded images and PDF download.
- `frontend/src/hooks/useChat.js`, `services/chatService.js`: immediate answers,
  progress, retry IDs, reconnect and current-step context.
- `frontend/src/components/chat/RepairControls.jsx`, `ChatThread.jsx`,
  `ChatMessage.jsx`, `ResponseRenderer.jsx`, `ChatInput.jsx`: active controls,
  completed history, real status messages, image preview and references.
- `frontend/src/components/chat/Documentation.jsx`,
  `services/documentationApi.js`: editable report preview and real PDF export.
- `frontend/src/styles/agent-response.css`: responsive step/report interfaces.

## Validation and operation

Run from the repository root in separate PowerShell terminals:

```powershell
.\backend\run.cmd
```

```powershell
.\frontend\run.cmd
```

The launchers use the existing backend virtual environment, API port 8001 and
frontend port 5173. Run checks with:

```powershell
cd backend
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

```powershell
npm.cmd --prefix frontend test
npm.cmd --prefix frontend run lint
npm.cmd --prefix frontend run build
```

New tests exercise safety/main concurrency, conditional cache advancement,
plan-version rejection, duplicate request handling, cross-user access,
separate-case history, revised plans, snapshot restoration, isolated report
selection, report editing and multi-page PDF/image rendering.

Existing logging remains active. `timings_json` includes planner
(classification), safety generation/readiness, retrieval, agent generation,
repair-plan advance and PDF-generation spans. Browser performance entries
measure safety delivery and step display. No prompt/image data is added to
performance logs.

## Operating limits

This persistence adapter targets the existing single-worker local deployment.
Use a persistent data directory. Multiple workers/replicas need a shared session
store and distributed transition locking before deployment. Conversations keep
the configured one-hour expiry; durable reports have separate storage. Live
cloud latency, manual relevance and generated branch coverage still depend on
the configured providers and indexed evidence. Provider outages yield a
grounded fallback or a retry state; zero failures and five-second delivery
cannot be guaranteed.
