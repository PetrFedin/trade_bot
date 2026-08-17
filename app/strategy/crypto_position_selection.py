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
    for candidate in rows:
        if candidate.signal.symbol != candidate.plan.symbol:
            raise ValueError("crypto position candidate signal/plan symbol mismatch")
        if candidate.signal.side is not candidate.plan.side:
            raise ValueError("crypto position candidate signal/plan side mismatch")
        _ = candidate.expected_net_r
        _ = candidate.cost_to_target_fraction
    return tuple(
        sorted(
            rows,
            key=lambda item: (
                -item.expected_net_r,
                -item.plan.expected_net_edge_usd,
                -item.plan.quality_score,
                item.cost_to_target_fraction,
                item.plan.symbol,
            ),
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
