"""Tests for PDF path discovery and cross-modal ingestion relationships."""

import json
import tempfile
import unittest
import logging
import importlib
import sys
from pathlib import Path
from unittest.mock import patch

from src.ingestion.file_naming import (
    document_output_directory,
    safe_filename_stem,
)
from src.ingestion.path_tracker import track_source_paths
from src.ingestion.pipeline import (
    build_elements,
    build_rag_units,
    build_relationships,
    match_table_text_blocks,
    run_document_pipeline,
    validate_document,
    _apply_confirmed_logical_table_schema,
)
from src.ingestion.pdf_extractor import classify_text_block, extract_pdf


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
        self.assertEqual(reference["target_id"], "b-caption")
        self.assertEqual(reference["relation_status"], "confirmed")
        self.assertEqual(reference["confidence"], 0.98)
        nearby = next(
            item for item in relationships
            if item["relationship"] == "nearby_context"
        )
        self.assertEqual(nearby["relation_status"], "candidate")
        self.assertIn("proximity alone", nearby["evidence"]["reason"])
        self.assertEqual(
            self.pages[0]["text_blocks"][1]["section_context"],
            "HYDRAULIC SYSTEM",
        )

    def test_link_url_and_uncertain_text_association_keep_provenance(self):
        pages = [{
            "page_index": 0,
            "page_number": 1,
            "document_id": "doc-1",
            "source_filename": "manual.pdf",
            "text_blocks": [{
                "block_id": "b-video-note",
                "document_id": "doc-1",
                "source_filename": "manual.pdf",
                "page_index": 0,
                "page_number": 1,
                "text": "This video demonstrates how to adjust the dresser.",
                "kind": "paragraph",
                "bbox": [100, 100, 400, 120],
                "section_context": "Wheel dresser adjustment",
            }],
            "links": [{
                "link_id": "link-video",
                "source_page": 1,
                "source_bbox": [120, 102, 260, 118],
                "target_uri": "https://www.youtube.com/watch?v=abc123",
                "target_page": None,
                "target_page_number": None,
                "target_named": None,
            }],
        }]

        relationships = build_relationships(pages, [], [])
        elements = build_elements(
            extraction={
                "document_id": "doc-1",
                "source_filename": "manual.pdf",
                "pages": pages,
                "images": [],
            },
            tables=[],
            extraction_filename="manual.extraction.json",
            tables_filename="manual.tables.json",
        )
        units = build_rag_units(
            pages=pages,
            tables=[],
            images=[],
            relationships=relationships,
            document_id="doc-1",
        )

        link_element = next(
            element for element in elements
            if element["element_type"] == "link"
        )
        link_unit = next(unit for unit in units if unit["kind"] == "link")
        relationship = next(
            item for item in relationships
            if item["relationship"] == "hyperlink_in_text"
        )
        self.assertEqual(link_element["element_id"], "link-video")
        self.assertEqual(link_element["bbox"], [120, 102, 260, 118])
        self.assertEqual(link_unit["target_uri"], pages[0]["links"][0]["target_uri"])
        self.assertEqual(link_unit["context_status"], "candidate")
        self.assertEqual(
            link_unit["section_context"],
            "Wheel dresser adjustment",
        )
        self.assertEqual(
            link_unit["section_context_status"],
            "candidate",
        )
        self.assertEqual(relationship["relation_status"], "candidate")
        self.assertEqual(relationship["source_id"], "b-video-note")

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
        self.assertEqual(image_unit["context_status"], "candidate")
        self.assertTrue(all(
            relation["relation_status"] == "candidate"
            for relation in image_unit["context_relationships"]
        ))

    def test_probable_false_table_is_retained_but_not_made_a_rag_row(self):
        table = {
            "table_id": "table-false",
            "document_id": "doc-1",
            "page_number": 1,
            "page_index": 0,
            "source_filename": "manual.pdf",
            "raw_rows": [["A full page of text captured as one cell"]],
            "headers": ["column_1"],
            "rows": [{"column_1": "A full page of text captured as one cell"}],
            "header_source": "unknown_header",
            "validation": {
                "status": "suspicious",
                "detection_class": "probable_false_positive",
            },
        }

        units = build_rag_units(
            pages=[],
            tables=[table],
            images=[],
            relationships=[],
            document_id="doc-1",
        )

        self.assertEqual(units, [])
        self.assertEqual(table["raw_rows"], [
            ["A full page of text captured as one cell"],
        ])
        self.assertFalse(table["included_in_rag_units"])
        self.assertEqual(
            table["rag_exclusion_reason"],
            "probable_false_positive",
        )

    def test_confirmed_logical_rows_replace_fragment_rows_without_duplicates(self):
        first = {
            "table_id": "table-p1",
            "document_id": "doc-1",
            "source_filename": "manual.pdf",
            "page_index": 0,
            "page_number": 1,
            "headers": ["NUMBER", "NAME"],
            "rows": [{"NUMBER": "1", "NAME": "First"}],
            "raw_rows": [["NUMBER", "NAME"], ["1", "First"]],
            "validation": {"status": "valid"},
        }
        second = {
            "table_id": "table-p2",
            "document_id": "doc-1",
            "source_filename": "manual.pdf",
            "page_index": 1,
            "page_number": 2,
            "headers": ["column_1", "column_2"],
            "header_source": "unknown_header",
            "rows": [{"column_1": "2", "column_2": "Second"}],
            "raw_rows": [["2", "Second"]],
            "validation": {
                "status": "uncertain",
                "fallback_needed": True,
                "warnings": [
                    {
                        "code": "unknown_header_structure",
                        "message": "No local header was detected.",
                    },
                    {
                        "code": "generic_headers",
                        "message": "Placeholder headers were used.",
                    },
                    {
                        "code": "numbered_entries_in_cell",
                        "message": "Several numbered rows were found.",
                    },
                ],
            },
            "extraction_warnings": [
                {"code": "unknown_header_structure"},
            ],
        }
        logical = {
            "logical_table_id": "table-p1_logical",
            "document_id": "doc-1",
            "source_filename": "manual.pdf",
            "headers": ["NUMBER", "NAME"],
            "merge_status": "confirmed",
            "source_fragments": [
                {"table_id": "table-p1"},
                {"table_id": "table-p2"},
            ],
            "rows": [
                {
                    "row": {"NUMBER": "1", "NAME": "First"},
                    "row_index": 0,
                    "source_fragment_id": "table-p1",
                    "source_page_index": 0,
                    "source_page_number": 1,
                    "source_bbox": [10, 600, 500, 780],
                    "source_raw_row_index": 1,
                },
                {
                    "row": {"NUMBER": "2", "NAME": "Second"},
                    "row_index": 1,
                    "source_fragment_id": "table-p2",
                    "source_page_index": 1,
                    "source_page_number": 2,
                    "source_bbox": [10, 10, 500, 180],
                    "source_raw_row_index": 0,
                },
            ],
        }

        _apply_confirmed_logical_table_schema(
            [first, second],
            [logical],
        )
        units = build_rag_units(
            pages=[],
            tables=[first, second],
            images=[],
            relationships=[],
            document_id="doc-1",
            logical_tables=[logical],
        )

        self.assertEqual(len(units), 2)
        self.assertEqual(
            [unit["row"]["NUMBER"] for unit in units],
            ["1", "2"],
        )
        self.assertEqual(units[1]["page_index"], 1)
        self.assertEqual(units[1]["source_element_id"], "table-p2")
        self.assertEqual(second["headers"], ["NUMBER", "NAME"])
        self.assertEqual(
            second["rows"],
            [{"NUMBER": "2", "NAME": "Second"}],
        )
        self.assertEqual(second["detected_headers"], ["column_1", "column_2"])
        self.assertEqual(second["raw_rows"], [["2", "Second"]])
        self.assertEqual(second["logical_table_id"], "table-p1_logical")
        self.assertEqual(
            [warning["code"] for warning in second["validation"]["warnings"]],
            ["numbered_entries_in_cell"],
        )
        self.assertEqual(
            [warning["code"] for warning in
             second["validation"]["resolved_header_warnings"]],
            ["unknown_header_structure", "generic_headers"],
        )
        self.assertEqual(
            second["extraction_warnings"][0]["code"],
            "unknown_header_structure",
        )
        self.assertEqual(
            first["rag_exclusion_reason"],
            "reconstructed_logical_table",
        )

    def test_uncertain_continuation_is_preserved_but_not_indexed_as_table_data(self):
        fragment = {
            "table_id": "table-p2",
            "document_id": "doc-1",
            "source_filename": "manual.pdf",
            "page_index": 1,
            "page_number": 2,
            "headers": ["column_1", "column_2"],
            "rows": [{"column_1": "unknown", "column_2": "mapping"}],
            "raw_rows": [["unknown", "mapping"]],
            "validation": {
                "status": "uncertain",
                "detection_class": "unknown_header_structure",
            },
        }
        relationship = {
            "relationship_id": "table-p1-continues-table-p2",
            "source_id": "table-p1",
            "target_id": "table-p2",
            "relationship": "continues_table",
            "relation_status": "candidate",
            "confidence": 0.6,
            "evidence": {"reason": "Header and row continuity are uncertain."},
        }

        units = build_rag_units(
            pages=[],
            tables=[fragment],
            images=[],
            relationships=[relationship],
            document_id="doc-1",
        )

        self.assertEqual(units, [])
        self.assertEqual(fragment["raw_rows"], [["unknown", "mapping"]])
        self.assertFalse(fragment["included_in_rag_units"])
        self.assertEqual(
            fragment["rag_exclusion_reason"],
            "uncertain_table_continuation",
        )

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
        self.assertEqual(explicit["target_id"], "b-figure-caption")
        self.assertEqual(explicit["relation_status"], "confirmed")
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
        self.assertEqual(
            classify_text_block("- Inspect the guard", spans, 8.0),
            "list",
        )


class ProvenanceValidationTests(unittest.TestCase):
    """Check normalized element references and report missing source assets."""

    def test_common_element_index_references_original_content(self):
        extraction = {
            "document_id": "doc-1",
            "source_filename": "manual.pdf",
            "pages": [{
                "page_number": 2,
                "page_index": 1,
                "text_blocks": [{
                    "block_id": "block-1",
                    "kind": "section_heading",
                    "text": "Hydraulics",
                    "bbox": [1, 2, 30, 15],
                }],
            }],
            "images": [],
        }
        elements = build_elements(
            extraction,
            [],
            "manual.extraction.json",
            "manual.tables.json",
        )

        self.assertEqual(len(elements), 1)
        self.assertEqual(elements[0]["element_type"], "heading")
        self.assertEqual(elements[0]["page_index"], 1)
        self.assertIsNone(elements[0]["printed_page_number"])
        self.assertEqual(
            elements[0]["content_ref"]["element_id"],
            "block-1",
        )

    def test_missing_image_asset_and_unit_provenance_are_reported(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            (output / "manual.extraction.json").write_text("{}", encoding="utf-8")
            (output / "manual.tables.json").write_text("{}", encoding="utf-8")
            extraction = {
                "document_id": "doc-1",
                "source_filename": "manual.pdf",
                "pages": [],
                "image_assets": [{
                    "asset_id": "asset-1",
                    "path": "images/missing.png",
                }],
                "images": [{
                    "image_id": "image-1",
                    "asset_id": "asset-1",
                    "page_number": 1,
                    "page_index": 0,
                    "path": "images/missing.png",
                }],
            }
            elements = build_elements(
                extraction,
                [],
                "manual.extraction.json",
                "manual.tables.json",
            )
            units = [{
                "unit_id": "unit-1",
                "kind": "image",
                "source_element_id": "image-1",
                "document_id": None,
                "page_index": None,
                "asset_id": "asset-1",
            }]
            validation = validate_document(
                extraction,
                [],
                [],
                units,
                elements,
                output,
                "manual.extraction.json",
                "manual.tables.json",
            )

        codes = {issue["code"] for issue in validation["issues"]}
        self.assertIn("image_asset_file_missing", codes)
        self.assertIn("rag_unit_document_provenance_missing", codes)
        self.assertIn("rag_unit_page_provenance_missing", codes)

    def test_invalid_bbox_missing_output_and_relationship_evidence_are_reported(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory)
            (output / "manual.tables.json").write_text("{}", encoding="utf-8")
            extraction = {
                "document_id": "doc-1",
                "source_filename": "manual.pdf",
                "page_count": 1,
                "pages": [{
                    "page_number": 1,
                    "page_index": 0,
                    "text_blocks": [{
                        "block_id": "block-1",
                        "kind": "paragraph",
                        "text": "Text",
                        "bbox": [20, 10, 5, 30],
                        "document_id": "doc-1",
                        "source_filename": "manual.pdf",
                        "page_index": 0,
                    }],
                }],
                "images": [],
            }
            elements = build_elements(
                extraction,
                [],
                "manual.extraction.json",
                "manual.tables.json",
            )
            validation = validate_document(
                extraction,
                [],
                [{
                    "source_id": "block-1",
                    "target_id": None,
                    "relationship": "unresolved_explicit_reference",
                    "relation_status": "unresolved",
                    "evidence": {},
                }],
                [],
                elements,
                output,
                "manual.extraction.json",
                "manual.tables.json",
            )

        codes = {issue["code"] for issue in validation["issues"]}
        self.assertIn("invalid_bbox", codes)
        self.assertIn("output_file_missing", codes)
        self.assertIn("relationship_evidence_missing", codes)


class ImageOccurrenceTests(unittest.TestCase):
    """Ensure one saved image asset can have several page occurrences."""

    def test_occurrences_have_separate_boxes_but_share_one_asset(self):
        class FakePage:
            rect = (0, 0, 600, 800)

            def __init__(self, rects):
                self.rects = rects

            def get_text(self, mode, sort=False):
                del sort
                if mode == "text":
                    return ""
                if mode == "dict":
                    return {"blocks": []}
                raise AssertionError(mode)

            def get_images(self, full=True):
                del full
                return [(7,)]

            def get_image_rects(self, xref):
                self.assert_xref = xref
                return self.rects

            def get_links(self):
                return []

        class FakeDocument:
            def __init__(self):
                self.pages = [
                    FakePage([(10, 20, 40, 50), (60, 20, 90, 50)]),
                    FakePage([(10, 30, 40, 60)]),
                ]

            def __enter__(self):
                return self

            def __exit__(self, *args):
                del args
                return False

            def __len__(self):
                return len(self.pages)

            def __iter__(self):
                return iter(self.pages)

            def extract_image(self, xref):
                del xref
                return {"image": b"same image bytes", "ext": "png"}

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pdf_path = root / "manual.pdf"
            pdf_path.write_bytes(b"fake pdf")
            output = root / "output"
            document_output = output / "manual"
            with patch(
                "src.ingestion.pdf_extractor.pymupdf.open",
                return_value=FakeDocument(),
            ):
                extracted = extract_pdf(
                    pdf_path,
                    output,
                    "machine",
                    "machine/manual.pdf",
                    document_output_dir=document_output,
                )

            self.assertEqual(extracted["schema_version"], 3)
            self.assertNotIn("text", extracted["pages"][0])
            self.assertTrue(all(
                "spans" not in block
                for page in extracted["pages"]
                for block in page["text_blocks"]
            ))
            saved_extraction = json.loads(
                (document_output / "extraction.json").read_text(
                    encoding="utf-8",
                )
            )
            self.assertNotIn("text", saved_extraction["pages"][0])
            self.assertEqual(saved_extraction["schema_version"], 3)
            self.assertEqual(len(extracted["image_assets"]), 1)
            self.assertEqual(len(extracted["images"]), 3)
            self.assertEqual(
                len({image["asset_id"] for image in extracted["images"]}),
                1,
            )
            self.assertEqual(
                [image["page_index"] for image in extracted["images"]],
                [0, 0, 1],
            )
            self.assertEqual(
                [image["bbox"] for image in extracted["images"]],
                [
                    [10.0, 20.0, 40.0, 50.0],
                    [60.0, 20.0, 90.0, 50.0],
                    [10.0, 30.0, 40.0, 60.0],
                ],
            )
            self.assertEqual(
                len(list((document_output / "images").iterdir())),
                1,
            )
            self.assertTrue(any(
                warning.get("page_number") == 1
                and "OCR was not run" in warning.get("warning", "")
                for warning in extracted["warnings"]
            ))


class BatchFailureTests(unittest.TestCase):
    """A failed manual must be recorded without preventing later PDFs."""

    def test_one_failed_pdf_does_not_stop_the_next_pdf(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            raw_root = root / "raw"
            (raw_root / "machine").mkdir(parents=True)
            (raw_root / "machine" / "1-bad.pdf").write_bytes(b"bad")
            (raw_root / "machine" / "2-good.pdf").write_bytes(b"good")
            processed_root = root / "processed"
            source_root = Path(__file__).parents[1] / "src"
            with patch.object(sys, "path", [str(source_root), *sys.path]):
                main_module = importlib.import_module("ingestion.main")

            calls = []

            def fake_pipeline(pdf_path, raw_root, output_root, machine_name,
                              logger=None):
                del raw_root, logger
                calls.append(pdf_path.name)
                if pdf_path.name == "1-bad.pdf":
                    raise RuntimeError("synthetic failure")
                output_dir = output_root / machine_name / "2-good"
                output_dir.mkdir(parents=True, exist_ok=True)
                for filename in (
                    "2-good.json",
                    "2-good.extraction.json",
                    "2-good.tables.json",
                ):
                    (output_dir / filename).write_text("{}", encoding="utf-8")
                return {
                    "output_directory": "machine/2-good",
                    "output_files": {
                        "document_json": "2-good.json",
                        "extraction_json": "2-good.extraction.json",
                        "tables_json": "2-good.tables.json",
                        "log": "2-good.log",
                    },
                    "document_id": "doc-good",
                    "page_count": 1,
                    "tables": [],
                    "image_count": 0,
                    "rag_units": [],
                    "validation": {
                        "status": "valid",
                        "extraction_warnings": [],
                        "issues": [],
                    },
                }

            with patch.object(
                main_module,
                "run_document_pipeline",
                side_effect=fake_pipeline,
            ):
                manifest = main_module.run_ingestion(
                    raw_root=raw_root,
                    processed_root=processed_root,
                )

        self.assertEqual(calls, ["1-bad.pdf", "2-good.pdf"])
        self.assertEqual(manifest["processed_pdf_count"], 1)
        self.assertEqual(manifest["failed_pdf_count"], 1)
        self.assertEqual(
            [item["status"] for item in manifest["documents"]],
            ["failed", "success"],
        )

    def test_review_warning_after_ingestion_does_not_fail_the_pdf(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            raw_root = root / "raw"
            (raw_root / "machine").mkdir(parents=True)
            (raw_root / "machine" / "manual.pdf").write_bytes(b"pdf")
            processed_root = root / "processed"
            source_root = Path(__file__).parents[1] / "src"
            with patch.object(sys, "path", [str(source_root), *sys.path]):
                main_module = importlib.import_module("ingestion.main")

            def fake_pipeline(pdf_path, raw_root, output_root, machine_name,
                              logger=None):
                del pdf_path, raw_root, logger
                output_dir = output_root / machine_name / "manual"
                output_dir.mkdir(parents=True, exist_ok=True)
                for filename in (
                    "manual.json",
                    "manual.extraction.json",
                    "manual.tables.json",
                ):
                    (output_dir / filename).write_text("{}", encoding="utf-8")
                return {
                    "output_directory": "machine/manual",
                    "output_files": {
                        "document_json": "manual.json",
                        "extraction_json": "manual.extraction.json",
                        "tables_json": "manual.tables.json",
                        "log": "manual.log",
                    },
                    "document_id": "doc-1",
                    "page_count": 1,
                    "tables": [],
                    "image_count": 0,
                    "rag_units": [],
                    "validation": {
                        "status": "valid",
                        "extraction_warnings": [],
                        "issues": [],
                    },
                }

            with patch.object(
                main_module,
                "run_document_pipeline",
                side_effect=fake_pipeline,
            ), patch.object(
                main_module,
                "audit_document",
                return_value={
                    "overall_status": "needs_review",
                    "summary": {"by_severity": {"warning": 1}},
                    "report_json": str(
                        processed_root
                        / "documents"
                        / "machine"
                        / "manual"
                        / "knowledge-audit.json"
                    ),
                },
            ):
                manifest = main_module.run_ingestion(
                    raw_root=raw_root,
                    processed_root=processed_root,
                    audit_after_ingestion=True,
                )

        self.assertEqual(manifest["processed_pdf_count"], 1)
        self.assertEqual(manifest["failed_pdf_count"], 0)
        self.assertEqual(manifest["documents"][0]["status"], "success")
        self.assertEqual(
            manifest["documents"][0]["audit_status"],
            "needs_review",
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
                ) as extract_pdf_mock,
                patch(
                    "src.ingestion.pipeline.extract_tables",
                    return_value={
                        "tables": [],
                        "table_count": 0,
                        "warnings": [],
                    },
                ) as extract_tables_mock,
            ):
                document = run_document_pipeline(
                    pdf_path=pdf_path,
                    raw_root=raw_root,
                    output_root=output_root,
                    machine_name="HSG-2550",
                    logger=logger,
                )

            self.assertFalse(
                extract_pdf_mock.call_args.kwargs["write_output"],
            )
            self.assertFalse(
                extract_tables_mock.call_args.kwargs["write_output"],
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
