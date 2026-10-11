"""Tests for PDF path discovery and cross-modal ingestion relationships."""

import tempfile
import unittest
import logging
from pathlib import Path
from unittest.mock import patch

from src.ingestion.file_naming import (
    document_output_directory,
    safe_filename_stem,
)
from src.ingestion.path_tracker import track_source_paths
from src.ingestion.pipeline import (
    build_rag_units,
    build_relationships,
    match_table_text_blocks,
    run_document_pipeline,
)
from src.ingestion.pdf_extractor import classify_text_block


class PathTrackerTests(unittest.TestCase):
    """Ensure discovery tracks directories and PDFs, not unrelated files."""

    def test_tracks_nested_directories_and_only_pdf_files(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            raw_root = Path(temporary_directory)
            (raw_root / "HSG-2550" / "chapter").mkdir(parents=True)
            (raw_root / "HSG-2550" / "chapter" / "manual.PDF").write_bytes(
                b"pdf"
            )
            (raw_root / "HSG-2550" / "notes.txt").write_text("ignore")
            (raw_root / "readme.md").write_text("ignore")

            tracker = track_source_paths(raw_root)

        self.assertEqual(
            tracker["folders"],
            ["HSG-2550", "HSG-2550/chapter"],
        )
        self.assertEqual(tracker["pdf_count"], 1)
        self.assertEqual(
            tracker["pdf_files"][0]["relative_path"],
            "HSG-2550/chapter/manual.PDF",
        )
        self.assertEqual(tracker["pdf_files"][0]["machine_name"], "HSG-2550")

    def test_document_outputs_use_source_name_and_keep_nested_paths(self):
        output_root = Path("processed") / "documents"
        output_dir = document_output_directory(
            output_root,
            "HSG-2550",
            "HSG-2550/chapter one/Maintenance: Weekly.pdf",
        )

        self.assertEqual(
            output_dir,
            output_root / "HSG-2550" / "chapter one" / "Maintenance_ Weekly",
        )
        self.assertEqual(
            safe_filename_stem("Manual.PDF"),
            "Manual",
        )
        self.assertEqual(safe_filename_stem("CON.pdf"), "_CON")


class RelationshipTests(unittest.TestCase):
    """Verify geometry and explicit labels create evidenced relationships."""

    def setUp(self):
        self.pages = [{
            "page_number": 1,
            "text_blocks": [
                {
                    "block_id": "b-heading",
                    "text": "HYDRAULIC SYSTEM",
                    "bbox": [10, 10, 300, 25],
                    "kind": "section_heading",
                },
                {
                    "block_id": "b-reference",
                    "text": "Refer to Figure 2 for hose routing.",
                    "bbox": [20, 40, 300, 55],
                    "kind": "paragraph",
                },
                {
                    "block_id": "b-caption",
                    "text": "Figure 2. Hydraulic hose layout",
                    "bbox": [100, 205, 300, 220],
                    "kind": "caption",
                },
                {
                    "block_id": "b-nearby",
                    "text": "The supply line connects to the pump.",
                    "bbox": [110, 222, 290, 240],
                    "kind": "paragraph",
                },
            ],
        }]
        self.tables = [{
            "table_id": "table-1",
            "document_id": "doc-1",
            "page_number": 1,
            "table_index_on_page": 1,
            "bbox": [350, 30, 550, 100],
            "raw_rows": [
                ["Part", "Description"],
                ["P-1", "Hydraulic pump assembly"],
            ],
            "headers": ["Part", "Description"],
            "rows": [{"Part": "P-1", "Description": "Hydraulic pump assembly"}],
            "header_source": "detected_header_row",
            "title": None,
            "validation": {"status": "valid"},
        }]
        self.images = [{
            "image_id": "image-1",
            "page_number": 1,
            "bbox": [100, 100, 300, 200],
            "bboxes": [[100, 100, 300, 200]],
            "path": "images/hydraulic.png",
            "sha256": "abc",
        }]

    def test_table_overlap_is_flagged_without_removing_content(self):
        block_text = "Part Description P-1 Hydraulic pump assembly"
        self.pages[0]["text_blocks"].append({
            "block_id": "b-table-text",
            "text": block_text,
            "bbox": [350, 30, 550, 100],
            "kind": "paragraph",
        })

        relationships = match_table_text_blocks(self.pages, self.tables)

        self.assertTrue(self.tables[0]["duplicate_detection"]
                        ["possible_duplicate_in_text"])
        self.assertFalse(self.tables[0]["duplicate_detection"]["content_removed"])
        self.assertEqual(
            relationships[0]["relationship"],
            "possible_duplicate_of_table",
        )

    def test_captions_references_and_nearby_text_keep_distinct_evidence(self):
        relationships = build_relationships(
            self.pages,
            self.tables,
            self.images,
        )
        kinds = {item["relationship"] for item in relationships}

        self.assertIn("caption_for", kinds)
        self.assertIn("explicit_reference", kinds)
        self.assertIn("nearby_context", kinds)
        reference = next(
            item for item in relationships
            if item["relationship"] == "explicit_reference"
        )
        self.assertEqual(reference["target_id"], "image-1")
        self.assertEqual(reference["confidence"], 0.98)
        self.assertEqual(
            self.pages[0]["text_blocks"][1]["section_context"],
            "HYDRAULIC SYSTEM",
        )

    def test_retrieval_rows_and_images_retain_page_and_document_links(self):
        relationships = build_relationships(
            self.pages,
            self.tables,
            self.images,
        )
        units = build_rag_units(
            pages=self.pages,
            tables=self.tables,
            images=self.images,
            relationships=relationships,
            document_id="doc-1",
        )
        table_unit = next(unit for unit in units if unit["kind"] == "table_row")
        image_unit = next(unit for unit in units if unit["kind"] == "image")

        self.assertIn("Part: P-1", table_unit["text"])
        self.assertEqual(table_unit["page_number"], 1)
        self.assertEqual(table_unit["document_id"], "doc-1")
        self.assertIn("Figure 2. Hydraulic hose layout", image_unit["text"])
        self.assertEqual(image_unit["source_element_id"], "image-1")

    def test_cross_page_section_and_figure_references_are_preserved(self):
        second_page = {
            "page_number": 2,
            "text_blocks": [
                {
                    "block_id": "b-cross-reference",
                    "text": "See Figure 3 for the wiring overview.",
                    "bbox": [20, 30, 300, 50],
                    "kind": "paragraph",
                },
                {
                    "block_id": "b-unmatched-reference",
                    "text": "Refer to Table 9 for torque values.",
                    "bbox": [20, 60, 300, 80],
                    "kind": "paragraph",
                },
                {
                    "block_id": "b-figure-caption",
                    "text": "Figure 3. Wiring overview",
                    "bbox": [100, 205, 300, 220],
                    "kind": "caption",
                },
            ],
        }
        image = {
            "image_id": "image-3",
            "page_number": 2,
            "bbox": [100, 100, 300, 200],
            "bboxes": [[100, 100, 300, 200]],
        }

        relationships = build_relationships(
            self.pages + [second_page],
            self.tables,
            self.images + [image],
        )

        explicit = next(
            relation for relation in relationships
            if relation["source_id"] == "b-cross-reference"
            and relation["relationship"] == "explicit_reference"
        )
        unresolved = next(
            relation for relation in relationships
            if relation["source_id"] == "b-unmatched-reference"
            and relation["relationship"] == "unresolved_explicit_reference"
        )
        self.assertEqual(explicit["target_id"], "image-3")
        self.assertEqual(
            unresolved["relationship"],
            "unresolved_explicit_reference",
        )
        self.assertIsNone(unresolved["target_id"])
        self.assertEqual(
            second_page["text_blocks"][0]["section_context"],
            "HYDRAULIC SYSTEM",
        )


class TextBlockClassificationTests(unittest.TestCase):
    """Avoid treating uppercase table contents as section headings."""

    def test_uppercase_is_not_enough_to_call_a_block_a_heading(self):
        spans = [{
            "text": "PART NUMBER",
            "size": 8.0,
            "flags": 16,
        }]

        self.assertEqual(
            classify_text_block("PART NUMBER", spans, 8.0),
            "paragraph",
        )
        self.assertEqual(
            classify_text_block("6.1 MAINTENANCE SCHEDULE", spans, 8.0),
            "section_heading",
        )
        self.assertEqual(
            classify_text_block(
                "Figure 2. Wiring",
                spans,
                8.0,
            ),
            "caption",
        )


class OutputNamingTests(unittest.TestCase):
    """Ensure a document pipeline writes consistently named JSON artifacts."""

    def test_pipeline_uses_the_source_name_for_all_document_outputs(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            raw_root = root / "raw"
            pdf_path = raw_root / "HSG-2550" / "Maintenance.pdf"
            pdf_path.parent.mkdir(parents=True)
            pdf_path.write_bytes(b"test")
            output_root = root / "processed" / "documents"
            extraction = {
                "document_id": "doc-id",
                "source_filename": pdf_path.name,
                "source_sha256": "hash",
                "page_count": 0,
                "pages": [],
                "images": [],
                "warnings": [],
            }
            logger = logging.getLogger("test.source.naming")
            logger.setLevel(logging.INFO)
            logger.handlers.clear()

            with (
                patch(
                    "src.ingestion.pipeline.extract_pdf",
                    return_value=extraction,
                ),
                patch(
                    "src.ingestion.pipeline.extract_tables",
                    return_value={
                        "tables": [],
                        "table_count": 0,
                        "warnings": [],
                    },
                ),
            ):
                document = run_document_pipeline(
                    pdf_path=pdf_path,
                    raw_root=raw_root,
                    output_root=output_root,
                    machine_name="HSG-2550",
                    logger=logger,
                )

            destination = output_root / "HSG-2550" / "Maintenance"
            self.assertEqual(
                document["output_directory"],
                "HSG-2550/Maintenance",
            )
            self.assertEqual(
                document["output_files"]["document_json"],
                "Maintenance.json",
            )
            self.assertEqual(
                document["output_files"]["extraction_json"],
                "Maintenance.extraction.json",
            )
            self.assertEqual(
                document["output_files"]["tables_json"],
                "Maintenance.tables.json",
            )
            self.assertTrue((destination / "Maintenance.json").is_file())
            self.assertTrue((destination / "Maintenance.tables.json").is_file())


if __name__ == "__main__":
    unittest.main()
