from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.release_governance import load_release_ownership, validate_release_ownership


def baseline() -> dict[str, object]:
    return {
        "schema_version": 1,
        "artifact_release_owner": "@PetrFedin",
        "rollback_owner": "@PetrFedin",
        "independent_live_approver": None,
        "branch_protection_verification": "UNVERIFIED_INTEGRATION_FORBIDDEN",
        "artifact_release_allowed": True,
        "live_release_allowed": False,
    }


def test_artifact_release_ownership_is_valid_without_live_approval() -> None:
    validate_release_ownership(baseline())


def test_live_release_requires_verified_branch_protection() -> None:
    data = baseline()
    data["live_release_allowed"] = True
    data["independent_live_approver"] = "@IndependentApprover"
    with pytest.raises(ValueError, match="verified branch protection"):
        validate_release_ownership(data)


def test_live_release_requires_distinct_independent_approver() -> None:
    data = baseline()
    data["live_release_allowed"] = True
    data["branch_protection_verification"] = "VERIFIED_ENABLED"
    data["independent_live_approver"] = "@PetrFedin"
    with pytest.raises(ValueError, match="must be distinct"):
        validate_release_ownership(data)


def test_load_rejects_invalid_release_owner(tmp_path: Path) -> None:
    data = baseline()
    data["artifact_release_owner"] = "PetrFedin"
    path = tmp_path / "ownership.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="beginning with @"):
        load_release_ownership(path)
