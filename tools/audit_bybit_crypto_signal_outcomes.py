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
from app.strategy.crypto_historical_diagnostics import (
    build_crypto_historical_trade_conditions,
)
from app.strategy.crypto_signal_event_outcomes import audit_all_crypto_signal_events
from app.strategy.crypto_signal_outcome_audit import (
    CryptoSignalOutcomeAuditPolicy,
    audit_crypto_signal_outcomes,
)
from tools.replay_bybit_crypto import default_crypto_config, replay_acquisition

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


def run_signal_outcome_audit(
    *,
    symbols: tuple[str, ...] = _DEFAULT_SYMBOLS,
    lookback_days: int = 7,
    opening_equity_usdt: Decimal = Decimal("1000"),
    target_usd: Decimal = Decimal("20"),
    policy: CryptoSignalOutcomeAuditPolicy | None = None,
    now: datetime | None = None,
    archive_workers: int = 4,
) -> dict[str, object]:
    cutoff = datetime.now(UTC) if now is None else now
    dates = completed_archive_dates(now=cutoff, lookback_days=lookback_days)
    acquisition = _fetch_archives_by_symbol(
        symbols=symbols,
        dates=dates,
        interval_minutes=5,
        archive_workers=archive_workers,
    )
    acquisition.validate(requested_symbols=symbols, minimum_bars=25)

    config = default_crypto_config().with_target(target_usd)
    all_signal_events = audit_all_crypto_signal_events(
        acquisition.klines,
        strategy_config=config,
        reference_equity_usdt=opening_equity_usdt,
    )
    replay = replay_acquisition(
        acquisition.klines,
        opening_equity_usdt=opening_equity_usdt,
        targets_usd=(target_usd,),
        base_config=config,
        interval="5",
    )
    variants = replay["variants"]
    if not isinstance(variants, dict) or len(variants) != 1:
        raise ValueError("signal audit expected exactly one replay target variant")
    variant = next(iter(variants.values()))
    if not isinstance(variant, dict):
        raise ValueError("signal audit replay target variant is invalid")

    records = build_crypto_historical_trade_conditions(
        acquisition.klines,
        variant,
        strategy_config=config,
    )
    audit = audit_crypto_signal_outcomes(records, strategy_config=config, policy=policy)
    audit.update(
        source="BYBIT_OFFICIAL_PUBLIC_TRADE_ARCHIVE_AGGREGATED_5M",
        archive_dates=[value.isoformat() for value in dates],
        archive_completed_utc_days_only=True,
        archive_download_workers=archive_workers,
        symbols=list(symbols),
        target_net_profit_usd=float(target_usd),
        opening_equity_usdt=float(opening_equity_usdt),
        all_eligible_signal_events=all_signal_events,
        eligible_signal_event_count=variant["eligible_signal_event_count"],
        accepted_trade_plan_event_count=variant["accepted_trade_plan_event_count"],
        replay_metrics=variant["metrics"],
        current_incomplete_bar_excluded=True,
        funding_costs_modeled=False,
        real_demo_fills=False,
        historical_observation_is_not_future_guarantee=True,
    )
    return audit


def _fetch_archives_by_symbol(
    *,
    symbols: tuple[str, ...],
    dates: tuple[date, ...],
    interval_minutes: int,
    archive_workers: int,
) -> BybitArchiveAcquisition:
    if not 1 <= archive_workers <= 8:
        raise ValueError("signal audit archive workers must be within [1, 8]")

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
            (
                bar
                for symbol in symbols
                for bar in loaded[symbol].klines.bars
            ),
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
        description="Audit historical outcomes of frozen Bybit signals"
    )
    parser.add_argument("--symbols", default=",".join(_DEFAULT_SYMBOLS))
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--opening-equity", default="1000")
    parser.add_argument("--target", default="20")
    parser.add_argument("--minimum-pattern-trades", type=int, default=5)
    parser.add_argument("--sample-sufficient-trades", type=int, default=30)
    parser.add_argument("--minimum-cross-symbol-count", type=int, default=2)
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
    policy = CryptoSignalOutcomeAuditPolicy(
        minimum_pattern_trades=args.minimum_pattern_trades,
        sample_sufficient_trades=args.sample_sufficient_trades,
        minimum_cross_symbol_count=args.minimum_cross_symbol_count,
    )
    report = run_signal_outcome_audit(
        symbols=symbols,
        lookback_days=args.lookback_days,
        opening_equity_usdt=Decimal(args.opening_equity),
        target_usd=Decimal(args.target),
        policy=policy,
        archive_workers=args.archive_workers,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    all_signals = report["all_eligible_signal_events"]
    if not isinstance(all_signals, dict):
        raise ValueError("signal audit all-signal section is invalid")
    print(
        "BYBIT_SIGNAL_OUTCOME_AUDIT="
        + json.dumps(
            {
                "closed_trade_count": report["trade_count"],
                "all_signal_event_count": all_signals["signal_event_count"],
                "symbols": report["symbols"],
                "perfect_positive_pattern_count": report["perfect_positive_pattern_count"],
                "perfect_planned_profit_pattern_count": report[
                    "perfect_planned_profit_pattern_count"
                ],
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
