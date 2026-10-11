"""Discover raw PDF manuals and run the complete local ingestion pipeline."""

import json
import logging
from pathlib import Path
from typing import Any

from ingestion.config import PROCESSED_DATA_DIR, RAW_DATA_DIR
from ingestion.file_naming import (
    document_output_directory,
    safe_filename_stem,
)
from ingestion.path_tracker import write_path_tracker
from ingestion.pipeline import run_document_pipeline


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def run_ingestion(
    raw_root: Path = RAW_DATA_DIR,
    processed_root: Path = PROCESSED_DATA_DIR,
) -> dict[str, Any]:
    """Track source folders/PDFs and process every discovered PDF.

    Per-file failures are written into the manifest and printed explicitly;
    one damaged manual does not prevent the remaining manuals from running.
    """
    raw_root = Path(raw_root)
    processed_root = Path(processed_root)
    processed_root.mkdir(parents=True, exist_ok=True)
    tracker = write_path_tracker(
        raw_root=raw_root,
        output_path=processed_root / "path_tracker.json",
    )

    results = []
    for source in tracker["pdf_files"]:
        pdf_path = raw_root / Path(source["relative_path"])
        output_base = processed_root / "documents"
        document_dir = document_output_directory(
            output_base,
            source["machine_name"],
            source["relative_path"],
        )
        document_dir.mkdir(parents=True, exist_ok=True)
        output_stem = safe_filename_stem(pdf_path.name)
        log_path = document_dir / f"{output_stem}.log"
        logger = logging.getLogger(
            f"pdf_ingestion.{source['relative_path']}"
        )
        logger.setLevel(logging.INFO)
        logger.propagate = False
        file_handler = logging.FileHandler(
            log_path,
            mode="w",
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s",
        ))
        logger.addHandler(file_handler)
        try:
            logger.info("Starting ingestion for %s", pdf_path)
            document = run_document_pipeline(
                pdf_path=pdf_path,
                raw_root=raw_root,
                output_root=output_base,
                machine_name=source["machine_name"],
                logger=logger,
            )
            output_directory = Path(document["output_directory"])
            output_path = processed_root / "documents" / output_directory
            expected_outputs = [
                document["output_files"]["document_json"],
                document["output_files"]["extraction_json"],
                document["output_files"]["tables_json"],
                document["output_files"]["log"],
            ]
            missing_outputs = [
                filename
                for filename in expected_outputs
                if not (output_path / filename).is_file()
            ]
            if missing_outputs:
                raise FileNotFoundError(
                    "Pipeline returned without writing expected output(s): "
                    + ", ".join(missing_outputs)
                )
            validation = document.get("validation", {})
            document_warnings = (
                validation.get("extraction_warnings", [])
                + validation.get("issues", [])
            )
            results.append({
                "relative_path": source["relative_path"],
                "status": "success",
                "document_id": document["document_id"],
                "validation_status": validation.get("status", "unknown"),
                "output_directory": (
                    Path("documents") / output_directory
                ).as_posix(),
                "document_json": (
                    Path("documents")
                    / output_directory
                    / document["output_files"]["document_json"]
                ).as_posix(),
                "log_file": (
                    Path("documents")
                    / output_directory
                    / document["output_files"]["log"]
                ).as_posix(),
                "page_count": document["page_count"],
                "table_count": len(document["tables"]),
                "image_count": document["image_count"],
                "rag_unit_count": len(document["rag_units"]),
                "warning_count": len(document_warnings),
                "warnings": document_warnings,
            })
            print(
                f"Processed {source['relative_path']}: "
                f"{document['page_count']} pages, "
                f"{len(document['tables'])} tables, "
                f"{document['image_count']} images"
            )
        except Exception as error:
            failure = {
                "relative_path": source["relative_path"],
                "status": "failed",
                "error": f"{type(error).__name__}: {error}",
                "log_file": str(
                    log_path.relative_to(processed_root)
                ).replace("\\", "/"),
            }
            results.append(failure)
            logger.exception("Ingestion failed for %s", pdf_path)
            print(f"FAILED {source['relative_path']}: {failure['error']}")
        finally:
            logger.removeHandler(file_handler)
            file_handler.close()

    manifest = {
        "schema_version": 1,
        "raw_root": tracker["raw_root"],
        "path_tracker": "path_tracker.json",
        "discovered_folder_count": tracker["folder_count"],
        "discovered_pdf_count": tracker["pdf_count"],
        "processed_pdf_count": sum(
            result["status"] == "success" for result in results
        ),
        "failed_pdf_count": sum(
            result["status"] == "failed" for result in results
        ),
        "documents": results,
    }
    (processed_root / "ingestion_manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return manifest


if __name__ == "__main__":
    run_ingestion()
