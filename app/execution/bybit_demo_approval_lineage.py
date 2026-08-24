from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.execution.bybit_demo_operator_approval import (
    BybitDemoOperatorApproval,
    validate_demo_approval_against_latest_review_row,
)


@dataclass(frozen=True)
class BybitDemoApprovedEntryAuthorization:
    approval_id: str
    source_snapshot_id: str
    source_evidence_rank: int
    source_market_rank: int
    symbol: str
    side: str
    decision_time: str
    signal_available_at: str
    approved_at: str
    expires_at: str
    expected_entry_order_link_id: str
    expected_close_order_link_id: str
    authorized_at: str
    operator_confirmed: bool = True
    environment: str = "BYBIT_DEMO"
    single_use_entry_required: bool = True
    outcome_free: bool = True
    diagnostics_only: bool = True
    trade_actionable: bool = False
    automatic_selector_retuning_allowed: bool = False
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


def build_bybit_demo_approved_entry_authorization(
    approval: BybitDemoOperatorApproval,
    latest_review_row: Mapping[str, Any],
    *,
    now: datetime,
) -> BybitDemoApprovedEntryAuthorization:
    """Bind one exact evidence approval to its deterministic Demo order identity.

    This record is intentionally persisted before any entry network write. It contains no fill,
    fee, funding, MFE/MAE or realized PnL. A later protected-entry provenance record and terminal
    attribution can join to it only through the same deterministic ``entry_order_link_id``.
    """

    validate_demo_approval_against_latest_review_row(
        approval,
        latest_review_row,
        now=now,
    )
    authorization = BybitDemoApprovedEntryAuthorization(
        approval_id=approval.approval_id,
        source_snapshot_id=approval.source_snapshot_id,
        source_evidence_rank=approval.source_evidence_rank,
        source_market_rank=approval.source_market_rank,
        symbol=approval.symbol,
        side=approval.side,
        decision_time=approval.decision_time,
        signal_available_at=approval.signal_available_at,
        approved_at=approval.approved_at,
        expires_at=approval.expires_at,
        expected_entry_order_link_id=approval.expected_entry_order_link_id,
        expected_close_order_link_id=approval.expected_close_order_link_id,
        authorized_at=approval.approved_at,
    )
    validate_bybit_demo_approved_entry_authorization(authorization)
    return authorization


def validate_bybit_demo_approved_entry_authorization(
    authorization: BybitDemoApprovedEntryAuthorization,
) -> None:
    _sha(authorization.approval_id, "approval id")
    _sha(authorization.source_snapshot_id, "source snapshot id")
    if not 1 <= authorization.source_evidence_rank <= 50:
        raise ValueError("approved entry authorization evidence rank must be within [1, 50]")
    if not 1 <= authorization.source_market_rank <= 50:
        raise ValueError("approved entry authorization market rank must be within [1, 50]")
    if not authorization.symbol or authorization.symbol != authorization.symbol.upper():
        raise ValueError("approved entry authorization symbol must be uppercase")
    if authorization.side not in {"LONG", "SHORT"}:
        raise ValueError("approved entry authorization side must be LONG or SHORT")
    for name, value in (
        ("decision_time", authorization.decision_time),
        ("signal_available_at", authorization.signal_available_at),
        ("approved_at", authorization.approved_at),
        ("expires_at", authorization.expires_at),
        ("authorized_at", authorization.authorized_at),
    ):
        if not isinstance(value, str) or not value:
            raise ValueError(f"approved entry authorization {name} is required")
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError(f"approved entry authorization {name} must be timezone-aware")
    if authorization.authorized_at != authorization.approved_at:
        raise ValueError("approved entry authorization timestamp must be approval-deterministic")
    if not authorization.expected_entry_order_link_id.startswith("ASTRA-DEMO-"):
        raise ValueError("approved entry authorization requires Demo entry orderLinkId")
    if not authorization.expected_close_order_link_id.startswith("ASTRA-DEMO-"):
        raise ValueError("approved entry authorization requires Demo close orderLinkId")
    if not authorization.operator_confirmed:
        raise ValueError("approved entry authorization requires operator confirmation")
    if authorization.environment != "BYBIT_DEMO":
        raise ValueError("approved entry authorization environment must be BYBIT_DEMO")
    if not authorization.single_use_entry_required:
        raise ValueError("approved entry authorization must remain single-use")
    if not authorization.outcome_free or not authorization.diagnostics_only:
        raise ValueError("approved entry authorization must remain outcome-free diagnostics")
    if authorization.trade_actionable:
        raise ValueError("approved entry authorization cannot itself make a trade actionable")
    if authorization.automatic_selector_retuning_allowed:
        raise ValueError("approved entry authorization cannot retune selection")
    if authorization.strategy_promotion_allowed:
        raise ValueError("approved entry authorization cannot promote strategy")
    if authorization.live_mainnet_order_routing_allowed:
        raise ValueError("approved entry authorization cannot enable mainnet routing")


def _sha(value: str, label: str) -> None:
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError(f"approved entry authorization {label} must be lowercase sha256")


__all__ = [
    "BybitDemoApprovedEntryAuthorization",
    "build_bybit_demo_approved_entry_authorization",
    "validate_bybit_demo_approved_entry_authorization",
]
