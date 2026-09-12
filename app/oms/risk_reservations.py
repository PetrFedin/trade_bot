from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class RiskReservationBudget:
    """Durable capacity available to one atomic pre-submit reservation decision."""

    available_cash: Decimal | None
    current_symbol_notional: Decimal
    current_gross_notional: Decimal
    maximum_symbol_notional: Decimal
    maximum_gross_notional: Decimal

    def validate(self) -> None:
        for name, value in (
            ("current_symbol_notional", self.current_symbol_notional),
            ("current_gross_notional", self.current_gross_notional),
        ):
            if not value.is_finite() or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        for name, value in (
            ("maximum_symbol_notional", self.maximum_symbol_notional),
            ("maximum_gross_notional", self.maximum_gross_notional),
        ):
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be positive and finite")
        if self.available_cash is not None and (
            not self.available_cash.is_finite() or self.available_cash < 0
        ):
            raise ValueError("available_cash must be finite and non-negative when supplied")


@dataclass(frozen=True)
class PendingBuyExposure:
    symbol: str
    remaining_notional: Decimal

    def validate(self) -> None:
        if not self.symbol.strip():
            raise ValueError("pending exposure symbol is required")
        if not self.remaining_notional.is_finite() or self.remaining_notional < 0:
            raise ValueError("pending remaining_notional must be finite and non-negative")


@dataclass(frozen=True)
class RiskReservationEvaluation:
    reasons: tuple[str, ...]
    reserved_cash_notional: Decimal
    reserved_symbol_notional: Decimal
    reserved_gross_notional: Decimal
    projected_cash_commitment: Decimal
    projected_symbol_notional: Decimal
    projected_gross_notional: Decimal

    @property
    def approved(self) -> bool:
        return not self.reasons

    def event_payload(self) -> dict[str, object]:
        return {
            "reservation": {
                "approved": self.approved,
                "reasons": list(self.reasons),
                "reserved_cash_notional": str(self.reserved_cash_notional),
                "reserved_symbol_notional": str(self.reserved_symbol_notional),
                "reserved_gross_notional": str(self.reserved_gross_notional),
                "projected_cash_commitment": str(self.projected_cash_commitment),
                "projected_symbol_notional": str(self.projected_symbol_notional),
                "projected_gross_notional": str(self.projected_gross_notional),
            }
        }


class RiskReservationRejected(ValueError):
    def __init__(self, reasons: tuple[str, ...]) -> None:
        normalized = tuple(sorted(set(reasons)))
        if not normalized:
            raise ValueError("reservation rejection requires at least one reason")
        self.reasons = normalized
        super().__init__(f"RISK_RESERVATION_REJECTED:{','.join(normalized)}")


def evaluate_buy_reservation(
    *,
    symbol: str,
    candidate_notional: Decimal,
    budget: RiskReservationBudget,
    active_buys: tuple[PendingBuyExposure, ...],
) -> RiskReservationEvaluation:
    budget.validate()
    if not symbol.strip():
        raise ValueError("symbol is required")
    if not candidate_notional.is_finite() or candidate_notional <= 0:
        raise ValueError("candidate_notional must be positive and finite")
    for exposure in active_buys:
        exposure.validate()

    reserved_gross = sum(
        (item.remaining_notional for item in active_buys),
        start=Decimal("0"),
    )
    reserved_symbol = sum(
        (
            item.remaining_notional
            for item in active_buys
            if item.symbol == symbol
        ),
        start=Decimal("0"),
    )
    projected_cash_commitment = reserved_gross + candidate_notional
    projected_symbol = (
        budget.current_symbol_notional + reserved_symbol + candidate_notional
    )
    projected_gross = budget.current_gross_notional + reserved_gross + candidate_notional

    reasons: list[str] = []
    if (
        budget.available_cash is not None
        and projected_cash_commitment > budget.available_cash
    ):
        reasons.append("INSUFFICIENT_AVAILABLE_CASH")
    if projected_symbol > budget.maximum_symbol_notional:
        reasons.append("SYMBOL_NOTIONAL_LIMIT_EXCEEDED")
    if projected_gross > budget.maximum_gross_notional:
        reasons.append("GROSS_NOTIONAL_LIMIT_EXCEEDED")

    return RiskReservationEvaluation(
        reasons=tuple(sorted(set(reasons))),
        reserved_cash_notional=reserved_gross,
        reserved_symbol_notional=reserved_symbol,
        reserved_gross_notional=reserved_gross,
        projected_cash_commitment=projected_cash_commitment,
        projected_symbol_notional=projected_symbol,
        projected_gross_notional=projected_gross,
    )
