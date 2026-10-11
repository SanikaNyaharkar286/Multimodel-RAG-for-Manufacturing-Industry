
import json
import sys
from pathlib import Path
from chunk_builder import build_all_chunks, save_chunks
from pdf_extractor import extract_pdf
from relationship_builder import build_relationships


# Project folders
PROJECT_ROOT = Path(__file__).resolve().parents[2]
RAW_DATA_DIR = PROJECT_ROOT / "data" / "raw"
PROCESSED_DATA_DIR = PROJECT_ROOT / "data" / "processed"

def process_all_chunks():
    """Generate chunks from all existing extraction JSON files."""
    extraction_files = PROCESSED_DATA_DIR.rglob("extraction.json")

    success_count = 0
    failure_count = 0

    for extraction_path in extraction_files:
        try:
            with extraction_path.open("r", encoding="utf-8") as file:
                extracted_data = json.load(file)

            machine_name = extraction_path.parent.parent.name
            document_name = extracted_data["document"]

            chunks = build_all_chunks(
                extracted_data,
                machine_name,
                document_name,
            )

            output_path = extraction_path.parent / "chunks.json"
            save_chunks(chunks, output_path)

            success_count += 1

        except Exception as error:
            failure_count += 1
            print(f"Failed: {extraction_path}")
            print(f"Reason: {error}")

    print("\nChunk generation completed.")
    print(f"Successful documents: {success_count}")
    print(f"Failed documents: {failure_count}")


def process_pdf(pdf_path, machine_name):
    """Extract one PDF and save its data under the machine folder."""

    pdf_path = Path(pdf_path)

    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    if pdf_path.suffix.lower() != ".pdf":
        raise ValueError("Input file must be a PDF.")

    if not machine_name or Path(machine_name).name != machine_name:
        raise ValueError("Provide a valid machine folder name.")

    # Create the output folder for this machine.
    # Create one output folder per source PDF.
    # Create a separate output folder for this PDF.
    file_stem = pdf_path.stem

    document_output_dir = PROCESSED_DATA_DIR / machine_name / file_stem
    assets_dir = document_output_dir / "assets"
    images_dir = assets_dir / "images"

    images_dir.mkdir(parents=True, exist_ok=True)

    # Keep the original PDF filename in the output names.
    file_stem = pdf_path.stem

    print(f"\nProcessing: {pdf_path.name}")
    print(f"Machine: {machine_name}")

    # Step 1: Extract PDF content and save its assets.
    extracted_data = extract_pdf(pdf_path, assets_dir)

    if not isinstance(extracted_data, dict):
        raise TypeError(
            "extract_pdf() must return a dictionary containing "
            "the extracted PDF data."
        )

    # Step 2: Save the extracted data.
    extraction_path = (
        document_output_dir / "extraction.json"
    )

    extraction_path.write_text(
        json.dumps(
            extracted_data,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print(f"Extraction JSON: {extraction_path}")

    # Step 3: Build relationships between images, tables and links.
    relationship_data = build_relationships(extracted_data)

    relationship_path = (
        document_output_dir / "relationships.json"
    )

    relationship_path.write_text(
        json.dumps(
            relationship_data,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    print(f"Relationship JSON: {relationship_path}")

    # Step 4: Print a summary.
    total_pages = len(extracted_data.get("pages", []))

    total_paragraphs = sum(
        len(page.get("paragraphs", []))
        for page in extracted_data.get("pages", [])
    )

    total_tables = sum(
        len(page.get("tables", []))
        for page in extracted_data.get("pages", [])
    )

    total_images = sum(
        len(page.get("images", []))
        for page in extracted_data.get("pages", [])
    )

    total_links = sum(
        len(page.get("links", []))
        for page in extracted_data.get("pages", [])
    )

    print("\n--- Processing Summary ---")
    print(f"Pages: {total_pages}")
    print(f"Paragraphs: {total_paragraphs}")
    print(f"Tables: {total_tables}")
    print(f"Images: {total_images}")
    print(f"Links: {total_links}")
    print(f"Image assets folder: {images_dir}")
    print("Processing completed.")



def process_all_pdfs():
    """Process every PDF under data/raw, preserving machine folders."""

    pdf_files = sorted(RAW_DATA_DIR.rglob("*.pdf"))

    if not pdf_files:
        print(f"No PDFs found under: {RAW_DATA_DIR}")
        return

    print(f"Found {len(pdf_files)} PDF(s).")

    successful = 0
    failed = []

    for pdf_path in pdf_files:
        relative_path = pdf_path.relative_to(RAW_DATA_DIR)

        # The first folder under data/raw is the machine name.
        if len(relative_path.parts) < 2:
            print(f"Skipping PDF outside a machine folder: {pdf_path}")
            failed.append((pdf_path, "No machine folder found"))
            continue

        machine_name = relative_path.parts[0]

        try:
            process_pdf(pdf_path, machine_name)
            successful += 1
        except Exception as error:
            print(f"\nFAILED: {pdf_path}")
            print(f"Reason: {error}")
            failed.append((pdf_path, str(error)))

    print("\n========== BATCH SUMMARY ==========")
    print(f"Total PDFs found: {len(pdf_files)}")
    print(f"Successful: {successful}")
    print(f"Failed: {len(failed)}")

    if failed:
        print("\nFailed files:")
        for pdf_path, reason in failed:
            print(f"- {pdf_path}: {reason}")


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "--all":
        process_all_pdfs()
        return

    if len(sys.argv) != 3:
        print(
            'Usage:\n'
            '  python main.py --all\n'
            '  python main.py "path/to/file.pdf" "MACHINE_NAME"'
        )
        sys.exit(1)

    pdf_path = Path(sys.argv[1])
    machine_name = sys.argv[2]

    try:
        process_pdf(pdf_path, machine_name)
    except Exception as error:
        print(f"\nProcessing failed: {error}")
        sys.exit(1)


if __name__ == "__main__":
    main()
