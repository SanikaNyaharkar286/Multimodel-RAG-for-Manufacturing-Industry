# API latency and logging guide

Implemented for the Manufacturing Equipment Copilot in October 2026. This measures the answer pipeline and avoids duplicate manual-image work while preserving the same evidence and image references.

## 1. Current activation status

Local JSON logging and development timing are configured. The dedicated BigQuery log table was created with a signed-in account, but export is still disabled in `backend/.env` because the application's service account cannot insert rows:

```text
403: bigquery.tables.updateData denied
Table: market-lens-506611:manufacturing_rag.api_call_logs
```

No successful API-log insert/readback has been claimed. The table exists with 30-day partition retention; an administrator must grant the app's service account write access before enabling export. Local logs work without the cloud exporter.

Restart the backend to load changed environment settings. Refresh the frontend afterward. The provided launcher is `backend/run.cmd`.

## 2. Follow one request

```text
Browser POST /chat + random X-Client-Request-ID
  -> FastAPI middleware creates its own unique request_id
  -> upload/session/agent/retrieval/media spans share that request context
  -> response contains X-Request-ID; /chat JSON also contains request_id
  -> one request summary goes to the local logging queue
  -> optional bounded BigQuery queue exports batches independently
```

The server ID is authoritative and is not taken from an incoming header. The browser's separate UUID is validated and stored as `client_request_id`, allowing a browser abort to be correlated even if response headers never arrived.

All HTTP endpoints are measured, including sessions, chat, reference images, health, CORS preflights and unmatched routes. Matched paths use templates such as `/sessions/{session_id}`. Arbitrary unmatched paths are stored as `/unmatched`, avoiding IDs or user content in logged URLs.

## 3. Local log file

The file is `backend/logs/api.log`, formatted as one JSON object per line. It rotates at 10 MiB with five backup files. The directory is Git-ignored.

The request path enqueues records without waiting for disk or BigQuery. A separate local listener writes the file; another worker handles BigQuery export. Queue saturation increments counters instead of blocking an answer.

From the project root:

```powershell
Get-Content backend/logs/api.log -Tail 10
Get-Content backend/logs/api.log -Tail 5 -Wait
```

To inspect a particular ID:

```powershell
Get-Content backend/logs/api.log |
  ForEach-Object { $_ | ConvertFrom-Json } |
  Where-Object { $_.request_id -eq 'REPLACE_WITH_ID' }
```

`event=api_request` identifies request summaries. Export failures and queue overflow produce separate diagnostic records. Raw user questions, full prompts, manual chunks, image bytes/base64, credentials and full session IDs are not part of performance records. Exception messages are excluded; only error categories are stored.

## 4. What each request records

| Group | Data |
|---|---|
| Identity | UTC start timestamp, server request ID, optional browser UUID, method and route template |
| Result | HTTP status, HTTP success flag, answer outcome and error category |
| Inputs | Question character count, current upload flag, previous-photo reuse flag, byte sizes |
| Evidence/media | Chunk count, final image/link counts, asset-warning count, model image bytes, payload character count |
| Model | Observed call count; input/output/thinking token usage where the provider exposes it |
| Timing | Total request time, major timing columns, detailed nested operation events |

`success` describes HTTP status. `outcome` distinguishes `answered`, `inventory`, `clarification`, `unsupported`, `out_of_scope`, `model_mismatch`, `completed` and `error`. A 200 response with `outcome=unsupported` is not counted as a grounded technical answer.

The keyed session hash permits correlation without logging the raw ID. A private stable `API_LOG_HASH_KEY` was added to the local environment; it must not be committed. Without one, the application generates a key per process, so hashes change on restart.

## 5. Stage measurements

| Stage | Meaning |
|---|---|
| `session_load`, `input_validation` | Session lookup and initial question checks |
| `upload_read`, `upload_normalization` | Reading and normalizing the user's uploaded file |
| `session_lock_wait` | Time waiting for another turn in the same session |
| `planner`, `planner_api`, `planner_validation` | Overall planning, model operation and structured-plan validation |
| `embedding`, `embedding_validation` | Query embedding generation and dimension check |
| `bq_seed_query`, `bq_context_query` | Ranked seed query and same-page enrichment query, separately |
| `qr_query`, `inventory_query` | QR lookup or complete document catalog query |
| `retrieval` | Entire normal retrieval phase, including embedding and enrichment |
| `media`, `manual_image_load` | Overall media preparation and each selected manual image |
| `gcs_download`, `gcs_bytes_download` | Entire asset fetch and bounded byte transfer; separate metadata calls were removed |
| `image_read`, `image_cache_miss` | Image resolution time and cache-miss work |
| `pdf_read`, `pdf_cache_miss`, `pdf_render` | PDF access/cache-miss work and page rendering |
| `image_normalization` | Pixel conversion, resizing and PNG encoding |
| `video_check`, `video_cache_miss` | Manual-linked video checking, including cache-miss work |
| `payload_build`, `agent` | Preparing the model input and running the answer agent |
| `answer_validation`, `answer_json_validation`, `evidence_validation` | Structured output and evidence-ID checks |
| `response_build`, `session_state_save`, `direct_reply_save`, `history_save` | Final blocks and conversation updates |

Spans record start offsets, duration, success/error/unfinished status and error class. BigQuery query spans include job ID, processed/billed bytes and cache status when exposed by the SDK's returned rows. Missing provider statistics remain absent; they are not invented or treated as zero.

Timings use `perf_counter()`, not wall-clock subtraction. Total backend duration starts at middleware entry, covering request parsing and lock waiting; it ends after response handling returns. `/chat.elapsed_seconds` starts at route entry, so it excludes earlier framework parsing. The development `Server-Timing` header reports response-ready timing before body transmission, so it can differ slightly from the final log total.

Parent/child durations overlap. Image/video worker operations can run concurrently. Do not add every span together to compute end-to-end latency. Missing stages mean unexecuted or unmeasured, not zero. Cache-miss spans identify actual cache work; their absence can indicate a cache hit when the corresponding read/check span is present. Per-request future sharing avoids repeated loads for identical image references; `cache_hits` counts those shared references. The 25 MiB source limit remains.

`asyncio.to_thread` retains request context; explicit context copies are used for media thread-pool tasks. Late-finishing work cannot mutate a summary after it is frozen. Cancellation may leave unfinished spans in that snapshot, and cancelling Python awaiters does not necessarily stop an underlying synchronous SDK operation.

SDK-internal retries are included in the enclosing operation duration but are not individually exposed by this implementation. Observed model-call counts are not a provider billing ledger, and ADK token usage is only recorded when exposed on events.

## 6. BigQuery setup

The proposed table is `market-lens-506611.manufacturing_rag.api_call_logs`. Schema and setup code live in `backend/app/observability/storage.py`; [create_api_call_logs.sql](create_api_call_logs.sql) is the generated administrator-ready DDL.

The table is partitioned on `timestamp`, clustered on endpoint/status, requires partition filters, and has 30-day partition expiration. The setup command checks schema/partition compatibility and does not silently overwrite an existing table.

From `backend`, with an authorized identity:

```powershell
.\.venv\Scripts\python.exe -m scripts.setup_api_logging --print-ddl
.\.venv\Scripts\python.exe -m scripts.setup_api_logging --verify
```

`--print-ddl` makes no cloud call. `--verify` provisions the table, inserts one diagnostic record, and runs a bounded query to confirm it can be read back. Diagnostic records use `method=DIAGNOSTIC` so reports can exclude them.

The table already exists. The application exporter still needs `bigquery.tables.updateData` on this log table. Verification/reporting additionally require query-job and data-read permissions. Ask the administrator to scope permissions to telemetry rather than granting broad write access to manual tables. Do not change the existing manual-retrieval role just to enable logging.

The exporter uses its own BigQuery client. Set `API_LOG_CREDENTIALS_FILE` only if a dedicated writer identity is supplied; otherwise it uses the existing application default credentials. Separate clients do not themselves create separate IAM permissions.

After successful provisioning, set this in `backend/.env` and restart:

```dotenv
BQ_API_LOGGING_ENABLED=true
```

The worker batches up to 20 rows or waits about two seconds for a partial batch. The queue holds up to 1,000 pending records. Export uses bounded attempts and nonblocking enqueue. No table creation is performed during chat. The log export is not recursively logged as another inbound HTTP request.

Retries can duplicate streaming records. Request IDs are supplied as insert IDs, and reporting SQL deduplicates by server request ID. Export is best effort, not exactly once: crashes, full queues or shutdown deadlines can leave cloud rows missing. Local summaries are a fallback, not an automatic replay system. `/health.api_logging` reports queue depth, exported/failed/dropped counts and the last error category.

## 7. Browser inspection

In Edge or Chrome, open Developer Tools before sending a question:

1. In Network, filter Fetch/XHR and select `/chat`.
2. Inspect status, duration, request `X-Client-Request-ID`, response `X-Request-ID` and `Server-Timing`.
3. Inspect image requests separately; successful chat JSON does not guarantee every reference image loaded.
4. In Console, enable Debug/Verbose messages to see `[copilot:chat-api]`, `[copilot:chat-error]`, `[copilot:answer-committed]`, and image load/error events.
5. Record the Performance panel for long JavaScript tasks, Markdown/table rendering and image decoding.

Console output is development-only. It contains timings and counts, not message text, session IDs or image URLs. Fetch timing separates time to response headers from body download/JSON processing. `response_to_frame_ms` is a post-commit animation-frame observation, not an exact paint duration; late/lazy images have separate events. Browser measurements are not written back into the original backend BigQuery row.

Client aborts may have no server response ID. Use their `client_request_id` to find the backend summary. Browser totals minus backend totals are not pure network time: boundaries, upload, parsing and scheduling differ.

`API_TIMING_DEBUG=true` enables Server-Timing. Disable it for deployments where detailed performance headers should not be exposed. The current local configuration enables it.

## 8. Reports and optimization workflow

[api_latency_reports.sql](api_latency_reports.sql) includes endpoint/outcome latency, major stage averages, per-operation P95/error counts, and a request-ID lookup. Queries filter partitions and deduplicate streaming retries.

Use the same inputs before/after optimization: inventory, text question, small upload, near-limit upload, reused-photo follow-up, manual diagram, PDF-page fallback, and video/QR request. Separate first-run and cached requests, and record the number of samples. A few smoke requests verify wiring but cannot establish a reliable P95.

Choose the next change from measured results: fix repeated missing-asset lookups, deduplicate redundant media, cache expensive normalized images/pages, inspect large evidence payloads, review BigQuery jobs, or profile frontend rendering. Preserve supported content and answer quality while measuring improvements. Longer timeouts alone are not a latency optimization.

The first media optimization removes the GCS metadata round trip by downloading at most 25 MiB plus one byte, then rejecting empty or oversized assets. It shares one future and normalization result for identical image references within a request, while retaining every evidence reference. Six image workers handle distinct assets. Linked websites are checked for extra videos only for video requests; original safe manual links remain available. In the control-panel diagnostic, 12 references represented six distinct images; observed GCS calls fell from 14 to eight. End-to-end times varied across runs because embedding and other cloud calls varied, so a consistent overall speedup is not yet established. Keep collecting separate retrieval and media wall times before attributing a gain.


## 9. Files added and changed

`backend/app/observability/` contains timing/context, ASGI middleware, local/cloud sinks and BigQuery schema/provisioning helpers. Existing route, planner, retrieval, media and agent files are instrumented where work actually occurs. `frontend/src/utils/performanceLog.js` keeps developer console output out of production; the service and response renderer supply its safe timing metadata.

Tests cover context isolation, worker propagation, frozen snapshots, privacy filtering, HTTP errors and request IDs, background-export failure/overflow, provisioning shape, browser timing correlation and existing chat behavior. They do not substitute for a live baseline on the manager's representative questions.
