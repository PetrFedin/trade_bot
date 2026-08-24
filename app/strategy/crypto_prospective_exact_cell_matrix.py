from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from statistics import median
from typing import Any

from app.strategy.crypto_prospective_liquidation_calibration import (
    CryptoProspectiveLiquidationCalibrationObservation,
)

_ZERO = Decimal("0")
_HORIZONS = (15, 60, 240)


@dataclass(frozen=True)
class CryptoProspectiveExactCellPolicy:
    minimum_cell_observations: int = 30

    def validate(self) -> None:
        if not 5 <= self.minimum_cell_observations <= 10_000:
            raise ValueError("prospective exact-cell minimum must be within [5, 10000]")


@dataclass(frozen=True)
class CryptoProspectiveSourceEvidenceCell:
    evidence_cell_key: str
    market_regime: str
    open_interest_regime: str
    crowding_regime: str
    prior_funding_regime: str
    stress_regime: str
    stress_score: int
    historical_trade_count: int
    historical_sample_sufficient: bool
    historical_profit_factor: Decimal | None
    historical_win_rate: Decimal | None
    historical_total_net_pnl_usdt: Decimal | None
    historical_average_net_pnl_usdt: Decimal | None
    historical_average_mfe_r: Decimal | None
    historical_average_mae_r: Decimal | None
    historical_drawdown_usdt: Decimal | None
    positive_historical_evidence: bool

    def validate(self) -> None:
        text_values = (
            self.evidence_cell_key,
            self.market_regime,
            self.open_interest_regime,
            self.crowding_regime,
            self.prior_funding_regime,
            self.stress_regime,
        )
        if any(not value or value != value.strip() for value in text_values):
            raise ValueError("prospective source evidence cell has empty/unnormalized label")
        if not 0 <= self.stress_score <= 5:
            raise ValueError("prospective source evidence stress score must be within [0, 5]")
        if self.historical_trade_count < 0:
            raise ValueError("prospective source evidence trade count cannot be negative")
        numeric_values = (
            self.historical_profit_factor,
            self.historical_win_rate,
            self.historical_total_net_pnl_usdt,
            self.historical_average_net_pnl_usdt,
            self.historical_average_mfe_r,
            self.historical_average_mae_r,
            self.historical_drawdown_usdt,
        )
        if any(value is not None and not value.is_finite() for value in numeric_values):
            raise ValueError("prospective source evidence numerics must be finite")
        if self.historical_profit_factor is not None and self.historical_profit_factor < 0:
            raise ValueError("prospective source evidence PF cannot be negative")
        if self.historical_win_rate is not None:
            if not _ZERO <= self.historical_win_rate <= Decimal("1"):
                raise ValueError("prospective source evidence win rate must be within [0, 1]")
        if self.historical_drawdown_usdt is not None and self.historical_drawdown_usdt < 0:
            raise ValueError("prospective source evidence drawdown cannot be negative")

    @property
    def semantic_key(self) -> tuple[str, str, str, str, str, int]:
        return (
            self.market_regime,
            self.open_interest_regime,
            self.crowding_regime,
            self.prior_funding_regime,
            self.stress_regime,
            self.stress_score,
        )


@dataclass(frozen=True)
class CryptoProspectiveExactCellObservation:
    prospective: CryptoProspectiveLiquidationCalibrationObservation
    cell_context_state: str
    cell_unavailable_reason: str | None
    source_cell: CryptoProspectiveSourceEvidenceCell | None

    def validate(self) -> None:
        self.prospective.validate()
        if self.cell_context_state not in {"CELL_COMPLETE", "CELL_UNAVAILABLE"}:
            raise ValueError("prospective exact-cell context state is unsupported")
        if self.cell_context_state == "CELL_COMPLETE":
            if self.cell_unavailable_reason is not None or self.source_cell is None:
                raise ValueError("complete prospective exact-cell context is inconsistent")
            self.source_cell.validate()
        else:
            if self.source_cell is not None or not self.cell_unavailable_reason:
                raise ValueError("unavailable prospective exact-cell context is inconsistent")


@dataclass(frozen=True)
class CryptoProspectiveExactCellDataset:
    observations: tuple[CryptoProspectiveExactCellObservation, ...]

    def validate(self) -> None:
        seed_ids: set[str] = set()
        key_semantics: dict[str, tuple[str, str, str, str, str, int]] = {}
        for item in self.observations:
            item.validate()
            seed_id = item.prospective.base.seed_id
            if seed_id in seed_ids:
                raise ValueError("prospective exact-cell dataset contains duplicate seed")
            seed_ids.add(seed_id)
            if item.source_cell is None:
                continue
            existing = key_semantics.get(item.source_cell.evidence_cell_key)
            if existing is not None and existing != item.source_cell.semantic_key:
                raise ValueError("one evidence_cell_key maps to divergent regime semantics")
            key_semantics[item.source_cell.evidence_cell_key] = item.source_cell.semantic_key


def diagnose_crypto_prospective_exact_cell_matrix(
    dataset: CryptoProspectiveExactCellDataset,
    *,
    policy: CryptoProspectiveExactCellPolicy | None = None,
) -> dict[str, Any]:
    """Build the forward evidence matrix without retuning or changing source rank."""

    dataset.validate()
    active = CryptoProspectiveExactCellPolicy() if policy is None else policy
    active.validate()
    complete = tuple(
        item for item in dataset.observations if item.cell_context_state == "CELL_COMPLETE"
    )
    unavailable = tuple(
        item for item in dataset.observations if item.cell_context_state == "CELL_UNAVAILABLE"
    )
    reasons: dict[str, int] = defaultdict(int)
    for item in unavailable:
        reasons[str(item.cell_unavailable_reason)] += 1
    exact = _group_table(complete, key=_exact_label, policy=active)
    symbol_side = _group_table(
        complete,
        key=lambda item: f"{item.prospective.base.symbol}|{item.prospective.base.side}",
        policy=active,
    )
    dimensions = {
        "market_regime": _group_table(
            complete,
            key=lambda item: _source(item).market_regime,
            policy=active,
        ),
        "open_interest_regime": _group_table(
            complete,
            key=lambda item: _source(item).open_interest_regime,
            policy=active,
        ),
        "crowding_regime": _group_table(
            complete,
            key=lambda item: _source(item).crowding_regime,
            policy=active,
        ),
        "prior_funding_regime": _group_table(
            complete,
            key=lambda item: _source(item).prior_funding_regime,
            policy=active,
        ),
        "stress_regime": _group_table(
            complete,
            key=lambda item: _source(item).stress_regime,
            policy=active,
        ),
    }
    liquidation_augmented = _group_table(
        tuple(
            item
            for item in complete
            if item.prospective.context_state == "COVERAGE_QUALIFIED"
        ),
        key=lambda item: (
            f"{_exact_label(item)}|LIQ15="
            f"{item.prospective.window(15).relative_pressure(item.prospective.base.side)}"
        ),
        policy=active,
    )
    return {
        "diagnostic": "BYBIT_PROSPECTIVE_EXACT_EVIDENCE_CELL_MATRIX",
        "observation_count": len(dataset.observations),
        "cell_complete_count": len(complete),
        "cell_unavailable_count": len(unavailable),
        "cell_unavailable_reason_counts": dict(sorted(reasons.items())),
        "minimum_cell_observations": active.minimum_cell_observations,
        "exact_cell_matrix": exact,
        "symbol_side_matrix": symbol_side,
        "dimension_tables": dimensions,
        "liquidation_augmented_exact_cell_matrix_15m": liquidation_augmented,
        "historical_reference_contract": (
            "historical metrics are the immutable exact-cell evidence values that existed in the "
            "source v111 candidate at signal time; prospective outcomes never rewrite them"
        ),
        "prospective_outcome_contract": "final v112 15m/60m/240m outcomes only",
        "liquidation_contract": (
            "15m liquidation augmentation uses only coverage-qualified v117 pre-signal context"
        ),
        "drawdown_contract": (
            "chronological cumulative prospective outcome-sequence drawdown; not shared-capital "
            "portfolio mark-to-market drawdown"
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


def _exact_label(item: CryptoProspectiveExactCellObservation) -> str:
    source = _source(item)
    base = item.prospective.base
    return "|".join(
        (
            base.symbol,
            base.side,
            source.market_regime,
            source.open_interest_regime,
            source.crowding_regime,
            source.prior_funding_regime,
            source.stress_regime,
            f"STRESS_SCORE_{source.stress_score}",
            source.evidence_cell_key,
        )
    )


def _source(item: CryptoProspectiveExactCellObservation) -> CryptoProspectiveSourceEvidenceCell:
    if item.source_cell is None:
        raise ValueError("prospective exact-cell source is unavailable")
    return item.source_cell


def _group_table(
    rows: Sequence[CryptoProspectiveExactCellObservation],
    *,
    key: Callable[[CryptoProspectiveExactCellObservation], str],
    policy: CryptoProspectiveExactCellPolicy,
) -> dict[str, Any]:
    grouped: dict[str, list[CryptoProspectiveExactCellObservation]] = defaultdict(list)
    for item in rows:
        grouped[key(item)].append(item)
    return {
        label: _summary(tuple(values), policy=policy)
        for label, values in sorted(grouped.items())
    }


def _summary(
    rows: Sequence[CryptoProspectiveExactCellObservation],
    *,
    policy: CryptoProspectiveExactCellPolicy,
) -> dict[str, Any]:
    base = tuple(item.prospective.base for item in rows)
    sources = tuple(_source(item) for item in rows)
    ordered = tuple(
        item for item in base if item.first_touch_state in {"TARGET_FIRST", "STOP_FIRST"}
    )
    target_first = sum(item.first_touch_state == "TARGET_FIRST" for item in ordered)
    return {
        "sample_size": len(rows),
        "sample_sufficient": len(rows) >= policy.minimum_cell_observations,
        "qualification_state_counts": _count(
            tuple(item.qualification_state for item in base)
        ),
        "ordered_touch_count": len(ordered),
        "target_first_rate_of_ordered_touches": _ratio(target_first, len(ordered)),
        "average_mfe_r": _average(tuple(item.mfe_r for item in base)),
        "median_mfe_r": _median(tuple(item.mfe_r for item in base)),
        "average_mae_r": _average(tuple(item.mae_r for item in base)),
        "median_mae_r": _median(tuple(item.mae_r for item in base)),
        "prospective_horizons": {
            str(horizon): _horizon_summary(base, horizon=horizon)
            for horizon in _HORIZONS
        },
        "source_historical_reference": _historical_reference(sources),
    }


def _historical_reference(
    sources: Sequence[CryptoProspectiveSourceEvidenceCell],
) -> dict[str, Any]:
    return {
        "source_observation_count": len(sources),
        "historical_trade_count": _range_stats(
            tuple(Decimal(item.historical_trade_count) for item in sources)
        ),
        "historical_profit_factor": _optional_range_stats(
            tuple(item.historical_profit_factor for item in sources)
        ),
        "historical_win_rate": _optional_range_stats(
            tuple(item.historical_win_rate for item in sources)
        ),
        "historical_average_net_pnl_usdt": _optional_range_stats(
            tuple(item.historical_average_net_pnl_usdt for item in sources)
        ),
        "historical_average_mfe_r": _optional_range_stats(
            tuple(item.historical_average_mfe_r for item in sources)
        ),
        "historical_average_mae_r": _optional_range_stats(
            tuple(item.historical_average_mae_r for item in sources)
        ),
        "historical_drawdown_usdt": _optional_range_stats(
            tuple(item.historical_drawdown_usdt for item in sources)
        ),
        "historical_sample_sufficient_rate": _ratio(
            sum(item.historical_sample_sufficient for item in sources),
            len(sources),
        ),
        "positive_historical_evidence_rate": _ratio(
            sum(item.positive_historical_evidence for item in sources),
            len(sources),
        ),
    }


def _horizon_summary(base: Sequence[Any], *, horizon: int) -> dict[str, Any]:
    returns: list[Decimal] = []
    pnl: list[Decimal] = []
    for item in base:
        directional_return, modeled_pnl = item.horizon_values(horizon)
        returns.append(directional_return)
        pnl.append(modeled_pnl)
    pnl_values = tuple(pnl)
    wins = tuple(value for value in pnl_values if value > 0)
    losses = tuple(value for value in pnl_values if value < 0)
    gross_profit = sum(wins, start=_ZERO)
    gross_loss = -sum(losses, start=_ZERO)
    return {
        "observation_count": len(pnl_values),
        "total_pnl_usdt": str(sum(pnl_values, start=_ZERO)),
        "average_pnl_usdt": _average(pnl_values),
        "median_pnl_usdt": _median(pnl_values),
        "profit_factor": None if gross_loss == 0 else str(gross_profit / gross_loss),
        "win_rate": _ratio(len(wins), len(pnl_values)),
        "average_directional_return_fraction": _average(tuple(returns)),
        "median_directional_return_fraction": _median(tuple(returns)),
        "sequence_drawdown_usdt": _sequence_drawdown(pnl_values),
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


def _range_stats(values: Sequence[Decimal]) -> dict[str, str | None]:
    if not values:
        return {"min": None, "median": None, "max": None}
    return {
        "min": str(min(values)),
        "median": str(median(values)),
        "max": str(max(values)),
    }


def _optional_range_stats(values: Sequence[Decimal | None]) -> dict[str, Any]:
    known = tuple(value for value in values if value is not None)
    result: dict[str, Any] = _range_stats(known)
    result["known_count"] = len(known)
    result["missing_count"] = len(values) - len(known)
    return result


def _average(values: Sequence[Decimal]) -> str | None:
    if not values:
        return None
    return str(sum(values, start=_ZERO) / Decimal(len(values)))


def _median(values: Sequence[Decimal]) -> str | None:
    if not values:
        return None
    return str(median(values))


def _ratio(numerator: int, denominator: int) -> str | None:
    if denominator == 0:
        return None
    return str(Decimal(numerator) / Decimal(denominator))


def _count(values: Sequence[str]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for value in values:
        counts[value] += 1
    return dict(sorted(counts.items()))


__all__ = [
    "CryptoProspectiveExactCellDataset",
    "CryptoProspectiveExactCellObservation",
    "CryptoProspectiveExactCellPolicy",
    "CryptoProspectiveSourceEvidenceCell",
    "diagnose_crypto_prospective_exact_cell_matrix",
]
