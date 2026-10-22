"""Evidence lineage and release-impact analysis for ASTRA research operations.

Cross-check the frozen strategy pointer against CURRENT_SYSTEM_STATUS and the
capability registry. This is agreement between two carried-forward records,
NOT proof of exchange fills or an independent strategy backtest.

No network, credentials, order execution, runtime authority or real positions.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

from tools.research_analytics import (
    COUNT_KEYS,
    ResearchAnalyticsError,
    calculate_research_overview,
)
from tools.research_capability_register import validate_register

ROOT = Path(__file__).resolve().parents[1]
STATUS_PATH = ROOT / "CURRENT_SYSTEM_STATUS.json"
REGISTER_PATH = ROOT / "research/competitive_capability_register_v1.json"
EVIDENCE_PATH = ROOT / "qualification/strategy/bybit_price_only_frozen_negative.json"
DIGEST_PREFIX = b"ASTRA_RESEARCH_EVIDENCE_LINEAGE_V1\n"
POINTER_FIELDS = frozenset({
    "schema_version", "strategy_status", "promotion_allowed", "evidence_kind",
    "source_repository_sha", "source_paths", "reference_equity_usdt",
    "eligible_signals", "plan_eligible_signals",
    "independent_target_stop_episodes", "target_first", "stop_first", "neither",
    "portfolio_trades", "wins", "breakeven", "losses", "net_pnl_usdt",
    "net_return_fraction_approx", "interpretation",
})
CARRIED_VALUES = (
    "reference_equity_usdt", "net_pnl_usdt", "net_return_fraction_approx",
    *COUNT_KEYS,
)
HEX_SHA = re.compile(r"[0-9a-f]{40}\Z")
PROVENANCE_SCOPE = "FROZEN_CARRIED_FORWARD_POINTER_NOT_INDEPENDENT_VENUE_PROOF"


class EvidenceLineageError(ValueError):
    """The source, provenance graph or authority chain is not trustworthy."""


def _pairs(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise EvidenceLineageError("duplicate evidence JSON field")
        result[key] = value
    return result


def _read_strict(path: Path, root: Path) -> tuple[dict, bytes]:
    path = Path(path)
    root = Path(root)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise EvidenceLineageError("evidence source outside repository") from exc
    if relative.is_absolute() or ".." in relative.parts or any(
        part.is_symlink() for part in (path, *path.parents) if part != root
    ):
        raise EvidenceLineageError("untrusted evidence source path")
    if not path.is_file() or path.stat().st_size > 256_000:
        raise EvidenceLineageError("missing or oversized evidence source")
    content = path.read_bytes()
    try:
        data = json.loads(
            content, object_pairs_hook=_pairs,
            parse_constant=lambda _v: (_ for _ in ()).throw(
                EvidenceLineageError("non-finite evidence JSON number")
            ),
        )
    except EvidenceLineageError:
        raise
    except (ValueError, UnicodeError, TypeError) as exc:
        raise EvidenceLineageError("invalid evidence JSON source") from exc
    if not isinstance(data, dict):
        raise EvidenceLineageError("evidence source must be JSON object")
    return data, content


def verify_frozen_pointer(status: dict, pointer: dict) -> dict:
    """Verify a *shared* prior-result pointer without claiming independence."""
    if not isinstance(status, dict) or not isinstance(pointer, dict):
        raise EvidenceLineageError("status/pointer must be structured records")
    replay = status.get("strategy", {}).get("latest_frozen_bybit_price_only_replay")
    if not isinstance(replay, dict) or set(pointer) != POINTER_FIELDS:
        raise EvidenceLineageError("unrecognized frozen strategy pointer schema")
    source_sha = pointer.get("source_repository_sha")
    if (
        pointer.get("schema_version") != "strategy-evidence-pointer-v1"
        or pointer.get("strategy_status") != "PROFITABILITY_NOT_PROVEN"
        or pointer.get("promotion_allowed") is not False
        or pointer.get("evidence_kind") != "CARRIED_FORWARD_FROZEN_NEGATIVE_RESULT"
        or not isinstance(source_sha, str)
        or HEX_SHA.fullmatch(source_sha) is None
        or pointer.get("source_paths") != ["README.md", "CURRENT_SYSTEM_STATUS.json"]
        or not isinstance(pointer.get("interpretation"), str)
        or len(pointer["interpretation"]) < 50
    ):
        raise EvidenceLineageError("untrusted frozen strategy pointer authority")
    changed = []
    for field in CARRIED_VALUES:
        a = replay.get(field)
        b = pointer.get(field)
        if type(a) is not type(b) or a != b:
            changed.append(field)
    if changed:
        raise EvidenceLineageError(
            "frozen strategy pointer disagrees with system status: "
            + ", ".join(sorted(changed))
        )
    if (
        status["strategy"].get("status") != pointer["strategy_status"]
        or status["strategy"].get("promotion_allowed") is not False
    ):
        raise EvidenceLineageError("pointer/status release authority disagreement")
    return {
        "source_repository_sha": source_sha,
        "compared_fields": len(CARRIED_VALUES),
        "value_agreement": True,
        "independent_market_evidence": False,
        "source_class": PROVENANCE_SCOPE,
    }


def dependency_impact(register: dict, *, root: Path) -> dict[str, Any]:
    """Trace exactly which planned capabilities depend on missing upstream evidence."""
    validate_register(register, root)
    entries = {row["id"]: row for row in register["capabilities"]}
    by_id = {name: tuple(row["depends_on"]) for name, row in entries.items()}
    blocked = frozenset(
        name for name, row in entries.items() if row["state"] == "BLOCKED_SOURCE"
    )
    cache: dict[str, frozenset[str]] = {}

    def ancestors(name: str) -> frozenset[str]:
        if name in cache:
            return cache[name]
        result = set(by_id[name])
        for parent in by_id[name]:
            result.update(ancestors(parent))
        cache[name] = frozenset(result)
        return cache[name]

    affected = []
    for name in sorted(entries):
        source_blockers = sorted(blocked & ancestors(name))
        if source_blockers:
            affected.append({
                "capability": name,
                "priority": entries[name]["priority"],
                "source_blockers": source_blockers,
                "distance": _shortest_blocked_dependency_distance(name, by_id, blocked),
            })
    blocked_rows = [
        {"capability": name, "priority": entries[name]["priority"]}
        for name in sorted(blocked)
    ]
    return {
        "blocked_source_roots": blocked_rows,
        "downstream_blocked_capabilities": affected,
        "downstream_impact_count": len(affected),
        "feature_count": len(entries),
        "claim_of_commercial_readiness": False,
    }


def _shortest_blocked_dependency_distance(
    start: str,
    dependencies: dict[str, tuple[str, ...]],
    blocked: frozenset[str],
) -> int:
    layer = {start}
    visited = {start}
    for depth in range(1, len(dependencies) + 1):
        next_layer = {
            dependency
            for node in layer for dependency in dependencies[node]
            if dependency not in visited
        }
        if next_layer & blocked:
            return depth
        visited.update(next_layer)
        layer = next_layer
        if not layer:
            break
    raise EvidenceLineageError("blocked source unreachable from feature ancestry")


def create_dossier(
    status: dict,
    register: dict,
    pointer: dict,
    *,
    root: Path,
    source_digests: dict[str, str],
) -> dict:
    try:
        overview = calculate_research_overview(status, register, evidence_root=root)
    except (ResearchAnalyticsError, ValueError) as exc:
        raise EvidenceLineageError("research status or register validation failed") from exc
    source = status.get("strategy", {}).get("evidence_source")
    if source != "qualification/strategy/bybit_price_only_frozen_negative.json":
        raise EvidenceLineageError("strategy evidence path unexpectedly changed")
    for key in ("status", "capabilities", "strategy_pointer"):
        value = source_digests.get(key)
        if (
            not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None
        ):
            raise EvidenceLineageError("required source digest missing")
    pointer_match = verify_frozen_pointer(status, pointer)
    graph = dependency_impact(register, root=root)
    strategy = status["strategy"]
    trading = status["trading_authority"]
    if (
        any(trading.get(name) is not False for name in (
            "external_order_routing_allowed", "demo_trading_allowed",
            "live_trading_allowed", "mainnet_entry_allowed",
            "production_release_allowed",
        ))
        or strategy.get("promotion_allowed") is not False
    ):
        raise EvidenceLineageError("trading authority must remain closed")
    checks = [
        {
            "id": "STATUS_SCHEMA_AND_REPLAY_CONSERVATION",
            "result": "PASS_REPOSITORY_SNAPSHOT_ONLY",
            "evidence": "CURRENT_SYSTEM_STATUS.json",
        },
        {
            "id": "CARRIED_POINTER_FIELD_AGREEMENT",
            "result": "PASS_SHARED_SOURCE_AGREEMENT_ONLY",
            "evidence": source,
        },
        {
            "id": "CAPABILITY_GRAPH_AND_PATH_INTEGRITY",
            "result": "PASS_REPOSITORY_SNAPSHOT_ONLY",
            "evidence": "research/competitive_capability_register_v1.json",
        },
        {
            "id": "TRADING_AUTHORITY",
            "result": "DENIED_BY_SOURCE",
            "evidence": "CURRENT_SYSTEM_STATUS.json",
        },
        {
            "id": "MARK_PRICE_PROVIDER_FACT",
            "result": "UNVERIFIED_EXTERNAL_SOURCE",
            "evidence": "MARK_PRICE_FUNDING_PROVENANCE",
        },
    ]
    payload = {
        "schema_version": "astra-research-evidence-dossier-v1",
        "scope": "OFFLINE_REPOSITORY_SNAPSHOT_NOT_RUNTIME_OR_PROVIDER_PROOF",
        "decision": "RESEARCH_HOLD",
        "status_snapshot_sha": overview["main_qualified_sha"],
        "source_sha256": dict(sorted(source_digests.items())),
        "frozen_pointer": pointer_match,
        "capability_impact": graph,
        "checks": checks,
        "portfolio": {
            "reference_equity_usdt": overview["reference_equity_usdt"],
            "net_pnl_usdt": overview["net_pnl_usdt"],
            "net_return_pct": overview["net_return_pct"],
            "portfolio_trades": overview["counts"]["portfolio_trades"],
            "win_rate_pct": overview["win_rate_pct"],
            "stop_first_pct": overview["stop_first_pct"],
            "source_type": overview["source_status"],
        },
        "unproven": [
            "ACTUAL_EXCHANGE_MARK_PRICE",
            "BROKER_EXECUTIONS",
            "TRUE_TRADE_LEVEL_DRAWDOWN",
            "INDEPENDENT_POSITIVE_STRATEGY_EDGE",
        ],
        "trading_authority_allowed": False,
        "profitability_proven": False,
    }
    canonical = json.dumps(
        payload, sort_keys=True, ensure_ascii=True, separators=(",", ":"),
    ).encode("utf-8")
    payload["dossier_sha256"] = hashlib.sha256(DIGEST_PREFIX + canonical).hexdigest()
    return payload


def build(root: Path = ROOT) -> dict:
    root = Path(root)
    paths = {
        "status": root / "CURRENT_SYSTEM_STATUS.json",
        "capabilities": root / "research/competitive_capability_register_v1.json",
        "strategy_pointer": root / "qualification/strategy/bybit_price_only_frozen_negative.json",
    }
    results = {name: _read_strict(path, root) for name, path in paths.items()}
    return create_dossier(
        results["status"][0], results["capabilities"][0], results["strategy_pointer"][0],
        root=root,
        source_digests={
            name: hashlib.sha256(result[1]).hexdigest() for name, result in results.items()
        },
    )


def _serialized(dossier: dict) -> str:
    return json.dumps(dossier, sort_keys=True, ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit", type=Path, required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    data = _serialized(build())
    args.emit.parent.mkdir(parents=True, exist_ok=True)
    if args.check:
        if not args.emit.is_file() or args.emit.read_text(encoding="utf-8") != data:
            print("ASTRA_EVIDENCE_DOSSIER_DRIFT", file=sys.stderr)
            return 1
    else:
        args.emit.write_text(data, encoding="utf-8")
    print("ASTRA_EVIDENCE_DOSSIER_HOLD", build()["dossier_sha256"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
