from __future__ import annotations

import argparse
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Protocol

from app.marketdata.bybit_public_archive import (
    BybitPublicTradeArchiveClient,
    completed_archive_dates,
)
from app.marketdata.bybit_v5 import BybitKlineAcquisition
from tools.qualify_bybit_crypto_walk_forward import (
    CryptoWalkForwardPolicy,
    run_crypto_walk_forward,
)

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


class _ArchiveAcquisition(Protocol):
    klines: BybitKlineAcquisition

    def validate(
        self,
        *,
        requested_symbols: tuple[str, ...],
        minimum_bars: int,
    ) -> None: ...


class _ArchiveClient(Protocol):
    def fetch_klines(
        self,
        *,
        symbols: tuple[str, ...],
        dates: tuple[date, ...],
        interval_minutes: int,
    ) -> _ArchiveAcquisition: ...


def acquire_archive_and_run_walk_forward(
    *,
    symbols: tuple[str, ...] = _DEFAULT_SYMBOLS,
    lookback_days: int = 28,
    opening_equity_usdt: Decimal = Decimal("1000"),
    policy: CryptoWalkForwardPolicy | None = None,
    client: _ArchiveClient | None = None,
) -> dict[str, object]:
    """Acquire completed Bybit archive days once, validate wrapper, then run cold-start folds."""

    active = CryptoWalkForwardPolicy() if policy is None else policy
    active.validate()
    minimum_days = active.fold_days * active.minimum_folds
    if lookback_days < minimum_days:
        raise ValueError(
            f"walk-forward requires at least {minimum_days} completed archive days"
        )
    if opening_equity_usdt <= 0:
        raise ValueError("walk-forward opening equity must be positive")
    dates = completed_archive_dates(lookback_days=lookback_days)
    archive_client: _ArchiveClient = (
        BybitPublicTradeArchiveClient() if client is None else client
    )
    acquisition = archive_client.fetch_klines(
        symbols=symbols,
        dates=dates,
        interval_minutes=5,
    )
    acquisition.validate(requested_symbols=symbols, minimum_bars=25)
    report = run_crypto_walk_forward(
        acquisition.klines,
        opening_equity_usdt=opening_equity_usdt,
        policy=active,
    )
    report.update(
        source="BYBIT_OFFICIAL_PUBLIC_TRADE_ARCHIVE_AGGREGATED_5M",
        requested_archive_dates=[value.isoformat() for value in dates],
        symbols=list(symbols),
        archive_completed_utc_days_only=True,
        raw_trade_archive_committed_to_repository=False,
        strategy_promotion_allowed=False,
        demo_observation_allowed=False,
        live_promotion_allowed=False,
        bybit_live_order_routing_allowed=False,
    )
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Acquire official Bybit archives and run fixed-parameter crypto walk-forward"
    )
    parser.add_argument("--lookback-days", type=int, default=28)
    parser.add_argument("--fold-days", type=int, default=7)
    parser.add_argument("--symbols", default=",".join(_DEFAULT_SYMBOLS))
    parser.add_argument("--opening-equity", default="1000")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    symbols = tuple(
        symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()
    )
    report = acquire_archive_and_run_walk_forward(
        symbols=symbols,
        lookback_days=args.lookback_days,
        opening_equity_usdt=Decimal(args.opening_equity),
        policy=CryptoWalkForwardPolicy(fold_days=args.fold_days),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("BYBIT_CRYPTO_WALK_FORWARD=" + json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
