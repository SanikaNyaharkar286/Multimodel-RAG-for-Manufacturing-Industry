# Multimodel-RAG-for-Manufacturing-Industry

## PDF ingestion and extracted data

The local ingestion pipeline discovers PDF manuals, extracts text, tables,
images, and links, records relationships with their evidence, and prepares
page-addressable retrieval units. PyMuPDF is the parser. The JSON files are
extraction artifacts and retrieval-ready inputs; they are not embeddings or a
vector database.

### Run the pipeline

From the repository root, activate the project's virtual environment and run:

```powershell
.\.venv\Scripts\Activate.ps1
Set-Location .\src
python -m ingestion.main
```

`data/raw` is scanned recursively. The path tracker records directories and
`.pdf` files only. Each discovered PDF is processed independently, so a failed
manual is reported in the manifest without stopping the remaining manuals.

Run tests from the repository root:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py"
```

### Output directory and naming

For each source PDF, the source-relative folders and filename stem are used to
create a readable, collision-resistant output directory:

```text
data/processed/
├── path_tracker.json
├── ingestion_manifest.json
└── documents/
    └── <machine>/
        └── <nested-source-folders>/
            └── <source-filename-stem>/
                ├── <source-filename-stem>.json
                ├── <source-filename-stem>.extraction.json
                ├── <source-filename-stem>.tables.json
                ├── <source-filename-stem>.log
                └── images/
```

For example, `6 - Haas Surface Grinder - Maintenance.pdf` is stored under:

```text
data/processed/documents/HSG-2550/6 - Haas Surface Grinder - Maintenance/
```

The original filename, relative source path, document ID, and source
SHA-256 are retained in metadata. Unsafe filename characters are sanitized for
filesystem use, while the original filename remains unchanged in the JSON.
Extracted image paths inside JSON are relative to the document folder.

### What each artifact stores

| Artifact | Contents and purpose |
| --- | --- |
| `path_tracker.json` | Inventory of discovered source directories and PDF files, using paths relative to `data/raw`. It does not include unrelated file types. |
| `ingestion_manifest.json` | One run's summary: discovered/processed/failed counts, each PDF's status, document ID, relative output paths, page/table/image/RAG-unit counts, and extraction warnings. |
| `<stem>.extraction.json` | Page-level text extraction. Stores source provenance and page count; each page has reading-order text blocks, block and span bounding boxes, font/style details, block kind, links, image IDs, and page warnings. Image records include IDs, page numbers, hashes, relative file paths, and placement bounding boxes. |
| `<stem>.tables.json` | PyMuPDF table detections for that PDF. Each table stores raw cell rows and bounding box, normalized headers and row records where possible, title/section context and its source, page/table indexes, header and extraction methods, validation warnings/status, and fallback-needed metadata. Suspicious detections remain in the file rather than being silently discarded. |
| `<stem>.json` | Combined document-level record. It repeats the tables for convenient access, points to the extraction/table files, and stores document provenance, relationship records, validation totals, and prepared RAG units. |
| `<stem>.log` | Per-PDF processing events, completion counts, and extraction/table warnings. A failed PDF has the exception and traceback here, and its failure status and log path in the manifest. |
| `images/*` | Extracted image bytes. The extraction JSON provides each image's stable ID, page, SHA-256, and placement box/path. |

### How relationships and RAG units are represented

Text blocks are kept separate from tables and figures. A table-to-text overlap
is recorded as `possible_duplicate_of_table`; the source text remains in the
extraction JSON, while near-exact duplicated blocks may be omitted from the
prepared RAG units to reduce repeated retrieval content.

Relationship entries have source and target IDs, a relationship type,
confidence, and evidence. The pipeline distinguishes likely section context,
same-page nearby image context, a labeled caption's geometric association,
an explicit figure/table reference, and an unresolved explicit reference.
Proximity is only a clue, not proof that a paragraph describes a particular
diagram or table.

RAG units are stored in `<stem>.json`:

- **Text units** retain the source text-block ID, page, bounding box, section
  context, and possible table relationship.
- **Table-row units** serialize each row with its header/value associations
  and include page, table, title/section, and validation provenance.
- **Image units** point to the extracted image and include a caption or nearby
  text only when a relationship was established. Images without linked text
  remain addressable but have no invented description.

### Validation and current limitations

Table checks flag likely page-wide false detections, unknown or malformed
headers, uneven row lengths, and other suspicious extraction patterns.
Warnings do not claim that a table is accurate; they help separate a dubious
detection from a table whose header is simply unknown.

Headings and captions are layout-based classifications. Figure/table
references may remain unresolved if a matching labeled caption and target
cannot be established. PyMuPDF's embedded-image extraction does not interpret
diagram labels. Pages with images but no extractable text are flagged for OCR
review, but OCR is not currently run. The metadata can request review through
the `text_spans_and_coordinates` fallback label, but coordinate-based
table reconstruction is not yet implemented. No cloud service or LLM is used.
