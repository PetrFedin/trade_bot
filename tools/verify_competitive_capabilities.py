"""Validate source-attributed ASTRA capability inventory without trading permissions.

Source URLs are attributed to vendor-owned domains, not network-fetched in CI.
Existence of source code is inventory evidence, never functional parity,
strategy profitability, broker authorization or production readiness.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import date
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

SCHEMA = "astra-competitive-capability-ledger-v1"
DEFAULT_LEDGER = "research/competitive_capabilities_2026-10-20.json"
SCOPE = "RESEARCH_AND_ENGINEERING_INVENTORY_NOT_PRODUCT_LAUNCH"
COMPARISON_METHOD = "OFFICIAL_PRODUCT_PAGES_ONLY_NOT_HANDS_ON_COMPETITOR_TESTS"
OFFICIAL_HOSTS = {
    "3commas": {"help.3commas.io"},
    "cryptohopper": {"www.cryptohopper.com"},
    "bitsgap": {"bitsgap.com"},
    "wundertrading": {"wundertrading.com"},
    "veles": {"help.veles.finance"},
    "revenuebot": {"revenuebot.io"},
    "moonbot": {"moon-bot.com"},
    "tradesanta": {"faq.tradesanta.com"},
    "freqtrade": {"docs.freqtrade.io", "www.freqtrade.io"},
    "hummingbot": {"hummingbot.org"},
    "quantconnect": {"www.quantconnect.com"},
}
AUTHORITY_FIELDS = {
    "external_order_routing_allowed",
    "demo_trading_allowed",
    "live_trading_allowed",
    "mainnet_entry_allowed",
    "production_release_allowed",
    "strategy_promotion_allowed",
    "profitability_proven",
}
STATUS = {"NOT_IMPLEMENTED", "ENGINEERING_ONLY", "RESEARCH_ONLY", "BLOCKED_SOURCE"}
DECISION = {"BUILD", "DEEPEN", "HOLD", "RESEARCH"}
PRIORITY = {"P0", "P1", "P2", "P3"}
ROOTS = {"app", "tools", "research", "docs", "tests", "qualification"}
ID = re.compile(r"[a-z][a-z0-9_]{2,80}\Z")


class CompetitiveEvidenceError(ValueError):
    """A capability claim is unsupported, unsafe or structurally inconsistent."""


def _require(ok: bool, message: str) -> None:
    if not ok:
        raise CompetitiveEvidenceError(message)


def _string(value: object, name: str) -> str:
    _require(
        isinstance(value, str) and 0 < len(value) <= 2500
        and value.strip() == value and bool(value),
        f"{name}: expected nonempty trimmed text",
    )
    return value


def _list(value: object, name: str) -> list:
    _require(isinstance(value, list), f"{name}: expected list")
    return value


def _id(value: object, name: str) -> str:
    text = _string(value, name)
    _require(bool(ID.fullmatch(text)), f"{name}: invalid identifier")
    return text


def _unique_sorted(values: object, name: str) -> list[str]:
    collection = _list(values, name)
    _require(
        all(isinstance(x, str) for x in collection)
        and collection == sorted(set(collection)),
        f"{name}: values must be unique and sorted",
    )
    return collection


def _url(url: str, hosts: set[str], name: str) -> None:
    parsed = urlsplit(_string(url, name))
    _require(
        parsed.scheme == "https"
        and parsed.hostname in hosts
        and parsed.port is None
        and parsed.username is None
        and parsed.password is None
        and not parsed.query
        and not parsed.fragment
        and bool(parsed.path),
        f"{name}: unapproved or malformed official URL",
    )


def _evidence_path(root: Path, name: str) -> None:
    name = _string(name, "evidence_paths")
    rel = PurePosixPath(name)
    _require(
        not rel.is_absolute()
        and ".." not in rel.parts
        and "." not in rel.parts
        and bool(rel.parts)
        and rel.parts[0] in ROOTS
        and len(rel.parts) > 1,
        f"unsafe evidence path: {name}",
    )
    path = root.joinpath(*rel.parts)
    _require(
        path.is_file()
        and not any(p.is_symlink() for p in (path, *path.parents) if p != root.parent),
        f"missing or symlinked ASTRA evidence file: {name}",
    )


def validate_ledger(ledger: dict, root: Path) -> dict:
    """Validate source provenance, all claims and fail-closed authority."""
    _require(isinstance(ledger, dict), "ledger root must be an object")
    _require(ledger.get("schema_version") == SCHEMA, "unsupported ledger schema")
    _require(ledger.get("product") == "ASTRA", "unsupported product identity")
    _require(ledger.get("scope") == SCOPE, "scope cannot imply a launched product")
    _require(
        ledger.get("comparison_method") == COMPARISON_METHOD,
        "competitor tests must not be misrepresented",
    )
    _require(
        bool(re.fullmatch(r"20\d{2}-\d{2}-\d{2}", str(ledger.get("snapshot_date")))),
        "missing snapshot date",
    )
    try:
        date.fromisoformat(ledger["snapshot_date"])
    except (TypeError, ValueError) as exc:
        raise CompetitiveEvidenceError("invalid snapshot date") from exc
    authority = ledger.get("authority")
    _require(isinstance(authority, dict) and set(authority) == AUTHORITY_FIELDS,
             "trading authority schema drift")
    _require(all(authority[x] is False for x in AUTHORITY_FIELDS),
             "competitor inventory cannot grant trading permission")

    competitors = _list(ledger.get("competitors"), "competitors")
    _require(competitors, "no competitor references")
    _require(set(OFFICIAL_HOSTS) == {row.get("id") for row in competitors
                                    if isinstance(row, dict)},
             "missing or unapproved competitor identity")
    by_id = {}
    for row in competitors:
        _require(isinstance(row, dict), "competitor must be a JSON object")
        cid = _id(row.get("id"), "competitor ID")
        _require(cid not in by_id, f"duplicate competitor: {cid}")
        _string(row.get("name"), f"{cid}.name")
        _string(row.get("market_group"), f"{cid}.market_group")
        urls = _unique_sorted(row.get("official_urls"), f"{cid}.official_urls")
        _require(bool(urls), f"{cid}: missing official source")
        for url in urls:
            _url(url, OFFICIAL_HOSTS[cid], f"{cid}.official_urls")
        by_id[cid] = set(urls)

    capabilities = _list(ledger.get("capabilities"), "capabilities")
    _require(capabilities, "capability inventory must not be empty")
    seen = set()
    counters = Counter()
    areas = set()
    evidence_references = 0
    source_refs = 0
    for row in capabilities:
        _require(isinstance(row, dict), "capability must be a JSON object")
        cid = _id(row.get("id"), "capability ID")
        _require(cid not in seen, f"duplicate capability: {cid}")
        seen.add(cid)
        for field in ("name_ru", "area", "acceptance", "evidence_limitations"):
            _string(row.get(field), f"{cid}.{field}")
        areas.add(row["area"])
        state = row.get("astra_status")
        decision = row.get("build_decision")
        _require(state in STATUS, f"{cid}: unsupported implementation status")
        _require(decision in DECISION, f"{cid}: unsupported build decision")
        _require(row.get("priority") in PRIORITY, f"{cid}: unsupported priority")
        _require(
            state != "BLOCKED_SOURCE" or decision == "HOLD",
            f"{cid}: blocked provider cannot be promoted",
        )
        _require(
            state != "NOT_IMPLEMENTED" or decision != "DEEPEN",
            f"{cid}: nonexistent feature cannot claim enhancement",
        )
        linked = _unique_sorted(row.get("competitor_ids"), f"{cid}.competitor_ids")
        urls = _unique_sorted(row.get("source_urls"), f"{cid}.source_urls")
        _require(all(v in by_id for v in linked),
                 f"{cid}: unrecognized competitor ID")
        approved = set().union(*(by_id[k] for k in linked)) if linked else set()
        _require(set(urls).issubset(approved),
                 f"{cid}: competitor citation missing from official source list")
        _require(bool(linked) == bool(urls),
                 f"{cid}: unmatched competitor references and sources")
        for url in urls:
            _require(any(url in by_id[k] for k in linked),
                     f"{cid}: source belongs to another competitor")
        paths = _unique_sorted(row.get("evidence_paths"), f"{cid}.evidence_paths")
        if state == "NOT_IMPLEMENTED":
            _require(not paths, f"{cid}: unbuilt feature may not claim code evidence")
        else:
            _require(bool(paths), f"{cid}: implemented/research feature has no code evidence")
        for path in paths:
            _evidence_path(root, path)
        counters[state] += 1
        source_refs += len(urls)
        evidence_references += len(paths)

    return {
        "schema_version": "astra-competitive-evidence-audit-v1",
        "result": "PASS_SOURCE_ATTRIBUTION_AND_PATH_INVENTORY_ONLY",
        "comparison_method": COMPARISON_METHOD,
        "snapshot_date": ledger["snapshot_date"],
        "competitors": len(by_id),
        "capabilities": len(seen),
        "capability_areas": len(areas),
        "source_references": source_refs,
        "source_file_references": evidence_references,
        "status_counts": dict(sorted(counters.items())),
        "hands_on_competitor_qa_verified": False,
        "feature_runtime_parity_verified": False,
        "broker_execution_verified": False,
        "profitability_proven": False,
        "strategy_promotion_allowed": False,
        "live_trading_allowed": False,
    }


def read_and_validate(path: Path, root: Path) -> dict:
    _require(path.is_file() and not path.is_symlink(), "missing ledger source")
    _require(path.stat().st_size <= 2_000_000, "oversized ledger source")

    def unique(pairs: list[tuple[str, object]]) -> dict:
        result = {}
        for key, value in pairs:
            _require(key not in result, f"duplicate JSON key: {key}")
            result[key] = value
        return result

    def reject_constant(value: str):
        raise CompetitiveEvidenceError(f"nonfinite JSON constant: {value}")

    raw = path.read_bytes()
    try:
        ledger = json.loads(raw, object_pairs_hook=unique, parse_constant=reject_constant)
    except (TypeError, UnicodeError, ValueError) as exc:
        raise CompetitiveEvidenceError("invalid capability JSON evidence") from exc
    report = validate_ledger(ledger, root)
    report["ledger_sha256"] = hashlib.sha256(raw).hexdigest()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--ledger", type=Path, default=Path(DEFAULT_LEDGER))
    args = parser.parse_args()
    report = read_and_validate(args.root / args.ledger, args.root.resolve())
    print(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
