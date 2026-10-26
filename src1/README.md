# Manufacturing RAG — local segmentation-first pipeline

This version integrates the PDF logical segmenter BEFORE extraction. It is local-only: no GCP, BigQuery, Cloud Functions, or Gemini API calls are required for the default run.

## Flow
1. Copy the original PDF into the processed document folder.
2. Split the original PDF into cropped virtual pages based on candidate table regions and surrounding content.
3. Save the segmented PDF, manifest, report, and preview PNGs under `segmentation/`.
4. Extract text, tables, images, and links from the segmented PDF.
5. Map virtual page coordinates and page references back to original physical pages and bounding boxes.
6. Build relationships and chunks locally.

## Install
From project root:
```powershell
python -m pip install --upgrade pip
pip install -r requirements.txt
```

## Run one PDF
```powershell
python src/simple_ingestion/main.py "data/raw/HSG-2550/5 - Haas Surface Grinder - Operation.pdf" "HSG-2550"
```

## Output
`data/processed/HSG-2550/<PDF name>/`
- Original PDF copy
- `segmentation/<name>_segmented.pdf`
- `segmentation/manifest.json`
- `segmentation/report.txt`
- `segmentation/segment_001.png`, etc.
- `extraction.json`
- `relationships.json`
- `chunks.json`
- `assets/images/`

Embeddings are off by default. Use `--embeddings` only when you are ready to configure Google authentication and incur possible API charges.

## Citation mapping
`extraction.json` records both `virtual_page_number` and original `page_number`; element bboxes are translated back to original page coordinates. The exact segment crop is also retained as `segment_bbox`, with `segment_id` and source filename.

## Limitations
Segmentation/table detection is heuristic. Inspect `segmentation/report.txt` and preview PNGs; ambiguous regions can be flagged for review. This is a test integration, not a guarantee that every PDF layout is perfectly segmented.
