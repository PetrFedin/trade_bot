from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

from app.marketdata.bybit_public_archive import (
    BybitArchiveAcquisition,
    BybitPublicTradeArchiveClient,
    completed_archive_dates,
)
from app.marketdata.bybit_v5 import BybitKlineAcquisition
from app.strategy.crypto_signal_first_touch_audit import (
    CryptoSignalFirstTouchPolicy,
    audit_crypto_plan_eligible_first_touch,
)
from tools.replay_bybit_crypto import default_crypto_config

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


def run_bybit_signal_first_touch_audit(
    *,
    symbols: tuple[str, ...] = _DEFAULT_SYMBOLS,
    lookback_days: int = 14,
    reference_equity_usdt: Decimal = Decimal("1000"),
    policy: CryptoSignalFirstTouchPolicy | None = None,
    now: datetime | None = None,
    archive_workers: int = 4,
) -> dict[str, object]:
    if lookback_days < 1:
        raise ValueError("first-touch lookback must be positive")
    cutoff = datetime.now(UTC) if now is None else now
    dates = completed_archive_dates(now=cutoff, lookback_days=lookback_days)
    acquisition = _fetch_archives_by_symbol(
        symbols=symbols,
        dates=dates,
        interval_minutes=5,
        archive_workers=archive_workers,
    )
    acquisition.validate(requested_symbols=symbols, minimum_bars=60)
    config = default_crypto_config().with_target(Decimal("20"))
    report = audit_crypto_plan_eligible_first_touch(
        acquisition.klines,
        strategy_config=config,
        reference_equity_usdt=reference_equity_usdt,
        policy=policy,
    )
    report.update(
        source="BYBIT_OFFICIAL_PUBLIC_TRADE_ARCHIVE_AGGREGATED_5M",
        archive_dates=[value.isoformat() for value in dates],
        archive_completed_utc_days_only=True,
        archive_download_workers=archive_workers,
        requested_symbols=list(symbols),
        target_net_profit_usd=20.0,
        current_incomplete_bar_excluded=True,
        funding_costs_modeled=False,
        real_demo_fills=False,
        historical_observation_is_not_future_guarantee=True,
    )
    return report


def _fetch_archives_by_symbol(
    *,
    symbols: tuple[str, ...],
    dates: tuple[date, ...],
    interval_minutes: int,
    archive_workers: int,
) -> BybitArchiveAcquisition:
    if not 1 <= archive_workers <= 8:
        raise ValueError("first-touch archive workers must be within [1, 8]")

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
        description="Audit first target/stop touch for plan-eligible Bybit crypto signals"
    )
    parser.add_argument("--symbols", default=",".join(_DEFAULT_SYMBOLS))
    parser.add_argument("--lookback-days", type=int, default=14)
    parser.add_argument("--reference-equity", default="1000")
    parser.add_argument("--horizon-minutes", type=int, default=240)
    parser.add_argument("--minimum-pattern-observations", type=int, default=5)
    parser.add_argument("--sample-sufficient-observations", type=int, default=30)
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
    policy = CryptoSignalFirstTouchPolicy(
        horizon_minutes=args.horizon_minutes,
        minimum_pattern_observations=args.minimum_pattern_observations,
        sample_sufficient_observations=args.sample_sufficient_observations,
        minimum_cross_symbol_count=args.minimum_cross_symbol_count,
    )
    report = run_bybit_signal_first_touch_audit(
        symbols=symbols,
        lookback_days=args.lookback_days,
        reference_equity_usdt=Decimal(args.reference_equity),
        policy=policy,
        archive_workers=args.archive_workers,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        "BYBIT_SIGNAL_FIRST_TOUCH_AUDIT="
        + json.dumps(
            {
                "archive_dates": report["archive_dates"],
                "plan_eligible_signal_count": report["plan_eligible_signal_count"],
                "aggregate": report["aggregate"],
                "by_symbol": report["by_symbol"],
                "by_clarity_band": report["by_clarity_band"],
                "perfect_target_first_pattern_count": report[
                    "perfect_target_first_pattern_count"
                ],
                "perfect_target_first_patterns": report[
                    "retrospective_perfect_target_first_patterns"
                ],
                "qualified_pattern_rows": report["qualified_pattern_rows"],
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
