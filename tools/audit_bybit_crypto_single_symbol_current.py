from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.marketdata.bybit_public_archive import (
    BybitPublicTradeArchiveClient,
    completed_archive_dates,
)
from app.strategy.crypto_historical_diagnostics import (
    build_crypto_historical_trade_conditions,
)
from app.strategy.crypto_signal_event_outcomes import audit_all_crypto_signal_events
from app.strategy.crypto_signal_outcome_audit import audit_crypto_signal_outcomes
from tools.replay_bybit_crypto import default_crypto_config
from tools.replay_bybit_crypto_single_symbol import (
    replay_open_ended_crypto_runner_single_symbol,
)


def run_single_symbol_current_audit(
    *,
    symbol: str,
    lookback_days: int = 7,
    opening_equity_usdt: Decimal = Decimal("1000"),
    now: datetime | None = None,
) -> dict[str, object]:
    normalized = symbol.strip().upper()
    if normalized != symbol or not symbol.endswith("USDT"):
        raise ValueError("single-symbol audit requires normalized USDT symbol")
    cutoff = datetime.now(UTC) if now is None else now
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("single-symbol audit cutoff must be timezone-aware")
    cutoff = cutoff.astimezone(UTC)
    dates = completed_archive_dates(now=cutoff, lookback_days=lookback_days)
    archive = BybitPublicTradeArchiveClient().fetch_klines(
        symbols=(symbol,),
        dates=dates,
        interval_minutes=5,
    )
    archive.validate(requested_symbols=(symbol,), minimum_bars=25)

    config = default_crypto_config()
    all_signals = audit_all_crypto_signal_events(
        archive.klines,
        strategy_config=config,
        reference_equity_usdt=opening_equity_usdt,
    )
    replay = replay_open_ended_crypto_runner_single_symbol(
        archive.klines,
        opening_equity_usdt=opening_equity_usdt,
        base_config=config,
        interval="5",
    )
    records = build_crypto_historical_trade_conditions(
        archive.klines,
        replay,
        strategy_config=config,
    )
    trade_audit = audit_crypto_signal_outcomes(records, strategy_config=config)
    return {
        "audit": "BYBIT_CRYPTO_SINGLE_SYMBOL_CURRENT_QUALIFIED_AUDIT_V1",
        "symbol": symbol,
        "source": "BYBIT_OFFICIAL_PUBLIC_TRADE_ARCHIVE_AGGREGATED_5M",
        "cutoff_utc": cutoff.isoformat(),
        "archive_dates": [value.isoformat() for value in dates],
        "opening_equity_usdt": float(opening_equity_usdt),
        "strategy_mode": "CONDITIONAL_1_5X_OPEN_ENDED_RUNNER",
        "runner_minimum_expected_edge_multiple": 1.5,
        "all_eligible_signal_events": all_signals,
        "trade_outcomes": trade_audit,
        "replay_metrics": replay["metrics"],
        "eligible_signal_event_count": replay["eligible_signal_event_count"],
        "accepted_trade_plan_event_count": replay["accepted_trade_plan_event_count"],
        "runner_activation_event_count": replay["runner_activation_event_count"],
        "strategy_promotion_allowed": False,
        "trade_actionable": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
        "predictive_guarantee_allowed": False,
        "portfolio_competition_modeled": False,
        "interpretation": (
            "independent per-symbol diagnostic; use the synchronized multi-symbol replay for "
            "shared-capital ranking, concurrency and realized portfolio selection"
        ),
    }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit one Bybit token with the qualified conditional runner"
    )
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--opening-equity", default="1000")
    parser.add_argument(
        "--cutoff",
        help="Timezone-aware ISO-8601 cutoff used to freeze completed archive days",
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def _parse_cutoff(value: str | None) -> datetime | None:
    if value is None:
        return None
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("single-symbol audit --cutoff must include timezone")
    return parsed.astimezone(UTC)


def main() -> None:
    args = _parse_args()
    report = run_single_symbol_current_audit(
        symbol=args.symbol.strip().upper(),
        lookback_days=args.lookback_days,
        opening_equity_usdt=Decimal(args.opening_equity),
        now=_parse_cutoff(args.cutoff),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    trade_outcomes = report["trade_outcomes"]
    all_signals = report["all_eligible_signal_events"]
    if not isinstance(trade_outcomes, dict) or not isinstance(all_signals, dict):
        raise ValueError("single-symbol audit produced invalid sections")
    print(
        "BYBIT_SINGLE_SYMBOL_SIGNAL_AUDIT="
        + json.dumps(
            {
                "symbol": report["symbol"],
                "cutoff_utc": report["cutoff_utc"],
                "archive_dates": report["archive_dates"],
                "signal_event_count": all_signals["signal_event_count"],
                "closed_trade_count": trade_outcomes["trade_count"],
                "aggregate": trade_outcomes["aggregate"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
