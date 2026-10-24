"""Source-recomputed offline ASTRA signal-to-slot-to-trade research UI.

No live price, server requests, trade placement or authority changes.
Original, gated, and BTC-regime evidence must match the retained archives.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools.coin_signal_funnel_attribution import (
    FunnelAuditError,
    build_report,
)
from tools.historical_research_console import _safe_json, publish_new_html

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "app/platform_assets/signal_funnel_terminal.html"


class SignalFunnelUIError(ValueError):
    """Fail closed when research source chain or permissions are not qualified."""


def render(
    original: dict, gates: dict, risk: dict, report: dict, btc: Path
) -> str:
    try:
        accepted = build_report(original, gates, risk, btc)
    except (FunnelAuditError, KeyError, TypeError) as exc:
        raise SignalFunnelUIError("unable to independently requalify source") from exc
    if report != accepted:
        raise SignalFunnelUIError("signal funnel result differs from original records")
    if (
        report.get("total_model_scenarios") != 100
        or report.get("ranker_pair_count") != 48
        or report.get("no_historical_rejected_signal_pnl_assumed") is not True
        or any(
            report.get(flag) is not False
            for flag in (
                "untouched_future_holdout",
                "live_trading_allowed",
                "strategy_promotion_allowed",
                "profitability_proven",
                "funding_settlements_verified",
                "real_exchange_fills_verified",
                "historical_orderbook_verified",
            )
        )
    ):
        raise SignalFunnelUIError("missing research lineage or authority boundary")
    summaries = []
    for model in report["scenario_records"]:
        summaries.append({
            "universe": model["universe"],
            "arm": model["arm"],
            "ranker": model["entry_ranker"],
            "gate": model["gate"],
            "fee": model["fee_bps_per_side"],
            "slip": model["slippage_bps_per_side"],
            "return_pct": model["original_model_return_pct"],
            "drawdown_pct": model["original_model_drawdown_pct"],
            "eligible": model["eligible_signals"],
            "accepted": model["accepted_signals"],
            "rejected": model["rejected_signals"],
            "rejection_reasons": model["rejection_reasons"],
            "gross_usdt": model["realized_gross_usdt"],
            "fees_usdt": model["realized_fee_usdt"],
            "slippage_usdt": model["realized_slippage_usdt"],
            "net_usdt": model["realized_net_usdt"],
            "coins": model["per_coin"],
        })
    bundle = {
        "models": summaries,
        "rankers": report["ranker_pairs"],
        "source_sha256": report["report_sha256"],
        "baseline_sha256": report["original_shared_sha256"],
        "risk_sha256": report["risk_regime_sha256"],
        "gates_sha256": report["experimental_gate_sha256"],
        "historical_execution_verified": False,
        "missed_signal_pnl_known": False,
        "live_order_authority": False,
        "profitability_proven": False,
    }
    substitutions = {
        "__RESEARCH_DATA__": _safe_json(bundle),
        "__FUNNEL_SHA__": report["report_sha256"],
        "__BASE_SHA__": report["original_shared_sha256"],
        "__GATE_SHA__": report["experimental_gate_sha256"],
        "__START_DATE__": original["first_utc"][:10],
        "__END_DATE__": original["last_utc"][:10],
    }
    text = TEMPLATE.read_text(encoding="utf-8")
    for marker, value in substitutions.items():
        if text.count(marker) != 1:
            raise SignalFunnelUIError(f"missing or duplicate marker: {marker}")
        text = text.replace(marker, value)
    return text


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared", type=Path, required=True)
    parser.add_argument("--gates", type=Path, required=True)
    parser.add_argument("--risk", type=Path, required=True)
    parser.add_argument("--funnel", type=Path, required=True)
    parser.add_argument("--btc", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    for source in (args.shared, args.gates, args.risk, args.funnel, args.btc):
        if not source.is_file() or source.is_symlink():
            raise SignalFunnelUIError("missing or symlinked original research file")
    if args.output.exists() or args.output.is_symlink():
        raise SignalFunnelUIError("immutable HTML source cannot be overwritten")
    def load(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))
    html = render(
        load(args.shared), load(args.gates),
        load(args.risk), load(args.funnel), args.btc,
    )
    publish_new_html(args.output, html)
    print("ASTRA_SIGNAL_FUNNEL_TERMINAL", args.output, len(html))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
