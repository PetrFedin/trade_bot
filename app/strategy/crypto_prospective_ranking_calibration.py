from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from statistics import median
from typing import Any

_ZERO = Decimal("0")
_HORIZONS = (15, 60, 240)
_TRACKABLE_STATES = (
    "QUALIFIED_POSITIVE_EVIDENCE",
    "QUALIFIED_MIXED_EVIDENCE",
    "NO_SAMPLE_SUFFICIENT_EXACT_CELL",
    "DERIVATIVES_CONTEXT_INCOMPLETE",
)
_FIRST_TOUCH_STATES = frozenset(
    {"TARGET_FIRST", "STOP_FIRST", "AMBIGUOUS_SAME_BAR", "NEITHER"}
)
_RANK_BUCKETS = (
    (1, 1, "RANK_01"),
    (2, 3, "RANK_02_03"),
    (4, 5, "RANK_04_05"),
    (6, 10, "RANK_06_10"),
    (11, 20, "RANK_11_20"),
    (21, 50, "RANK_21_50"),
)


@dataclass(frozen=True)
class CryptoProspectiveCalibrationPolicy:
    minimum_group_observations: int = 30
    minimum_comparison_observations: int = 50

    def validate(self) -> None:
        if not 5 <= self.minimum_group_observations <= 10_000:
            raise ValueError("calibration minimum group observations must be within [5, 10000]")
        if not self.minimum_group_observations <= self.minimum_comparison_observations <= 50_000:
            raise ValueError(
                "calibration minimum comparison observations must be >= group minimum"
            )


@dataclass(frozen=True)
class CryptoProspectiveCalibrationObservation:
    seed_id: str
    evidence_rank: int
    market_rank: int
    qualification_state: str
    symbol: str
    side: str
    signal_available_at: str
    signal_quality_score: Decimal
    first_touch_state: str
    first_touch_modeled_net_pnl_usdt: Decimal | None
    mfe_r: Decimal
    mae_r: Decimal
    horizon_15_directional_return_fraction: Decimal
    horizon_15_modeled_net_pnl_usdt: Decimal
    horizon_60_directional_return_fraction: Decimal
    horizon_60_modeled_net_pnl_usdt: Decimal
    horizon_240_directional_return_fraction: Decimal
    horizon_240_modeled_net_pnl_usdt: Decimal

    def validate(self) -> None:
        if len(self.seed_id) != 64 or any(char not in "0123456789abcdef" for char in self.seed_id):
            raise ValueError("calibration seed id must be lowercase sha256")
        if not 1 <= self.evidence_rank <= 50 or not 1 <= self.market_rank <= 50:
            raise ValueError("calibration ranks must be within [1, 50]")
        if self.qualification_state not in _TRACKABLE_STATES:
            raise ValueError("calibration qualification state is unsupported")
        if (
            not self.symbol
            or self.symbol != self.symbol.strip().upper()
            or not self.symbol.endswith("USDT")
            or not self.symbol.isalnum()
        ):
            raise ValueError("calibration symbol must be normalized USDT")
        if self.side not in {"LONG", "SHORT"}:
            raise ValueError("calibration side must be LONG or SHORT")
        _parse_time(self.signal_available_at)
        if self.first_touch_state not in _FIRST_TOUCH_STATES:
            raise ValueError("calibration first-touch state must be final and non-incomplete")
        if self.first_touch_state in {"TARGET_FIRST", "STOP_FIRST"}:
            if self.first_touch_modeled_net_pnl_usdt is None:
                raise ValueError("ordered calibration touch requires modeled PnL")
        elif self.first_touch_modeled_net_pnl_usdt is not None:
            raise ValueError("unordered calibration touch cannot carry ordered-touch PnL")
        values = (
            self.signal_quality_score,
            self.mfe_r,
            self.mae_r,
            self.horizon_15_directional_return_fraction,
            self.horizon_15_modeled_net_pnl_usdt,
            self.horizon_60_directional_return_fraction,
            self.horizon_60_modeled_net_pnl_usdt,
            self.horizon_240_directional_return_fraction,
            self.horizon_240_modeled_net_pnl_usdt,
        )
        if any(not value.is_finite() for value in values):
            raise ValueError("calibration numeric values must be finite")
        if self.first_touch_modeled_net_pnl_usdt is not None:
            if not self.first_touch_modeled_net_pnl_usdt.is_finite():
                raise ValueError("calibration first-touch PnL must be finite")
        if self.mfe_r < 0 or self.mae_r > 0:
            raise ValueError("calibration MFE/MAE signs are invalid")

    def horizon_values(self, horizon: int) -> tuple[Decimal, Decimal]:
        if horizon == 15:
            return (
                self.horizon_15_directional_return_fraction,
                self.horizon_15_modeled_net_pnl_usdt,
            )
        if horizon == 60:
            return (
                self.horizon_60_directional_return_fraction,
                self.horizon_60_modeled_net_pnl_usdt,
            )
        if horizon == 240:
            return (
                self.horizon_240_directional_return_fraction,
                self.horizon_240_modeled_net_pnl_usdt,
            )
        raise ValueError("calibration horizon must be 15, 60 or 240 minutes")


@dataclass(frozen=True)
class CryptoProspectiveCalibrationDataset:
    raw_final_seed_count: int
    observations: tuple[CryptoProspectiveCalibrationObservation, ...]

    def validate(self) -> None:
        if self.raw_final_seed_count < 0:
            raise ValueError("calibration raw final seed count cannot be negative")
        if self.raw_final_seed_count < len(self.observations):
            raise ValueError("calibration raw count cannot be below deduplicated count")
        identities: set[tuple[str, str, str]] = set()
        seed_ids: set[str] = set()
        previous: tuple[datetime, str, str] | None = None
        for item in self.observations:
            item.validate()
            identity = (item.symbol, item.side, item.signal_available_at)
            if identity in identities:
                raise ValueError("calibration dataset contains duplicate signal identity")
            if item.seed_id in seed_ids:
                raise ValueError("calibration dataset contains duplicate seed id")
            identities.add(identity)
            seed_ids.add(item.seed_id)
            ordering = (_parse_time(item.signal_available_at), item.symbol, item.side)
            if previous is not None and ordering < previous:
                raise ValueError("calibration observations must be chronological")
            previous = ordering

    @property
    def deduplicated_observation_count(self) -> int:
        return len(self.observations)

    @property
    def duplicate_signal_observation_count(self) -> int:
        return self.raw_final_seed_count - len(self.observations)


def diagnose_crypto_prospective_ranking_calibration(
    dataset: CryptoProspectiveCalibrationDataset,
    *,
    policy: CryptoProspectiveCalibrationPolicy | None = None,
) -> dict[str, Any]:
    """Measure prospective ranking discrimination without changing strategy or rank weights."""

    dataset.validate()
    active = CryptoProspectiveCalibrationPolicy() if policy is None else policy
    active.validate()
    rows = dataset.observations

    overall = _summary(rows, policy=active)
    by_state = _group_table(rows, key=lambda item: item.qualification_state, policy=active)
    by_evidence_rank = _group_table(
        rows,
        key=lambda item: _rank_bucket(item.evidence_rank),
        policy=active,
    )
    by_market_rank = _group_table(
        rows,
        key=lambda item: _rank_bucket(item.market_rank),
        policy=active,
    )
    by_side = _group_table(rows, key=lambda item: item.side, policy=active)
    by_symbol = _group_table(rows, key=lambda item: item.symbol, policy=active)
    by_state_rank = _group_table(
        rows,
        key=lambda item: f"{item.qualification_state}|{_rank_bucket(item.evidence_rank)}",
        policy=active,
    )

    positive_rows = tuple(
        item for item in rows if item.qualification_state == "QUALIFIED_POSITIVE_EVIDENCE"
    )
    control_comparisons = {
        state: _compare_groups(
            positive_rows,
            tuple(item for item in rows if item.qualification_state == state),
            policy=active,
        )
        for state in _TRACKABLE_STATES
        if state != "QUALIFIED_POSITIVE_EVIDENCE"
    }

    rank_bucket_sequence = tuple(label for _low, _high, label in _RANK_BUCKETS)
    positive_rank_summaries = {
        bucket: _summary(
            tuple(item for item in positive_rows if _rank_bucket(item.evidence_rank) == bucket),
            policy=active,
        )
        for bucket in rank_bucket_sequence
    }
    return {
        "diagnostic": "BYBIT_PROSPECTIVE_RANKING_CALIBRATION",
        "raw_final_seed_count": dataset.raw_final_seed_count,
        "deduplicated_signal_observation_count": dataset.deduplicated_observation_count,
        "duplicate_signal_observation_count": dataset.duplicate_signal_observation_count,
        "deduplication_contract": (
            "one earliest observed seed per symbol x side x signal_available_at; "
            "one earliest final 240m outcome per retained seed"
        ),
        "minimum_group_observations": active.minimum_group_observations,
        "minimum_comparison_observations": active.minimum_comparison_observations,
        "overall": overall,
        "by_qualification_state": by_state,
        "by_evidence_rank_bucket": by_evidence_rank,
        "by_market_rank_bucket": by_market_rank,
        "by_side": by_side,
        "by_symbol": by_symbol,
        "by_state_and_evidence_rank_bucket": by_state_rank,
        "positive_evidence_vs_controls": control_comparisons,
        "positive_evidence_rank_bucket_sequence": list(rank_bucket_sequence),
        "positive_evidence_by_rank_bucket": positive_rank_summaries,
        "ranking_interpretation_contract": (
            "prospective descriptive discrimination only; no retrospective strategy tuning, "
            "causal claim, statistical-significance claim, or profitability guarantee"
        ),
        "horizon_contract": "15m / 60m / 240m outcomes after signal availability",
        "first_touch_ambiguity_contract": (
            "AMBIGUOUS_SAME_BAR is retained as ambiguity and excluded from ordered-touch hit rate"
        ),
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


def _group_table(
    rows: Sequence[CryptoProspectiveCalibrationObservation],
    *,
    key: Callable[[CryptoProspectiveCalibrationObservation], str],
    policy: CryptoProspectiveCalibrationPolicy,
) -> dict[str, Any]:
    grouped: dict[str, list[CryptoProspectiveCalibrationObservation]] = defaultdict(list)
    for item in rows:
        grouped[key(item)].append(item)
    return {
        group: _summary(tuple(values), policy=policy)
        for group, values in sorted(grouped.items())
    }


def _summary(
    rows: Sequence[CryptoProspectiveCalibrationObservation],
    *,
    policy: CryptoProspectiveCalibrationPolicy,
) -> dict[str, Any]:
    count = len(rows)
    touch_counts = {
        state: sum(item.first_touch_state == state for item in rows)
        for state in ("TARGET_FIRST", "STOP_FIRST", "AMBIGUOUS_SAME_BAR", "NEITHER")
    }
    ordered_touch_count = touch_counts["TARGET_FIRST"] + touch_counts["STOP_FIRST"]
    first_touch_pnl = tuple(
        item.first_touch_modeled_net_pnl_usdt
        for item in rows
        if item.first_touch_modeled_net_pnl_usdt is not None
    )
    horizons = {
        str(horizon): _horizon_summary(rows, horizon=horizon)
        for horizon in _HORIZONS
    }
    return {
        "observation_count": count,
        "sample_sufficient": count >= policy.minimum_group_observations,
        "target_first_count": touch_counts["TARGET_FIRST"],
        "stop_first_count": touch_counts["STOP_FIRST"],
        "ambiguous_same_bar_count": touch_counts["AMBIGUOUS_SAME_BAR"],
        "neither_count": touch_counts["NEITHER"],
        "ordered_touch_count": ordered_touch_count,
        "target_first_rate_of_ordered_touches": _ratio(
            touch_counts["TARGET_FIRST"],
            ordered_touch_count,
        ),
        "ambiguous_same_bar_rate": _ratio(
            touch_counts["AMBIGUOUS_SAME_BAR"],
            count,
        ),
        "neither_rate": _ratio(touch_counts["NEITHER"], count),
        "first_touch_modeled_net_pnl": _pnl_summary(first_touch_pnl),
        "average_mfe_r": _average(tuple(item.mfe_r for item in rows)),
        "average_mae_r": _average(tuple(item.mae_r for item in rows)),
        "median_mfe_r": _median_decimal(tuple(item.mfe_r for item in rows)),
        "median_mae_r": _median_decimal(tuple(item.mae_r for item in rows)),
        "average_signal_quality_score": _average(
            tuple(item.signal_quality_score for item in rows)
        ),
        "horizons": horizons,
    }


def _horizon_summary(
    rows: Sequence[CryptoProspectiveCalibrationObservation],
    *,
    horizon: int,
) -> dict[str, Any]:
    directional_returns: list[Decimal] = []
    net_pnl: list[Decimal] = []
    for item in rows:
        directional_return, modeled_pnl = item.horizon_values(horizon)
        directional_returns.append(directional_return)
        net_pnl.append(modeled_pnl)
    return {
        "observation_count": len(rows),
        "positive_net_pnl_count": sum(value > 0 for value in net_pnl),
        "zero_net_pnl_count": sum(value == 0 for value in net_pnl),
        "negative_net_pnl_count": sum(value < 0 for value in net_pnl),
        "positive_net_pnl_rate": _ratio(sum(value > 0 for value in net_pnl), len(net_pnl)),
        "modeled_net_pnl": _pnl_summary(tuple(net_pnl)),
        "average_directional_return_fraction": _average(tuple(directional_returns)),
        "median_directional_return_fraction": _median_decimal(tuple(directional_returns)),
    }


def _compare_groups(
    positive: Sequence[CryptoProspectiveCalibrationObservation],
    control: Sequence[CryptoProspectiveCalibrationObservation],
    *,
    policy: CryptoProspectiveCalibrationPolicy,
) -> dict[str, Any]:
    positive_summary = _summary(positive, policy=policy)
    control_summary = _summary(control, policy=policy)
    comparison_sufficient = (
        len(positive) >= policy.minimum_comparison_observations
        and len(control) >= policy.minimum_comparison_observations
    )
    horizon_deltas = {
        str(horizon): _horizon_delta(positive_summary, control_summary, horizon=horizon)
        for horizon in _HORIZONS
    }
    return {
        "positive_evidence_observation_count": len(positive),
        "control_observation_count": len(control),
        "comparison_sample_sufficient": comparison_sufficient,
        "ordered_target_first_rate_delta": _optional_decimal_delta(
            positive_summary["target_first_rate_of_ordered_touches"],
            control_summary["target_first_rate_of_ordered_touches"],
        ),
        "average_mfe_r_delta": _optional_decimal_delta(
            positive_summary["average_mfe_r"],
            control_summary["average_mfe_r"],
        ),
        "average_mae_r_delta": _optional_decimal_delta(
            positive_summary["average_mae_r"],
            control_summary["average_mae_r"],
        ),
        "horizon_deltas": horizon_deltas,
    }


def _horizon_delta(
    positive_summary: dict[str, Any],
    control_summary: dict[str, Any],
    *,
    horizon: int,
) -> dict[str, str | None]:
    positive = positive_summary["horizons"][str(horizon)]
    control = control_summary["horizons"][str(horizon)]
    return {
        "average_modeled_net_pnl_usdt_delta": _optional_decimal_delta(
            positive["modeled_net_pnl"]["average_usdt"],
            control["modeled_net_pnl"]["average_usdt"],
        ),
        "positive_net_pnl_rate_delta": _optional_decimal_delta(
            positive["positive_net_pnl_rate"],
            control["positive_net_pnl_rate"],
        ),
        "average_directional_return_fraction_delta": _optional_decimal_delta(
            positive["average_directional_return_fraction"],
            control["average_directional_return_fraction"],
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


def _rank_bucket(rank: int) -> str:
    for low, high, label in _RANK_BUCKETS:
        if low <= rank <= high:
            return label
    raise ValueError("calibration rank is outside supported buckets")


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


def _optional_decimal_delta(left: Any, right: Any) -> str | None:
    if left is None or right is None:
        return None
    return str(Decimal(str(left)) - Decimal(str(right)))


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("calibration timestamp must be timezone-aware")
    return parsed
