from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from app.marketdata.bybit_public_archive import (
    BybitPublicTradeArchiveClient,
    completed_archive_dates,
)
from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar
from app.strategy.crypto_execution_risk import (
    CryptoExecutionRiskPolicy,
    resize_trade_plan_at_next_open,
)
from app.strategy.crypto_perp import (
    CryptoPerpStrategyConfig,
    CryptoSide,
    build_trade_plan,
    rank_crypto_signals,
)
from tools.replay_bybit_crypto import default_crypto_config

_ZERO = Decimal("0")
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


def audit_next_open_quality(
    acquisition: BybitKlineAcquisition,
    *,
    equity_usdt: Decimal = Decimal("1000"),
    config: CryptoPerpStrategyConfig | None = None,
) -> dict[str, Any]:
    """Measure completed-close to next-open gap quality without choosing a threshold."""

    if equity_usdt <= 0:
        raise ValueError("next-open quality audit equity must be positive")
    active = default_crypto_config() if config is None else config
    active = active.with_target(Decimal("20"))
    active.validate()
    execution_policy = CryptoExecutionRiskPolicy()

    grouped = _bars_by_symbol_time(acquisition.bars)
    common_times = _common_times(grouped)
    histories: dict[str, list[BybitKlineBar]] = {
        symbol: [] for symbol in sorted(grouped)
    }
    directional_gap_atr: list[Decimal] = []
    absolute_gap_atr: list[Decimal] = []
    chase_gap_atr: list[Decimal] = []
    adverse_gap_atr: list[Decimal] = []
    execution_resize_count = 0
    execution_block_count = 0
    block_reasons: Counter[str] = Counter()
    plan_count = 0

    for index, timestamp in enumerate(common_times[:-1]):
        for symbol, rows in grouped.items():
            histories[symbol].append(rows[timestamp])
        next_time = common_times[index + 1]
        rankings = rank_crypto_signals(histories, active)
        for evaluation in rankings:
            signal = evaluation.signal
            if signal is None:
                continue
            plan_evaluation = build_trade_plan(
                signal,
                equity_usdt=equity_usdt,
                config=active,
            )
            if not plan_evaluation.eligible or plan_evaluation.plan is None:
                continue
            plan_count += 1
            next_open = grouped[signal.symbol][next_time].open
            raw_gap = next_open / signal.reference_price - Decimal("1")
            directional_gap = raw_gap if signal.side is CryptoSide.LONG else -raw_gap
            gap_atr = directional_gap / signal.atr_fraction
            directional_gap_atr.append(gap_atr)
            absolute_gap_atr.append(abs(gap_atr))
            if gap_atr > 0:
                chase_gap_atr.append(gap_atr)
            elif gap_atr < 0:
                adverse_gap_atr.append(-gap_atr)

            execution = resize_trade_plan_at_next_open(
                plan_evaluation.plan,
                raw_next_open_price=next_open,
                strategy_config=active,
                policy=execution_policy,
            )
            if execution.resized:
                execution_resize_count += 1
            if not execution.eligible:
                execution_block_count += 1
                block_reasons.update(execution.reasons)

    return {
        "qualification": "BYBIT_CRYPTO_NEXT_OPEN_QUALITY_AUDIT",
        "eligible_trade_plan_count": plan_count,
        "directional_gap_atr": _distribution(directional_gap_atr),
        "absolute_gap_atr": _distribution(absolute_gap_atr),
        "chase_gap_atr": _distribution(chase_gap_atr),
        "adverse_gap_atr": _distribution(adverse_gap_atr),
        "execution_risk_resize_count": execution_resize_count,
        "execution_risk_block_count": execution_block_count,
        "execution_risk_block_reasons": dict(block_reasons),
        "directional_gap_contract": (
            "positive = next open moved farther in signal direction; negative = next open moved "
            "against signal direction; value is normalized by completed-bar ATR fraction"
        ),
        "gap_threshold_selected": False,
        "automatic_execution_gate_activation_allowed": False,
        "strategy_promotion_allowed": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
    }


def acquire_archive_and_audit_next_open_quality(
    *,
    symbols: tuple[str, ...] = _DEFAULT_SYMBOLS,
    lookback_days: int = 14,
    equity_usdt: Decimal = Decimal("1000"),
    client: BybitPublicTradeArchiveClient | None = None,
) -> dict[str, Any]:
    if lookback_days < 1:
        raise ValueError("next-open quality lookback_days must be positive")
    dates = completed_archive_dates(lookback_days=lookback_days)
    archive = BybitPublicTradeArchiveClient() if client is None else client
    acquisition = archive.fetch_klines(
        symbols=symbols,
        dates=dates,
        interval_minutes=5,
    )
    acquisition.validate(requested_symbols=symbols, minimum_bars=25)
    report = audit_next_open_quality(
        acquisition.klines,
        equity_usdt=equity_usdt,
    )
    report.update(
        source="BYBIT_OFFICIAL_PUBLIC_TRADE_ARCHIVE_AGGREGATED_5M",
        archive_dates=[value.isoformat() for value in dates],
        symbols=list(symbols),
        archive_completed_utc_days_only=True,
        raw_trade_archive_committed_to_repository=False,
    )
    return report


def _distribution(values: list[Decimal]) -> dict[str, float | int | None]:
    if not values:
        return {
            "count": 0,
            "mean": None,
            "p50": None,
            "p90": None,
            "p95": None,
            "max": None,
        }
    ordered = sorted(values)
    return {
        "count": len(ordered),
        "mean": float(sum(ordered, start=_ZERO) / Decimal(len(ordered))),
        "p50": float(_nearest_rank(ordered, Decimal("0.50"))),
        "p90": float(_nearest_rank(ordered, Decimal("0.90"))),
        "p95": float(_nearest_rank(ordered, Decimal("0.95"))),
        "max": float(ordered[-1]),
    }


def _nearest_rank(values: list[Decimal], quantile: Decimal) -> Decimal:
    if not values:
        raise ValueError("nearest-rank requires values")
    if not _ZERO < quantile <= Decimal("1"):
        raise ValueError("nearest-rank quantile must be within (0, 1]")
    rank = int((Decimal(len(values)) * quantile).to_integral_value(rounding="ROUND_CEILING"))
    return values[max(0, rank - 1)]


def _bars_by_symbol_time(
    bars: tuple[BybitKlineBar, ...],
) -> dict[str, dict[datetime, BybitKlineBar]]:
    grouped: dict[str, dict[datetime, BybitKlineBar]] = defaultdict(dict)
    for bar in bars:
        if bar.start_time in grouped[bar.symbol]:
            raise ValueError("next-open quality duplicate symbol timestamp")
        grouped[bar.symbol][bar.start_time] = bar
    return dict(grouped)


def _common_times(
    grouped: dict[str, dict[datetime, BybitKlineBar]],
) -> tuple[datetime, ...]:
    if not grouped:
        return ()
    return tuple(sorted(set.intersection(*(set(rows) for rows in grouped.values()))))


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Audit Bybit crypto next-open signal quality")
    parser.add_argument("--symbols", default=",".join(_DEFAULT_SYMBOLS))
    parser.add_argument("--lookback-days", type=int, default=14)
    parser.add_argument("--equity", default="1000")
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    symbols = tuple(
        symbol.strip().upper() for symbol in args.symbols.split(",") if symbol.strip()
    )
    report = acquire_archive_and_audit_next_open_quality(
        symbols=symbols,
        lookback_days=args.lookback_days,
        equity_usdt=Decimal(args.equity),
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print("BYBIT_NEXT_OPEN_QUALITY=" + json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
