import json
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_upstream_pins_are_full_sha_and_provenance_only() -> None:
    payload = json.loads((ROOT / "docs/model_upstream_pins.json").read_text(encoding="utf-8"))
    assert payload["pin_policy"] == {
        "reference_type": "full_commit_sha",
        "floating_refs_allowed": False,
        "vendored": False,
        "runtime_dependency": False,
        "training_authorized": False,
    }
    pins = payload["pins"]
    assert {item["name"] for item in pins} == {
        "Time-Series-Library",
        "TLOB",
        "Qlib",
        "EarnHFT",
    }
    for item in pins:
        assert len(item["commit_sha"]) == 40
        assert item["commit_sha"].isalnum()
        assert item["runtime_dependency"] is False
        assert "blob" not in item["repository"]


def test_license_boundary_is_explicit() -> None:
    payload = json.loads((ROOT / "docs/model_upstream_pins.json").read_text(encoding="utf-8"))
    by_name = {item["name"]: item for item in payload["pins"]}
    assert by_name["Time-Series-Library"]["license"] == "MIT"
    assert by_name["TLOB"]["license"] == "MIT"
    assert by_name["Qlib"]["license"] == "MIT"
    assert by_name["EarnHFT"]["license"] is None
    assert by_name["EarnHFT"]["license_review_status"].startswith("REVIEW_REQUIRED")


def test_sequence_manifest_isolated_from_dataset_v2_holdout() -> None:
    payload = json.loads((ROOT / "docs/sequence_readiness.json").read_text(encoding="utf-8"))
    assert payload["dataset_id"] == "historical-research-f2d529adfb95080971becdaf"
    assert payload["dataset_v2_touched"] is False
    assert payload["holdout_accessed"] is False
    assert payload["model_training"] is False
    assert payload["fold_plan"]["purge_embargo_seconds"] >= 600
