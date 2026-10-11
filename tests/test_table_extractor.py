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
    reconstruct_logical_tables,
    validate_table,
)
from src.ingestion.pdf_extractor import extract_pdf
from src.ingestion.pipeline import (
    _apply_confirmed_logical_table_schema,
    build_relationships,
)


class FakeTable:
    """Small PyMuPDF-table substitute used to test the extraction boundary."""

    def __init__(
        self,
        rows,
        bbox=(50.0, 80.0, 500.0, 400.0),
        cell_rectangles=None,
    ):
        self._extracted_rows = rows
        self.bbox = bbox
        self.rows = [
            SimpleNamespace(cells=rectangles)
            for rectangles in (cell_rectangles or [])
        ]

    def extract(self):
        return self._extracted_rows


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

    def test_vertical_merged_cell_values_use_geometry_but_keep_raw_rows(self):
        source_rows = [
            ["NUMBER", "TYPE", "NAME"],
            ["1", "Screen", "Main screen"],
            ["2", "Dials", "Electro-magnetic chuck voltage"],
            ["3", None, "Y-axis intermittent feed"],
            ["4", "Switches", "Work lamp on/off"],
            ["5", None, "Coolant pump on/off"],
            ["6", None, "Electro-magnetic chuck on/off"],
        ]
        row_bounds = [(index * 10.0, (index + 1) * 10.0)
                      for index in range(len(source_rows))]
        cell_rectangles = []
        for row_index, (top, bottom) in enumerate(row_bounds):
            cells = [
                (0.0, top, 10.0, bottom),
                (10.0, top, 20.0, bottom),
                (20.0, top, 30.0, bottom),
            ]
            if row_index == 2:
                cells[1] = (10.0, 20.0, 20.0, 40.0)
            elif row_index == 3:
                cells[1] = None
            elif row_index == 4:
                cells[1] = (10.0, 40.0, 20.0, 70.0)
            elif row_index in (5, 6):
                cells[1] = None
            cell_rectangles.append(cells)

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pdf_path = root / "manual.pdf"
            pdf_path.write_bytes(b"synthetic source PDF identity")
            output_dir = root / "output"
            fake_table = FakeTable(
                source_rows,
                cell_rectangles=cell_rectangles,
            )
            with patch(
                "src.ingestion.table_extractor.pymupdf.open",
                return_value=FakeDocument([FakePage([fake_table])]),
            ):
                result = extract_tables(pdf_path, output_dir)

        table = result["tables"][0]
        self.assertIsNone(table["raw_rows"][3][1])
        self.assertIsNone(table["raw_rows"][5][1])
        self.assertEqual(
            [row["TYPE"] for row in table["rows"]],
            ["Screen", "Dials", "Dials", "Switches", "Switches", "Switches"],
        )
        self.assertEqual(len(table["layout_adjustments"]), 3)

    def test_sequential_numbered_data_row_is_not_mistaken_for_header(self):
        table = self.extract_from_rows([
            ["1", "Screen", "Indicator lights"],
            ["2", None, "Pump buttons"],
            ["3", None, "Spindle buttons"],
        ])

        self.assertEqual(table["header_source"], "unknown_header")
        self.assertEqual(len(table["rows"]), 3)
        self.assertEqual(table["raw_rows"][0][0], "1")

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
        self.assertTrue(
            table["validation"]["signals"]["probable_false_positive"],
        )

    def test_large_multirow_engineering_table_is_not_rejected_for_size(self):
        rows = [
            ["ITEM", "MINIMUM", "MAXIMUM"],
            *[
                [f"Measurement {index}", str(index), str(index + 10)]
                for index in range(1, 9)
            ],
        ]
        table = self.extract_from_rows(
            rows,
            bbox=(10.0, 10.0, 590.0, 790.0),
        )

        self.assertEqual(table["validation"]["detection_class"],
                         "table_candidate")
        self.assertFalse(
            table["validation"]["signals"]["probable_false_positive"],
        )

    def test_full_page_two_row_table_is_not_rejected_for_size_alone(self):
        table = self.extract_from_rows(
            [
                ["ITEM", "RANGE", "ACTION"],
                ["Hydraulic pressure", "0-10 MPa", "Inspect gauge"],
            ],
            bbox=(5.0, 5.0, 595.0, 795.0),
        )

        self.assertNotEqual(
            table["validation"]["detection_class"],
            "probable_false_positive",
        )
        self.assertIn(
            "page_wide_text_detection",
            {item["code"] for item in table["validation"]["warnings"]},
        )

    def test_flattened_numbered_list_combines_multiple_false_positive_signals(self):
        rows = [[
            "7 Power up 8 E-Stop 9 Z-axis rapid up 10 Z-axis rapid down "
            "11 Y-axis rapid forward 12 Y-axis rapid backwards",
        ]]
        result = validate_table(
            rows,
            ["column_1", "column_2", "column_3"],
            (20.0, 20.0, 580.0, 300.0),
            (0.0, 0.0, 600.0, 800.0),
            "unknown_header",
        )

        warning_codes = {warning["code"] for warning in result["warnings"]}
        self.assertEqual(
            result["detection_class"],
            "probable_false_positive",
        )
        self.assertIn("generic_headers", warning_codes)
        self.assertIn("numbered_entries_in_cell", warning_codes)
        self.assertIn("column_structure_mismatch", warning_codes)
        self.assertGreaterEqual(
            result["signals"]["numbered_entries_in_cell"],
            4,
        )

    def _fragment(
        self,
        table_id,
        page_index,
        bbox,
        raw_rows,
        headers,
        rows,
        context=None,
        header_source="detected_header_row",
    ):
        """Build provenance-complete page fragments for reconstruction tests."""
        return {
            "table_id": table_id,
            "document_id": "doc-1",
            "source_filename": "manual.pdf",
            "source_sha256": "hash",
            "page_index": page_index,
            "page_number": page_index + 1,
            "page_rect": [0.0, 0.0, 600.0, 800.0],
            "bbox": bbox,
            "headers": headers,
            "raw_rows": raw_rows,
            "rows": rows,
            "header_source": header_source,
            "section_context": context,
            "validation": {
                "detection_class": "table_candidate",
                "status": "valid",
                "warnings": [],
            },
        }

    def test_continuation_without_repeated_header_uses_contiguous_rows(self):
        first = self._fragment(
            "table-p1", 0, [50, 600, 550, 780],
            [["NUMBER", "TYPE", "NAME"], ["6", None, "Last row"]],
            ["NUMBER", "TYPE", "NAME"],
            [{"NUMBER": "6", "TYPE": None, "NAME": "Last row"}],
            context="Main Control",
        )
        second = self._fragment(
            "table-p2", 1, [50, 20, 550, 180],
            [["7", "Buttons", "Power up"], ["8", None, "E-Stop"]],
            ["7", "Buttons", "Power up"],
            [{"7": "8", "Buttons": None, "Power up": "E-Stop"}],
            context="Main Control",
        )

        logical, relationships = reconstruct_logical_tables([first, second])

        self.assertEqual(len(logical), 1)
        self.assertEqual(logical[0]["merge_status"], "confirmed")
        self.assertEqual(logical[0]["headers"], ["NUMBER", "TYPE", "NAME"])
        self.assertEqual(
            logical[0]["rows"][-2]["row"],
            {"NUMBER": "7", "TYPE": "Buttons", "NAME": "Power up"},
        )
        self.assertIsNone(logical[0]["rows"][-1]["row"]["TYPE"])
        self.assertEqual(
            logical[0]["rows"][-1]["source_fragment_id"],
            "table-p2",
        )
        self.assertEqual(logical[0]["rows"][-1]["source_page_index"], 1)
        self.assertEqual(logical[0]["source_fragments"][1]["raw_rows"],
                         second["raw_rows"])
        self.assertEqual(relationships[0]["relation_status"], "confirmed")

    def test_continuation_with_repeated_header_drops_only_repeated_header(self):
        first = self._fragment(
            "table-p1", 0, [50, 600, 550, 780],
            [["NUMBER", "NAME"], ["1", "A"]],
            ["NUMBER", "NAME"], [{"NUMBER": "1", "NAME": "A"}],
            context="Parts",
        )
        second = self._fragment(
            "table-p2", 1, [50, 20, 550, 180],
            [["NUMBER", "NAME"], ["2", "B"]],
            ["NUMBER", "NAME"], [{"NUMBER": "2", "NAME": "B"}],
            context="Parts",
        )

        logical, relationships = reconstruct_logical_tables([first, second])

        self.assertEqual(len(logical), 1)
        self.assertEqual([row["row"] for row in logical[0]["rows"]], [
            {"NUMBER": "1", "NAME": "A"},
            {"NUMBER": "2", "NAME": "B"},
        ])
        self.assertTrue(relationships[0]["evidence"]["signals"]["repeated_header"])

    def test_unrelated_consecutive_page_tables_are_not_merged(self):
        first = self._fragment(
            "table-p1", 0, [50, 600, 550, 780],
            [["PART", "DESCRIPTION"], ["A", "Bearing"]],
            ["PART", "DESCRIPTION"], [{"PART": "A", "DESCRIPTION": "Bearing"}],
        )
        second = self._fragment(
            "table-p2", 1, [50, 20, 550, 180],
            [["CODE", "ACTION"], ["Z", "Reset machine"]],
            ["CODE", "ACTION"], [{"CODE": "Z", "ACTION": "Reset machine"}],
        )

        logical, relationships = reconstruct_logical_tables([first, second])

        self.assertEqual(logical, [])
        self.assertEqual(relationships, [])

    def test_ambiguous_continuation_is_candidate_not_logical_data(self):
        first = self._fragment(
            "table-p1", 0, [50, 600, 550, 780],
            [["NUMBER", "NAME"], ["2", "A"]],
            ["NUMBER", "NAME"], [{"NUMBER": "2", "NAME": "A"}],
            context="Inspection",
        )
        second = self._fragment(
            "table-p2", 1, [50, 20, 550, 180],
            [["NUMBER", "NAME"], ["9", "Different table"]],
            ["NUMBER", "NAME"],
            [{"NUMBER": "9", "NAME": "Different table"}],
            context="Inspection",
        )

        logical, relationships = reconstruct_logical_tables([first, second])

        self.assertEqual(logical, [])
        self.assertEqual(relationships[0]["relation_status"], "candidate")
        self.assertFalse(relationships[0]["evidence"]["signals"]["numbering_continues"])

    def test_real_operation_manual_continuation_reconstructs_rows(self):
        repository_root = Path(__file__).resolve().parents[1]
        pdf_path = (
            repository_root / "data" / "raw" / "HSG-2550"
            / "5 - Haas Surface Grinder - Operation.pdf"
        )
        if not pdf_path.is_file():
            self.skipTest("Local Haas Operation PDF fixture is unavailable.")

        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_path = Path(temporary_directory)
            pdf_extraction = extract_pdf(
                pdf_path=pdf_path,
                output_dir=temporary_path,
                machine_name="HSG-2550",
                relative_source_path=(
                    "HSG-2550/5 - Haas Surface Grinder - Operation.pdf"
                ),
                document_output_dir=temporary_path / "document",
            )
            extracted = extract_tables(
                pdf_path,
                temporary_path,
                source_sha256=pdf_extraction["source_sha256"],
            )
            build_relationships(
                pdf_extraction["pages"],
                extracted["tables"],
                pdf_extraction["images"],
            )

        logical, relationships = reconstruct_logical_tables(
            extracted["tables"],
        )
        _apply_confirmed_logical_table_schema(
            extracted["tables"],
            logical,
        )
        operation_table = next(
            item for item in logical
            if item["headers"] == ["NUMBER", "TYPE", "NAME"]
        )
        self.assertEqual(
            [row["row"]["NUMBER"] for row in operation_table["rows"]],
            [str(number) for number in range(1, 14)],
        )
        self.assertEqual(
            operation_table["rows"][6]["row"]["NAME"],
            "Power up",
        )
        self.assertEqual(
            operation_table["rows"][7]["row"]["NAME"],
            "E-Stop",
        )
        self.assertEqual(
            [operation_table["rows"][index]["row"]["TYPE"]
             for index in range(1, 6)],
            ["Dials", "Dials", "Switches", "Switches", "Switches"],
        )
        self.assertEqual(
            operation_table["rows"][6]["row"]["TYPE"],
            "Buttons",
        )
        self.assertEqual(
            operation_table["source_fragments"][0]["raw_rows"][3][1],
            None,
        )
        self.assertEqual(
            operation_table["source_fragments"][0]["normalized_raw_rows"][3][1],
            "Dials",
        )
        running_page_table = next(
            table for table in extracted["tables"]
            if table["table_id"] == "7ced28eafbd76082_p003_t02"
        )
        self.assertEqual(len(running_page_table["rows"]), 10)
        self.assertEqual(running_page_table["rows"][0]["column_1"], "1")
        self.assertEqual(
            {row["source_page_index"] for row in operation_table["rows"]},
            {0, 1},
        )
        self.assertEqual(
            operation_table["source_fragments"][1]["table_id"],
            "7ced28eafbd76082_p002_t02",
        )
        self.assertTrue(any(
            relation["relation_status"] == "confirmed"
            for relation in relationships
        ))

        abrasives = next(
            item for item in logical
            if item["headers"]
            == ["ABRASIVE", "ABRASIVE DENOTATION"]
        )
        abrasive_names = [
            row["row"]["ABRASIVE"] for row in abrasives["rows"]
        ]
        self.assertEqual(abrasives["merge_status"], "confirmed")
        self.assertEqual(len(abrasives["rows"]), 14)
        self.assertIn("Zirconia with Brown Aluminum Oxide", abrasive_names)
        self.assertNotIn(
            "kshi/OneDrive/Desktop/raw/HAB-3232/5 - Haas Surface Grinder - Operatio",
            abrasive_names,
        )
        self.assertIn(
            "page_footer_row_not_reconstructed",
            {warning["code"] for warning in abrasives["warnings"]},
        )
        abrasive_continuation = next(
            table for table in extracted["tables"]
            if table["table_id"] == "7ced28eafbd76082_p005_t02"
        )
        self.assertEqual(
            abrasive_continuation["headers"],
            ["ABRASIVE", "ABRASIVE DENOTATION"],
        )
        self.assertEqual(
            abrasive_continuation["rows"][0],
            {
                "ABRASIVE": "Zirconia with Brown Aluminum Oxide",
                "ABRASIVE DENOTATION": "ZA",
            },
        )
        self.assertEqual(
            abrasive_continuation["detected_headers"],
            ["Zirconia with Brown Aluminum Oxide", "ZA"],
        )
        self.assertIn(
            "continuation_first_row_reclassified_as_data",
            {
                warning["code"]
                for warning in abrasive_continuation["validation"][
                    "resolved_header_warnings"
                ]
            },
        )
        self.assertTrue(any(
            row["source_page_index"] == 4 for row in abrasives["rows"]
        ))

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
