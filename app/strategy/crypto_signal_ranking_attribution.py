from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar
from app.strategy.crypto_perp import (
    CryptoPerpStrategyConfig,
    build_trade_plan,
    rank_crypto_signals,
)
from app.strategy.crypto_position_selection import (
    CryptoPositionCandidate,
    rank_crypto_position_candidates,
)

_ZERO = Decimal("0")
_FIVE_MINUTES = timedelta(minutes=5)


@dataclass(frozen=True)
class CryptoRankingAttributionPolicy:
    target_exit_cooldown_bars: int = 1
    other_exit_cooldown_bars: int = 3

    def validate(self) -> None:
        if self.target_exit_cooldown_bars < 0 or self.other_exit_cooldown_bars < 0:
            raise ValueError("ranking attribution cooldown bars must be non-negative")


@dataclass(frozen=True)
class CryptoRankingDecisionAttribution:
    decision_time: str
    equity_usdt: Decimal
    open_position_count: int
    available_slots: int
    candidate_count: int
    canonical_symbols: tuple[str, ...]
    actual_symbols: tuple[str, ...]
    economic_shadow_symbols: tuple[str, ...]
    canonical_reconstruction_matches_actual: bool
    selected_set_changed: bool
    canonical_outcomes: tuple[dict[str, Any], ...]
    economic_shadow_outcomes: tuple[dict[str, Any], ...]

    def to_payload(self) -> dict[str, Any]:
        return {
            "decision_time": self.decision_time,
            "equity_usdt": float(self.equity_usdt),
            "open_position_count": self.open_position_count,
            "available_slots": self.available_slots,
            "candidate_count": self.candidate_count,
            "canonical_symbols": list(self.canonical_symbols),
            "actual_symbols": list(self.actual_symbols),
            "economic_shadow_symbols": list(self.economic_shadow_symbols),
            "canonical_reconstruction_matches_actual": (
                self.canonical_reconstruction_matches_actual
            ),
            "selected_set_changed": self.selected_set_changed,
            "canonical_outcomes": list(self.canonical_outcomes),
            "economic_shadow_outcomes": list(self.economic_shadow_outcomes),
        }


def synchronize_crypto_portfolio_acquisition(
    acquisition: BybitKlineAcquisition,
) -> BybitKlineAcquisition:
    """Apply the exact common-timestamp universe contract used by portfolio replay."""

    by_symbol: dict[str, dict[datetime, BybitKlineBar]] = defaultdict(dict)
    for bar in acquisition.bars:
        bar.validate()
        if bar.start_time in by_symbol[bar.symbol]:
            raise ValueError("ranking attribution acquisition has duplicate symbol timestamp")
        by_symbol[bar.symbol][bar.start_time] = bar
    if not by_symbol:
        raise ValueError("ranking attribution acquisition cannot be empty")

    common_times: set[datetime] | None = None
    for rows in by_symbol.values():
        timestamps = set(rows)
        common_times = timestamps if common_times is None else common_times & timestamps
    if common_times is None or len(common_times) < 3:
        raise ValueError("ranking attribution requires synchronized common timestamps")
    ordered_times = tuple(sorted(common_times))
    bars = tuple(
        by_symbol[symbol][timestamp]
        for symbol in sorted(by_symbol)
        for timestamp in ordered_times
    )
    synchronized = BybitKlineAcquisition(
        bars=bars,
        pages_by_symbol=dict(acquisition.pages_by_symbol),
    )
    synchronized.validate(
        requested_symbols=tuple(sorted(by_symbol)),
        minimum_bars=3,
    )
    return synchronized


def attribute_crypto_portfolio_ranking(
    acquisition: BybitKlineAcquisition,
    replay: Mapping[str, Any],
    all_signal_events: Mapping[str, Any],
    *,
    strategy_config: CryptoPerpStrategyConfig | None = None,
    policy: CryptoRankingAttributionPolicy | None = None,
) -> dict[str, Any]:
    """Compare canonical quality-first slots with the existing economic shadow ranker.

    The comparison is retrospective attribution only. Both ranking orders use information that
    was available at the completed decision bar. Future outcomes are joined only after each
    ranking decision and are never used to choose a candidate.
    """

    config = CryptoPerpStrategyConfig() if strategy_config is None else strategy_config
    config.validate()
    active = CryptoRankingAttributionPolicy() if policy is None else policy
    active.validate()
    _validate_replay_boundary(replay)

    synchronized = synchronize_crypto_portfolio_acquisition(acquisition)
    bars_by_symbol = _bars_by_symbol(synchronized.bars)
    equity_by_time = _equity_by_time(replay)
    trades = _closed_trades(replay)
    actual_by_decision = _actual_entries_by_decision(replay)
    outcomes = _signal_outcome_map(all_signal_events)

    decisions: list[CryptoRankingDecisionAttribution] = []
    for decision_text, actual_symbols in sorted(actual_by_decision.items()):
        decision_at = _parse_time(decision_text)
        equity = equity_by_time.get(decision_text)
        if equity is None:
            raise ValueError("ranking attribution cannot match decision to replay equity")
        open_symbols = _open_symbols_at(trades, decision_at)
        available_slots = max(0, config.maximum_concurrent_positions - len(open_symbols))
        if available_slots <= 0:
            raise ValueError("ranking attribution found entry decision without an available slot")

        histories = {
            symbol: tuple(bar for bar in rows if bar.start_time <= decision_at)
            for symbol, rows in bars_by_symbol.items()
        }
        canonical_evaluations = rank_crypto_signals(histories, config)
        canonical_candidates: list[CryptoPositionCandidate] = []
        canonical_candidate_symbols: list[str] = []
        for evaluation in canonical_evaluations:
            signal = evaluation.signal
            if signal is None:
                continue
            if signal.symbol in open_symbols:
                continue
            if _cooldown_active(
                signal.symbol,
                decision_at=decision_at,
                trades=trades,
                policy=active,
            ):
                continue
            plan_evaluation = build_trade_plan(signal, equity_usdt=equity, config=config)
            if not plan_evaluation.eligible or plan_evaluation.plan is None:
                continue
            candidate = CryptoPositionCandidate(
                signal=signal,
                plan=plan_evaluation.plan,
            )
            canonical_candidates.append(candidate)
            canonical_candidate_symbols.append(signal.symbol)

        if not canonical_candidates:
            raise ValueError(
                "ranking attribution cannot reconstruct any accepted candidates:"
                f"{decision_text}"
            )
        slots = min(available_slots, len(canonical_candidates))
        canonical_selected = tuple(canonical_candidate_symbols[:slots])
        economic_ranked = rank_crypto_position_candidates(canonical_candidates)
        economic_selected = tuple(item.signal.symbol for item in economic_ranked[:slots])
        actual = tuple(actual_symbols)
        canonical_matches = canonical_selected == actual
        if not canonical_matches:
            raise ValueError(
                "ranking attribution reconstruction differs from canonical replay entries:"
                f"{decision_text}:expected={canonical_selected}:actual={actual}"
            )

        decisions.append(
            CryptoRankingDecisionAttribution(
                decision_time=decision_text,
                equity_usdt=equity,
                open_position_count=len(open_symbols),
                available_slots=available_slots,
                candidate_count=len(canonical_candidates),
                canonical_symbols=canonical_selected,
                actual_symbols=actual,
                economic_shadow_symbols=economic_selected,
                canonical_reconstruction_matches_actual=True,
                selected_set_changed=set(canonical_selected) != set(economic_selected),
                canonical_outcomes=_selected_outcomes(
                    canonical_selected,
                    decision_time=decision_text,
                    outcomes=outcomes,
                ),
                economic_shadow_outcomes=_selected_outcomes(
                    economic_selected,
                    decision_time=decision_text,
                    outcomes=outcomes,
                ),
            )
        )

    canonical_rows = tuple(
        row for decision in decisions for row in decision.canonical_outcomes
    )
    economic_rows = tuple(
        row for decision in decisions for row in decision.economic_shadow_outcomes
    )
    changed = tuple(item for item in decisions if item.selected_set_changed)
    pairwise = _changed_decision_comparison(changed)
    return {
        "diagnostic": "BYBIT_CRYPTO_PORTFOLIO_RANKING_ATTRIBUTION_V1",
        "decision_count": len(decisions),
        "selected_slot_count": len(canonical_rows),
        "canonical_reconstruction_verified": all(
            item.canonical_reconstruction_matches_actual for item in decisions
        ),
        "portfolio_synchronized_bar_count_per_symbol": (
            len(synchronized.bars) // len(synchronized.symbols)
        ),
        "selection_changed_decision_count": len(changed),
        "selection_changed_decision_fraction": (
            None if not decisions else len(changed) / len(decisions)
        ),
        "canonical_quality_first": _outcome_summary(canonical_rows),
        "economic_shadow": _outcome_summary(economic_rows),
        "changed_decision_comparison": pairwise,
        "decisions": [item.to_payload() for item in decisions],
        "canonical_ranking_contract": (
            "eligible signal quality_score descending, then canonical symbol tie-break; "
            "trade-plan and runtime state gates remain unchanged"
        ),
        "portfolio_history_contract": (
            "all ranking reconstruction uses the exact intersection of symbol timestamps, "
            "matching canonical portfolio replay"
        ),
        "economic_shadow_ranking_contract": [
            "expected_net_r_desc",
            "expected_net_edge_usd_desc",
            "quality_score_desc",
            "cost_to_target_fraction_asc",
            "symbol_asc",
        ],
        "future_outcome_use_contract": (
            "15m/60m/240m and MFE/MAE are joined only after ranking reconstruction and are "
            "never inputs to either ranking order"
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


def _bars_by_symbol(
    bars: Sequence[BybitKlineBar],
) -> dict[str, tuple[BybitKlineBar, ...]]:
    grouped: dict[str, list[BybitKlineBar]] = defaultdict(list)
    for bar in bars:
        bar.validate()
        grouped[bar.symbol].append(bar)
    result = {
        symbol: tuple(sorted(rows, key=lambda item: item.start_time))
        for symbol, rows in grouped.items()
    }
    counts = {len(rows) for rows in result.values()}
    timestamp_sets = {tuple(bar.start_time for bar in rows) for rows in result.values()}
    if len(counts) != 1 or len(timestamp_sets) != 1:
        raise ValueError("ranking attribution portfolio bars are not synchronized")
    return result


def _equity_by_time(replay: Mapping[str, Any]) -> dict[str, Decimal]:
    raw = replay.get("equity_curve")
    if not isinstance(raw, list):
        raise ValueError("ranking attribution replay equity curve is missing")
    result: dict[str, Decimal] = {}
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("ranking attribution equity row must be an object")
        time = item.get("time")
        equity = item.get("equity")
        if not isinstance(time, str):
            raise ValueError("ranking attribution equity time is invalid")
        value = Decimal(str(equity))
        if not value.is_finite() or value <= 0:
            raise ValueError("ranking attribution equity must be positive and finite")
        result[time] = value
    return result


def _closed_trades(replay: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    raw = replay.get("closed_trades")
    if not isinstance(raw, list):
        raise ValueError("ranking attribution closed trades are missing")
    rows: list[Mapping[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("ranking attribution closed trade must be an object")
        _parse_time(_required_text(item, "entry_time"))
        _parse_time(_required_text(item, "exit_time"))
        rows.append(item)
    return tuple(rows)


def _actual_entries_by_decision(replay: Mapping[str, Any]) -> dict[str, list[str]]:
    raw = replay.get("decision_events")
    if not isinstance(raw, list):
        raise ValueError("ranking attribution decision events are missing")
    result: dict[str, list[str]] = defaultdict(list)
    for event in raw:
        if not isinstance(event, Mapping) or event.get("event") != "ENTRY":
            continue
        decision = _required_text(event, "decision_time")
        symbol = _required_text(event, "symbol")
        result[decision].append(symbol)
    return dict(result)


def _signal_outcome_map(
    all_signal_events: Mapping[str, Any],
) -> dict[tuple[str, str, str], Mapping[str, Any]]:
    raw = all_signal_events.get("signal_rows")
    if not isinstance(raw, list):
        raise ValueError("ranking attribution signal rows are missing")
    result: dict[tuple[str, str, str], Mapping[str, Any]] = {}
    for row in raw:
        if not isinstance(row, Mapping):
            raise ValueError("ranking attribution signal row must be an object")
        key = (
            _required_text(row, "symbol"),
            _required_text(row, "side"),
            _required_text(row, "decision_time"),
        )
        if key in result:
            raise ValueError("ranking attribution signal outcome identity is duplicated")
        result[key] = row
    return result


def _open_symbols_at(
    trades: Sequence[Mapping[str, Any]],
    decision_at: datetime,
) -> set[str]:
    result: set[str] = set()
    for trade in trades:
        entry = _parse_time(_required_text(trade, "entry_time"))
        exit_at = _parse_time(_required_text(trade, "exit_time"))
        if entry <= decision_at < exit_at:
            result.add(_required_text(trade, "symbol"))
    return result


def _cooldown_active(
    symbol: str,
    *,
    decision_at: datetime,
    trades: Sequence[Mapping[str, Any]],
    policy: CryptoRankingAttributionPolicy,
) -> bool:
    exits = [
        trade
        for trade in trades
        if _required_text(trade, "symbol") == symbol
        and _parse_time(_required_text(trade, "exit_time")) <= decision_at
    ]
    if not exits:
        return False
    latest = max(exits, key=lambda item: _parse_time(_required_text(item, "exit_time")))
    exit_at = _parse_time(_required_text(latest, "exit_time"))
    reason = _required_text(latest, "exit_reason")
    bars = (
        policy.target_exit_cooldown_bars
        if reason == "NET_TARGET"
        else policy.other_exit_cooldown_bars
    )
    return decision_at < exit_at + bars * _FIVE_MINUTES


def _selected_outcomes(
    symbols: Sequence[str],
    *,
    decision_time: str,
    outcomes: Mapping[tuple[str, str, str], Mapping[str, Any]],
) -> tuple[dict[str, Any], ...]:
    rows: list[dict[str, Any]] = []
    for symbol in symbols:
        matches = [
            row
            for (candidate_symbol, _side, candidate_time), row in outcomes.items()
            if candidate_symbol == symbol and candidate_time == decision_time
        ]
        if len(matches) != 1:
            raise ValueError(
                "ranking attribution cannot uniquely match selected signal outcome:"
                f"{symbol}:{decision_time}"
            )
        rows.append(_compact_outcome(matches[0]))
    return tuple(rows)


def _compact_outcome(row: Mapping[str, Any]) -> dict[str, Any]:
    horizons = row.get("horizons")
    if not isinstance(horizons, list):
        raise ValueError("ranking attribution signal horizons are missing")
    by_minutes: dict[int, float | None] = {}
    for horizon in horizons:
        if not isinstance(horizon, Mapping):
            raise ValueError("ranking attribution horizon must be an object")
        minutes = int(horizon["minutes"])
        value = horizon.get("directional_return_fraction")
        by_minutes[minutes] = None if value is None else float(value)
    return {
        "symbol": _required_text(row, "symbol"),
        "side": _required_text(row, "side"),
        "quality_score": float(row["quality_score"]),
        "quality_ratio_to_entry_gate": float(row["quality_ratio_to_entry_gate"]),
        "clarity_band": _required_text(row, "clarity_band"),
        "maximum_favorable_r_240m": _optional_float(row.get("maximum_favorable_r_240m")),
        "maximum_adverse_r_240m": _optional_float(row.get("maximum_adverse_r_240m")),
        "directional_return_15m": by_minutes.get(15),
        "directional_return_60m": by_minutes.get(60),
        "directional_return_240m": by_minutes.get(240),
    }


def _outcome_summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "selection_count": len(rows),
        "median_quality_ratio": _median(rows, "quality_ratio_to_entry_gate"),
        "median_mfe_r_240m": _median(rows, "maximum_favorable_r_240m"),
        "median_mae_r_240m": _median(rows, "maximum_adverse_r_240m"),
        "horizons": {
            str(minutes): _direction_summary(rows, f"directional_return_{minutes}m")
            for minutes in (15, 60, 240)
        },
    }


def _changed_decision_comparison(
    decisions: Sequence[CryptoRankingDecisionAttribution],
) -> dict[str, Any]:
    quality_better = 0
    economic_better = 0
    ties = 0
    comparable = 0
    for decision in decisions:
        quality = _mean_240(decision.canonical_outcomes)
        economic = _mean_240(decision.economic_shadow_outcomes)
        if quality is None or economic is None:
            continue
        comparable += 1
        if economic > quality:
            economic_better += 1
        elif quality > economic:
            quality_better += 1
        else:
            ties += 1
    return {
        "changed_decision_count": len(decisions),
        "comparable_240m_decision_count": comparable,
        "economic_shadow_better_240m_count": economic_better,
        "canonical_quality_better_240m_count": quality_better,
        "tie_240m_count": ties,
        "economic_shadow_better_240m_rate": (
            None if comparable == 0 else economic_better / comparable
        ),
    }


def _mean_240(rows: Sequence[Mapping[str, Any]]) -> float | None:
    values = [
        float(row["directional_return_240m"])
        for row in rows
        if row.get("directional_return_240m") is not None
    ]
    if not values:
        return None
    return sum(values) / len(values)


def _direction_summary(
    rows: Sequence[Mapping[str, Any]],
    field: str,
) -> dict[str, Any]:
    values = [float(row[field]) for row in rows if row.get(field) is not None]
    return {
        "complete_count": len(values),
        "positive_count": sum(value > 0 for value in values),
        "positive_rate": (
            None if not values else sum(value > 0 for value in values) / len(values)
        ),
        "average": None if not values else sum(values) / len(values),
        "median": None if not values else statistics.median(values),
    }


def _median(rows: Sequence[Mapping[str, Any]], field: str) -> float | None:
    values = [float(row[field]) for row in rows if row.get(field) is not None]
    return None if not values else statistics.median(values)


def _optional_float(value: object) -> float | None:
    return None if value is None else float(value)


def _validate_replay_boundary(replay: Mapping[str, Any]) -> None:
    if replay.get("mode") != "MIN_20_NET_EDGE_CONDITIONAL_OPEN_ENDED_RUNNER":
        raise ValueError("ranking attribution requires canonical conditional runner replay")
    for field in (
        "strategy_promotion_allowed",
        "bybit_demo_order_writes_enabled",
        "bybit_live_order_routing_allowed",
    ):
        if replay.get(field) is not False:
            raise ValueError(f"ranking attribution requires explicit {field}=false")


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"ranking attribution {field} must be non-empty text")
    return value


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("ranking attribution timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


__all__ = [
    "CryptoRankingAttributionPolicy",
    "attribute_crypto_portfolio_ranking",
    "synchronize_crypto_portfolio_acquisition",
]
