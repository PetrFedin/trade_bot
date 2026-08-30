from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.marketdata.bybit_public_archive import (
    BybitArchiveAcquisition,
    BybitPublicTradeArchiveClient,
    completed_archive_dates,
)
from app.marketdata.bybit_v5 import BybitKlineAcquisition
from app.strategy.crypto_historical_diagnostics import build_crypto_historical_trade_conditions
from app.strategy.crypto_protection_quality import evaluate_protection_quality
from app.strategy.crypto_signal_event_outcomes import audit_all_crypto_signal_events
from app.strategy.crypto_signal_outcome_audit import audit_crypto_signal_outcomes
from app.strategy.crypto_signal_ranking_attribution import attribute_crypto_portfolio_ranking
from app.strategy.crypto_trade_quality_diagnostics import diagnose_crypto_replay_quality
from tools.audit_bybit_crypto_position_selection import audit_position_selection
from tools.replay_bybit_crypto import default_crypto_config
from tools.replay_bybit_crypto_runner import replay_open_ended_crypto_runner

_DEFAULT_SYMBOLS = (
    "BTCUSDT",
    "ETHUSDT",
    "SOLUSDT",
    "XRPUSDT",
    "BNBUSDT",
    "DOGEUSDT",
    "LINKUSDT",
    "ADAUSDT",
)


def run_signal_failure_attribution(
    *,
    symbols: tuple[str, ...] = _DEFAULT_SYMBOLS,
    lookback_days: int = 7,
    opening_equity_usdt: Decimal = Decimal("1000"),
    now: datetime | None = None,
    archive_workers: int = 4,
) -> dict[str, Any]:
    if lookback_days < 1:
        raise ValueError("signal failure attribution lookback must be positive")
    if opening_equity_usdt <= 0:
        raise ValueError("signal failure attribution opening equity must be positive")
    cutoff = datetime.now(UTC) if now is None else now
    dates = completed_archive_dates(now=cutoff, lookback_days=lookback_days)
    acquisition = _fetch_archives_by_symbol(
        symbols=symbols,
        dates=dates,
        interval_minutes=5,
        archive_workers=archive_workers,
    )
    acquisition.validate(requested_symbols=symbols, minimum_bars=25)

    config = default_crypto_config().with_target(Decimal("20"))
    all_signals = audit_all_crypto_signal_events(
        acquisition.klines,
        strategy_config=config,
        reference_equity_usdt=opening_equity_usdt,
    )
    replay = replay_open_ended_crypto_runner(
        acquisition.klines,
        opening_equity_usdt=opening_equity_usdt,
        base_config=config,
        interval="5",
    )
    conditions = build_crypto_historical_trade_conditions(
        acquisition.klines,
        replay,
        strategy_config=config,
    )
    signal_outcomes = audit_crypto_signal_outcomes(
        conditions,
        strategy_config=config,
    )
    ranking = attribute_crypto_portfolio_ranking(
        acquisition.klines,
        replay,
        all_signals,
        strategy_config=config,
    )
    static_selection = audit_position_selection(
        acquisition.klines,
        equity_usdt=opening_equity_usdt,
        config=config,
        maximum_positions=config.maximum_concurrent_positions,
    )
    trade_quality = diagnose_crypto_replay_quality(replay)
    protection_quality = evaluate_protection_quality(
        list(replay["closed_trades"])
    ).as_dict()

    return {
        "report": "BYBIT_CRYPTO_SIGNAL_RANKING_PROTECTION_ATTRIBUTION_V1",
        "source": "BYBIT_OFFICIAL_PUBLIC_TRADE_ARCHIVE_AGGREGATED_5M",
        "archive_dates": [value.isoformat() for value in dates],
        "archive_completed_utc_days_only": True,
        "symbols": list(symbols),
        "opening_equity_usdt": float(opening_equity_usdt),
        "canonical_replay_mode": replay["mode"],
        "canonical_replay_metrics": replay["metrics"],
        "canonical_exit_reason_counts": {
            row["exit_reason"]: sum(
                trade["exit_reason"] == row["exit_reason"]
                for trade in replay["closed_trades"]
            )
            for row in replay["closed_trades"]
        },
        "all_eligible_signal_events": all_signals,
        "accepted_trade_signal_outcomes": signal_outcomes,
        "state_aware_ranking_attribution": ranking,
        "static_reference_equity_position_selection": static_selection,
        "trade_quality": trade_quality,
        "protection_quality": protection_quality,
        "interpretation_contract": (
            "selection comparison is retrospective attribution only; future outcomes are joined "
            "after both ranking orders are reconstructed and cannot change historical entries"
        ),
        "counterfactual_portfolio_pnl_claim_allowed": False,
        "parameter_retuning_performed": False,
        "ranking_weights_changed": False,
        "strategy_selection_allowed": False,
        "strategy_promotion_allowed": False,
        "trade_actionable": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
        "predictive_guarantee_allowed": False,
    }


def _fetch_archives_by_symbol(
    *,
    symbols: tuple[str, ...],
    dates: tuple[date, ...],
    interval_minutes: int,
    archive_workers: int,
) -> BybitArchiveAcquisition:
    if not 1 <= archive_workers <= 8:
        raise ValueError("signal failure attribution archive workers must be within [1, 8]")

    def fetch_one(symbol: str) -> tuple[str, BybitArchiveAcquisition]:
        result = BybitPublicTradeArchiveClient().fetch_klines(
            symbols=(symbol,),
            dates=dates,
            interval_minutes=interval_minutes,
        )
        result.validate(requested_symbols=(symbol,), minimum_bars=1)
        return symbol, result

    workers = min(archive_workers, len(symbols))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        loaded = dict(executor.map(fetch_one, symbols))

    bars = tuple(
        sorted(
            (bar for symbol in symbols for bar in loaded[symbol].klines.bars),
            key=lambda item: (item.symbol, item.start_time),
        )
    )
    combined = BybitArchiveAcquisition(
        klines=BybitKlineAcquisition(
            bars=bars,
            pages_by_symbol={symbol: len(dates) for symbol in symbols},
        ),
        files_by_symbol={
            symbol: loaded[symbol].files_by_symbol[symbol]
            for symbol in symbols
        },
        trade_rows_by_symbol={
            symbol: loaded[symbol].trade_rows_by_symbol[symbol]
            for symbol in symbols
        },
    )
    combined.validate(requested_symbols=symbols, minimum_bars=1)
    return combined


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Attribute Bybit crypto ranking and protection failures"
    )
    parser.add_argument("--symbols", default=",".join(_DEFAULT_SYMBOLS))
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--opening-equity", default="1000")
    parser.add_argument("--archive-workers", type=int, default=4)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    symbols = tuple(
        symbol.strip().upper()
        for symbol in args.symbols.split(",")
        if symbol.strip()
    )
    report = run_signal_failure_attribution(
        symbols=symbols,
        lookback_days=args.lookback_days,
        opening_equity_usdt=Decimal(args.opening_equity),
        archive_workers=args.archive_workers,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    ranking = report["state_aware_ranking_attribution"]
    trade_quality = report["trade_quality"]
    print(
        "BYBIT_SIGNAL_FAILURE_ATTRIBUTION="
        + json.dumps(
            {
                "archive_dates": report["archive_dates"],
                "symbols": report["symbols"],
                "replay_metrics": report["canonical_replay_metrics"],
                "ranking": {
                    "decision_count": ranking["decision_count"],
                    "selection_changed_decision_count": ranking[
                        "selection_changed_decision_count"
                    ],
                    "canonical_quality_first": ranking["canonical_quality_first"],
                    "economic_shadow": ranking["economic_shadow"],
                    "changed_decision_comparison": ranking[
                        "changed_decision_comparison"
                    ],
                },
                "trade_quality_overall": trade_quality["overall"],
                "protection_quality": report["protection_quality"],
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
