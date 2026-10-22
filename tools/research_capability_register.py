"""Validate ASTRA's fail-closed, evidence-backed competitor capability register.

Path existence is an inventory check, never qualification of readiness,
strategy profitability, or authority to execute orders.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "research" / "competitive_capability_register_v1.json"
SCHEMA = "astra-competitive-capability-register-v1"
STATES = frozenset({
    "PLANNED", "EXISTING_RESEARCH_CODE", "IMPLEMENTED_PENDING_QUALIFICATION",
    "BLOCKED_SOURCE",
})
PRIORITIES = frozenset({"P0", "P1", "P2"})
AUTHORITY_FIELDS = frozenset({
    "broker_routing_allowed", "demo_trading_allowed", "live_trading_allowed",
    "strategy_promotion_allowed", "profitability_proven",
})
HOSTS = frozenset({
    "help.3commas.io", "help.veles.finance", "docs.freqtrade.io",
    "hummingbot.org",
})
ID_RE = re.compile(r"[A-Z][A-Z0-9_]{5,64}\Z")
ENTRY_FIELDS = frozenset({
    "id", "priority", "state", "code_paths", "official_references",
    "acceptance_gate", "depends_on", "trading_authority_allowed",
})
TOP_FIELDS = frozenset({
    "schema_version", "project", "scope", "authority_boundary",
    "verification_note", "capabilities",
})


class CapabilityRegisterError(ValueError):
    """Reject unverified claims, tampered structure or broken dependencies."""


def _json_unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise CapabilityRegisterError("duplicate capability JSON key")
        result[key] = value
    return result


def _reference(root: Path, name: str) -> Path:
    if (
        not isinstance(name, str)
        or not name
        or "\\" in name
        or name.startswith("/")
        or any(part in ("", ".", "..") for part in name.split("/"))
    ):
        raise CapabilityRegisterError("unsafe code evidence path")
    resolved = root / name
    if any(part.is_symlink() for part in (resolved, *resolved.parents)):
        raise CapabilityRegisterError("symlinked capability evidence path")
    if not resolved.is_file():
        raise CapabilityRegisterError(f"missing capability evidence path: {name}")
    return resolved


def validate_register(payload: dict, root: Path) -> dict:
    if not isinstance(payload, dict) or set(payload) != TOP_FIELDS:
        raise CapabilityRegisterError("unexpected capability register envelope")
    if (
        payload["schema_version"] != SCHEMA
        or payload["project"] != "ASTRA"
        or payload["scope"] != "RESEARCH_PLANNING_AND_ENGINEERING_ONLY"
        or not isinstance(payload["verification_note"], str)
        or len(payload["verification_note"]) < 30
    ):
        raise CapabilityRegisterError("capability registry authority mismatch")
    authority = payload["authority_boundary"]
    if (
        not isinstance(authority, dict)
        or set(authority) != AUTHORITY_FIELDS
        or any(value is not False for value in authority.values())
    ):
        raise CapabilityRegisterError("capability register cannot authorize trading")
    capabilities = payload["capabilities"]
    if not isinstance(capabilities, list) or len(capabilities) < 8:
        raise CapabilityRegisterError("insufficient capability inventory")
    found: dict[str, tuple[str, ...]] = {}
    statuses = Counter()
    for entry in capabilities:
        if not isinstance(entry, dict) or set(entry) != ENTRY_FIELDS:
            raise CapabilityRegisterError("invalid capability entry fields")
        identifier = entry["id"]
        if (
            not isinstance(identifier, str)
            or ID_RE.fullmatch(identifier) is None
            or identifier in found
        ):
            raise CapabilityRegisterError("duplicate or malformed capability identifier")
        priority = entry["priority"]
        state = entry["state"]
        if priority not in PRIORITIES or state not in STATES:
            raise CapabilityRegisterError("unknown priority or unsupported readiness claim")
        if entry["trading_authority_allowed"] is not False:
            raise CapabilityRegisterError("feature cannot grant trading authority")
        if not isinstance(entry["acceptance_gate"], str) or len(entry["acceptance_gate"]) < 35:
            raise CapabilityRegisterError("missing practical feature acceptance criteria")

        paths = entry["code_paths"]
        references = entry["official_references"]
        dependencies = entry["depends_on"]
        if (
            not isinstance(paths, list)
            or not isinstance(references, list)
            or not isinstance(dependencies, list)
            or len(paths) != len(set(paths))
            or len(references) != len(set(references))
            or len(dependencies) != len(set(dependencies))
        ):
            raise CapabilityRegisterError("invalid capability paths or dependencies")
        if state == "PLANNED" and paths:
            raise CapabilityRegisterError("planned capability must not claim code evidence")
        if state != "PLANNED" and not paths:
            raise CapabilityRegisterError("existing capability requires inspected code paths")
        for path in paths:
            _reference(root, path)
        for url in references:
            if not isinstance(url, str) or len(url) > 300:
                raise CapabilityRegisterError("invalid external capability reference")
            parsed = urlparse(url)
            if (
                parsed.scheme != "https"
                or parsed.netloc not in HOSTS
                or not parsed.path.startswith("/")
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise CapabilityRegisterError("unapproved capability reference domain")
        if any(not isinstance(item, str) or ID_RE.fullmatch(item) is None for item in dependencies):
            raise CapabilityRegisterError("invalid feature dependency identifier")
        found[identifier] = tuple(dependencies)
        statuses[state] += 1
    if list(found) != sorted(found):
        raise CapabilityRegisterError("capability register must be deterministically sorted")
    for feature, dependencies in found.items():
        if feature in dependencies or any(dep not in found for dep in dependencies):
            raise CapabilityRegisterError("self-reference or missing feature dependency")

    visiting = set()
    complete = set()

    def visit(feature: str) -> None:
        if feature in visiting:
            raise CapabilityRegisterError("capability dependency cycle")
        if feature in complete:
            return
        visiting.add(feature)
        for dependency in found[feature]:
            visit(dependency)
        visiting.remove(feature)
        complete.add(feature)

    for feature in found:
        visit(feature)
    return {
        "schema_version": "astra-capability-register-validation-v1",
        "valid": True,
        "feature_count": len(found),
        "counts_by_state": dict(sorted(statuses.items())),
        "all_authorities_closed": True,
        "real_trading_ready": False,
        "commercial_feature_parity_proven": False,
    }


def read_validate_register(path: Path = REGISTRY, root: Path = ROOT) -> dict:
    path = Path(path)
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 256_000:
        raise CapabilityRegisterError("missing or untrusted capability manifest")
    try:
        payload = json.loads(path.read_bytes(), object_pairs_hook=_json_unique)
    except (UnicodeError, ValueError) as exc:
        raise CapabilityRegisterError("invalid capability register JSON") from exc
    return validate_register(payload, root)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", required=True)
    args = parser.parse_args()
    assert args.check
    print("ASTRA_CAPABILITY_REGISTER", json.dumps(read_validate_register(), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
