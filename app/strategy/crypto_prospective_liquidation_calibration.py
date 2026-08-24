from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import Any

from app.strategy.crypto_prospective_ranking_calibration import (
    CryptoProspectiveCalibrationDataset,
    CryptoProspectiveCalibrationObservation,
)

_ZERO = Decimal("0")
_WINDOWS = (5, 15, 60)
_HORIZONS = (15, 60, 240)
_CONTEXT_STATES = frozenset(
    {"NOT_MATERIALIZED", "COVERAGE_UNQUALIFIED", "COVERAGE_QUALIFIED"}
)


@dataclass(frozen=True)
class CryptoLiquidationCalibrationPolicy:
    minimum_group_observations: int = 30
    minimum_comparison_observations: int = 50

    def validate(self) -> None:
        if not 5 <= self.minimum_group_observations <= 10_000:
            raise ValueError(
                "liquidation calibration group minimum must be within [5, 10000]"
            )
        if not (
            self.minimum_group_observations
            <= self.minimum_comparison_observations
            <= 50_000
        ):
            raise ValueError(
                "liquidation calibration comparison minimum must be >= group minimum"
            )


@dataclass(frozen=True)
class CryptoLiquidationCalibrationWindow:
    window_minutes: int
    event_count: int
    long_liquidation_count: int
    short_liquidation_count: int
    long_estimated_notional_usdt: Decimal
    short_estimated_notional_usdt: Decimal
    total_estimated_notional_usdt: Decimal
    signed_long_minus_short_notional_usdt: Decimal
    normalized_long_minus_short_imbalance: Decimal
    largest_event_estimated_notional_usdt: Decimal
    known_zero: bool

    def validate(self) -> None:
        if self.window_minutes not in _WINDOWS:
            raise ValueError("liquidation calibration window is unsupported")
        if min(
            self.event_count,
            self.long_liquidation_count,
            self.short_liquidation_count,
        ) < 0:
            raise ValueError("liquidation calibration counts cannot be negative")
        if self.event_count != (
            self.long_liquidation_count + self.short_liquidation_count
        ):
            raise ValueError("liquidation calibration counts do not reconcile")
        values = (
            self.long_estimated_notional_usdt,
            self.short_estimated_notional_usdt,
            self.total_estimated_notional_usdt,
            self.signed_long_minus_short_notional_usdt,
            self.normalized_long_minus_short_imbalance,
            self.largest_event_estimated_notional_usdt,
        )
        if any(not value.is_finite() for value in values):
            raise ValueError("liquidation calibration numerics must be finite")
        if min(
            self.long_estimated_notional_usdt,
            self.short_estimated_notional_usdt,
            self.total_estimated_notional_usdt,
            self.largest_event_estimated_notional_usdt,
        ) < 0:
            raise ValueError("liquidation calibration notionals cannot be negative")
        expected_total = (
            self.long_estimated_notional_usdt
            + self.short_estimated_notional_usdt
        )
        if self.total_estimated_notional_usdt != expected_total:
            raise ValueError("liquidation calibration total notional does not reconcile")
        expected_signed = (
            self.long_estimated_notional_usdt
            - self.short_estimated_notional_usdt
        )
        if self.signed_long_minus_short_notional_usdt != expected_signed:
            raise ValueError("liquidation calibration signed notional does not reconcile")
        imbalance = self.normalized_long_minus_short_imbalance
        if not Decimal("-1") <= imbalance <= Decimal("1"):
            raise ValueError("liquidation calibration imbalance must be within [-1, 1]")
        if self.event_count == 0:
            if not self.known_zero:
                raise ValueError("zero-event liquidation context must be a known zero")
            if any(value != 0 for value in values):
                raise ValueError("known-zero liquidation context must contain zero metrics")
            return
        if self.known_zero:
            raise ValueError("non-empty liquidation context cannot be known-zero")
        if self.total_estimated_notional_usdt <= 0:
            raise ValueError("non-empty liquidation context requires positive notional")
        if self.largest_event_estimated_notional_usdt <= 0:
            raise ValueError("non-empty liquidation context requires positive largest event")
        if imbalance != expected_signed / self.total_estimated_notional_usdt:
            raise ValueError("liquidation calibration imbalance does not reconcile")

    @property
    def absolute_pressure(self) -> str:
        self.validate()
        if self.known_zero:
            return "KNOWN_ZERO"
        if self.signed_long_minus_short_notional_usdt > 0:
            return "LONG_LIQUIDATIONS_DOMINANT"
        if self.signed_long_minus_short_notional_usdt < 0:
            return "SHORT_LIQUIDATIONS_DOMINANT"
        return "BALANCED_NONZERO"

    def relative_pressure(self, trade_side: str) -> str:
        if trade_side not in {"LONG", "SHORT"}:
            raise ValueError("liquidation calibration trade side is unsupported")
        pressure = self.absolute_pressure
        if pressure in {"KNOWN_ZERO", "BALANCED_NONZERO"}:
            return pressure
        same_side = (
            trade_side == "LONG" and pressure == "LONG_LIQUIDATIONS_DOMINANT"
        ) or (
            trade_side == "SHORT" and pressure == "SHORT_LIQUIDATIONS_DOMINANT"
        )
        return (
            "SAME_SIDE_LIQUIDATIONS_DOMINANT"
            if same_side
            else "OPPOSITE_SIDE_LIQUIDATIONS_DOMINANT"
        )


@dataclass(frozen=True)
class CryptoProspectiveLiquidationCalibrationObservation:
    base: CryptoProspectiveCalibrationObservation
    context_state: str
    coverage_reason_codes: tuple[str, ...]
    windows: tuple[CryptoLiquidationCalibrationWindow, ...]

    def validate(self) -> None:
        self.base.validate()
        if self.context_state not in _CONTEXT_STATES:
            raise ValueError("liquidation calibration context state is unsupported")
        if len(set(self.coverage_reason_codes)) != len(self.coverage_reason_codes):
            raise ValueError("liquidation calibration coverage reasons must be unique")
        if self.context_state == "COVERAGE_QUALIFIED":
            if self.coverage_reason_codes:
                raise ValueError("qualified liquidation context cannot carry blockers")
            if tuple(item.window_minutes for item in self.windows) != _WINDOWS:
                raise ValueError("qualified liquidation context requires 5m/15m/60m windows")
            for item in self.windows:
                item.validate()
        else:
            if self.windows:
                raise ValueError("unavailable liquidation context cannot carry windows")
            if self.context_state == "COVERAGE_UNQUALIFIED" and not self.coverage_reason_codes:
                raise ValueError("unqualified liquidation context requires blockers")
            if self.context_state == "NOT_MATERIALIZED" and self.coverage_reason_codes:
                raise ValueError("not-materialized context cannot invent coverage blockers")

    def window(self, minutes: int) -> CryptoLiquidationCalibrationWindow:
        if self.context_state != "COVERAGE_QUALIFIED":
            raise ValueError("liquidation window is unavailable without qualified coverage")
        for item in self.windows:
            if item.window_minutes == minutes:
                return item
        raise ValueError("liquidation calibration window is missing")


@dataclass(frozen=True)
class CryptoProspectiveLiquidationCalibrationDataset:
    base_dataset: CryptoProspectiveCalibrationDataset
    observations: tuple[CryptoProspectiveLiquidationCalibrationObservation, ...]

    def validate(self) -> None:
        self.base_dataset.validate()
        if len(self.observations) != len(self.base_dataset.observations):
            raise ValueError("liquidation calibration must retain every base observation")
        base_by_seed = {item.seed_id: item for item in self.base_dataset.observations}
        if len(base_by_seed) != len(self.base_dataset.observations):
            raise ValueError("base liquidation calibration dataset has duplicate seed")
        seen: set[str] = set()
        for item in self.observations:
            item.validate()
            seed_id = item.base.seed_id
            if seed_id in seen:
                raise ValueError("liquidation calibration dataset has duplicate seed")
            if base_by_seed.get(seed_id) != item.base:
                raise ValueError("liquidation calibration base observation drifted")
            seen.add(seed_id)


def diagnose_crypto_prospective_liquidation_calibration(
    dataset: CryptoProspectiveLiquidationCalibrationDataset,
    *,
    policy: CryptoLiquidationCalibrationPolicy | None = None,
) -> dict[str, Any]:
    """Describe liquidation-context discrimination without changing ranking or strategy."""

    dataset.validate()
    active = CryptoLiquidationCalibrationPolicy() if policy is None else policy
    active.validate()
    rows = dataset.observations
    qualified = tuple(
        item for item in rows if item.context_state == "COVERAGE_QUALIFIED"
    )
    unavailable = tuple(
        item for item in rows if item.context_state != "COVERAGE_QUALIFIED"
    )
    coverage_reason_counts: dict[str, int] = defaultdict(int)
    for item in unavailable:
        for reason in item.coverage_reason_codes:
            coverage_reason_counts[reason] += 1

    by_window_absolute: dict[str, dict[str, Any]] = {}
    by_window_relative: dict[str, dict[str, Any]] = {}
    by_window_relative_side: dict[str, dict[str, Any]] = {}
    by_window_relative_symbol: dict[str, dict[str, Any]] = {}
    for minutes in _WINDOWS:
        by_window_absolute[str(minutes)] = _group_table(
            qualified,
            key=lambda item, m=minutes: item.window(m).absolute_pressure,
            policy=active,
        )
        by_window_relative[str(minutes)] = _group_table(
            qualified,
            key=lambda item, m=minutes: item.window(m).relative_pressure(item.base.side),
            policy=active,
        )
        by_window_relative_side[str(minutes)] = _group_table(
            qualified,
            key=lambda item, m=minutes: (
                f"{item.base.side}|{item.window(m).relative_pressure(item.base.side)}"
            ),
            policy=active,
        )
        by_window_relative_symbol[str(minutes)] = _group_table(
            qualified,
            key=lambda item, m=minutes: (
                f"{item.base.symbol}|{item.window(m).relative_pressure(item.base.side)}"
            ),
            policy=active,
        )

    comparisons = {
        str(minutes): _pressure_comparisons(
            qualified,
            window_minutes=minutes,
            policy=active,
        )
        for minutes in _WINDOWS
    }
    return {
        "diagnostic": "BYBIT_PROSPECTIVE_LIQUIDATION_CALIBRATION",
        "base_raw_final_seed_count": dataset.base_dataset.raw_final_seed_count,
        "base_deduplicated_signal_count": len(dataset.base_dataset.observations),
        "coverage_qualified_count": len(qualified),
        "coverage_unavailable_count": len(unavailable),
        "coverage_qualified_rate": _ratio(len(qualified), len(rows)),
        "context_state_counts": _count_context_states(rows),
        "coverage_reason_counts": dict(sorted(coverage_reason_counts.items())),
        "coverage_bias_guard": (
            "all final base observations are retained; missing/unqualified liquidation context "
            "is reported rather than silently dropped from dataset accounting"
        ),
        "minimum_group_observations": active.minimum_group_observations,
        "minimum_comparison_observations": active.minimum_comparison_observations,
        "qualified_overall": _summary(qualified, policy=active),
        "by_window_absolute_pressure": by_window_absolute,
        "by_window_relative_pressure": by_window_relative,
        "by_window_relative_pressure_and_side": by_window_relative_side,
        "by_window_relative_pressure_and_symbol": by_window_relative_symbol,
        "relative_pressure_comparisons": comparisons,
        "pressure_group_contract": (
            "sign-only descriptive groups: known zero, balanced nonzero, same-side "
            "liquidations dominant, opposite-side liquidations dominant; no fitted threshold"
        ),
        "drawdown_contract": (
            "chronological cumulative outcome-sequence drawdown only; not shared-capital "
            "portfolio mark-to-market drawdown"
        ),
        "outcome_contract": "final prospective v112 15m/60m/240m outcomes only",
        "liquidation_feature_used_for_source_ranking": False,
        "parameter_retuning_performed": False,
        "ranking_weights_changed": False,
        "strategy_selection_allowed": False,
        "strategy_promotion_allowed": False,
        "trade_actionable": False,
        "operator_review_required": True,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
        "causal_claim_allowed": False,
        "statistical_significance_claim_allowed": False,
        "predictive_guarantee_allowed": False,
    }


def _pressure_comparisons(
    rows: Sequence[CryptoProspectiveLiquidationCalibrationObservation],
    *,
    window_minutes: int,
    policy: CryptoLiquidationCalibrationPolicy,
) -> dict[str, Any]:
    grouped: dict[str, tuple[CryptoProspectiveLiquidationCalibrationObservation, ...]] = {}
    labels = (
        "KNOWN_ZERO",
        "BALANCED_NONZERO",
        "SAME_SIDE_LIQUIDATIONS_DOMINANT",
        "OPPOSITE_SIDE_LIQUIDATIONS_DOMINANT",
    )
    for label in labels:
        grouped[label] = tuple(
            item
            for item in rows
            if item.window(window_minutes).relative_pressure(item.base.side) == label
        )
    same = grouped["SAME_SIDE_LIQUIDATIONS_DOMINANT"]
    opposite = grouped["OPPOSITE_SIDE_LIQUIDATIONS_DOMINANT"]
    return {
        "same_side_vs_opposite_side": _compare_groups(
            same,
            opposite,
            policy=policy,
        ),
        "group_counts": {label: len(values) for label, values in grouped.items()},
    }


def _group_table(
    rows: Sequence[CryptoProspectiveLiquidationCalibrationObservation],
    *,
    key: Callable[[CryptoProspectiveLiquidationCalibrationObservation], str],
    policy: CryptoLiquidationCalibrationPolicy,
) -> dict[str, Any]:
    grouped: dict[str, list[CryptoProspectiveLiquidationCalibrationObservation]] = defaultdict(list)
    for item in rows:
        grouped[key(item)].append(item)
    return {
        label: _summary(tuple(values), policy=policy)
        for label, values in sorted(grouped.items())
    }


def _summary(
    rows: Sequence[CryptoProspectiveLiquidationCalibrationObservation],
    *,
    policy: CryptoLiquidationCalibrationPolicy,
) -> dict[str, Any]:
    base_rows = tuple(item.base for item in rows)
    count = len(base_rows)
    ordered = tuple(
        item
        for item in base_rows
        if item.first_touch_state in {"TARGET_FIRST", "STOP_FIRST"}
    )
    target_first = sum(item.first_touch_state == "TARGET_FIRST" for item in ordered)
    horizons = {
        str(horizon): _horizon_summary(base_rows, horizon=horizon)
        for horizon in _HORIZONS
    }
    return {
        "observation_count": count,
        "sample_sufficient": count >= policy.minimum_group_observations,
        "ordered_touch_count": len(ordered),
        "target_first_count": target_first,
        "stop_first_count": len(ordered) - target_first,
        "target_first_rate_of_ordered_touches": _ratio(target_first, len(ordered)),
        "average_mfe_r": _average(tuple(item.mfe_r for item in base_rows)),
        "average_mae_r": _average(tuple(item.mae_r for item in base_rows)),
        "median_mfe_r": _median_decimal(tuple(item.mfe_r for item in base_rows)),
        "median_mae_r": _median_decimal(tuple(item.mae_r for item in base_rows)),
        "horizons": horizons,
    }


def _horizon_summary(
    rows: Sequence[CryptoProspectiveCalibrationObservation],
    *,
    horizon: int,
) -> dict[str, Any]:
    pnl: list[Decimal] = []
    returns: list[Decimal] = []
    for item in rows:
        directional_return, modeled_pnl = item.horizon_values(horizon)
        returns.append(directional_return)
        pnl.append(modeled_pnl)
    values = tuple(pnl)
    positive = sum(value > 0 for value in values)
    zero = sum(value == 0 for value in values)
    return {
        "observation_count": len(values),
        "win_rate": _ratio(positive, len(values)),
        "positive_count": positive,
        "zero_count": zero,
        "negative_count": len(values) - positive - zero,
        "pnl": _pnl_summary(values),
        "average_directional_return_fraction": _average(tuple(returns)),
        "median_directional_return_fraction": _median_decimal(tuple(returns)),
        "chronological_sequence_drawdown_usdt": _sequence_drawdown(values),
    }


def _compare_groups(
    left: Sequence[CryptoProspectiveLiquidationCalibrationObservation],
    right: Sequence[CryptoProspectiveLiquidationCalibrationObservation],
    *,
    policy: CryptoLiquidationCalibrationPolicy,
) -> dict[str, Any]:
    left_summary = _summary(left, policy=policy)
    right_summary = _summary(right, policy=policy)
    sufficient = (
        len(left) >= policy.minimum_comparison_observations
        and len(right) >= policy.minimum_comparison_observations
    )
    return {
        "left_observation_count": len(left),
        "right_observation_count": len(right),
        "comparison_sample_sufficient": sufficient,
        "average_mfe_r_delta": _delta(
            left_summary["average_mfe_r"],
            right_summary["average_mfe_r"],
        ),
        "average_mae_r_delta": _delta(
            left_summary["average_mae_r"],
            right_summary["average_mae_r"],
        ),
        "horizon_deltas": {
            str(horizon): _horizon_delta(left_summary, right_summary, horizon)
            for horizon in _HORIZONS
        },
    }


def _horizon_delta(
    left: Mapping[str, Any],
    right: Mapping[str, Any],
    horizon: int,
) -> dict[str, str | None]:
    left_horizon = left["horizons"][str(horizon)]
    right_horizon = right["horizons"][str(horizon)]
    return {
        "average_pnl_usdt_delta": _delta(
            left_horizon["pnl"]["average_usdt"],
            right_horizon["pnl"]["average_usdt"],
        ),
        "win_rate_delta": _delta(
            left_horizon["win_rate"],
            right_horizon["win_rate"],
        ),
        "average_directional_return_fraction_delta": _delta(
            left_horizon["average_directional_return_fraction"],
            right_horizon["average_directional_return_fraction"],
        ),
    }


def _pnl_summary(values: Sequence[Decimal]) -> dict[str, str | int | None]:
    wins = tuple(value for value in values if value > 0)
    losses = tuple(value for value in values if value < 0)
    total = sum(values, start=_ZERO)
    gross_profit = sum(wins, start=_ZERO)
    gross_loss = -sum(losses, start=_ZERO)
    return {
        "observation_count": len(values),
        "total_usdt": str(total),
        "average_usdt": None if not values else str(total / Decimal(len(values))),
        "median_usdt": _median_decimal(values),
        "profit_factor": None if gross_loss == 0 else str(gross_profit / gross_loss),
    }


def _sequence_drawdown(values: Sequence[Decimal]) -> str:
    cumulative = _ZERO
    peak = _ZERO
    maximum_drawdown = _ZERO
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        maximum_drawdown = max(maximum_drawdown, peak - cumulative)
    return str(maximum_drawdown)


def _count_context_states(
    rows: Sequence[CryptoProspectiveLiquidationCalibrationObservation],
) -> dict[str, int]:
    counts = {state: 0 for state in sorted(_CONTEXT_STATES)}
    for item in rows:
        counts[item.context_state] += 1
    return counts


def _average(values: Sequence[Decimal]) -> str | None:
    if not values:
        return None
    return str(sum(values, start=_ZERO) / Decimal(len(values)))


def _median_decimal(values: Sequence[Decimal]) -> str | None:
    if not values:
        return None
    return str(median(values))


def _ratio(numerator: int, denominator: int) -> str | None:
    if denominator == 0:
        return None
    return str(Decimal(numerator) / Decimal(denominator))


def _delta(left: Any, right: Any) -> str | None:
    if left is None or right is None:
        return None
    return str(Decimal(str(left)) - Decimal(str(right)))


__all__ = [
    "CryptoLiquidationCalibrationPolicy",
    "CryptoLiquidationCalibrationWindow",
    "CryptoProspectiveLiquidationCalibrationDataset",
    "CryptoProspectiveLiquidationCalibrationObservation",
    "diagnose_crypto_prospective_liquidation_calibration",
]
