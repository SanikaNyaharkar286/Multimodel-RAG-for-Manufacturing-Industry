
"""
Read-only ID-generation audit for the src1 RAG pipeline.

Checks:
1. Locates Python source files under src1.
2. Finds ID-related generation and assignment code.
3. Finds UUID, hash, and random ID-generation calls.
4. Reports line numbers and nearby code for manual review.

Does not execute pipeline code or access BigQuery.
"""

from pathlib import Path
import argparse
import re


ID_TERMS = re.compile(
    r"chunk_id|source_id|image_id|table_id|link_id|section_id|"
    r"element_id|segment_id|relationship_id|"
    r"uuid|uuid4|uuid5|hashlib|sha256|md5|"
    r"generate.*id|create.*id|make.*id",
    re.IGNORECASE,
)

GENERATION_TERMS = re.compile(
    r"uuid|uuid4|uuid5|sha256|md5|hashlib|"
    r"hash\(|random|secrets|"
    r"chunk_id\s*=|source_id\s*=|image_id\s*=|"
    r"table_id\s*=|link_id\s*=|section_id\s*=|"
    r"element_id\s*=|segment_id\s*=",
    re.IGNORECASE,
)

SKIP_DIRS = {
    ".venv",
    "venv",
    "__pycache__",
    ".git",
    "site-packages",
    "node_modules",
}


def find_python_files(source_root: Path):
    for path in source_root.rglob("*.py"):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        yield path


def inspect_file(path: Path, source_root: Path, context: int):
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        print(f"\n[SKIPPED] {path}: {exc}")
        return

    matches = [
        i for i, line in enumerate(lines)
        if ID_TERMS.search(line)
    ]

    if not matches:
        return

    print("\n" + "=" * 90)
    print(f"FILE: {path.relative_to(source_root)}")
    print("=" * 90)

    shown = set()

    for index in matches:
        start = max(0, index - context)
        end = min(len(lines), index + context + 1)

        # Avoid printing overlapping context repeatedly.
        if any(i in shown for i in range(start, end)):
            continue

        print(f"\n--- Around line {index + 1} ---")

        for i in range(start, end):
            marker = ">>" if i == index else "  "
            line = lines[i]
            flag = (
                "  [POSSIBLE GENERATION]"
                if GENERATION_TERMS.search(line)
                else ""
            )
            print(f"{marker} {i + 1:5}: {line}{flag}")
            shown.add(i)


def main():
    parser = argparse.ArgumentParser(
        description="Inspect ID-generation logic without running the pipeline."
    )
    parser.add_argument(
        "--source",
        default="..",
        help="Path to src1, relative to the current directory (default: ..)",
    )
    parser.add_argument(
        "--context",
        type=int,
        default=3,
        help="Number of surrounding lines to show (default: 3)",
    )
    args = parser.parse_args()

    source_root = Path(args.source).resolve()

    if not source_root.is_dir():
        raise SystemExit(f"Source directory not found: {source_root}")

    if args.context < 0:
        raise SystemExit("--context must be non-negative")

    print(f"Source root: {source_root}")
    print("Mode: read-only static source inspection")
    print("No pipeline functions are executed.")
    print("No JSON files or BigQuery tables are modified.")

    files = list(find_python_files(source_root))
    print(f"Python files found: {len(files)}")

    if not files:
        raise SystemExit("No Python files found.")

    for path in sorted(files):
        inspect_file(path, source_root, args.context)

    print("\n" + "=" * 90)
    print("AUDIT COMPLETE")
    print("=" * 90)
    print(
        "Review the actual assignment expressions and helper implementations. "
        "A matching line alone does not prove that an ID is generated there."
    )


if __name__ == "__main__":
    main()
