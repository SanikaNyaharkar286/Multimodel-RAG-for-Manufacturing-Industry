# Manufacturing RAG PDF ingestion: the code flow

This guide follows one PDF from your computer to BigQuery. It uses simple
language and shows which file each part of the code lives in.

## The big picture

There are **two separate commands**:

1. `gcs_uploader.py` puts the original, raw PDF in the GCS `raw` folder.
2. `main.py` downloads that PDF, runs the ingestion pipeline, uploads the
   processed files to GCS, and loads them into BigQuery.

The first command only uploads the original PDF. It does **not** start the
processing pipeline. The second command starts that pipeline.

```text
+-----------------------------------+
| Start command 1                   |
| File: gcs_uploader.py             |
| Function: main()                  |
+-----------------------------------+
                 |
                 v
+-----------------------------------+
| Check the PDF and machine name    |
| File: gcs_uploader.py             |
| Functions: validate_pdf(),        |
|            validate_machine_name()|
+-----------------------------------+
                 |
                 v
+-----------------------------------+
| Upload original PDF to GCS        |
| File: gcs_uploader.py             |
| Function: upload_pdf()            |
+-----------------------------------+
                 |
                 v
       Raw PDF is in GCS:
       gs://bucket/raw/MACHINE/file.pdf

       This is the end of command 1.
       It does not process the PDF.

                 |
                 | You run command 2 using the GCS URI
                 v
+-----------------------------------+
| Start command 2                   |
| File: main.py                     |
| Function: main()                  |
+-----------------------------------+
                 |
                 v
+-----------------------------------+
| Read the GCS URI, download the    |
| raw PDF temporarily, and call     |
| process_pdf()                     |
| File: main.py                     |
| Function: process_gcs_uri()       |
+-----------------------------------+
                 |
                 v
+-----------------------------------+
| Run the complete PDF pipeline     |
| File: pipeline.py                 |
| Function: process_pdf()           |
+-----------------------------------+
                 |
                 v
+-----------------------------------+
| Split the PDF into useful pieces  |
| File: pdf_segmenter.py            |
| Function: process_pdf(), called   |
| as split_pdf() by pipeline.py     |
+-----------------------------------+
                 |
                 v
+-----------------------------------+
| Read text, tables, pictures,      |
| and links from the segmented PDF  |
| File: pdf_extractor.py            |
| Function: extract_pdf()           |
+-----------------------------------+
                 |
                 v
+-----------------------------------+
| Build document relationships     |
| File: relationship_builder.py    |
| Function: build_relationships()   |
+-----------------------------------+
                 |
                 v
+-----------------------------------+
| Turn extracted content into      |
| searchable chunks                 |
| File: chunk_builder.py            |
| Function: build_all_chunks()      |
+-----------------------------------+
                 |
                 v
+-----------------------------------+
| Create embeddings for the chunks |
| File: pipeline.py calls helpers  |
| in embedding_generator.py         |
| Function: _generate_and_save_     |
| embeddings()                      |
+-----------------------------------+
                 |
                 v
+-----------------------------------+
| Upload processed files to GCS    |
| File: pipeline.py calls          |
| processed_uploader.py             |
| Function: upload_directory()      |
+-----------------------------------+
                 |
                 v
+-----------------------------------+
| Load the processed information   |
| from GCS into BigQuery            |
| File: pipeline.py calls          |
| bigquery_loader.py                |
| Function: load_gcs_document_to_   |
| bigquery()                        |
+-----------------------------------+
                 |
                 v
       Ingestion is complete.
```

## What the boxes mean

- A **function** is a named job the program knows how to do.
- A **file** is where that job is written down in code.
- An **arrow** means “when this job finishes, it calls or hands work to the
  next job.”
- The two `main()` functions are in different files. The first command starts
  `gcs_uploader.py`; the second command starts `main.py`.

## The journey in simple words

1. **Upload the original.** `gcs_uploader.py` checks that the machine name and
   PDF look valid, then places the PDF under `raw/<machine>/` in GCS.
2. **Start processing.** `main.py` reads the GCS address. Its
   `process_gcs_uri()` function downloads the PDF to a temporary local folder
   and calls `process_pdf()` in `pipeline.py`.
3. **Prepare the document.** `pipeline.py` copies the PDF into the local
   processed-document folder and asks `pdf_segmenter.py` to split it into
   smaller, useful page regions. Segmentation previews and a manifest are
   saved too.
4. **Read and organize the contents.** `pdf_extractor.py` reads text, tables,
   images, and links. `pipeline.py` maps the segmented pages back to their
   original page numbers. `relationship_builder.py` connects related document
   elements, and `chunk_builder.py` makes smaller searchable pieces called
   **chunks**.
5. **Make it searchable.** Because the command asks to load into BigQuery,
   `pipeline.py` also creates an **embedding** for each chunk. An embedding is
   a list of numbers that helps BigQuery find chunks related to a question.
6. **Send processed files to the cloud.** The pipeline uploads the processed
   document folder to GCS and transfers upload-audit records.
7. **Load into BigQuery.** The BigQuery loader reads the processed files from
   GCS and loads their information into the selected tables. The
   `--commit-bigquery` option tells it to actually commit the load; without
   that option, a BigQuery load request is a dry run.

During processing, local files such as `extraction.json`,
`relationships.json`, `chunks.json`, and `embedded_chunks.json` are created in
the processed-document folder. The segmented PDF, manifest, report, and
preview images are kept in its `segmentation` folder.

## Before running the commands

Open PowerShell and move to the folder containing `main.py` and
`gcs_uploader.py`:

```powershell
Set-Location "C:\Users\Sonakshi\OneDrive\Desktop\main\Multimodel-RAG-for-Manufacturing-Industry\src1\simple_ingestion"
```

Make sure the required Python packages, Google Cloud credentials, and project
configuration are set up. The configured bucket in this project is
`market-lens-506611-manufacturing-rag`. The machine folder in the upload
command must match the machine folder used in the GCS URI in the next command.

## Your two commands

### 1. Upload the raw PDF to GCS

Replace the example local path with the real path to your PDF. Use the machine
name that should appear in the GCS path, for example `HAB`.

```powershell
python .\gcs_uploader.py `
  --file "C:\path\to\your\machine-manual.pdf" `
  --machine "HAB"
```

The command prints the GCS URI after uploading. Copy that URI for command 2.
It will look like:

```text
gs://market-lens-506611-manufacturing-rag/raw/HAB/your-file.pdf
```

### 2. Run the complete pipeline through the BigQuery commit

Replace `your-file.pdf` with the actual filename shown in the GCS URI from
command 1. Keep the bucket and machine folder consistent with that URI.

```powershell
python .\main.py `
  --gcs-uri "gs://market-lens-506611-manufacturing-rag/raw/HAB/your-file.pdf" `
  --load-to-bigquery `
  --commit-bigquery
```

When `--load-to-bigquery` is used, `main.py` automatically enables embedding
generation and processed-artifact upload to GCS. You do **not** need to add
`--embeddings` or `--upload-to-gcs` for this command.

`--commit-bigquery` makes the BigQuery load a real commit. If you want to test
the BigQuery load without committing it, remove `--commit-bigquery`; the
pipeline will run the BigQuery load in dry-run mode instead.

## What the main output files are

| File or folder | Plain-language meaning |
|---|---|
| `segmentation/` | Smaller regions made from the original PDF, plus a map and preview pictures. |
| `extraction.json` | The text, tables, images, links, and page details read from the PDF. |
| `relationships.json` | The relationships found between headings and document elements. |
| `chunks.json` | Small content pieces prepared for searching. |
| `embedded_chunks.json` | The chunks plus their numeric search representations. |
| `assets/` | Extracted PDF pictures and other local document assets. |

This diagram follows the GCS-to-BigQuery route above. The project also has
other `main.py` options, such as processing a local PDF or rebuilding chunks
from existing processed files; those are separate routes through the code.
