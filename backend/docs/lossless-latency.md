# Complete manual evidence with lower latency

The chat path keeps every retrieved text row and original table in full. Image
references are resolved from embedding-matched chunks within the same machine
and document through indexed relationships, section membership or exact indexed
caption context. The current image rows do not identify their embedding model;
their vectors are not blindly compared against the configured text vector.
There is no fixed image top-K or image-count cap, and same-page proximity alone
does not add images. The model still selects the images
needed for its answer and assigns them to their actual guided steps. Every image
it selects is rendered by the existing authenticated UI.

`MANUAL_IMAGE_TRANSPORT=gcs` sends validated private `gs://` references and the
correct MIME type to Vertex AI. The chat path does not download image bytes or
inspect every image from the containing PDF page. The Vertex project must be
able to read the approved bucket; the bucket does not need public access. Provider
vision/token limits still apply; reference transport does not make vision free
or guarantee a particular response time.

Image bytes are fetched only when an authorized browser asks to display a
selected image or when an authorized report snapshot needs to embed its evidence.
Reports retain PNG bytes so they remain self-contained after a session expires.
Technician uploads use their existing separate, user-owned path.

The manual image cache lives in `backend/.cache/manual-images` (no new database).
It revalidates the Cloud Storage object generation, checks cached byte integrity,
uses atomic files, shares concurrent reads and is bounded by
`MANUAL_IMAGE_CACHE_BYTES` (256 MiB by default). No technician uploads are put in
this shared cache. A failed cache write does not hide a valid image.

Embedding generation overlaps the independent inventory lookup. Inventory reads
have a short `MANUAL_INVENTORY_CACHE_SECONDS` TTL (30 seconds by default). After
updating indexed manuals, run from `backend`:

```powershell
.\.venv\Scripts\python.exe -m app.retrieval.cache_revision
```

The revision marker invalidates running workers' inventory/search/reference
caches on their next request. It also prevents unresolved old image references
from silently serving a changed manual. Refresh an old conversation after a
corpus update. `MANUAL_CORPUS_VERSION` can also version a deployment's corpus.

The complete, stable manual text appears before the variable question/workflow
in the model payload to permit provider implicit context-cache reuse. No text is
summarized, no images are dropped for this optimization, and no additional model
or cache-creation API call is introduced. `cached_input_tokens` in the existing
timing JSON records provider-reported hits; cache reuse is not guaranteed.

`MANUAL_COVERAGE_REVIEW_ENABLED` retains the existing optional independent review.
Normal grounding, original table handling, technician confirmations, guarded
branches and step validation remain active independently of that extra call.
`MANUAL_IMAGE_TRANSPORT=inline` is an explicit troubleshooting option for a
deployment unable to grant Vertex access to its bucket; it restores local byte
loading. It is not used as an automatic download fallback in the reference flow.

Validate on the same questions with cold and warm caches. Compare all returned
chunk IDs/full text, table cells, source references, model-selected image blocks,
and report evidence, together with latency. Offline tests cannot establish live
provider response time or whether a deployed Vertex identity has bucket access.
