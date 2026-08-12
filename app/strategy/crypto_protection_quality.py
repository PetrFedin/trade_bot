from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

_PROTECTIVE_REASONS = {"BREAK_EVEN_STOP", "PROFIT_PROTECTION"}


@dataclass(frozen=True)
class CryptoProtectionQuality:
    protective_exit_count: int
    protective_net_pnl_usdt: Decimal
    gap_through_count: int
    gap_through_net_pnl_usdt: Decimal
    non_gap_count: int
    non_gap_net_pnl_usdt: Decimal
    profitable_protective_exit_count: int
    losing_protective_exit_count: int
    gap_through_loss_count: int
    worst_gap_loss_usdt: Decimal | None

    @property
    def gap_through_share(self) -> Decimal | None:
        if self.protective_exit_count == 0:
            return None
        return Decimal(self.gap_through_count) / Decimal(self.protective_exit_count)

    @property
    def gap_loss_share_of_protective_losses(self) -> Decimal | None:
        if self.losing_protective_exit_count == 0:
            return None
        return Decimal(self.gap_through_loss_count) / Decimal(self.losing_protective_exit_count)

    def as_dict(self) -> dict[str, object]:
        return {
            "protective_exit_count": self.protective_exit_count,
            "protective_net_pnl_usdt": float(self.protective_net_pnl_usdt),
            "gap_through_count": self.gap_through_count,
            "gap_through_net_pnl_usdt": float(self.gap_through_net_pnl_usdt),
            "gap_through_share": (
                None if self.gap_through_share is None else float(self.gap_through_share)
            ),
            "non_gap_count": self.non_gap_count,
            "non_gap_net_pnl_usdt": float(self.non_gap_net_pnl_usdt),
            "profitable_protective_exit_count": self.profitable_protective_exit_count,
            "losing_protective_exit_count": self.losing_protective_exit_count,
            "gap_through_loss_count": self.gap_through_loss_count,
            "gap_loss_share_of_protective_losses": (
                None
                if self.gap_loss_share_of_protective_losses is None
                else float(self.gap_loss_share_of_protective_losses)
            ),
            "worst_gap_loss_usdt": (
                None if self.worst_gap_loss_usdt is None else float(self.worst_gap_loss_usdt)
            ),
        }


def evaluate_protection_quality(closed_trades: list[dict[str, Any]]) -> CryptoProtectionQuality:
    protective = [
        trade
        for trade in closed_trades
        if str(trade.get("normalized_exit_reason", trade.get("exit_reason", "")))
        in _PROTECTIVE_REASONS
    ]
    gaps = [trade for trade in protective if bool(trade.get("gap_through", False))]
    non_gaps = [trade for trade in protective if not bool(trade.get("gap_through", False))]
    profitable = [trade for trade in protective if _pnl(trade) > 0]
    losing = [trade for trade in protective if _pnl(trade) < 0]
    gap_losses = [trade for trade in gaps if _pnl(trade) < 0]
    worst_gap_loss = min((_pnl(trade) for trade in gaps), default=None)
    return CryptoProtectionQuality(
        protective_exit_count=len(protective),
        protective_net_pnl_usdt=_sum_pnl(protective),
        gap_through_count=len(gaps),
        gap_through_net_pnl_usdt=_sum_pnl(gaps),
        non_gap_count=len(non_gaps),
        non_gap_net_pnl_usdt=_sum_pnl(non_gaps),
        profitable_protective_exit_count=len(profitable),
        losing_protective_exit_count=len(losing),
        gap_through_loss_count=len(gap_losses),
        worst_gap_loss_usdt=worst_gap_loss,
    )


def add_protection_quality_to_report(report: dict[str, Any]) -> dict[str, Any]:
    for variant in report.get("variants", {}).values():
        if isinstance(variant, dict):
            _annotate_variant(variant)
    candidates = report.get("notional_cap_shadow_candidates", {})
    if isinstance(candidates, dict):
        for candidate in candidates.values():
            if not isinstance(candidate, dict):
                continue
            variants = candidate.get("variants", {})
            if not isinstance(variants, dict):
                continue
            for variant in variants.values():
                if isinstance(variant, dict):
                    _annotate_variant(variant)
    strategy_candidates = report.get("strategy_shadow_candidates", {})
    if isinstance(strategy_candidates, dict):
        for candidate in strategy_candidates.values():
            if isinstance(candidate, dict):
                _annotate_variant(candidate)
    report["protection_quality_contract"] = {
        "protective_reasons": sorted(_PROTECTIVE_REASONS),
        "gap_through_means_bar_open_crossed_already_armed_protective_stop": True,
        "profit_protection_label_does_not_guarantee_positive_realized_pnl": True,
        "open_ended_runner_protection_is_not_a_profit_guarantee": True,
    }
    return report


def _annotate_variant(variant: dict[str, Any]) -> None:
    variant["protection_quality"] = evaluate_protection_quality(
        list(variant.get("closed_trades", []))
    ).as_dict()


def _pnl(trade: dict[str, Any]) -> Decimal:
    return Decimal(str(trade.get("net_pnl_usdt", "0")))


def _sum_pnl(trades: list[dict[str, Any]]) -> Decimal:
    return sum((_pnl(trade) for trade in trades), start=Decimal("0"))
