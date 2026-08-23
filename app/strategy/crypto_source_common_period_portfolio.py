from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from app.marketdata.bybit_derivatives_history import BybitDerivativesHistory
from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar
from app.strategy.crypto_derivatives_context import build_crypto_trade_derivatives_context
from app.strategy.crypto_historical_diagnostics import build_crypto_historical_trade_conditions
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_profit_runner import CryptoProfitRunnerPolicy
from app.strategy.crypto_runner_admission import CryptoRunnerAdmissionPolicy
from app.strategy.crypto_source_common_period_evidence import ArchivedBybitDerivativesHistoryView
from app.strategy.crypto_strategy_evidence_matrix import (
    build_crypto_strategy_evidence_rows,
    build_crypto_trade_execution_economics,
    diagnose_crypto_strategy_evidence_matrix,
)
from app.strategy.crypto_trade_management import CryptoProtectionPolicy
from tools.replay_bybit_crypto_runner import replay_open_ended_crypto_runner

_FIVE_MINUTES = timedelta(minutes=5)
_FIVE_MINUTES_MS = 5 * 60 * 1000
_RUNNER_EDGE_MULTIPLE = Decimal("1.50")
_ZERO = Decimal("0")


def run_source_common_period_portfolio_replay(
    *,
    ordered_symbols: Sequence[str],
    bars_by_symbol: Mapping[str, Sequence[BybitKlineBar]],
    common_start_at: datetime,
    end_exclusive_at: datetime,
    opening_equity_usdt: Decimal = Decimal("1000"),
    derivatives_history_by_symbol: Mapping[
        str, ArchivedBybitDerivativesHistoryView
    ] | None = None,
    strategy_config: CryptoPerpStrategyConfig | None = None,
) -> dict[str, Any]:
    """Replay the fixed strategy with one shared capital pool over a synchronized universe.

    Historical selection is driven only by the canonical point-in-time signal ranking inside
    ``replay_open_ended_crypto_runner``. Retrospective derivatives/evidence attribution is built
    only after the replay is complete and cannot change which historical trades were accepted.
    """

    symbols = _validate_ordered_symbols(ordered_symbols)
    start = _utc(common_start_at)
    end = _utc(end_exclusive_at)
    if end <= start:
        raise ValueError("source-common portfolio interval is invalid")
    if not opening_equity_usdt.is_finite() or opening_equity_usdt <= 0:
        raise ValueError("source-common portfolio opening equity must be positive and finite")
    config = CryptoPerpStrategyConfig() if strategy_config is None else strategy_config
    config.validate()
    if config != CryptoPerpStrategyConfig():
        raise ValueError("source-common portfolio requires the qualified fixed strategy config")

    first_bar_at = _ceil_five_minutes(start)
    interval_seconds = (end - first_bar_at).total_seconds()
    if first_bar_at >= end or interval_seconds % _FIVE_MINUTES.total_seconds() != 0:
        raise ValueError("source-common portfolio interval is not an exact 5m grid")
    expected_count = int(interval_seconds / _FIVE_MINUTES.total_seconds())
    if expected_count < 60:
        raise ValueError("source-common portfolio requires at least 60 synchronized bars")

    if set(bars_by_symbol) != set(symbols):
        raise ValueError("source-common portfolio bars do not match selected symbols")
    normalized: dict[str, tuple[BybitKlineBar, ...]] = {}
    for symbol in symbols:
        rows = tuple(bars_by_symbol[symbol])
        _validate_symbol_grid(
            symbol,
            rows,
            first_bar_at=first_bar_at,
            expected_count=expected_count,
        )
        normalized[symbol] = rows

    acquisition = BybitKlineAcquisition(
        bars=tuple(
            bar
            for symbol in sorted(symbols)
            for bar in normalized[symbol]
        ),
        pages_by_symbol={symbol: 1 for symbol in symbols},
    )
    acquisition.validate(
        requested_symbols=tuple(sorted(symbols)),
        minimum_bars=60,
    )
    replay = replay_open_ended_crypto_runner(
        acquisition,
        opening_equity_usdt=opening_equity_usdt,
        base_config=config,
        protection_policy=CryptoProtectionPolicy(),
        runner_policy=CryptoProfitRunnerPolicy(),
        runner_admission_policy=CryptoRunnerAdmissionPolicy(
            minimum_expected_edge_multiple=_RUNNER_EDGE_MULTIPLE
        ),
        interval="5",
    )
    _validate_replay_safety(replay)

    closed_trades = replay.get("closed_trades")
    if not isinstance(closed_trades, list):
        raise ValueError("source-common portfolio replay is missing closed trades")
    decision_events = replay.get("decision_events")
    if not isinstance(decision_events, list):
        raise ValueError("source-common portfolio replay is missing decision events")
    metrics = replay.get("metrics")
    if not isinstance(metrics, Mapping):
        raise ValueError("source-common portfolio replay is missing metrics")
    plan_blocks = replay.get("trade_plan_block_reason_counts")
    if not isinstance(plan_blocks, Mapping):
        raise ValueError("source-common portfolio replay is missing plan block counts")

    per_symbol = _per_symbol_trade_summary(closed_trades, symbols)
    max_initial_notional = _maximum_initial_gross_notional(closed_trades)
    entry_event_count = sum(
        isinstance(event, Mapping) and event.get("event") == "ENTRY"
        for event in decision_events
    )
    concurrency_blocks = _non_negative_int(
        plan_blocks.get("CONCURRENCY_LIMIT", 0),
        field="CONCURRENCY_LIMIT",
    )
    eligible_signal_count = _non_negative_int(
        replay.get("eligible_signal_event_count", 0),
        field="eligible_signal_event_count",
    )
    accepted_plan_count = _non_negative_int(
        replay.get("accepted_trade_plan_event_count", 0),
        field="accepted_trade_plan_event_count",
    )

    evidence_report: dict[str, Any] | None = None
    if derivatives_history_by_symbol is not None:
        if set(derivatives_history_by_symbol) != set(symbols):
            raise ValueError("source-common portfolio derivatives do not match selected symbols")
        compatible: dict[str, BybitDerivativesHistory] = {}
        for symbol in symbols:
            history = derivatives_history_by_symbol[symbol]
            history.validate()
            compatible[symbol] = cast(BybitDerivativesHistory, history)
        conditions = build_crypto_historical_trade_conditions(
            acquisition,
            replay,
            strategy_config=config,
        )
        derivatives = build_crypto_trade_derivatives_context(replay, compatible)
        economics = build_crypto_trade_execution_economics(
            replay,
            strategy_config=config,
        )
        rows = build_crypto_strategy_evidence_rows(
            conditions,
            derivatives,
            economics,
            strategy_config=config,
        )
        if not (len(rows) == len(conditions) == len(derivatives) == len(economics)):
            raise ValueError("source-common portfolio evidence joins are incomplete")
        evidence_report = diagnose_crypto_strategy_evidence_matrix(rows)
        evidence_report["evidence_scope"] = (
            "ACTUAL_SHARED_CAPITAL_PORTFOLIO_TRADES_POST_REPLAY_ATTRIBUTION"
        )
        evidence_report["historical_selection_uses_future_evidence"] = False
        evidence_report["evidence_used_for_historical_selection"] = False
        evidence_report["portfolio_competition_modeled"] = True
        evidence_report["trade_actionable"] = False
        evidence_report["operator_review_required"] = True
        evidence_report["strategy_parameters_changed"] = False
        evidence_report["parameter_retuning_performed"] = False
        evidence_report["strategy_selection_allowed"] = False
        evidence_report["strategy_promotion_allowed"] = False
        evidence_report["demo_activation_allowed"] = False
        evidence_report["live_activation_allowed"] = False
        evidence_report["bybit_live_order_routing_allowed"] = False
        evidence_report["causal_claim_allowed"] = False
        evidence_report["predictive_guarantee_allowed"] = False

    total_net_pnl = _decimal_metric(metrics, "total_net_pnl_usdt")
    final_equity = _decimal_metric(metrics, "final_equity_usdt")
    return {
        "diagnostic": "BYBIT_SOURCE_COMMON_PERIOD_SHARED_CAPITAL_PORTFOLIO_REPLAY",
        "ordered_symbols_at_research_time": list(symbols),
        "common_start_at": start.isoformat(),
        "first_synchronized_bar_at": first_bar_at.isoformat(),
        "end_exclusive_at": end.isoformat(),
        "synchronized_bar_count_per_symbol": expected_count,
        "opening_equity_usdt": str(opening_equity_usdt),
        "final_equity_usdt": str(final_equity),
        "total_net_pnl_usdt": str(total_net_pnl),
        "portfolio_metrics": dict(metrics),
        "per_symbol": per_symbol,
        "eligible_signal_event_count": eligible_signal_count,
        "accepted_trade_plan_event_count": accepted_plan_count,
        "entry_event_count": entry_event_count,
        "concurrency_block_count": concurrency_blocks,
        "concurrency_block_fraction_of_eligible_signals": (
            None
            if eligible_signal_count == 0
            else str(Decimal(concurrency_blocks) / Decimal(eligible_signal_count))
        ),
        "maximum_initial_gross_notional_usdt": str(max_initial_notional),
        "maximum_initial_gross_notional_to_opening_equity": str(
            max_initial_notional / opening_equity_usdt
        ),
        "replay": replay,
        "portfolio_trade_evidence_matrix": evidence_report,
        "portfolio_competition_modeled": True,
        "shared_capital_modeled": True,
        "historical_selection_contract": (
            "completed-bar fixed-strategy rank -> bounded shared slots -> next-bar-open execution"
        ),
        "historical_selection_uses_future_evidence": False,
        "evidence_used_for_historical_selection": False,
        "selection_uses_point_in_time_price_signal_only": True,
        "derivatives_used_for_post_replay_attribution_only": (
            derivatives_history_by_symbol is not None
        ),
        "strategy_parameters_changed": False,
        "parameter_retuning_performed": False,
        "strategy_selection_allowed": False,
        "strategy_promotion_allowed": False,
        "trade_actionable": False,
        "operator_review_required": True,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
        "causal_claim_allowed": False,
        "predictive_guarantee_allowed": False,
    }


def _validate_symbol_grid(
    symbol: str,
    bars: Sequence[BybitKlineBar],
    *,
    first_bar_at: datetime,
    expected_count: int,
) -> None:
    if len(bars) != expected_count:
        raise ValueError(
            "source-common portfolio price grid count mismatch:"
            f"{symbol}:actual={len(bars)}:expected={expected_count}"
        )
    for index, bar in enumerate(bars):
        bar.validate()
        if bar.symbol != symbol:
            raise ValueError("source-common portfolio price grid contains another symbol")
        expected = first_bar_at + index * _FIVE_MINUTES
        if bar.start_time.astimezone(UTC) != expected:
            raise ValueError(
                "source-common portfolio price grid timestamp mismatch:"
                f"{symbol}:actual={bar.start_time.isoformat()}:expected={expected.isoformat()}"
            )


def _per_symbol_trade_summary(
    trades: Sequence[Any],
    ordered_symbols: Sequence[str],
) -> list[dict[str, Any]]:
    by_symbol: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for raw in trades:
        if not isinstance(raw, Mapping):
            raise ValueError("source-common portfolio closed trade must be an object")
        symbol = raw.get("symbol")
        if symbol not in ordered_symbols:
            raise ValueError("source-common portfolio trade contains unknown symbol")
        by_symbol[str(symbol)].append(raw)

    summaries: list[dict[str, Any]] = []
    for market_rank, symbol in enumerate(ordered_symbols, start=1):
        rows = by_symbol[symbol]
        pnl = [_required_decimal(row, "net_pnl_usdt") for row in rows]
        wins = [value for value in pnl if value > 0]
        losses = [value for value in pnl if value < 0]
        gross_profit = sum(wins, start=_ZERO)
        gross_loss = -sum(losses, start=_ZERO)
        fees = sum(
            (_required_decimal(row, "fees_usdt") for row in rows),
            start=_ZERO,
        )
        total = sum(pnl, start=_ZERO)
        summaries.append(
            {
                "market_rank_at_research_time": market_rank,
                "symbol": symbol,
                "trade_count": len(rows),
                "win_count": len(wins),
                "loss_count": len(losses),
                "win_rate": None if not rows else str(Decimal(len(wins)) / Decimal(len(rows))),
                "total_net_pnl_usdt": str(total),
                "average_net_pnl_usdt": (
                    None if not rows else str(total / Decimal(len(rows)))
                ),
                "profit_factor": (
                    None if gross_loss == 0 else str(gross_profit / gross_loss)
                ),
                "fees_usdt": str(fees),
            }
        )
    return summaries


def _maximum_initial_gross_notional(trades: Sequence[Any]) -> Decimal:
    events: list[tuple[datetime, int, Decimal]] = []
    for raw in trades:
        if not isinstance(raw, Mapping):
            raise ValueError("source-common portfolio closed trade must be an object")
        entry = _parse_time(_required_text(raw, "entry_time"))
        exit_at = _parse_time(_required_text(raw, "exit_time"))
        if exit_at < entry:
            raise ValueError("source-common portfolio trade timestamps are invalid")
        notional = _required_decimal(raw, "entry_notional_usdt")
        if notional <= 0:
            raise ValueError("source-common portfolio entry notional must be positive")
        events.append((entry, 1, notional))
        exit_priority = 2 if exit_at == entry else 0
        events.append((exit_at, exit_priority, -notional))
    events.sort(key=lambda item: (item[0], item[1]))
    active = _ZERO
    maximum = _ZERO
    for _timestamp, _order, delta in events:
        active += delta
        if active < _ZERO:
            raise ValueError("source-common portfolio notional event accounting is invalid")
        maximum = max(maximum, active)
    if active != _ZERO:
        raise ValueError("source-common portfolio notional event accounting did not close")
    return maximum


def _validate_replay_safety(replay: Mapping[str, Any]) -> None:
    for field in (
        "strategy_promotion_allowed",
        "bybit_demo_order_writes_enabled",
        "bybit_live_order_routing_allowed",
    ):
        if replay.get(field) is not False:
            raise ValueError(f"source-common portfolio requires explicit {field}=false")


def _validate_ordered_symbols(symbols: Sequence[str]) -> tuple[str, ...]:
    normalized = tuple(symbols)
    if not 2 <= len(normalized) <= 50 or len(set(normalized)) != len(normalized):
        raise ValueError("source-common portfolio requires 2-50 unique symbols")
    for symbol in normalized:
        if (
            not symbol
            or symbol != symbol.strip().upper()
            or not symbol.endswith("USDT")
            or not symbol.isalnum()
        ):
            raise ValueError("source-common portfolio symbols must be normalized USDT")
    return normalized


def _ceil_five_minutes(value: datetime) -> datetime:
    utc = _utc(value)
    epoch_ms = int(utc.timestamp() * 1000)
    ceiled_ms = ((epoch_ms + _FIVE_MINUTES_MS - 1) // _FIVE_MINUTES_MS) * _FIVE_MINUTES_MS
    return datetime.fromtimestamp(ceiled_ms / 1000, tz=UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("source-common portfolio timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _parse_time(value: str) -> datetime:
    return _utc(datetime.fromisoformat(value))


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"source-common portfolio trade missing {field}")
    return value


def _required_decimal(row: Mapping[str, Any], field: str) -> Decimal:
    value = row.get(field)
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"source-common portfolio trade {field} is invalid") from exc
    if not parsed.is_finite():
        raise ValueError(f"source-common portfolio trade {field} must be finite")
    return parsed


def _decimal_metric(metrics: Mapping[str, Any], field: str) -> Decimal:
    value = metrics.get(field)
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"source-common portfolio metric {field} is invalid") from exc
    if not parsed.is_finite():
        raise ValueError(f"source-common portfolio metric {field} must be finite")
    return parsed


def _non_negative_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"source-common portfolio {field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"source-common portfolio {field} must be an integer") from exc
    if parsed < 0:
        raise ValueError(f"source-common portfolio {field} cannot be negative")
    return parsed
