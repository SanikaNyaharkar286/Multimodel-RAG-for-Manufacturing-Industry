"""Unit tests for structural PDF table extraction and validation."""

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.ingestion.table_extractor import (
    _infer_table_structure,
    extract_tables,
    validate_table,
)


class FakeTable:
    """Small PyMuPDF-table substitute used to test the extraction boundary."""

    def __init__(self, rows, bbox=(50.0, 80.0, 500.0, 400.0)):
        self.rows = rows
        self.bbox = bbox

    def extract(self):
        return self.rows


class FakePage:
    """Provide only the page properties used by the table extractor."""

    rect = (0.0, 0.0, 600.0, 800.0)

    def __init__(self, tables, blocks=()):
        self.tables = tables
        self.blocks = blocks

    def find_tables(self):
        return SimpleNamespace(tables=self.tables)

    def get_text(self, mode):
        if mode != "blocks":
            raise AssertionError(f"Unexpected text mode: {mode}")
        return self.blocks


class FakeDocument:
    """Minimal context-managed document with an iterable page collection."""

    def __init__(self, pages):
        self.pages = pages

    def __enter__(self):
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback):
        del _exc_type, _exc_value, _traceback
        return False

    def __iter__(self):
        return iter(self.pages)


class TableExtractorTests(unittest.TestCase):
    """Exercise header inference, preservation, validation, and JSON output."""

    def extract_from_rows(self, rows, bbox=(50.0, 80.0, 500.0, 400.0)):
        """Run the public extractor against a synthetic, mocked PDF page."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pdf_path = root / "manual.pdf"
            pdf_path.write_bytes(b"synthetic source PDF identity")
            output_dir = root / "output"
            page = FakePage([FakeTable(rows, bbox=bbox)])

            with patch(
                "src.ingestion.table_extractor.pymupdf.open",
                return_value=FakeDocument([page]),
            ):
                result = extract_tables(pdf_path, output_dir)

            saved = json.loads((output_dir / "tables.json").read_text(
                encoding="utf-8",
            ))
            self.assertEqual(result, saved)
            return result["tables"][0]

    def test_conventional_parts_list_keeps_values_with_their_columns(self):
        table = self.extract_from_rows([
            ["PART NUMBER", "DESCRIPTION"],
            ["93-4718", "AC CONTACTOR"],
            ["93-4719", "TRANSFORMER"],
        ])

        self.assertEqual(table["headers"], ["PART NUMBER", "DESCRIPTION"])
        self.assertEqual(table["rows"], [
            {"PART NUMBER": "93-4718", "DESCRIPTION": "AC CONTACTOR"},
            {"PART NUMBER": "93-4719", "DESCRIPTION": "TRANSFORMER"},
        ])
        self.assertEqual(table["extraction_method"], "pymupdf")

    def test_merged_title_is_not_used_as_a_column_header(self):
        title, headers, rows, source, warnings = _infer_table_structure([
            ["MAINTENANCE", None, None],
            ["Items", "Lubrication", "Interval"],
            ["Inspect tank", "ISO 68 oil", "Daily"],
            ["Clean filter", "ISO 68 oil", "Biannually"],
        ])

        self.assertEqual(title, "MAINTENANCE")
        self.assertEqual(headers, ["Items", "Lubrication", "Interval"])
        self.assertEqual(rows[0], ["Inspect tank", "ISO 68 oil", "Daily"])
        self.assertEqual(source, "detected_header_row")
        self.assertEqual(warnings, [])

    def test_multi_level_header_is_combined_by_column_structure(self):
        table = self.extract_from_rows([
            ["ITEM", "INSPECTION ITEMS", "MACHINE STATE", None, "ACTION"],
            [None, None, "Off", "On", None],
            ["1", "Tank level", "X", "", "Check the limit"],
            ["2", "Control position", "", "X", "Set the control"],
        ])

        self.assertEqual(table["header_source"], "detected_multilevel_header")
        self.assertEqual(table["headers"], [
            "ITEM",
            "INSPECTION ITEMS",
            "MACHINE STATE - Off",
            "MACHINE STATE - On",
            "ACTION",
        ])
        self.assertEqual(table["rows"][0]["MACHINE STATE - Off"], "X")
        self.assertEqual(table["rows"][0]["MACHINE STATE - On"], None)
        self.assertEqual(table["rows"][1]["MACHINE STATE - Off"], None)
        self.assertEqual(table["rows"][1]["MACHINE STATE - On"], "X")
        self.assertEqual(table["validation"]["status"], "valid")

    def test_page_wide_one_row_detection_is_retained_and_flagged(self):
        table = self.extract_from_rows(
            [["", "Full-page diagram text " * 12, ""]],
            bbox=(0.0, 0.0, 600.0, 800.0),
        )

        self.assertEqual(table["raw_rows"][0][1], "Full-page diagram text " * 12)
        self.assertEqual(table["validation"]["detection_class"],
                         "probable_false_positive")
        self.assertTrue(table["fallback"]["needed"])
        self.assertEqual(table["fallback"]["method"],
                         "text_spans_and_coordinates")
        self.assertEqual(table["fallback"]["status"], "pending")

    def test_unknown_headers_preserve_rows_and_report_uncertainty(self):
        table = self.extract_from_rows([
            ["Inspect seals", "Monthly"],
            ["Replace filter", "Every six months"],
        ])

        self.assertEqual(table["header_source"], "unknown_header")
        self.assertEqual(table["validation"]["detection_class"],
                         "unknown_header_structure")
        self.assertEqual(table["validation"]["status"], "uncertain")
        self.assertEqual(table["raw_rows"], [
            ["Inspect seals", "Monthly"],
            ["Replace filter", "Every six months"],
        ])
        self.assertEqual(len(table["rows"]), 2)
        self.assertTrue(table["fallback"]["needed"])

    def test_malformed_row_lengths_are_warned_and_extra_values_are_kept(self):
        title, headers, rows, source, _ = _infer_table_structure([
            ["Part", "Description"],
            ["93-1", "Bearing", "Unmapped extra cell"],
            ["93-2", "Seal", None],
        ])
        validation = validate_table(
            [["Part", "Description"],
             ["93-1", "Bearing", "Unmapped extra cell"]],
            headers + ["column_3"],
            (50.0, 80.0, 500.0, 400.0),
            (0.0, 0.0, 600.0, 800.0),
            source,
        )

        self.assertIsNone(title)
        self.assertEqual(headers, ["Part", "Description", "column_3"])
        self.assertEqual(rows[0][2], "Unmapped extra cell")
        self.assertIn(
            "inconsistent_row_lengths",
            {warning["code"] for warning in validation["warnings"]},
        )
        self.assertEqual(validation["status"], "suspicious")

    def test_empty_and_duplicate_header_labels_are_flagged(self):
        duplicate = self.extract_from_rows([
            ["Part", "Part"],
            ["93-1", "Bearing"],
            ["93-2", "Seal"],
        ])
        empty = self.extract_from_rows([
            ["Part", None],
            ["93-1", "Bearing"],
            ["93-2", "Seal"],
        ])

        duplicate_codes = {
            warning["code"] for warning in duplicate["validation"]["warnings"]
        }
        empty_codes = {
            warning["code"] for warning in empty["validation"]["warnings"]
        }
        self.assertEqual(duplicate["headers"], ["Part", "Part_2"])
        self.assertIn("duplicate_headers", duplicate_codes)
        self.assertEqual(duplicate["validation"]["status"], "suspicious")
        self.assertEqual(empty["headers"], ["Part", "column_2"])
        self.assertIn("empty_headers", empty_codes)
        self.assertEqual(empty["validation"]["status"], "suspicious")

    def test_unidentified_title_and_section_remain_null(self):
        table = self.extract_from_rows([
            ["Part", "Description"],
            ["93-1", "Bearing"],
            ["93-2", "Seal"],
        ])

        self.assertIsNone(table["title"])
        self.assertEqual(table["title_source"], "unknown")
        self.assertIsNone(table["section_heading"])
        self.assertEqual(table["section_heading_source"], "unknown")

    def test_provenance_fields_and_schema_version_are_written(self):
        table = self.extract_from_rows([
            ["Part", "Description"],
            ["93-1", "Bearing"],
            ["93-2", "Seal"],
        ])

        self.assertEqual(table["document_id"], table["source_sha256"][:16])
        self.assertEqual(table["source_filename"], "manual.pdf")
        self.assertEqual(table["page_number"], 1)
        self.assertEqual(table["table_index_on_page"], 1)
        self.assertEqual(table["bbox"], [50.0, 80.0, 500.0, 400.0])


if __name__ == "__main__":
    unittest.main()
