"""Offline risk and BTC gate visual audit over independently rebuilt evidence.

The HTML is a static presentation only. It never connects to a broker or
creates trades and cannot promote a retrospective winning configuration.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from tools.historical_research_console import _safe_json, publish_new_html
from tools.independent_coin_strategy_lab import canonical

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "app/platform_assets/risk_gate_terminal.html"


class RiskGateUIError(ValueError):
    """Refuse altered, incomplete or authority-elevating research input."""


def verify_digest(document: dict) -> None:
    if not isinstance(document, dict):
        raise RiskGateUIError("missing research document")
    observed = document.get("report_sha256")
    unchanged = {k: v for k, v in document.items() if k != "report_sha256"}
    expected = hashlib.sha256(canonical(unchanged)).hexdigest()
    if not isinstance(observed, str) or observed != expected:
        raise RiskGateUIError("research document hash invalid")


def render(shared: dict, risk: dict, gates: dict) -> str:
    for document in (shared, risk, gates):
        verify_digest(document)
        if document.get("profitability_proven") is not False:
            raise RiskGateUIError("profitability claim not qualified")
        if document.get("strategy_promotion_allowed") is not False:
            raise RiskGateUIError("strategy authorization denied")
        if document.get("live_trading_allowed") is not False:
            raise RiskGateUIError("live order authority denied")
    if (
        shared.get("schema_version") != "astra-shared-capital-candidate-study-v1"
        or risk.get("schema_version") != "astra-shared-portfolio-risk-attribution-v1"
        or gates.get("schema_version") != "astra-previous-close-btc-entry-gate-study-v1"
        or shared.get("scenario_count") != 36
        or risk.get("scenario_count") != 36
        or gates.get("scenario_count") != 64
        or risk.get("historical_days") != 930
        or gates.get("historical_days") != 930
        or gates.get("untouched_future_holdout") is not False
        or risk.get("unseen_holdout") is not False
        or risk.get("source_shared_sha256") != shared.get("report_sha256")
        or gates.get("baseline_shared_report_sha256") != shared.get("report_sha256")
        or gates.get("source_sha256") != shared.get("source_sha256")
        or risk.get("source_btc_sha256") != shared["source_sha256"]["BTCUSDT_D.csv"]
    ):
        raise RiskGateUIError("source join, population, or research scope invalid")
    base_rows = {}
    for model in risk["models"]:
        key = (
            model["universe"], model["arm"], model["ranker"],
            model["fee_bps"], model["slippage_bps"],
        )
        if key in base_rows:
            raise RiskGateUIError("duplicate baseline research scenario")
        base_rows[key] = model
    if len(base_rows) != 36:
        raise RiskGateUIError("missing original risk scenarios")
    gate_rows = []
    gate_keys = set()
    for item in gates["scenarios"]:
        model = item["model"]
        key = (
            item["universe"], item["arm"], item["entry_ranker"],
            item["fee_bps_per_fill"], item["slippage_bps_per_fill"],
        )
        full = (*key, item["gate"])
        if full in gate_keys or key not in base_rows:
            raise RiskGateUIError("unexpected, duplicated or absent gate comparison")
        gate_keys.add(full)
        if (
            model.get("entry_gate_policy") != item["gate"]
            or model.get("historical_funding_included") is not False
            or model.get("live_trading_allowed") is not False
        ):
            raise RiskGateUIError("gate result is unqualified")
        gate_rows.append({
            "universe": item["universe"], "arm": item["arm"],
            "ranker": item["entry_ranker"], "fee": item["fee_bps_per_fill"],
            "slip": item["slippage_bps_per_fill"], "gate": item["gate"],
            "return": model["return_pct"],
            "drawdown": model["max_daily_close_drawdown_pct"],
            "trades": model["closed_trade_count"],
            "blocked": item["gate_blocked_signals"],
            "delta": item["gate_minus_original_percentage_points"],
        })
    if len(gate_keys) != 64:
        raise RiskGateUIError("incomplete predeclared two-gate research matrix")
    base = [{
        "universe": m["universe"], "arm": m["arm"], "ranker": m["ranker"],
        "fee": m["fee_bps"], "slip": m["slippage_bps"],
        "return": m["return_pct"], "drawdown": m["max_drawdown_pct"],
        "worst_day": m["worst_day_pct"],
        "worst_5pct_daily": m["mean_worst_5pct_days_pct"],
        "underwater_days": m["longest_daily_close_underwater_days"],
        "stop_share": m["trades"]["stop_exit_pct"],
        "fees": m["trades"]["fee_usdt"],
        "slippage": m["trades"]["slippage_usdt"],
        "trades": m["trades"]["closed_trade_count"],
        "coins": m["trades"]["per_coin"],
        "BTC_regimes": m["BTC_previous_close_regimes"],
    } for m in risk["models"]]
    payload = {
        "baseline": base, "gates": gate_rows,
        "original_sha256": shared["report_sha256"],
        "risk_sha256": risk["report_sha256"],
        "gate_sha256": gates["report_sha256"],
        "regime_day_counts": risk["BTC_regime_days"],
        "trading_enabled": False, "profitability_proven": False,
        "historical_orderbook_verified": False,
        "historical_mark_settlement_verified": False,
        "holdout_verified": False,
    }
    replacements = {
        "__RESEARCH_JSON__": _safe_json(payload),
        "__ORIGINAL_SHA__": shared["report_sha256"],
        "__RISK_SHA__": risk["report_sha256"],
        "__GATES_SHA__": gates["report_sha256"],
        "__FIRST_DATE__": shared["first_utc"][:10],
        "__LAST_DATE__": shared["last_utc"][:10],
    }
    page = TEMPLATE.read_text(encoding="utf-8")
    for marker, value in replacements.items():
        if page.count(marker) != 1:
            raise RiskGateUIError("missing or repeated static template marker")
        page = page.replace(marker, value)
    return page


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared", type=Path, required=True)
    parser.add_argument("--risk", type=Path, required=True)
    parser.add_argument("--gates", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    for path in (args.shared, args.risk, args.gates):
        if not path.is_file() or path.is_symlink():
            raise RiskGateUIError("source input unavailable or symlinked")
    def load(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))
    page = render(load(args.shared), load(args.risk), load(args.gates))
    publish_new_html(args.output, page)
    print("ASTRA_RISK_GATE_TERMINAL", args.output, len(page))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
