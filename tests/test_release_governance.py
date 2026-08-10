from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.release_governance import load_release_ownership, validate_release_ownership


def disabled_evidence() -> dict[str, object]:
    return {
        "source": "github_branch_summary",
        "repository": "PetrFedin/trade_bot",
        "branch": "main",
        "protected": False,
        "protection_enabled": False,
        "required_status_checks_enforcement": "off",
    }


def enabled_evidence() -> dict[str, object]:
    return {
        "source": "github_branch_summary",
        "repository": "PetrFedin/trade_bot",
        "branch": "main",
        "protected": True,
        "protection_enabled": True,
        "required_status_checks_enforcement": "everyone",
    }


def baseline() -> dict[str, object]:
    return {
        "schema_version": 1,
        "artifact_release_owner": "@PetrFedin",
        "rollback_owner": "@PetrFedin",
        "independent_live_approver": None,
        "branch_protection_verification": "VERIFIED_DISABLED",
        "branch_protection_evidence": disabled_evidence(),
        "artifact_release_allowed": True,
        "live_release_allowed": False,
    }


def test_artifact_release_ownership_is_valid_with_verified_disabled_protection() -> None:
    validate_release_ownership(baseline())


def test_live_release_rejects_verified_disabled_branch_protection() -> None:
    data = baseline()
    data["live_release_allowed"] = True
    data["independent_live_approver"] = "@IndependentApprover"
    with pytest.raises(ValueError, match="verified branch protection"):
        validate_release_ownership(data)


def test_verified_disabled_evidence_cannot_claim_protection() -> None:
    data = baseline()
    evidence = disabled_evidence()
    evidence["protected"] = True
    data["branch_protection_evidence"] = evidence
    with pytest.raises(ValueError, match="must prove protection is disabled"):
        validate_release_ownership(data)


def test_live_release_requires_distinct_independent_approver() -> None:
    data = baseline()
    data["live_release_allowed"] = True
    data["branch_protection_verification"] = "VERIFIED_ENABLED"
    data["branch_protection_evidence"] = enabled_evidence()
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
