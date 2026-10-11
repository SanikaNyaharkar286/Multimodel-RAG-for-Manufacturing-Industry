"""Consistent, filesystem-safe output paths derived from source PDF names."""

import re
from pathlib import Path, PurePosixPath


INVALID_FILENAME_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
WINDOWS_RESERVED_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{index}" for index in range(1, 10)),
    *(f"LPT{index}" for index in range(1, 10)),
}


def safe_filename_stem(source_filename: str) -> str:
    """Return a readable filename stem that is safe on Windows and POSIX.

    The original source filename remains separately recorded in metadata;
    replacing forbidden path characters prevents the output escaping its
    document folder or failing on Windows.
    """
    stem = Path(source_filename).stem
    safe_stem = INVALID_FILENAME_CHARACTERS.sub("_", stem).strip(" .")
    if not safe_stem:
        safe_stem = "unnamed_document"
    if safe_stem.split(".", maxsplit=1)[0].upper() in WINDOWS_RESERVED_NAMES:
        safe_stem = f"_{safe_stem}"
    return safe_stem


def document_output_directory(
    output_root: Path,
    machine_name: str,
    relative_source_path: str,
) -> Path:
    """Build a readable destination without collisions between nested PDFs."""
    source_parts = PurePosixPath(relative_source_path).parts
    nested_parts = list(source_parts[:-1])
    if nested_parts and nested_parts[0].casefold() == machine_name.casefold():
        nested_parts = nested_parts[1:]

    safe_directories = [
        INVALID_FILENAME_CHARACTERS.sub("_", part).strip(" .") or "unnamed"
        for part in nested_parts
    ]
    source_filename = source_parts[-1] if source_parts else "unnamed.pdf"
    return (
        Path(output_root)
        / machine_name
        / Path(*safe_directories)
        / safe_filename_stem(source_filename)
    )
