"""Discover only source folders and PDF files under the raw-data directory."""

import json
from pathlib import Path
from typing import Any


def track_source_paths(raw_root: Path) -> dict[str, Any]:
    """List nested folders and PDFs without indexing unrelated file types.

    Returned paths are relative to ``raw_root`` so the tracker stays portable
    when the project folder is moved to another machine.
    """
    raw_root = Path(raw_root)
    if not raw_root.exists():
        raise FileNotFoundError(f"Raw data directory not found: {raw_root}")
    if not raw_root.is_dir():
        raise NotADirectoryError(f"Raw data path is not a directory: {raw_root}")

    folders = []
    pdf_files = []
    for path in raw_root.rglob("*"):
        relative_path = path.relative_to(raw_root).as_posix()
        if path.is_dir():
            folders.append(relative_path)
        elif path.is_file() and path.suffix.lower() == ".pdf":
            source_parts = path.relative_to(raw_root).parts
            pdf_files.append({
                "relative_path": relative_path,
                "source_filename": path.name,
                "machine_name": source_parts[0] if len(source_parts) > 1 else "unassigned",
            })

    return {
        "raw_root": str(raw_root.resolve()),
        "folder_count": len(folders),
        "folders": sorted(folders, key=str.casefold),
        "pdf_count": len(pdf_files),
        "pdf_files": sorted(
            pdf_files,
            key=lambda item: item["relative_path"].casefold(),
        ),
    }


def write_path_tracker(raw_root: Path, output_path: Path) -> dict[str, Any]:
    """Write the discovery result as JSON and return the same tracker data."""
    tracker = track_source_paths(raw_root)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(tracker, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return tracker
