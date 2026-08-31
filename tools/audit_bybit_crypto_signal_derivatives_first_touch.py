from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.marketdata.bybit_derivatives_history import (
    BybitDerivativesHistory,
    BybitHistoricalDerivativesClient,
)
from app.strategy.crypto_signal_derivatives_first_touch import (
    CryptoSignalDerivativesFirstTouchPolicy,
    audit_crypto_signal_derivatives_first_touch,
)
from tools.audit_bybit_crypto_signal_first_touch import (
    _DEFAULT_SYMBOLS,
    run_bybit_signal_first_touch_audit,
)
from tools.replay_bybit_crypto import default_crypto_config

_DERIVATIVES_INTERVAL = "1h"
_DERIVATIVES_WARMUP = timedelta(days=1)


def run_bybit_signal_derivatives_first_touch_audit(
    *,
    symbols: tuple[str, ...] = _DEFAULT_SYMBOLS,
    lookback_days: int = 14,
    reference_equity_usdt: Decimal = Decimal("1000"),
    policy: CryptoSignalDerivativesFirstTouchPolicy | None = None,
    now: datetime | None = None,
    archive_workers: int = 4,
    derivatives_workers: int = 4,
) -> dict[str, object]:
    """Run the frozen first-touch audit plus public pre-entry derivatives evidence."""

    cutoff = datetime.now(UTC) if now is None else now
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("derivatives first-touch cutoff must be timezone-aware")
    cutoff = cutoff.astimezone(UTC)
    first_touch = run_bybit_signal_first_touch_audit(
        symbols=symbols,
        lookback_days=lookback_days,
        reference_equity_usdt=reference_equity_usdt,
        now=cutoff,
        archive_workers=archive_workers,
    )
    raw_rows = first_touch.get("outcome_rows")
    if not isinstance(raw_rows, list) or not raw_rows:
        raise ValueError("derivatives first-touch requires non-empty first-touch outcomes")
    decision_times = tuple(
        _parse_time(row.get("decision_time"))
        for row in raw_rows
        if isinstance(row, dict)
    )
    if len(decision_times) != len(raw_rows):
        raise ValueError("derivatives first-touch outcome row is invalid")
    start_at = min(decision_times) - _DERIVATIVES_WARMUP
    end_at = max(decision_times)
    histories = _fetch_derivatives_by_symbol(
        symbols=symbols,
        start_at=start_at,
        end_at=end_at,
        workers=derivatives_workers,
    )
    config = default_crypto_config().with_target(Decimal("20"))
    report = audit_crypto_signal_derivatives_first_touch(
        first_touch,
        histories,
        strategy_config=config,
        policy=policy,
    )
    report.update(
        price_source=first_touch["source"],
        derivatives_source="BYBIT_V5_PUBLIC_DERIVATIVES_HISTORY",
        derivatives_interval=_DERIVATIVES_INTERVAL,
        derivatives_warmup_hours=int(_DERIVATIVES_WARMUP.total_seconds() // 3600),
        derivatives_download_workers=derivatives_workers,
        archive_dates=first_touch["archive_dates"],
        archive_completed_utc_days_only=True,
        requested_symbols=list(symbols),
        target_net_profit_usd=20.0,
        reference_equity_usdt=float(reference_equity_usdt),
        current_incomplete_bar_excluded=True,
        authenticated_bybit_request_performed=False,
        real_demo_fills=False,
        historical_observation_is_not_future_guarantee=True,
    )
    return report


def _fetch_derivatives_by_symbol(
    *,
    symbols: tuple[str, ...],
    start_at: datetime,
    end_at: datetime,
    workers: int,
) -> dict[str, BybitDerivativesHistory]:
    if not 1 <= workers <= 8:
        raise ValueError("derivatives first-touch workers must be within [1, 8]")
    if start_at.tzinfo is None or start_at.utcoffset() is None:
        raise ValueError("derivatives first-touch start must be timezone-aware")
    if end_at.tzinfo is None or end_at.utcoffset() is None:
        raise ValueError("derivatives first-touch end must be timezone-aware")
    start_ms = int(start_at.astimezone(UTC).timestamp() * 1000)
    end_ms = int(end_at.astimezone(UTC).timestamp() * 1000)
    if end_ms <= start_ms:
        raise ValueError("derivatives first-touch history range is empty")

    def fetch_one(symbol: str) -> tuple[str, BybitDerivativesHistory]:
        history = BybitHistoricalDerivativesClient().fetch_history(
            symbol=symbol,
            start_ms=start_ms,
            end_ms=end_ms,
            interval=_DERIVATIVES_INTERVAL,
        )
        history.validate()
        return symbol, history

    pool_size = min(workers, len(symbols))
    with ThreadPoolExecutor(max_workers=pool_size) as executor:
        return dict(executor.map(fetch_one, symbols))


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("derivatives first-touch decision time must be text")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("derivatives first-touch decision time must be timezone-aware")
    return parsed.astimezone(UTC)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit Bybit first-touch signals with pre-entry derivatives evidence"
    )
    parser.add_argument("--symbols", default=",".join(_DEFAULT_SYMBOLS))
    parser.add_argument("--lookback-days", type=int, default=14)
    parser.add_argument("--reference-equity", default="1000")
    parser.add_argument("--minimum-pattern-observations", type=int, default=5)
    parser.add_argument("--sample-sufficient-observations", type=int, default=30)
    parser.add_argument("--minimum-cross-symbol-count", type=int, default=2)
    parser.add_argument("--minimum-distinct-days", type=int, default=3)
    parser.add_argument("--archive-workers", type=int, default=4)
    parser.add_argument("--derivatives-workers", type=int, default=4)
    parser.add_argument("--output", type=Path, required=True)
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    symbols = tuple(
        symbol.strip().upper()
        for symbol in args.symbols.split(",")
        if symbol.strip()
    )
    policy = CryptoSignalDerivativesFirstTouchPolicy(
        minimum_pattern_observations=args.minimum_pattern_observations,
        sample_sufficient_observations=args.sample_sufficient_observations,
        minimum_cross_symbol_count=args.minimum_cross_symbol_count,
        minimum_distinct_days=args.minimum_distinct_days,
    )
    report = run_bybit_signal_derivatives_first_touch_audit(
        symbols=symbols,
        lookback_days=args.lookback_days,
        reference_equity_usdt=Decimal(args.reference_equity),
        policy=policy,
        archive_workers=args.archive_workers,
        derivatives_workers=args.derivatives_workers,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(
        "BYBIT_SIGNAL_DERIVATIVES_FIRST_TOUCH_AUDIT="
        + json.dumps(
            {
                "archive_dates": report["archive_dates"],
                "raw_signal_count": report["raw_signal_count"],
                "independent_episode_count": report["independent_episode_count"],
                "complete_derivatives_episode_count": report[
                    "complete_derivatives_episode_count"
                ],
                "episode_aggregate": report["episode_aggregate"],
                "by_open_interest_regime": report["by_open_interest_regime"],
                "by_crowding_regime": report["by_crowding_regime"],
                "by_prior_funding_regime": report["by_prior_funding_regime"],
                "by_stress_regime": report["by_stress_regime"],
                "perfect_transferable_pattern_count": report[
                    "perfect_transferable_pattern_count"
                ],
                "perfect_exact_cell_count": report["perfect_exact_cell_count"],
                "oos_ready_retrospective_exact_cell_count": report[
                    "oos_ready_retrospective_exact_cell_count"
                ],
                "output": str(args.output),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
