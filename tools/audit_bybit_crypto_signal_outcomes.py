from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.marketdata.bybit_public_archive import BybitPublicTradeArchiveClient, completed_archive_dates
from app.strategy.crypto_historical_diagnostics import build_crypto_historical_trade_conditions
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
) -> dict[str, object]:
    cutoff = datetime.now(UTC) if now is None else now
    dates = completed_archive_dates(now=cutoff, lookback_days=lookback_days)
    client = BybitPublicTradeArchiveClient()
    acquisition = client.fetch_klines(symbols=symbols, dates=dates, interval_minutes=5)
    acquisition.validate(requested_symbols=symbols, minimum_bars=25)

    config = default_crypto_config().with_target(target_usd)
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
        symbols=list(symbols),
        target_net_profit_usd=float(target_usd),
        opening_equity_usdt=float(opening_equity_usdt),
        eligible_signal_event_count=variant["eligible_signal_event_count"],
        accepted_trade_plan_event_count=variant["accepted_trade_plan_event_count"],
        replay_metrics=variant["metrics"],
        current_incomplete_bar_excluded=True,
        funding_costs_modeled=False,
        real_demo_fills=False,
        historical_observation_is_not_future_guarantee=True,
    )
    return audit


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit historical outcomes of frozen Bybit signals")
    parser.add_argument("--symbols", default=",".join(_DEFAULT_SYMBOLS))
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--opening-equity", default="1000")
    parser.add_argument("--target", default="20")
    parser.add_argument("--minimum-pattern-trades", type=int, default=5)
    parser.add_argument("--sample-sufficient-trades", type=int, default=30)
    parser.add_argument("--minimum-cross-symbol-count", type=int, default=2)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    symbols = tuple(symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip())
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
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(
        "BYBIT_SIGNAL_OUTCOME_AUDIT="
        + json.dumps(
            {
                "trade_count": report["trade_count"],
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
