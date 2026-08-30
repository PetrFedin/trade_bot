from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.strategy.crypto_perp import CryptoSignal, CryptoTradePlan
from app.strategy.crypto_position_selection import (
    CryptoPositionCandidate,
    crypto_position_rank_inputs,
)

_ZERO = Decimal("0")


@dataclass(frozen=True)
class CryptoPlanEligibleCandidateTrace:
    """Point-in-time plan-eligible candidate evidence before concurrency selection.

    This is observability only. It contains no future outcome, exit or realized-PnL field and
    cannot itself change selection or execution.
    """

    decision_time: str
    planned_execution_time: str
    symbol: str
    side: str
    quality_score: Decimal
    expected_net_edge_usd: Decimal
    risk_budget_usdt: Decimal
    expected_net_r: Decimal
    estimated_round_trip_cost_usdt: Decimal
    target_net_profit_usd: Decimal
    cost_to_target_fraction: Decimal
    open_position_count: int
    already_pending_count: int
    maximum_concurrent_positions: int

    @property
    def available_slots_before_selection(self) -> int:
        return max(
            0,
            self.maximum_concurrent_positions
            - self.open_position_count
            - self.already_pending_count,
        )

    def validate(self) -> None:
        if not self.decision_time or not self.planned_execution_time:
            raise ValueError("plan candidate trace timestamps are required")
        if not self.symbol or self.symbol != self.symbol.strip().upper():
            raise ValueError("plan candidate trace symbol is invalid")
        if self.side not in {"LONG", "SHORT"}:
            raise ValueError("plan candidate trace side is invalid")
        values = (
            self.quality_score,
            self.expected_net_edge_usd,
            self.risk_budget_usdt,
            self.expected_net_r,
            self.estimated_round_trip_cost_usdt,
            self.target_net_profit_usd,
            self.cost_to_target_fraction,
        )
        if any(not value.is_finite() for value in values):
            raise ValueError("plan candidate trace numerics must be finite")
        if self.expected_net_edge_usd <= _ZERO or self.risk_budget_usdt <= _ZERO:
            raise ValueError("plan candidate trace expected edge and risk must be positive")
        if self.expected_net_r != self.expected_net_edge_usd / self.risk_budget_usdt:
            raise ValueError("plan candidate trace expected-net-R is inconsistent")
        if self.estimated_round_trip_cost_usdt < _ZERO:
            raise ValueError("plan candidate trace round-trip cost cannot be negative")
        if self.target_net_profit_usd <= _ZERO:
            raise ValueError("plan candidate trace target must be positive")
        if (
            self.cost_to_target_fraction
            != self.estimated_round_trip_cost_usdt / self.target_net_profit_usd
        ):
            raise ValueError("plan candidate trace cost/target fraction is inconsistent")
        counts = (
            self.open_position_count,
            self.already_pending_count,
            self.maximum_concurrent_positions,
        )
        if any(value < 0 for value in counts):
            raise ValueError("plan candidate trace position counts cannot be negative")
        if self.maximum_concurrent_positions < 1:
            raise ValueError("plan candidate trace concurrency limit must be positive")

    @property
    def trace_id(self) -> str:
        payload = json.dumps(
            self.to_payload(include_trace_id=False),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def to_payload(self, *, include_trace_id: bool = True) -> dict[str, Any]:
        self.validate()
        payload: dict[str, Any] = {
            "event": "PLAN_ELIGIBLE",
            "decision_time": self.decision_time,
            "planned_execution_time": self.planned_execution_time,
            "symbol": self.symbol,
            "side": self.side,
            "quality_score": float(self.quality_score),
            "expected_net_edge_usd": float(self.expected_net_edge_usd),
            "risk_budget_usdt": float(self.risk_budget_usdt),
            "expected_net_r": float(self.expected_net_r),
            "estimated_round_trip_cost_usdt": float(
                self.estimated_round_trip_cost_usdt
            ),
            "target_net_profit_usd": float(self.target_net_profit_usd),
            "cost_to_target_fraction": float(self.cost_to_target_fraction),
            "open_position_count": self.open_position_count,
            "already_pending_count": self.already_pending_count,
            "maximum_concurrent_positions": self.maximum_concurrent_positions,
            "available_slots_before_selection": self.available_slots_before_selection,
            "future_outcome_fields_present": False,
            "selection_mutation_allowed": False,
            "execution_mutation_allowed": False,
        }
        if include_trace_id:
            payload["trace_id"] = self.trace_id
        return payload


def build_crypto_plan_eligible_candidate_trace(
    signal: CryptoSignal,
    plan: CryptoTradePlan,
    *,
    planned_execution_time: str,
    open_position_count: int,
    already_pending_count: int,
    maximum_concurrent_positions: int,
) -> CryptoPlanEligibleCandidateTrace:
    """Build the trace from the exact signal and plan already accepted by canonical replay."""

    candidate = CryptoPositionCandidate(signal=signal, plan=plan)
    rank = crypto_position_rank_inputs(candidate)
    trace = CryptoPlanEligibleCandidateTrace(
        decision_time=signal.decision_time,
        planned_execution_time=planned_execution_time,
        symbol=signal.symbol,
        side=signal.side.value,
        quality_score=rank.quality_score,
        expected_net_edge_usd=rank.expected_net_edge_usd,
        risk_budget_usdt=plan.risk_budget_usdt,
        expected_net_r=rank.expected_net_r,
        estimated_round_trip_cost_usdt=plan.estimated_round_trip_cost_usdt,
        target_net_profit_usd=plan.target_net_profit_usd,
        cost_to_target_fraction=rank.cost_to_target_fraction,
        open_position_count=open_position_count,
        already_pending_count=already_pending_count,
        maximum_concurrent_positions=maximum_concurrent_positions,
    )
    trace.validate()
    return trace


__all__ = [
    "CryptoPlanEligibleCandidateTrace",
    "build_crypto_plan_eligible_candidate_trace",
]
