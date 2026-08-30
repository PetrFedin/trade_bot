from __future__ import annotations

import math
import statistics
from bisect import bisect_right
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, TypeVar

from app.marketdata.bybit_derivatives_history import BybitDerivativesHistory
from app.strategy.crypto_perp import CryptoPerpStrategyConfig
from app.strategy.crypto_strategy_evidence_matrix import (
    CryptoStrategyEvidencePolicy,
    classify_crypto_stress_regime,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_LONG_HEAVY = Decimal("0.55")
_SHORT_HEAVY = Decimal("0.45")
_Z_95 = 1.959963984540054
TPoint = TypeVar("TPoint", bound="_TimestampPoint")


class _TimestampPoint(Protocol):
    timestamp_ms: int


@dataclass(frozen=True)
class CryptoFirstTouchDerivativesEvidencePolicy:
    minimum_cell_episodes: int = 5
    sample_sufficient_episodes: int = 30
    minimum_cross_symbol_count: int = 2
    minimum_distinct_days: int = 3

    def validate(self) -> None:
        if not 1 <= self.minimum_cell_episodes <= self.sample_sufficient_episodes:
            raise ValueError("first-touch derivatives minimum episode support is invalid")
        if self.sample_sufficient_episodes > 100_000:
            raise ValueError("first-touch derivatives sufficient support is unreasonable")
        if not 1 <= self.minimum_cross_symbol_count <= 1000:
            raise ValueError("first-touch derivatives cross-symbol support is invalid")
        if not 1 <= self.minimum_distinct_days <= 3650:
            raise ValueError("first-touch derivatives day support is invalid")


@dataclass(frozen=True)
class CryptoFirstTouchDerivativesEvidenceRow:
    symbol: str
    side: str
    decision_time: str
    signal_available_at: str
    utc_day: str
    first_touch_state: str
    price_pattern: str
    market_regime: str
    volatility_regime: str
    trend_regime: str
    breakout_regime: str
    turnover_regime: str
    open_interest_regime: str
    crowding_regime: str
    prior_funding_regime: str
    stress_regime: str
    stress_score: int
    decision_context_complete: bool
    missing_reasons: tuple[str, ...]
    open_interest_timestamp_ms: int | None
    open_interest_delta_fraction: Decimal | None
    account_ratio_timestamp_ms: int | None
    long_account_ratio: Decimal | None
    prior_funding_timestamp_ms: int | None
    prior_funding_rate: Decimal | None
    quality_ratio_to_entry_gate: Decimal
    expected_net_edge_usd: Decimal
    maximum_favorable_r: Decimal
    maximum_adverse_r: Decimal

    @property
    def cross_token_cell_key(self) -> str:
        return "|".join(
            (
                self.side,
                self.market_regime,
                self.open_interest_regime,
                self.crowding_regime,
                self.prior_funding_regime,
                self.stress_regime,
            )
        )

    @property
    def exact_cell_key(self) -> str:
        return f"{self.symbol}|{self.cross_token_cell_key}"

    def validate(self) -> None:
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("first-touch derivatives symbol is invalid")
        if self.side not in {"LONG", "SHORT"}:
            raise ValueError("first-touch derivatives side is invalid")
        decision = _parse_time(self.decision_time)
        available = _parse_time(self.signal_available_at)
        if decision >= available:
            raise ValueError("first-touch derivatives signal timing is not monotonic")
        if available.date().isoformat() != self.utc_day:
            raise ValueError("first-touch derivatives UTC day is inconsistent")
        if self.first_touch_state not in {"TARGET_FIRST", "STOP_FIRST", "NEITHER"}:
            raise ValueError("first-touch derivatives requires complete non-ambiguous outcome")
        if self.stress_score < 0 or self.stress_score > 5:
            raise ValueError("first-touch derivatives stress score must be within [0, 5]")
        if self.decision_context_complete and self.missing_reasons:
            raise ValueError("complete first-touch derivatives context cannot have missing reasons")
        if not self.decision_context_complete and not self.missing_reasons:
            raise ValueError("incomplete first-touch derivatives context requires missing reasons")
        if self.decision_context_complete and self.stress_regime == "STRESS_UNKNOWN":
            raise ValueError("complete first-touch derivatives stress cannot be unknown")
        if not self.decision_context_complete and self.stress_regime != "STRESS_UNKNOWN":
            raise ValueError("incomplete first-touch derivatives stress must be unknown")
        for name, value in (
            ("quality_ratio_to_entry_gate", self.quality_ratio_to_entry_gate),
            ("expected_net_edge_usd", self.expected_net_edge_usd),
            ("maximum_favorable_r", self.maximum_favorable_r),
            ("maximum_adverse_r", self.maximum_adverse_r),
        ):
            if not value.is_finite():
                raise ValueError(f"first-touch derivatives {name} must be finite")
        for name, value in (
            ("open_interest_delta_fraction", self.open_interest_delta_fraction),
            ("long_account_ratio", self.long_account_ratio),
            ("prior_funding_rate", self.prior_funding_rate),
        ):
            if value is not None and not value.is_finite():
                raise ValueError(f"first-touch derivatives {name} must be finite when present")
        decision_ms = int(decision.timestamp() * 1000)
        for name, timestamp in (
            ("open interest", self.open_interest_timestamp_ms),
            ("account ratio", self.account_ratio_timestamp_ms),
            ("prior funding", self.prior_funding_timestamp_ms),
        ):
            if timestamp is not None and timestamp > decision_ms:
                raise ValueError(f"first-touch derivatives {name} contains lookahead")

    def to_payload(self) -> dict[str, Any]:
        self.validate()
        return {
            "symbol": self.symbol,
            "side": self.side,
            "decision_time": self.decision_time,
            "signal_available_at": self.signal_available_at,
            "utc_day": self.utc_day,
            "first_touch_state": self.first_touch_state,
            "price_pattern": self.price_pattern,
            "market_regime": self.market_regime,
            "volatility_regime": self.volatility_regime,
            "trend_regime": self.trend_regime,
            "breakout_regime": self.breakout_regime,
            "turnover_regime": self.turnover_regime,
            "open_interest_regime": self.open_interest_regime,
            "crowding_regime": self.crowding_regime,
            "prior_funding_regime": self.prior_funding_regime,
            "stress_regime": self.stress_regime,
            "stress_score": self.stress_score,
            "decision_context_complete": self.decision_context_complete,
            "missing_reasons": list(self.missing_reasons),
            "open_interest_timestamp_ms": self.open_interest_timestamp_ms,
            "open_interest_delta_fraction": _optional_float(
                self.open_interest_delta_fraction
            ),
            "account_ratio_timestamp_ms": self.account_ratio_timestamp_ms,
            "long_account_ratio": _optional_float(self.long_account_ratio),
            "prior_funding_timestamp_ms": self.prior_funding_timestamp_ms,
            "prior_funding_rate": _optional_float(self.prior_funding_rate),
            "quality_ratio_to_entry_gate": float(self.quality_ratio_to_entry_gate),
            "expected_net_edge_usd": float(self.expected_net_edge_usd),
            "maximum_favorable_r": float(self.maximum_favorable_r),
            "maximum_adverse_r": float(self.maximum_adverse_r),
            "cross_token_cell_key": self.cross_token_cell_key,
            "exact_cell_key": self.exact_cell_key,
        }


def build_crypto_first_touch_derivatives_evidence(
    episode_rows: Sequence[Mapping[str, Any]],
    histories: Mapping[str, BybitDerivativesHistory],
    *,
    strategy_config: CryptoPerpStrategyConfig | None = None,
    stress_policy: CryptoStrategyEvidencePolicy | None = None,
) -> tuple[CryptoFirstTouchDerivativesEvidenceRow, ...]:
    """Join frozen first-touch episodes to derivatives known no later than decision time."""

    config = CryptoPerpStrategyConfig() if strategy_config is None else strategy_config
    config.validate()
    stress = CryptoStrategyEvidencePolicy() if stress_policy is None else stress_policy
    stress.validate()
    rows = tuple(episode_rows)
    turnover_values = tuple(_required_decimal(row, "average_turnover_usdt") for row in rows)
    turnover_median = _median_decimal(turnover_values) if turnover_values else None
    result: list[CryptoFirstTouchDerivativesEvidenceRow] = []
    seen: set[tuple[str, str, str]] = set()
    for raw in rows:
        symbol = _required_text(raw, "symbol")
        side = _required_text(raw, "side")
        decision_time = _required_text(raw, "decision_time")
        signal_available_at = _required_text(raw, "signal_available_at")
        identity = (symbol, side, signal_available_at)
        if identity in seen:
            raise ValueError("first-touch derivatives episode rows contain duplicate identity")
        seen.add(identity)
        state = _required_text(raw, "first_touch_state")
        if state in {"INCOMPLETE", "AMBIGUOUS_SAME_BAR"}:
            raise ValueError("first-touch derivatives requires finalized non-ambiguous episodes")
        decision_ms = int(_parse_time(decision_time).timestamp() * 1000)
        history = histories.get(symbol)
        if history is None:
            context = _missing_context()
        else:
            history.validate()
            if history.symbol != symbol:
                raise ValueError("first-touch derivatives history key/symbol mismatch")
            if not history.start_ms <= decision_ms <= history.end_ms:
                raise ValueError("first-touch derivatives decision falls outside history")
            context = _point_in_time_context(history, decision_ms=decision_ms)

        volatility, trend, breakout = _price_regimes(raw, config=config)
        turnover = _required_decimal(raw, "average_turnover_usdt")
        turnover_regime = (
            "TURNOVER_UNKNOWN"
            if turnover_median is None
            else ("TURNOVER_HIGH" if turnover >= turnover_median else "TURNOVER_LOW")
        )
        market_regime = "|".join((volatility, trend, breakout, turnover_regime))
        stress_regime, stress_score, complete, reasons = classify_crypto_stress_regime(
            volatility_regime=volatility,
            one_bar_atr_multiple=_required_decimal(raw, "one_bar_atr_multiple"),
            open_interest_delta_fraction=context["open_interest_delta_fraction"],
            crowding_regime=context["crowding_regime"],
            prior_funding_regime=context["prior_funding_regime"],
            decision_context_complete=context["decision_context_complete"],
            missing_reasons=context["missing_reasons"],
            strategy_config=config,
            policy=stress,
        )
        if complete != context["decision_context_complete"]:
            raise ValueError("first-touch derivatives stress completeness drifted")
        evidence = CryptoFirstTouchDerivativesEvidenceRow(
            symbol=symbol,
            side=side,
            decision_time=decision_time,
            signal_available_at=signal_available_at,
            utc_day=_required_text(raw, "utc_day"),
            first_touch_state=state,
            price_pattern=_required_text(raw, "pattern"),
            market_regime=market_regime,
            volatility_regime=volatility,
            trend_regime=trend,
            breakout_regime=breakout,
            turnover_regime=turnover_regime,
            open_interest_regime=context["open_interest_regime"],
            crowding_regime=context["crowding_regime"],
            prior_funding_regime=context["prior_funding_regime"],
            stress_regime=stress_regime,
            stress_score=stress_score,
            decision_context_complete=complete,
            missing_reasons=reasons,
            open_interest_timestamp_ms=context["open_interest_timestamp_ms"],
            open_interest_delta_fraction=context["open_interest_delta_fraction"],
            account_ratio_timestamp_ms=context["account_ratio_timestamp_ms"],
            long_account_ratio=context["long_account_ratio"],
            prior_funding_timestamp_ms=context["prior_funding_timestamp_ms"],
            prior_funding_rate=context["prior_funding_rate"],
            quality_ratio_to_entry_gate=_required_decimal(raw, "quality_ratio_to_entry_gate"),
            expected_net_edge_usd=_required_decimal(raw, "expected_net_edge_usd"),
            maximum_favorable_r=_required_decimal(raw, "maximum_favorable_r"),
            maximum_adverse_r=_required_decimal(raw, "maximum_adverse_r"),
        )
        evidence.validate()
        result.append(evidence)
    return tuple(sorted(result, key=lambda item: (_parse_time(item.signal_available_at), item.symbol)))


def diagnose_crypto_first_touch_derivatives_evidence(
    rows: Sequence[CryptoFirstTouchDerivativesEvidenceRow],
    *,
    policy: CryptoFirstTouchDerivativesEvidencePolicy | None = None,
) -> dict[str, Any]:
    active = CryptoFirstTouchDerivativesEvidencePolicy() if policy is None else policy
    active.validate()
    records = tuple(rows)
    for row in records:
        row.validate()
    complete = tuple(row for row in records if row.decision_context_complete)
    grouped: dict[str, list[CryptoFirstTouchDerivativesEvidenceRow]] = defaultdict(list)
    exact: dict[str, list[CryptoFirstTouchDerivativesEvidenceRow]] = defaultdict(list)
    by_stress: dict[str, list[CryptoFirstTouchDerivativesEvidenceRow]] = defaultdict(list)
    by_oi: dict[str, list[CryptoFirstTouchDerivativesEvidenceRow]] = defaultdict(list)
    by_crowding: dict[str, list[CryptoFirstTouchDerivativesEvidenceRow]] = defaultdict(list)
    by_funding: dict[str, list[CryptoFirstTouchDerivativesEvidenceRow]] = defaultdict(list)
    for row in records:
        grouped[row.cross_token_cell_key].append(row)
        exact[row.exact_cell_key].append(row)
        by_stress[row.stress_regime].append(row)
        by_oi[row.open_interest_regime].append(row)
        by_crowding[row.crowding_regime].append(row)
        by_funding[row.prior_funding_regime].append(row)

    cross_cells = [
        _cell_summary(key, values, policy=active, require_cross_symbol=True)
        for key, values in grouped.items()
    ]
    exact_cells = [
        _cell_summary(key, values, policy=active, require_cross_symbol=False)
        for key, values in exact.items()
    ]
    qualified_cross = [row for row in cross_cells if row["qualified_complete_cell"]]
    perfect_cross = [row for row in qualified_cross if row["observed_perfect_target_first"]]
    qualified_exact = [row for row in exact_cells if row["qualified_complete_cell"]]
    perfect_exact = [row for row in qualified_exact if row["observed_perfect_target_first"]]
    missing_counts = Counter(
        reason
        for row in records
        if not row.decision_context_complete
        for reason in row.missing_reasons
    )
    return {
        "diagnostic": "BYBIT_FIRST_TOUCH_DERIVATIVES_EVIDENCE_V1",
        "episode_count": len(records),
        "complete_context_count": len(complete),
        "complete_context_fraction": (
            None if not records else len(complete) / len(records)
        ),
        "aggregate": _summary(records),
        "complete_context_aggregate": _summary(complete),
        "by_stress_regime": _group_payload(by_stress),
        "by_open_interest_regime": _group_payload(by_oi),
        "by_crowding_regime": _group_payload(by_crowding),
        "by_prior_funding_regime": _group_payload(by_funding),
        "missing_reason_counts": dict(sorted(missing_counts.items())),
        "cross_token_cells": sorted(cross_cells, key=_cell_sort_key, reverse=True),
        "exact_symbol_cells": sorted(exact_cells, key=_cell_sort_key, reverse=True),
        "qualified_cross_token_cells": sorted(
            qualified_cross, key=_cell_sort_key, reverse=True
        ),
        "qualified_exact_symbol_cells": sorted(
            qualified_exact, key=_cell_sort_key, reverse=True
        ),
        "retrospective_perfect_cross_token_cells": sorted(
            perfect_cross, key=_cell_sort_key, reverse=True
        ),
        "retrospective_perfect_exact_symbol_cells": sorted(
            perfect_exact, key=_cell_sort_key, reverse=True
        ),
        "perfect_cross_token_cell_count": len(perfect_cross),
        "perfect_exact_symbol_cell_count": len(perfect_exact),
        "minimum_cell_episodes": active.minimum_cell_episodes,
        "sample_sufficient_episodes": active.sample_sufficient_episodes,
        "minimum_cross_symbol_count": active.minimum_cross_symbol_count,
        "minimum_distinct_days": active.minimum_distinct_days,
        "cell_contract": (
            "side|market_regime|OI_regime|crowding|prior_funding|stress; exact cells prepend symbol"
        ),
        "feature_timing_contract": (
            "all derivatives features use only observations timestamped <= decision_time; "
            "TARGET_FIRST/STOP_FIRST is joined afterward as outcome"
        ),
        "regime_thresholds_reused_from_pr58": True,
        "outcome_fitted_thresholds_added": False,
        "parameter_retuning_performed": False,
        "strategy_selection_allowed": False,
        "strategy_promotion_allowed": False,
        "trade_actionable": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
        "causal_claim_allowed": False,
        "predictive_guarantee_allowed": False,
        "evidence_rows": [row.to_payload() for row in records],
    }


def _point_in_time_context(
    history: BybitDerivativesHistory,
    *,
    decision_ms: int,
) -> dict[str, Any]:
    current_oi = _latest_at_or_before(history.open_interest, decision_ms)
    previous_oi = (
        None if current_oi is None else _latest_before(history.open_interest, current_oi.timestamp_ms)
    )
    ratio = _latest_at_or_before(history.account_ratio, decision_ms)
    prior_funding = _latest_at_or_before(history.funding, decision_ms)
    missing: list[str] = []
    if current_oi is None:
        missing.append("OPEN_INTEREST_AT_DECISION_MISSING")
    if previous_oi is None:
        missing.append("OPEN_INTEREST_PREVIOUS_POINT_MISSING")
    if ratio is None:
        missing.append("ACCOUNT_RATIO_AT_DECISION_MISSING")
    if prior_funding is None:
        missing.append("PRIOR_FUNDING_RATE_MISSING")
    delta_fraction: Decimal | None = None
    if current_oi is not None and previous_oi is not None:
        if previous_oi.open_interest > 0:
            delta_fraction = (
                current_oi.open_interest - previous_oi.open_interest
            ) / previous_oi.open_interest
        else:
            missing.append("PREVIOUS_OPEN_INTEREST_ZERO")
    long_ratio = None if ratio is None else ratio.buy_ratio
    return {
        "open_interest_timestamp_ms": None if current_oi is None else current_oi.timestamp_ms,
        "open_interest_delta_fraction": delta_fraction,
        "account_ratio_timestamp_ms": None if ratio is None else ratio.timestamp_ms,
        "long_account_ratio": long_ratio,
        "prior_funding_timestamp_ms": (
            None if prior_funding is None else prior_funding.timestamp_ms
        ),
        "prior_funding_rate": None if prior_funding is None else prior_funding.funding_rate,
        "open_interest_regime": _oi_regime(delta_fraction),
        "crowding_regime": _crowding_regime(long_ratio),
        "prior_funding_regime": _funding_regime(
            None if prior_funding is None else prior_funding.funding_rate
        ),
        "decision_context_complete": not missing,
        "missing_reasons": tuple(dict.fromkeys(missing)),
    }


def _missing_context() -> dict[str, Any]:
    return {
        "open_interest_timestamp_ms": None,
        "open_interest_delta_fraction": None,
        "account_ratio_timestamp_ms": None,
        "long_account_ratio": None,
        "prior_funding_timestamp_ms": None,
        "prior_funding_rate": None,
        "open_interest_regime": "OI_UNKNOWN",
        "crowding_regime": "CROWDING_UNKNOWN",
        "prior_funding_regime": "FUNDING_UNKNOWN",
        "decision_context_complete": False,
        "missing_reasons": ("DERIVATIVES_HISTORY_MISSING",),
    }


def _price_regimes(
    row: Mapping[str, Any],
    *,
    config: CryptoPerpStrategyConfig,
) -> tuple[str, str, str]:
    atr = _required_decimal(row, "atr_fraction")
    span = config.maximum_atr_fraction - config.minimum_atr_fraction
    lower = config.minimum_atr_fraction + span / Decimal("3")
    upper = config.minimum_atr_fraction + span * Decimal("2") / Decimal("3")
    if atr <= lower:
        volatility = "VOL_LOW_NORMAL"
    elif atr <= upper:
        volatility = "VOL_MID_NORMAL"
    else:
        volatility = "VOL_HIGH_NORMAL"
    trend = (
        "TREND_STRONG"
        if _required_decimal(row, "trend_strength_atr") >= _ONE
        else "TREND_MODERATE"
    )
    breakout = (
        "BREAKOUT_CONFIRMED"
        if _required_decimal(row, "breakout_strength_atr") >= _ZERO
        else "BREAKOUT_PULLBACK"
    )
    return volatility, trend, breakout


def _oi_regime(delta_fraction: Decimal | None) -> str:
    if delta_fraction is None:
        return "OI_UNKNOWN"
    if delta_fraction > 0:
        return "OI_RISING"
    if delta_fraction < 0:
        return "OI_FALLING"
    return "OI_FLAT"


def _crowding_regime(long_ratio: Decimal | None) -> str:
    if long_ratio is None:
        return "CROWDING_UNKNOWN"
    if long_ratio >= _LONG_HEAVY:
        return "LONG_HEAVY"
    if long_ratio <= _SHORT_HEAVY:
        return "SHORT_HEAVY"
    return "BALANCED"


def _funding_regime(rate: Decimal | None) -> str:
    if rate is None:
        return "FUNDING_UNKNOWN"
    if rate > 0:
        return "FUNDING_POSITIVE"
    if rate < 0:
        return "FUNDING_NEGATIVE"
    return "FUNDING_ZERO"


def _latest_at_or_before(points: Sequence[TPoint], timestamp_ms: int) -> TPoint | None:
    timestamps = [point.timestamp_ms for point in points]
    index = bisect_right(timestamps, timestamp_ms) - 1
    return None if index < 0 else points[index]


def _latest_before(points: Sequence[TPoint], timestamp_ms: int) -> TPoint | None:
    timestamps = [point.timestamp_ms for point in points]
    index = bisect_right(timestamps, timestamp_ms - 1) - 1
    return None if index < 0 else points[index]


def _cell_summary(
    key: str,
    rows: Sequence[CryptoFirstTouchDerivativesEvidenceRow],
    *,
    policy: CryptoFirstTouchDerivativesEvidencePolicy,
    require_cross_symbol: bool,
) -> dict[str, Any]:
    summary = _summary(rows)
    symbols = sorted({row.symbol for row in rows})
    days = sorted({row.utc_day for row in rows})
    complete = all(row.decision_context_complete for row in rows)
    support = len(rows) >= policy.minimum_cell_episodes
    symbol_support = (
        len(symbols) >= policy.minimum_cross_symbol_count if require_cross_symbol else True
    )
    day_support = len(days) >= policy.minimum_distinct_days
    qualified = complete and support and symbol_support and day_support
    perfect = bool(rows) and summary["target_first_count"] == len(rows)
    ordered = tuple(sorted(rows, key=lambda row: _parse_time(row.signal_available_at)))
    midpoint = len(ordered) // 2
    return {
        "cell_key": key,
        "symbol_count": len(symbols),
        "symbols": symbols,
        "distinct_day_count": len(days),
        "utc_days": days,
        "context_complete_for_all_rows": complete,
        "minimum_support_met": support,
        "cross_symbol_support_met": symbol_support,
        "distinct_day_support_met": day_support,
        "sample_sufficient": len(rows) >= policy.sample_sufficient_episodes,
        "qualified_complete_cell": qualified,
        "observed_perfect_target_first": perfect,
        "chronological_halves": {
            "early": _summary(ordered[:midpoint]),
            "late": _summary(ordered[midpoint:]),
        },
        **summary,
    }


def _summary(rows: Sequence[CryptoFirstTouchDerivativesEvidenceRow]) -> dict[str, Any]:
    count = len(rows)
    states = Counter(row.first_touch_state for row in rows)
    target = states["TARGET_FIRST"]
    return {
        "episode_count": count,
        "target_first_count": target,
        "stop_first_count": states["STOP_FIRST"],
        "neither_count": states["NEITHER"],
        "target_first_rate": None if not count else target / count,
        "target_first_wilson_lower_95": _wilson_lower(target, count),
        "median_quality_ratio": (
            None if not rows else float(statistics.median(row.quality_ratio_to_entry_gate for row in rows))
        ),
        "median_expected_net_edge_usd": (
            None if not rows else float(statistics.median(row.expected_net_edge_usd for row in rows))
        ),
        "median_mfe_r": (
            None if not rows else float(statistics.median(row.maximum_favorable_r for row in rows))
        ),
        "median_mae_r": (
            None if not rows else float(statistics.median(row.maximum_adverse_r for row in rows))
        ),
    }


def _group_payload(
    groups: Mapping[str, Sequence[CryptoFirstTouchDerivativesEvidenceRow]],
) -> dict[str, Any]:
    return {key: _summary(values) for key, values in sorted(groups.items())}


def _cell_sort_key(row: Mapping[str, Any]) -> tuple[Any, ...]:
    return (
        bool(row["observed_perfect_target_first"]),
        bool(row["sample_sufficient"]),
        float(row["target_first_rate"] or 0.0),
        int(row["episode_count"]),
        float(row["target_first_wilson_lower_95"] or 0.0),
        str(row["cell_key"]),
    )


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("first-touch derivatives timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("first-touch derivatives timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _required_text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"first-touch derivatives missing {field}")
    return value


def _required_decimal(row: Mapping[str, Any], field: str) -> Decimal:
    value = row.get(field)
    if value is None or isinstance(value, bool):
        raise ValueError(f"first-touch derivatives missing {field}")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"first-touch derivatives invalid {field}") from exc
    if not parsed.is_finite():
        raise ValueError(f"first-touch derivatives non-finite {field}")
    return parsed


def _median_decimal(values: Sequence[Decimal]) -> Decimal:
    if not values:
        raise ValueError("first-touch derivatives median requires values")
    return statistics.median(values)


def _optional_float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _wilson_lower(successes: int, total: int) -> float | None:
    if total <= 0:
        return None
    p = successes / total
    z2 = _Z_95 * _Z_95
    denominator = 1.0 + z2 / total
    center = p + z2 / (2.0 * total)
    margin = _Z_95 * math.sqrt((p * (1.0 - p) + z2 / (4.0 * total)) / total)
    return max(0.0, (center - margin) / denominator)


__all__ = [
    "CryptoFirstTouchDerivativesEvidencePolicy",
    "CryptoFirstTouchDerivativesEvidenceRow",
    "build_crypto_first_touch_derivatives_evidence",
    "diagnose_crypto_first_touch_derivatives_evidence",
]
