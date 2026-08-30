from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

from app.strategy.crypto_perp import CryptoSignal, CryptoTradePlan

_ZERO = Decimal("0")


@dataclass(frozen=True)
class CryptoPositionCandidate:
    signal: CryptoSignal
    plan: CryptoTradePlan

    @property
    def expected_net_r(self) -> Decimal:
        if self.plan.risk_budget_usdt <= 0:
            raise ValueError("position candidate risk budget must be positive")
        return self.plan.expected_net_edge_usd / self.plan.risk_budget_usdt

    @property
    def cost_to_target_fraction(self) -> Decimal:
        if self.plan.target_net_profit_usd <= 0:
            raise ValueError("position candidate target must be positive")
        return (
            self.plan.estimated_round_trip_cost_usdt
            / self.plan.target_net_profit_usd
        )


@dataclass(frozen=True)
class CryptoPositionRankInputs:
    """Minimal immutable inputs for the qualified economic shadow ordering."""

    symbol: str
    expected_net_r: Decimal
    expected_net_edge_usd: Decimal
    quality_score: Decimal
    cost_to_target_fraction: Decimal

    def validate(self) -> None:
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("position rank symbol must be normalized uppercase text")
        values = (
            self.expected_net_r,
            self.expected_net_edge_usd,
            self.quality_score,
            self.cost_to_target_fraction,
        )
        if any(not value.is_finite() for value in values):
            raise ValueError("position rank inputs must be finite")
        if self.expected_net_r <= 0 or self.expected_net_edge_usd <= 0:
            raise ValueError("position rank expected edge must be positive")
        if self.cost_to_target_fraction < 0:
            raise ValueError("position rank cost fraction cannot be negative")


def crypto_position_rank_inputs(
    candidate: CryptoPositionCandidate,
) -> CryptoPositionRankInputs:
    if candidate.signal.symbol != candidate.plan.symbol:
        raise ValueError("crypto position candidate signal/plan symbol mismatch")
    if candidate.signal.side is not candidate.plan.side:
        raise ValueError("crypto position candidate signal/plan side mismatch")
    inputs = CryptoPositionRankInputs(
        symbol=candidate.plan.symbol,
        expected_net_r=candidate.expected_net_r,
        expected_net_edge_usd=candidate.plan.expected_net_edge_usd,
        quality_score=candidate.plan.quality_score,
        cost_to_target_fraction=candidate.cost_to_target_fraction,
    )
    inputs.validate()
    return inputs


def crypto_position_rank_key(
    inputs: CryptoPositionRankInputs,
) -> tuple[Decimal, Decimal, Decimal, Decimal, str]:
    """Return the one canonical lexicographic key used by economic shadow ranking."""

    inputs.validate()
    return (
        -inputs.expected_net_r,
        -inputs.expected_net_edge_usd,
        -inputs.quality_score,
        inputs.cost_to_target_fraction,
        inputs.symbol,
    )


@dataclass(frozen=True)
class CryptoPositionSelection:
    selected: tuple[CryptoPositionCandidate, ...]
    rejected: tuple[CryptoPositionCandidate, ...]
    ranking_contract: tuple[str, ...]
    shadow_only: bool = True
    demo_activation_allowed: bool = False
    live_activation_allowed: bool = False


def rank_crypto_position_candidates(
    candidates: Iterable[CryptoPositionCandidate],
) -> tuple[CryptoPositionCandidate, ...]:
    """Rank eligible plans lexicographically without fitted score weights."""

    rows = tuple(candidates)
    inputs_by_id: dict[int, CryptoPositionRankInputs] = {}
    for candidate in rows:
        inputs_by_id[id(candidate)] = crypto_position_rank_inputs(candidate)
    return tuple(
        sorted(
            rows,
            key=lambda item: crypto_position_rank_key(inputs_by_id[id(item)]),
        )
    )


def select_crypto_positions(
    candidates: Iterable[CryptoPositionCandidate],
    *,
    maximum_positions: int,
) -> CryptoPositionSelection:
    if maximum_positions < 1:
        raise ValueError("maximum_positions must be positive")
    ranked = rank_crypto_position_candidates(candidates)
    return CryptoPositionSelection(
        selected=ranked[:maximum_positions],
        rejected=ranked[maximum_positions:],
        ranking_contract=(
            "expected_net_r_desc",
            "expected_net_edge_usd_desc",
            "quality_score_desc",
            "cost_to_target_fraction_asc",
            "symbol_asc",
        ),
        shadow_only=True,
        demo_activation_allowed=False,
        live_activation_allowed=False,
    )


def average_expected_net_r(
    candidates: Iterable[CryptoPositionCandidate],
) -> Decimal | None:
    rows = tuple(candidates)
    if not rows:
        return None
    return sum((row.expected_net_r for row in rows), start=_ZERO) / Decimal(len(rows))


__all__ = [
    "CryptoPositionCandidate",
    "CryptoPositionRankInputs",
    "CryptoPositionSelection",
    "average_expected_net_r",
    "crypto_position_rank_inputs",
    "crypto_position_rank_key",
    "rank_crypto_position_candidates",
    "select_crypto_positions",
]
