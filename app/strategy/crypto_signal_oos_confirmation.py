from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from app.strategy.crypto_prospective_exact_cell_matrix import (
    CryptoProspectiveExactCellDataset,
    CryptoProspectiveExactCellObservation,
)

_ZERO = Decimal("0")
_ONE = Decimal("1")
_Z_95 = 1.959963984540054


class CryptoSignalOosStatus(StrEnum):
    OOS_NOT_OBSERVED = "OOS_NOT_OBSERVED"
    OOS_INSUFFICIENT = "OOS_INSUFFICIENT"
    OOS_CONFIRMED = "OOS_CONFIRMED"
    OOS_REJECTED = "OOS_REJECTED"


@dataclass(frozen=True)
class CryptoSignalOosConfirmationPolicy:
    minimum_historical_trades: int = 5
    minimum_oos_observations: int = 30

    def validate(self) -> None:
        if not 1 <= self.minimum_historical_trades <= 100_000:
            raise ValueError("signal OOS minimum historical trades is invalid")
        if not 5 <= self.minimum_oos_observations <= 100_000:
            raise ValueError("signal OOS minimum prospective observations is invalid")


@dataclass(frozen=True)
class CryptoHistoricalPerfectEvidenceCell:
    evidence_snapshot_id: str
    evidence_snapshot_observed_at: str
    cell_key: str
    symbol: str
    side: str
    market_regime: str
    open_interest_regime: str
    crowding_regime: str
    prior_funding_regime: str
    stress_regime: str
    historical_trade_count: int
    historical_win_rate: Decimal
    historical_total_net_pnl_usdt: Decimal
    historical_average_net_pnl_usdt: Decimal
    historical_profit_factor: Decimal | None
    historical_average_mfe_r: Decimal | None
    historical_average_mae_r: Decimal | None

    def validate(self) -> None:
        if len(self.evidence_snapshot_id) != 64 or any(
            char not in "0123456789abcdef" for char in self.evidence_snapshot_id
        ):
            raise ValueError("signal OOS snapshot id must be lowercase sha256")
        _parse_time(self.evidence_snapshot_observed_at)
        texts = (
            self.cell_key,
            self.symbol,
            self.side,
            self.market_regime,
            self.open_interest_regime,
            self.crowding_regime,
            self.prior_funding_regime,
            self.stress_regime,
        )
        if any(not value or value != value.strip() for value in texts):
            raise ValueError("signal OOS candidate contains empty/unnormalized text")
        if self.symbol != self.symbol.upper() or not self.symbol.endswith("USDT"):
            raise ValueError("signal OOS candidate symbol is invalid")
        if self.side not in {"LONG", "SHORT"}:
            raise ValueError("signal OOS candidate side is invalid")
        if self.historical_trade_count <= 0:
            raise ValueError("signal OOS historical trade count must be positive")
        values = (
            self.historical_win_rate,
            self.historical_total_net_pnl_usdt,
            self.historical_average_net_pnl_usdt,
            self.historical_profit_factor,
            self.historical_average_mfe_r,
            self.historical_average_mae_r,
        )
        if any(value is not None and not value.is_finite() for value in values):
            raise ValueError("signal OOS historical numerics must be finite")
        if self.historical_win_rate != _ONE:
            raise ValueError("signal OOS candidate must be frozen at 100% historical win rate")
        if self.historical_total_net_pnl_usdt <= 0 or self.historical_average_net_pnl_usdt <= 0:
            raise ValueError("signal OOS historical perfect candidate must have positive PnL")
        if self.historical_profit_factor is not None and self.historical_profit_factor < 0:
            raise ValueError("signal OOS historical PF cannot be negative")

    @property
    def semantic_key(self) -> tuple[str, ...]:
        return (
            self.symbol,
            self.side,
            self.market_regime,
            self.open_interest_regime,
            self.crowding_regime,
            self.prior_funding_regime,
            self.stress_regime,
            self.cell_key,
        )


@dataclass(frozen=True)
class CryptoHistoricalPerfectEvidenceSnapshot:
    evidence_snapshot_id: str
    observed_at: str
    minimum_cell_trades: int
    candidates: tuple[CryptoHistoricalPerfectEvidenceCell, ...]

    def validate(self) -> None:
        if len(self.evidence_snapshot_id) != 64 or any(
            char not in "0123456789abcdef" for char in self.evidence_snapshot_id
        ):
            raise ValueError("signal OOS historical snapshot id is invalid")
        _parse_time(self.observed_at)
        if self.minimum_cell_trades <= 0:
            raise ValueError("signal OOS historical snapshot minimum cell trades is invalid")
        seen: set[tuple[str, ...]] = set()
        for candidate in self.candidates:
            candidate.validate()
            if candidate.evidence_snapshot_id != self.evidence_snapshot_id:
                raise ValueError("signal OOS candidate snapshot id drifted")
            if candidate.evidence_snapshot_observed_at != self.observed_at:
                raise ValueError("signal OOS candidate snapshot timestamp drifted")
            if candidate.semantic_key in seen:
                raise ValueError("signal OOS historical snapshot has duplicate candidate")
            seen.add(candidate.semantic_key)


def confirm_crypto_historical_perfect_cells_oos(
    snapshot: CryptoHistoricalPerfectEvidenceSnapshot,
    dataset: CryptoProspectiveExactCellDataset,
    *,
    policy: CryptoSignalOosConfirmationPolicy | None = None,
) -> dict[str, Any]:
    """Test one frozen historical 100% exact-cell snapshot on later prospective outcomes.

    The success rule is deliberately fixed and conservative: a prospective observation confirms
    the historical perfect hypothesis only when the target was touched before the stop and the
    final modeled 240-minute PnL is positive. Any other final outcome rejects the *perfect*
    hypothesis for that cell; it is not silently dropped or threshold-fitted away.
    """

    snapshot.validate()
    dataset.validate()
    active = CryptoSignalOosConfirmationPolicy() if policy is None else policy
    active.validate()
    if snapshot.minimum_cell_trades < active.minimum_historical_trades:
        raise ValueError("signal OOS snapshot was built with insufficient historical support")

    cutoff = _parse_time(snapshot.observed_at)
    usable = tuple(
        item
        for item in dataset.observations
        if item.cell_context_state == "CELL_COMPLETE"
        and _parse_time(item.prospective.base.signal_available_at) > cutoff
    )
    by_key: dict[tuple[str, ...], list[CryptoProspectiveExactCellObservation]] = defaultdict(list)
    for item in usable:
        by_key[_observation_semantic_key(item)].append(item)

    rows = [
        _confirm_candidate(candidate, tuple(by_key.get(candidate.semantic_key, ())), active=active)
        for candidate in snapshot.candidates
        if candidate.historical_trade_count >= active.minimum_historical_trades
    ]
    rows.sort(
        key=lambda item: (
            _status_order(str(item["oos_status"])),
            int(item["oos_observation_count"]),
            int(item["historical_trade_count"]),
            str(item["cell_key"]),
        ),
        reverse=True,
    )
    counts = Counter(str(item["oos_status"]) for item in rows)
    return {
        "diagnostic": "BYBIT_SIGNAL_HISTORICAL_PERFECT_EXACT_CELL_OOS_CONFIRMATION_V1",
        "evidence_snapshot_id": snapshot.evidence_snapshot_id,
        "evidence_snapshot_observed_at": snapshot.observed_at,
        "historical_candidate_count": len(rows),
        "usable_prospective_observation_count": len(usable),
        "minimum_historical_trades": active.minimum_historical_trades,
        "minimum_oos_observations": active.minimum_oos_observations,
        "status_counts": dict(sorted(counts.items())),
        "candidates": rows,
        "historical_candidate_contract": (
            "candidate set is frozen from one immutable v111 evidence snapshot before OOS data"
        ),
        "oos_success_contract": (
            "TARGET_FIRST and positive final 240m modeled net PnL; every other final observation "
            "rejects the historical-perfect hypothesis"
        ),
        "lookahead_prevention": (
            "only final v112 observations with signal_available_at strictly later than the frozen "
            "historical evidence snapshot timestamp are eligible"
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


def _confirm_candidate(
    candidate: CryptoHistoricalPerfectEvidenceCell,
    observations: Sequence[CryptoProspectiveExactCellObservation],
    *,
    active: CryptoSignalOosConfirmationPolicy,
) -> dict[str, Any]:
    ordered = tuple(
        sorted(
            observations,
            key=lambda item: (
                _parse_time(item.prospective.base.signal_available_at),
                item.prospective.base.seed_id,
            ),
        )
    )
    successes = tuple(item for item in ordered if _strict_success(item))
    failures = tuple(item for item in ordered if not _strict_success(item))
    count = len(ordered)
    if count == 0:
        status = CryptoSignalOosStatus.OOS_NOT_OBSERVED
    elif failures:
        status = CryptoSignalOosStatus.OOS_REJECTED
    elif count < active.minimum_oos_observations:
        status = CryptoSignalOosStatus.OOS_INSUFFICIENT
    else:
        status = CryptoSignalOosStatus.OOS_CONFIRMED

    touch_counts = Counter(item.prospective.base.first_touch_state for item in ordered)
    pnl_240 = tuple(item.prospective.base.horizon_240_modeled_net_pnl_usdt for item in ordered)
    return {
        "cell_key": candidate.cell_key,
        "symbol": candidate.symbol,
        "side": candidate.side,
        "market_regime": candidate.market_regime,
        "open_interest_regime": candidate.open_interest_regime,
        "crowding_regime": candidate.crowding_regime,
        "prior_funding_regime": candidate.prior_funding_regime,
        "stress_regime": candidate.stress_regime,
        "historical_trade_count": candidate.historical_trade_count,
        "historical_win_rate": str(candidate.historical_win_rate),
        "historical_total_net_pnl_usdt": str(candidate.historical_total_net_pnl_usdt),
        "historical_average_net_pnl_usdt": str(candidate.historical_average_net_pnl_usdt),
        "historical_profit_factor": (
            None if candidate.historical_profit_factor is None else str(candidate.historical_profit_factor)
        ),
        "oos_status": status.value,
        "oos_observation_count": count,
        "oos_success_count": len(successes),
        "oos_failure_count": len(failures),
        "oos_success_rate": None if count == 0 else len(successes) / count,
        "oos_success_wilson_lower_95": _wilson_lower(len(successes), count),
        "first_touch_state_counts": dict(sorted(touch_counts.items())),
        "positive_240m_count": sum(value > _ZERO for value in pnl_240),
        "non_positive_240m_count": sum(value <= _ZERO for value in pnl_240),
        "total_240m_modeled_net_pnl_usdt": str(sum(pnl_240, start=_ZERO)),
        "first_failure": None if not failures else _failure_identity(failures[0]),
        "perfect_hypothesis_survived_oos": not failures and count > 0,
        "sample_sufficient": count >= active.minimum_oos_observations,
    }


def _strict_success(item: CryptoProspectiveExactCellObservation) -> bool:
    base = item.prospective.base
    return (
        base.first_touch_state == "TARGET_FIRST"
        and base.horizon_240_modeled_net_pnl_usdt > _ZERO
    )


def _failure_identity(item: CryptoProspectiveExactCellObservation) -> dict[str, str]:
    base = item.prospective.base
    return {
        "seed_id": base.seed_id,
        "signal_available_at": base.signal_available_at,
        "first_touch_state": base.first_touch_state,
        "horizon_240_modeled_net_pnl_usdt": str(base.horizon_240_modeled_net_pnl_usdt),
    }


def _observation_semantic_key(item: CryptoProspectiveExactCellObservation) -> tuple[str, ...]:
    if item.source_cell is None:
        raise ValueError("signal OOS observation has no source cell")
    base = item.prospective.base
    source = item.source_cell
    return (
        base.symbol,
        base.side,
        source.market_regime,
        source.open_interest_regime,
        source.crowding_regime,
        source.prior_funding_regime,
        source.stress_regime,
        source.evidence_cell_key,
    )


def _wilson_lower(successes: int, total: int) -> float | None:
    if total <= 0:
        return None
    p = successes / total
    z2 = _Z_95 * _Z_95
    denominator = 1.0 + z2 / total
    center = p + z2 / (2.0 * total)
    margin = _Z_95 * math.sqrt((p * (1.0 - p) + z2 / (4.0 * total)) / total)
    return max(0.0, (center - margin) / denominator)


def _status_order(value: str) -> int:
    return {
        CryptoSignalOosStatus.OOS_CONFIRMED.value: 4,
        CryptoSignalOosStatus.OOS_INSUFFICIENT.value: 3,
        CryptoSignalOosStatus.OOS_NOT_OBSERVED.value: 2,
        CryptoSignalOosStatus.OOS_REJECTED.value: 1,
    }[value]


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("signal OOS timestamp is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("signal OOS timestamp must be timezone-aware")
    return parsed


__all__ = [
    "CryptoHistoricalPerfectEvidenceCell",
    "CryptoHistoricalPerfectEvidenceSnapshot",
    "CryptoSignalOosConfirmationPolicy",
    "CryptoSignalOosStatus",
    "confirm_crypto_historical_perfect_cells_oos",
]
