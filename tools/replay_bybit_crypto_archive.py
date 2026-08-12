from __future__ import annotations

import argparse
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.marketdata.bybit_public_archive import (
    BybitPublicTradeArchiveClient,
    completed_archive_dates,
)
from tools.replay_bybit_crypto import (
    default_crypto_config,
    replay_acquisition,
    write_trade_csvs,
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
_DEFAULT_TARGETS = (Decimal("15"), Decimal("20"), Decimal("25"))


def acquire_archive_and_replay(
    *,
    symbols: tuple[str, ...] = _DEFAULT_SYMBOLS,
    lookback_days: int = 7,
    opening_equity_usdt: Decimal = Decimal("1000"),
    targets_usd: tuple[Decimal, ...] = _DEFAULT_TARGETS,
    now: datetime | None = None,
    client: BybitPublicTradeArchiveClient | None = None,
) -> dict[str, object]:
    cutoff = datetime.now(UTC) if now is None else now
    dates = completed_archive_dates(now=cutoff, lookback_days=lookback_days)
    archive_client = BybitPublicTradeArchiveClient() if client is None else client
    acquisition = archive_client.fetch_klines(
        symbols=symbols,
        dates=dates,
        interval_minutes=5,
    )
    acquisition.validate(requested_symbols=symbols, minimum_bars=25)
    report = replay_acquisition(
        acquisition.klines,
        opening_equity_usdt=opening_equity_usdt,
        targets_usd=targets_usd,
        interval="5",
    )
    three_x_candidate = replay_acquisition(
        acquisition.klines,
        opening_equity_usdt=opening_equity_usdt,
        targets_usd=targets_usd,
        base_config=replace(
            default_crypto_config(),
            maximum_notional_to_equity=Decimal("3.0"),
        ),
        interval="5",
    )
    report.update(
        source="BYBIT_OFFICIAL_PUBLIC_TRADE_ARCHIVE_AGGREGATED_5M",
        evidence_scope="BYBIT_OFFICIAL_ARCHIVE_COMPLETED_UTC_DAYS_COUNTERFACTUAL_REPLAY",
        archive_dates=[value.isoformat() for value in dates],
        archive_files_by_symbol={
            symbol: list(urls) for symbol, urls in acquisition.files_by_symbol.items()
        },
        archive_trade_rows_by_symbol=acquisition.trade_rows_by_symbol,
        archive_completed_utc_days_only=True,
        current_incomplete_bar_excluded=True,
        raw_trade_archive_committed_to_repository=False,
        rest_api_required=False,
        notional_cap_shadow_candidates={
            "MAX_NOTIONAL_3X_EQUITY": {
                "maximum_notional_to_equity": 3.0,
                "risk_fraction_per_trade": float(
                    three_x_candidate["strategy"]["risk_fraction_per_trade"]
                ),
                "variants": three_x_candidate["variants"],
                "strategy_promotion_allowed": False,
                "bybit_demo_order_writes_enabled": False,
                "bybit_live_order_routing_allowed": False,
                "purpose": (
                    "Test whether a larger notional cap unlocks $20/$25 net-edge while "
                    "holding the same cost-aware per-trade risk budget."
                ),
            }
        },
    )
    report["limitations"] = [
        *report["limitations"],
        "Five-minute OHLCV is aggregated from Bybit's official public trade archives; "
        "order-book depth and queue position are not reconstructed.",
        "Only fully completed UTC archive days are included, so the current UTC day is "
        "intentionally absent from this historical qualification run.",
        "The 3x notional-cap run is a predeclared shadow candidate with unchanged 1% "
        "cost-aware risk budgeting; it is not permission to use 3x leverage in demo or live.",
    ]
    return report


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Replay crypto strategy from official Bybit public trade archives"
    )
    parser.add_argument("--symbols", default=",".join(_DEFAULT_SYMBOLS))
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--opening-equity", default="1000")
    parser.add_argument("--targets", default="15,20,25")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--trades-dir", type=Path)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    symbols = tuple(
        symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()
    )
    targets = tuple(
        Decimal(value.strip()) for value in args.targets.split(",") if value.strip()
    )
    report = acquire_archive_and_replay(
        symbols=symbols,
        lookback_days=args.lookback_days,
        opening_equity_usdt=Decimal(args.opening_equity),
        targets_usd=targets,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    if args.trades_dir is not None:
        write_trade_csvs(report, args.trades_dir)
    print(
        json.dumps(
            {
                "qualification": report["qualification"],
                "source": report["source"],
                "output": str(args.output),
            }
        )
    )


if __name__ == "__main__":
    main()