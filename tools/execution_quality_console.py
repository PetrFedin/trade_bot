"""Audited offline execution-quality UI: reproducible history and current depth.

No exchange authority, external JS, new strategy execution, or current prices
in the historical replay. Source hashes are reproducibility, not authenticity.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from tools.depth_execution_research import build_depth_stress
from tools.historical_research_console import _safe_json, publish_new_html
from tools.independent_coin_strategy_lab import BROAD21, study

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "app/platform_assets/execution_quality_terminal.html"


class ExecutionConsoleError(ValueError):
    """Refuse mixed, altered or permission-elevating research evidence."""


def render(candidates: dict, depth: dict, book_source: dict, bars_dir: Path) -> str:
    """Only render after regenerating both original studies independently."""
    if candidates != study(bars_dir, BROAD21):
        raise ExecutionConsoleError("candidate output differs from retained daily data")
    if depth != build_depth_stress(book_source):
        raise ExecutionConsoleError("depth report differs from pinned public snapshot")
    for evidence in (candidates, depth):
        for field in ("live_trading_allowed", "profitability_proven",
                      "strategy_promotion_allowed"):
            if evidence.get(field) is not False:
                raise ExecutionConsoleError("research permission escalation refused")
    if (
        candidates.get("scenario_count") != 210
        or depth.get("quotes") != 144
        or depth.get("historical_execution_data_available") is not False
        or candidates.get("historical_orderbook_verified") is not False
        or candidates.get("historical_funding_settlement_verified") is not False
        or candidates.get("unseen_holdout_available") is not False
    ):
        raise ExecutionConsoleError("unqualified dataset or historical book claim")
    payload = {
        "candidate_rows": [{
            "symbol": row["symbol"], "arm": row["arm"],
            "fee_bps": row["fee_bps"], "slip_bps": row["slip_bps"],
            "return_pct": row["return_pct"],
            "drawdown_pct": row["max_daily_close_drawdown_pct"],
            "closed_trades": row["trades"], "wins": row["wins"],
            "fees_paid_usdt": row["fees_paid_usdt"],
            "slippage_paid_usdt": row["slippage_paid_usdt"],
        } for row in candidates["scenarios"]],
        "quote_rows": depth["rows"],
        "candidate_sha256": candidates["report_sha256"],
        "depth_sha256": depth["report_sha256"],
        "visible_depth_is_historical": False,
        "account_mark_settlement_verified": False,
        "exchange_fills_verified": False,
        "promotion_allowed": False,
    }
    replacements = {
        "__RESEARCH_PAYLOAD__": _safe_json(payload),
        "__CANDIDATE_SHA__": candidates["report_sha256"],
        "__SNAPSHOT_SHA__": depth["report_sha256"],
        "__HISTORY_START__": candidates["date_range_utc"][0][:10],
        "__HISTORY_END__": candidates["date_range_utc"][1][:10],
        "__BOOK_DATE__": depth["latest_exchange_timestamp_utc"][:16],
    }
    html = TEMPLATE.read_text(encoding="utf-8")
    for key, value in replacements.items():
        if html.count(key) != 1:
            raise ExecutionConsoleError(f"HTML marker missing or repeated: {key}")
        html = html.replace(key, value)
    return html


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--depth", type=Path, required=True)
    parser.add_argument("--book-source", type=Path, required=True)
    parser.add_argument("--bars-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    for source in (args.candidate, args.depth, args.book_source):
        if not source.is_file() or source.is_symlink():
            raise ExecutionConsoleError("missing or symlinked research input")
    def load(path: Path) -> dict:
        return json.loads(path.read_text(encoding="utf-8"))
    html = render(
        load(args.candidate), load(args.depth), load(args.book_source),
        args.bars_dir,
    )
    publish_new_html(args.output, html)
    print("ASTRA_EXECUTION_RESEARCH_CREATED", args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
