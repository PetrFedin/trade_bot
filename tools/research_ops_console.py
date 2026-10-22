"""Render and verify the ASTRA offline research operations screen.

The HTML is generated from canonical status + capability evidence. It contains
no broker controls, external resources, demo values or unverified live data.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import sys
from collections.abc import Mapping
from pathlib import Path

from tools.research_analytics import calculate_research_overview
from tools.research_evidence_lineage import (
    _read_strict as read_evidence_source,
)
from tools.research_evidence_lineage import (
    create_dossier,
    dependency_impact,
)

ROOT = Path(__file__).resolve().parents[1]
STATUS_PATH = ROOT / "CURRENT_SYSTEM_STATUS.json"
REGISTER_PATH = ROOT / "research" / "competitive_capability_register_v1.json"
TEMPLATE_PATH = ROOT / "tools" / "research_ops_console.template.html"
OUTPUT_PATH = ROOT / "docs" / "research-ops-console.html"

STATES = {
    "EXISTING_RESEARCH_CODE": ("existing", "Исследовательский код"),
    "IMPLEMENTED_PENDING_QUALIFICATION": ("pending", "Ожидает проверки"),
    "PLANNED": ("planned", "Запланировано"),
    "BLOCKED_SOURCE": ("blocked", "Внешний блокер"),
}


class ResearchConsoleError(ValueError):
    """Rendering failed because the exact source or template was inconsistent."""


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def _read_strict(path: Path) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 256_000:
        raise ResearchConsoleError(f"missing or oversized console source: {path.name}")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ResearchConsoleError(f"duplicate JSON field: {key}")
            result[key] = value
        return result

    try:
        value = json.loads(
            path.read_bytes(), object_pairs_hook=unique,
            parse_constant=lambda v: (_ for _ in ()).throw(
                ResearchConsoleError(f"non-finite source constant: {v}")
            ),
        )
    except (ValueError, UnicodeError, TypeError) as exc:
        if isinstance(exc, ResearchConsoleError):
            raise
        raise ResearchConsoleError("invalid console source JSON") from exc
    if not isinstance(value, dict):
        raise ResearchConsoleError("console source must be JSON object")
    return value


def _row(entry: Mapping[str, object]) -> str:
    identifier = str(entry["id"])
    state = str(entry["state"])
    if state not in STATES:
        raise ResearchConsoleError("unrecognized capability state")
    css_class, label = STATES[state]
    dependencies = tuple(entry["depends_on"])
    if dependencies:
        links = ", ".join(
            f'<a href="#cap-{esc(name)}"><u>{esc(name)}</u></a>'
            for name in dependencies
        )
    else:
        links = "нет"
    paths = tuple(entry["code_paths"])
    evidence = ", ".join(esc(path) for path in paths) if paths else "нет"
    return (
        f'      <tr id="cap-{esc(identifier)}" data-state="{esc(state)}">'
        f'<td><strong>{esc(identifier.replace("_", " "))}</strong>'
        f'<small>{esc(identifier)}</small></td>'
        f'<td><span class="tag {css_class}">{esc(label)}</span></td>'
        f'<td><code>{esc(entry["priority"])}</code></td>'
        f'<td>{esc(entry["acceptance_gate"])}'
        f'<small>Зависимости: {links}</small>'
        f'<small>Исходники: {evidence}</small></td></tr>'
    )


def _lineage_row(item: Mapping[str, object]) -> str:
    identifier = str(item["capability"])
    blockers = tuple(item["source_blockers"])
    references = ", ".join(
        f'<a href="#cap-{esc(blocker)}"><u>{esc(blocker)}</u></a>'
        for blocker in blockers
    )
    return (
        f'<tr><td><a href="#cap-{esc(identifier)}">{esc(identifier)}</a></td>'
        f'<td>{references}</td><td class="num">{esc(item["distance"])}</td></tr>'
    )


def render(
    status: dict, register: dict, template: str, *,
    evidence_root: Path, dossier: dict | None = None,
) -> str:
    overview = calculate_research_overview(status, register, evidence_root=evidence_root)
    counts = overview["counts"]
    state_counts = overview["capability_summary"]["counts_by_state"]
    impact = dependency_impact(register, root=evidence_root)
    if dossier is not None:
        if (
            dossier.get("decision") != "RESEARCH_HOLD"
            or dossier.get("trading_authority_allowed") is not False
            or dossier.get("profitability_proven") is not False
            or dossier.get("capability_impact") != impact
            or dossier.get("frozen_pointer", {}).get("value_agreement") is not True
            or dossier.get("frozen_pointer", {}).get("independent_market_evidence") is not False
        ):
            raise ResearchConsoleError("invalid research evidence dossier")
    pointer_count = (
        dossier["frozen_pointer"]["compared_fields"] if dossier is not None else "—"
    )
    origin_sha = (
        dossier["frozen_pointer"]["source_repository_sha"][:12]
        if dossier is not None else "не проверен"
    )
    data = {
        "QUALIFIED_SHA": overview["main_qualified_sha"][:12],
        "NET_PNL": overview["net_pnl_usdt"],
        "NET_RETURN": overview["net_return_pct"],
        "EQUITY": overview["reference_equity_usdt"],
        "TRADES": counts["portfolio_trades"],
        "WINS": counts["wins"],
        "LOSSES": counts["losses"],
        "BREAKEVEN": counts["breakeven"],
        "WIN_RATE": overview["win_rate_pct"],
        "LOSS_RATE": overview["loss_rate_pct"],
        "NEUTRAL_RATE": overview["neutral_rate_pct"],
        "ELIGIBLE": counts["eligible_signals"],
        "PLANNED": counts["plan_eligible_signals"],
        "PLAN_RATE": overview["plan_eligibility_pct"],
        "EPISODES": counts["independent_target_stop_episodes"],
        "TARGET_RATE": overview["target_first_pct"],
        "STOP_RATE": overview["stop_first_pct"],
        "PNL_PER_TRADE": overview["net_pnl_per_trade_usdt"],
        "FEATURE_COUNT": overview["capability_summary"]["feature_count"],
        "EXISTING": state_counts.get("EXISTING_RESEARCH_CODE", 0),
        "PENDING": state_counts.get("IMPLEMENTED_PENDING_QUALIFICATION", 0),
        "PLANNED_FEATURES": state_counts.get("PLANNED", 0),
        "BLOCKED": state_counts.get("BLOCKED_SOURCE", 0),
        "EVIDENCE_SOURCE": overview["evidence_source"],
        "SCHEMA": overview["schema_version"],
        "LINEAGE_FIELDS": pointer_count,
        "LINEAGE_ORIGIN": origin_sha,
        "BLOCKED_SOURCES": len(impact["blocked_source_roots"]),
        "DOWNSTREAM_IMPACT": impact["downstream_impact_count"],
    }
    result = template
    for key, value in data.items():
        placeholder = "@@" + key + "@@"
        if placeholder not in result:
            raise ResearchConsoleError(f"missing UI placeholder: {key}")
        result = result.replace(placeholder, esc(value))
    rows_token = "@@CAPABILITY_ROWS@@"
    if rows_token not in result:
        raise ResearchConsoleError("missing capability table binding")
    result = result.replace(
        rows_token, "\n".join(_row(entry) for entry in overview["capability_rows"]),
    )
    impact_token = "@@LINEAGE_IMPACT_ROWS@@"
    if impact_token not in result:
        raise ResearchConsoleError("missing blocked-dependency table binding")
    result = result.replace(
        impact_token, "\n".join(
            _lineage_row(entry) for entry in impact["downstream_blocked_capabilities"]
        ),
    )
    if "@@" in result:
        raise ResearchConsoleError("unresolved console placeholders")
    if "<script src=" in result or "<link rel=" in result:
        raise ResearchConsoleError("external runtime assets forbidden")
    return result


def build(
    status_path: Path = STATUS_PATH,
    register_path: Path = REGISTER_PATH,
    template_path: Path = TEMPLATE_PATH,
    root: Path = ROOT,
) -> str:
    if template_path.is_symlink():
        raise ResearchConsoleError("symlinked template forbidden")
    if not template_path.is_file() or template_path.stat().st_size > 100_000:
        raise ResearchConsoleError("missing or oversized interface template")
    root = Path(root)
    status, status_bytes = read_evidence_source(status_path, root)
    register, register_bytes = read_evidence_source(register_path, root)
    pointer, pointer_bytes = read_evidence_source(
        root / "qualification/strategy/bybit_price_only_frozen_negative.json", root,
    )
    dossier = create_dossier(
        status, register, pointer, root=root,
        source_digests={
            "status": hashlib.sha256(status_bytes).hexdigest(),
            "capabilities": hashlib.sha256(register_bytes).hexdigest(),
            "strategy_pointer": hashlib.sha256(pointer_bytes).hexdigest(),
        },
    )
    return render(
        status, register, template_path.read_text(encoding="utf-8"),
        evidence_root=root, dossier=dossier,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--output", type=Path, default=OUTPUT_PATH)
    args = parser.parse_args(argv)
    if args.check == args.write:
        parser.error("choose exactly one of --check or --write")
    document = build()
    if args.write:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(document, encoding="utf-8")
        print("ASTRA_CONSOLE_GENERATED", args.output)
        return 0
    if not args.output.is_file() or args.output.read_text(encoding="utf-8") != document:
        print("ASTRA_CONSOLE_STALE_OR_MISSING", file=sys.stderr)
        return 1
    print("ASTRA_RESEARCH_CONSOLE_SOURCE_MATCH")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
