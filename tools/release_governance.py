from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

_ALLOWED_BRANCH_PROTECTION_STATES = {
    "UNVERIFIED_INTEGRATION_FORBIDDEN",
    "VERIFIED_DISABLED",
    "VERIFIED_ENABLED",
}
_ALLOWED_STATUS_ENFORCEMENT = {"off", "non_admins", "everyone"}


def _owner(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("@") or len(value) < 2:
        raise ValueError(f"{field} must be a GitHub handle beginning with @")
    return value


def _required_text(data: dict[str, Any], field: str) -> str:
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value.strip()


def _validate_branch_protection_evidence(
    data: dict[str, Any], *, branch_state: str
) -> dict[str, Any] | None:
    evidence = data.get("branch_protection_evidence")
    if branch_state == "UNVERIFIED_INTEGRATION_FORBIDDEN":
        if evidence is not None:
            raise ValueError("unverified branch protection must not carry verified evidence")
        return None
    if not isinstance(evidence, dict):
        raise ValueError("verified branch protection requires branch_protection_evidence")

    if _required_text(evidence, "source") != "github_branch_summary":
        raise ValueError("branch protection evidence source must be github_branch_summary")
    _required_text(evidence, "repository")
    if _required_text(evidence, "branch") != "main":
        raise ValueError("branch protection evidence must describe main")

    protected = evidence.get("protected")
    enabled = evidence.get("protection_enabled")
    enforcement = evidence.get("required_status_checks_enforcement")
    if not isinstance(protected, bool) or not isinstance(enabled, bool):
        raise ValueError("branch protection evidence booleans are required")
    if enforcement not in _ALLOWED_STATUS_ENFORCEMENT:
        raise ValueError("required status-check enforcement is invalid")

    if branch_state == "VERIFIED_DISABLED":
        if protected or enabled or enforcement != "off":
            raise ValueError("VERIFIED_DISABLED evidence must prove protection is disabled")
    if branch_state == "VERIFIED_ENABLED" and (not protected or not enabled):
        raise ValueError("VERIFIED_ENABLED evidence must prove protection is enabled")
    return evidence


def validate_release_ownership(data: dict[str, Any]) -> None:
    if data.get("schema_version") != 1:
        raise ValueError("schema_version must be 1")

    release_owner = _owner(data.get("artifact_release_owner"), field="artifact_release_owner")
    rollback_owner = _owner(data.get("rollback_owner"), field="rollback_owner")

    branch_state = data.get("branch_protection_verification")
    if branch_state not in _ALLOWED_BRANCH_PROTECTION_STATES:
        raise ValueError("branch_protection_verification is invalid")
    _validate_branch_protection_evidence(data, branch_state=branch_state)

    artifact_release_allowed = data.get("artifact_release_allowed")
    live_release_allowed = data.get("live_release_allowed")
    if not isinstance(artifact_release_allowed, bool):
        raise ValueError("artifact_release_allowed must be boolean")
    if not isinstance(live_release_allowed, bool):
        raise ValueError("live_release_allowed must be boolean")

    independent = data.get("independent_live_approver")
    if independent is not None:
        independent = _owner(independent, field="independent_live_approver")

    if artifact_release_allowed and (not release_owner or not rollback_owner):
        raise ValueError("artifact release requires release and rollback owners")

    if live_release_allowed:
        if branch_state != "VERIFIED_ENABLED":
            raise ValueError("live release requires verified branch protection")
        if independent is None:
            raise ValueError("live release requires an independent live approver")
        if independent in {release_owner, rollback_owner}:
            raise ValueError(
                "independent live approver must be distinct from release/rollback owner"
            )


def load_release_ownership(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("release ownership document must be a JSON object")
    validate_release_ownership(data)
    return data


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate ASTRA release ownership governance")
    parser.add_argument("path", type=Path, nargs="?", default=Path("release/ownership.json"))
    parser.add_argument("--require-artifact-release-ready", action="store_true")
    parser.add_argument("--require-live-release-ready", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    data = load_release_ownership(args.path)
    if args.require_artifact_release_ready and not data["artifact_release_allowed"]:
        raise SystemExit("artifact release is not allowed by release ownership governance")
    if args.require_live_release_ready and not data["live_release_allowed"]:
        raise SystemExit("live release is not allowed by release ownership governance")
    print(json.dumps(data, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
