import json
from pathlib import Path
from copy import deepcopy

from embedding_generator import normalize_chunk_metadata
def test_missing_metadata():
    chunk = {"chunk_id": "chunk_1"}

    normalize_chunk_metadata(chunk)

    for field in (
        "related_chunk_ids",
        "related_links",
        "asset_uris",
        "relationship_candidates",
        "associated_images",
        "relationships",
    ):
        assert chunk[field] == [], f"{field} should default to []"


def test_none_metadata():
    chunk = {
        "chunk_id": "chunk_2",
        "metadata": None,
    }

    normalize_chunk_metadata(chunk)

    assert chunk["related_chunk_ids"] == []
    assert chunk["relationships"] == []


def test_none_and_empty_nested_values():
    chunk = {
        "chunk_id": "chunk_3",
        "metadata": {
            "related_chunk_ids": None,
            "related_links": [],
            "asset_uris": None,
        },
    }

    normalize_chunk_metadata(chunk)

    assert chunk["related_chunk_ids"] == []
    assert chunk["related_links"] == []
    assert chunk["asset_uris"] == []


def test_duplicate_values_are_removed():
    chunk = {
        "chunk_id": "chunk_4",
        "related_chunk_ids": ["chunk_A", "chunk_A"],
        "metadata": {
            "related_chunk_ids": ["chunk_A", "chunk_B"],
        },
    }

    normalize_chunk_metadata(chunk)

    assert chunk["related_chunk_ids"] == [
        "chunk_A",
        "chunk_B",
    ]


def test_relationship_status_is_preserved():
    relationship = {
        "source_id": "chunk_A",
        "target_id": "chunk_B",
        "relation_type": "belongs_to_section",
        "status": "candidate",
    }

    chunk = {
        "chunk_id": "chunk_A",
        "metadata": {
            "relationships": [relationship],
        },
    }

    normalize_chunk_metadata(chunk)

    assert chunk["relationships"][0]["status"] == "candidate"
    assert chunk["relationships"][0]["target_id"] == "chunk_B"


def test_image_path_is_not_promoted_to_uri():
    chunk = {
        "chunk_id": "chunk_5",
        "metadata": {
            "associated_images": [
                "assets/images/image_1.png",
            ],
        },
    }

    normalize_chunk_metadata(chunk)

    assert chunk["associated_images"] == [
        "assets/images/image_1.png",
    ]
    assert chunk["asset_uris"] == []


def test_normalization_is_idempotent():
    chunk = {
        "chunk_id": "chunk_6",
        "related_chunk_ids": ["chunk_A"],
        "metadata": {
            "related_chunk_ids": ["chunk_A", "chunk_B"],
        },
    }

    normalize_chunk_metadata(chunk)
    first_result = deepcopy(chunk)

    normalize_chunk_metadata(chunk)

    assert chunk == first_result


def test_invalid_metadata_type():
    chunk = {
        "chunk_id": "chunk_7",
        "metadata": "unexpected string",
    }

    try:
        normalize_chunk_metadata(chunk)
    except (AttributeError, TypeError):
        print("PASS: invalid metadata rejected")
    else:
        raise AssertionError(
            "Expected invalid metadata to raise an error"
        )


if __name__ == "__main__":
    tests = [
        test_missing_metadata,
        test_none_metadata,
        test_none_and_empty_nested_values,
        test_duplicate_values_are_removed,
        test_relationship_status_is_preserved,
        test_image_path_is_not_promoted_to_uri,
        test_normalization_is_idempotent,
        test_invalid_metadata_type,

    ]

    passed = 0
    failed = 0

    for test in tests:
        try:
            test()
            print(f"PASS: {test.__name__}")
            passed += 1
        except Exception as error:
            print(f"FAIL: {test.__name__}: {error}")
            failed += 1

    print(f"\nResults: {passed} passed, {failed} failed")

    if failed:
        raise SystemExit(1)

