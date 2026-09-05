from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

_APPROVAL_KEYS = frozenset(
    {
        "approval_id",
        "source_snapshot_id",
        "source_evidence_rank",
        "source_market_rank",
        "symbol",
        "side",
        "decision_time",
        "signal_available_at",
        "approved_at",
        "expires_at",
        "expected_entry_order_link_id",
        "expected_close_order_link_id",
        "authorized_at",
        "operator_confirmed",
        "environment",
        "single_use_entry_required",
        "outcome_free",
        "diagnostics_only",
        "trade_actionable",
        "automatic_selector_retuning_allowed",
        "strategy_promotion_allowed",
        "live_mainnet_order_routing_allowed",
    }
)
_FALLBACK_KEYS = frozenset(
    {"symbol", "side", "stage", "reasons", "quote_price", "modeled_entry_price"}
)
_ENTRY_KEYS = frozenset(
    {
        "entry_order_link_id",
        "symbol",
        "side",
        "decision_time",
        "selected_signal_rank",
        "executable_candidate_count",
        "candidate_audit_count",
        "economic_shadow_selected_symbol",
        "economic_shadow_selected_side",
        "economic_shadow_differs_from_current",
        "selected_after_fallback",
        "fallback_attempts",
        "expected_net_edge_usd",
        "risk_budget_usdt",
        "quality_score",
        "target_net_profit_usd",
        "planned_reference_price",
        "planned_reference_quantity",
        "planned_notional_usdt",
        "modeled_round_trip_cost_usdt",
        "pre_entry_quote_price",
        "pre_entry_modeled_entry_price",
        "pre_entry_original_quantity",
        "pre_entry_adjusted_quantity",
        "pre_entry_quote_resized",
        "pre_entry_quantity_retention_fraction",
        "actual_average_entry_price",
        "actual_filled_quantity",
        "actual_fill_notional_usdt",
        "actual_fill_adverse_slippage_bps_vs_modeled_entry",
        "account_taker_fee_rate",
        "exit_mode",
        "runner_admission_reasons",
        "liquidation_safety_reason",
        "stop_to_liquidation_r",
        "effective_account_equity_usdt",
        "effective_peak_equity_usdt",
        "margin_mode",
        "realized_pnl_used_for_selection",
        "diagnostics_only",
        "automatic_selector_retuning_allowed",
        "strategy_promotion_allowed",
        "live_mainnet_order_routing_allowed",
    }
)
_TERMINAL_WRAPPER_KEYS = frozenset({"entry_order_link_id", "checkpoint_revision", "evidence"})
_TERMINAL_EVIDENCE_KEYS = frozenset(
    {
        "symbol",
        "side",
        "observation_count",
        "observed_peak_favorable_r",
        "observed_max_adverse_r",
        "realized_gross_exit_r",
        "observed_peak_capture_fraction",
        "giveback_from_observed_peak_to_exit_r",
        "exit_exceeded_observed_peak",
        "partial_close_seen",
        "realized_gross_pnl_usdt",
        "realized_net_after_execution_fees_usdt",
        "execution_fees_usdt",
        "account_closed_pnl_usdt",
        "funding_net_usdt",
        "all_in_net_pnl_usdt",
        "profit_outcome_status",
        "positive_peak_nonpositive_gross_exit",
        "gross_positive_fill_nonpositive",
        "fill_positive_account_nonpositive",
        "account_positive_all_in_nonpositive",
        "positive_peak_nonpositive_all_in",
        "fully_reconciled_all_in",
        "diagnostics_only",
        "exit_threshold_retuning_allowed",
        "strategy_promotion_allowed",
        "live_mainnet_order_routing_allowed",
    }
)
_FALLBACK_STAGES = frozenset({"PRE_ENTRY_QUOTE", "ACCOUNT_FEE_ECONOMICS"})
_FULLY_RECONCILED_OUTCOMES = frozenset(
    {
        "FULLY_RECONCILED_PROFIT",
        "FULLY_RECONCILED_FLAT",
        "FULLY_RECONCILED_LOSS",
    }
)
_EXIT_MODES = frozenset({"FIXED_20_TARGET", "OPEN_ENDED_RUNNER"})
_SIDES = frozenset({"LONG", "SHORT"})


@dataclass(frozen=True)
class BybitDemoApprovedEntryAuthorizationV120:
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


@dataclass(frozen=True)
class BybitDemoFallbackAttemptV120:
    symbol: str
    side: str
    stage: str
    reasons: tuple[str, ...]
    quote_price: Decimal | None
    modeled_entry_price: Decimal | None


@dataclass(frozen=True)
class BybitDemoEntryDecisionProvenanceV120:
    entry_order_link_id: str
    symbol: str
    side: str
    decision_time: str
    selected_signal_rank: int
    executable_candidate_count: int
    candidate_audit_count: int
    economic_shadow_selected_symbol: str | None
    economic_shadow_selected_side: str | None
    economic_shadow_differs_from_current: bool
    selected_after_fallback: bool
    fallback_attempts: tuple[BybitDemoFallbackAttemptV120, ...]
    expected_net_edge_usd: Decimal
    risk_budget_usdt: Decimal
    quality_score: Decimal
    target_net_profit_usd: Decimal
    planned_reference_price: Decimal
    planned_reference_quantity: Decimal
    planned_notional_usdt: Decimal
    modeled_round_trip_cost_usdt: Decimal
    pre_entry_quote_price: Decimal | None
    pre_entry_modeled_entry_price: Decimal | None
    pre_entry_original_quantity: Decimal | None
    pre_entry_adjusted_quantity: Decimal | None
    pre_entry_quote_resized: bool
    pre_entry_quantity_retention_fraction: Decimal | None
    actual_average_entry_price: Decimal
    actual_filled_quantity: Decimal
    actual_fill_notional_usdt: Decimal
    actual_fill_adverse_slippage_bps_vs_modeled_entry: Decimal | None
    account_taker_fee_rate: Decimal
    exit_mode: str
    runner_admission_reasons: tuple[str, ...]
    liquidation_safety_reason: str | None
    stop_to_liquidation_r: Decimal | None
    effective_account_equity_usdt: Decimal
    effective_peak_equity_usdt: Decimal
    margin_mode: str | None
    realized_pnl_used_for_selection: bool = False
    diagnostics_only: bool = True
    automatic_selector_retuning_allowed: bool = False
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class BybitDemoTerminalEvidenceFactsV120:
    symbol: str
    side: str
    observation_count: int
    observed_peak_favorable_r: Decimal
    observed_max_adverse_r: Decimal
    realized_gross_exit_r: Decimal
    observed_peak_capture_fraction: Decimal | None
    giveback_from_observed_peak_to_exit_r: Decimal
    exit_exceeded_observed_peak: bool
    partial_close_seen: bool
    realized_gross_pnl_usdt: Decimal
    realized_net_after_execution_fees_usdt: Decimal
    execution_fees_usdt: Decimal
    account_closed_pnl_usdt: Decimal | None
    funding_net_usdt: Decimal | None
    all_in_net_pnl_usdt: Decimal | None
    profit_outcome_status: str
    positive_peak_nonpositive_gross_exit: bool
    gross_positive_fill_nonpositive: bool
    fill_positive_account_nonpositive: bool
    account_positive_all_in_nonpositive: bool
    positive_peak_nonpositive_all_in: bool | None
    fully_reconciled_all_in: bool
    diagnostics_only: bool = True
    exit_threshold_retuning_allowed: bool = False
    strategy_promotion_allowed: bool = False
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class BybitDemoTerminalEvidenceV120:
    entry_order_link_id: str
    checkpoint_revision: str
    evidence: BybitDemoTerminalEvidenceFactsV120


def canonical_sha256(canonical_record: str) -> str:
    if not canonical_record:
        raise ValueError("canonical record is required")
    return hashlib.sha256(canonical_record.encode("utf-8")).hexdigest()


def encode_approved_entry_authorization_v120(
    authorization: BybitDemoApprovedEntryAuthorizationV120,
) -> tuple[str, str]:
    validate_approved_entry_authorization_v120(authorization)
    payload = asdict(authorization)
    canonical = _canonical_json(payload)
    return canonical, canonical_sha256(canonical)


def decode_approved_entry_authorization_v120(
    canonical_record: str,
) -> BybitDemoApprovedEntryAuthorizationV120:
    payload = _json_object(canonical_record, "approved entry authorization")
    _require_exact_keys(payload, _APPROVAL_KEYS, "approved entry authorization")
    authorization = BybitDemoApprovedEntryAuthorizationV120(
        approval_id=_text(payload, "approval_id", "approved entry authorization"),
        source_snapshot_id=_text(payload, "source_snapshot_id", "approved entry authorization"),
        source_evidence_rank=_integer(
            payload, "source_evidence_rank", "approved entry authorization"
        ),
        source_market_rank=_integer(payload, "source_market_rank", "approved entry authorization"),
        symbol=_text(payload, "symbol", "approved entry authorization"),
        side=_text(payload, "side", "approved entry authorization"),
        decision_time=_text(payload, "decision_time", "approved entry authorization"),
        signal_available_at=_text(
            payload, "signal_available_at", "approved entry authorization"
        ),
        approved_at=_text(payload, "approved_at", "approved entry authorization"),
        expires_at=_text(payload, "expires_at", "approved entry authorization"),
        expected_entry_order_link_id=_text(
            payload, "expected_entry_order_link_id", "approved entry authorization"
        ),
        expected_close_order_link_id=_text(
            payload, "expected_close_order_link_id", "approved entry authorization"
        ),
        authorized_at=_text(payload, "authorized_at", "approved entry authorization"),
        operator_confirmed=_boolean(
            payload, "operator_confirmed", "approved entry authorization"
        ),
        environment=_text(payload, "environment", "approved entry authorization"),
        single_use_entry_required=_boolean(
            payload, "single_use_entry_required", "approved entry authorization"
        ),
        outcome_free=_boolean(payload, "outcome_free", "approved entry authorization"),
        diagnostics_only=_boolean(
            payload, "diagnostics_only", "approved entry authorization"
        ),
        trade_actionable=_boolean(
            payload, "trade_actionable", "approved entry authorization"
        ),
        automatic_selector_retuning_allowed=_boolean(
            payload,
            "automatic_selector_retuning_allowed",
            "approved entry authorization",
        ),
        strategy_promotion_allowed=_boolean(
            payload, "strategy_promotion_allowed", "approved entry authorization"
        ),
        live_mainnet_order_routing_allowed=_boolean(
            payload,
            "live_mainnet_order_routing_allowed",
            "approved entry authorization",
        ),
    )
    validate_approved_entry_authorization_v120(authorization)
    return authorization


def validate_approved_entry_authorization_v120(
    authorization: BybitDemoApprovedEntryAuthorizationV120,
) -> None:
    _sha256_text(authorization.approval_id, "approved entry authorization approval id")
    _sha256_text(
        authorization.source_snapshot_id,
        "approved entry authorization source snapshot id",
    )
    if not 1 <= authorization.source_evidence_rank <= 50:
        raise ValueError("approved entry authorization evidence rank must be within [1, 50]")
    if not 1 <= authorization.source_market_rank <= 50:
        raise ValueError("approved entry authorization market rank must be within [1, 50]")
    if not authorization.symbol or authorization.symbol != authorization.symbol.upper():
        raise ValueError("approved entry authorization symbol must be uppercase")
    _side(authorization.side, "approved entry authorization")
    for field, value in (
        ("decision_time", authorization.decision_time),
        ("signal_available_at", authorization.signal_available_at),
        ("approved_at", authorization.approved_at),
        ("expires_at", authorization.expires_at),
        ("authorized_at", authorization.authorized_at),
    ):
        _aware_iso(value, f"approved entry authorization {field}")
    if authorization.authorized_at != authorization.approved_at:
        raise ValueError("approved entry authorization timestamp must be approval-deterministic")
    _demo_order_link(authorization.expected_entry_order_link_id, "approved entry authorization")
    _demo_order_link(authorization.expected_close_order_link_id, "approved entry authorization")
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


def encode_entry_provenance_v120(
    provenance: BybitDemoEntryDecisionProvenanceV120,
) -> tuple[str, str]:
    validate_entry_provenance_v120(provenance)
    payload: dict[str, object] = {
        "entry_order_link_id": provenance.entry_order_link_id,
        "symbol": provenance.symbol,
        "side": provenance.side,
        "decision_time": provenance.decision_time,
        "selected_signal_rank": provenance.selected_signal_rank,
        "executable_candidate_count": provenance.executable_candidate_count,
        "candidate_audit_count": provenance.candidate_audit_count,
        "economic_shadow_selected_symbol": provenance.economic_shadow_selected_symbol,
        "economic_shadow_selected_side": provenance.economic_shadow_selected_side,
        "economic_shadow_differs_from_current": provenance.economic_shadow_differs_from_current,
        "selected_after_fallback": provenance.selected_after_fallback,
        "fallback_attempts": [
            {
                "symbol": attempt.symbol,
                "side": attempt.side,
                "stage": attempt.stage,
                "reasons": list(attempt.reasons),
                "quote_price": _decimal_text(attempt.quote_price),
                "modeled_entry_price": _decimal_text(attempt.modeled_entry_price),
            }
            for attempt in provenance.fallback_attempts
        ],
        "expected_net_edge_usd": str(provenance.expected_net_edge_usd),
        "risk_budget_usdt": str(provenance.risk_budget_usdt),
        "quality_score": str(provenance.quality_score),
        "target_net_profit_usd": str(provenance.target_net_profit_usd),
        "planned_reference_price": str(provenance.planned_reference_price),
        "planned_reference_quantity": str(provenance.planned_reference_quantity),
        "planned_notional_usdt": str(provenance.planned_notional_usdt),
        "modeled_round_trip_cost_usdt": str(provenance.modeled_round_trip_cost_usdt),
        "pre_entry_quote_price": _decimal_text(provenance.pre_entry_quote_price),
        "pre_entry_modeled_entry_price": _decimal_text(
            provenance.pre_entry_modeled_entry_price
        ),
        "pre_entry_original_quantity": _decimal_text(provenance.pre_entry_original_quantity),
        "pre_entry_adjusted_quantity": _decimal_text(provenance.pre_entry_adjusted_quantity),
        "pre_entry_quote_resized": provenance.pre_entry_quote_resized,
        "pre_entry_quantity_retention_fraction": _decimal_text(
            provenance.pre_entry_quantity_retention_fraction
        ),
        "actual_average_entry_price": str(provenance.actual_average_entry_price),
        "actual_filled_quantity": str(provenance.actual_filled_quantity),
        "actual_fill_notional_usdt": str(provenance.actual_fill_notional_usdt),
        "actual_fill_adverse_slippage_bps_vs_modeled_entry": _decimal_text(
            provenance.actual_fill_adverse_slippage_bps_vs_modeled_entry
        ),
        "account_taker_fee_rate": str(provenance.account_taker_fee_rate),
        "exit_mode": provenance.exit_mode,
        "runner_admission_reasons": list(provenance.runner_admission_reasons),
        "liquidation_safety_reason": provenance.liquidation_safety_reason,
        "stop_to_liquidation_r": _decimal_text(provenance.stop_to_liquidation_r),
        "effective_account_equity_usdt": str(provenance.effective_account_equity_usdt),
        "effective_peak_equity_usdt": str(provenance.effective_peak_equity_usdt),
        "margin_mode": provenance.margin_mode,
        "realized_pnl_used_for_selection": provenance.realized_pnl_used_for_selection,
        "diagnostics_only": provenance.diagnostics_only,
        "automatic_selector_retuning_allowed": provenance.automatic_selector_retuning_allowed,
        "strategy_promotion_allowed": provenance.strategy_promotion_allowed,
        "live_mainnet_order_routing_allowed": provenance.live_mainnet_order_routing_allowed,
    }
    canonical = _canonical_json(payload)
    return canonical, canonical_sha256(canonical)


def decode_entry_provenance_v120(canonical_record: str) -> BybitDemoEntryDecisionProvenanceV120:
    payload = _json_object(canonical_record, "entry provenance")
    _require_exact_keys(payload, _ENTRY_KEYS, "entry provenance")
    fallback_raw = payload.get("fallback_attempts")
    if not isinstance(fallback_raw, list):
        raise ValueError("entry provenance fallback_attempts must be a list")
    fallback_attempts = tuple(_fallback_from_payload(item) for item in fallback_raw)
    provenance = BybitDemoEntryDecisionProvenanceV120(
        entry_order_link_id=_text(payload, "entry_order_link_id", "entry provenance"),
        symbol=_text(payload, "symbol", "entry provenance"),
        side=_text(payload, "side", "entry provenance"),
        decision_time=_text(payload, "decision_time", "entry provenance"),
        selected_signal_rank=_integer(payload, "selected_signal_rank", "entry provenance"),
        executable_candidate_count=_integer(
            payload, "executable_candidate_count", "entry provenance"
        ),
        candidate_audit_count=_integer(payload, "candidate_audit_count", "entry provenance"),
        economic_shadow_selected_symbol=_optional_text(
            payload, "economic_shadow_selected_symbol", "entry provenance"
        ),
        economic_shadow_selected_side=_optional_text(
            payload, "economic_shadow_selected_side", "entry provenance"
        ),
        economic_shadow_differs_from_current=_boolean(
            payload, "economic_shadow_differs_from_current", "entry provenance"
        ),
        selected_after_fallback=_boolean(payload, "selected_after_fallback", "entry provenance"),
        fallback_attempts=fallback_attempts,
        expected_net_edge_usd=_decimal(payload, "expected_net_edge_usd", "entry provenance"),
        risk_budget_usdt=_decimal(payload, "risk_budget_usdt", "entry provenance"),
        quality_score=_decimal(payload, "quality_score", "entry provenance"),
        target_net_profit_usd=_decimal(payload, "target_net_profit_usd", "entry provenance"),
        planned_reference_price=_decimal(
            payload, "planned_reference_price", "entry provenance"
        ),
        planned_reference_quantity=_decimal(
            payload, "planned_reference_quantity", "entry provenance"
        ),
        planned_notional_usdt=_decimal(payload, "planned_notional_usdt", "entry provenance"),
        modeled_round_trip_cost_usdt=_decimal(
            payload, "modeled_round_trip_cost_usdt", "entry provenance"
        ),
        pre_entry_quote_price=_optional_decimal(
            payload, "pre_entry_quote_price", "entry provenance"
        ),
        pre_entry_modeled_entry_price=_optional_decimal(
            payload, "pre_entry_modeled_entry_price", "entry provenance"
        ),
        pre_entry_original_quantity=_optional_decimal(
            payload, "pre_entry_original_quantity", "entry provenance"
        ),
        pre_entry_adjusted_quantity=_optional_decimal(
            payload, "pre_entry_adjusted_quantity", "entry provenance"
        ),
        pre_entry_quote_resized=_boolean(
            payload, "pre_entry_quote_resized", "entry provenance"
        ),
        pre_entry_quantity_retention_fraction=_optional_decimal(
            payload, "pre_entry_quantity_retention_fraction", "entry provenance"
        ),
        actual_average_entry_price=_decimal(
            payload, "actual_average_entry_price", "entry provenance"
        ),
        actual_filled_quantity=_decimal(payload, "actual_filled_quantity", "entry provenance"),
        actual_fill_notional_usdt=_decimal(
            payload, "actual_fill_notional_usdt", "entry provenance"
        ),
        actual_fill_adverse_slippage_bps_vs_modeled_entry=_optional_decimal(
            payload,
            "actual_fill_adverse_slippage_bps_vs_modeled_entry",
            "entry provenance",
        ),
        account_taker_fee_rate=_decimal(payload, "account_taker_fee_rate", "entry provenance"),
        exit_mode=_text(payload, "exit_mode", "entry provenance"),
        runner_admission_reasons=_text_tuple(
            payload, "runner_admission_reasons", "entry provenance"
        ),
        liquidation_safety_reason=_optional_text(
            payload, "liquidation_safety_reason", "entry provenance"
        ),
        stop_to_liquidation_r=_optional_decimal(
            payload, "stop_to_liquidation_r", "entry provenance"
        ),
        effective_account_equity_usdt=_decimal(
            payload, "effective_account_equity_usdt", "entry provenance"
        ),
        effective_peak_equity_usdt=_decimal(
            payload, "effective_peak_equity_usdt", "entry provenance"
        ),
        margin_mode=_optional_text(payload, "margin_mode", "entry provenance"),
        realized_pnl_used_for_selection=_boolean(
            payload, "realized_pnl_used_for_selection", "entry provenance"
        ),
        diagnostics_only=_boolean(payload, "diagnostics_only", "entry provenance"),
        automatic_selector_retuning_allowed=_boolean(
            payload, "automatic_selector_retuning_allowed", "entry provenance"
        ),
        strategy_promotion_allowed=_boolean(
            payload, "strategy_promotion_allowed", "entry provenance"
        ),
        live_mainnet_order_routing_allowed=_boolean(
            payload, "live_mainnet_order_routing_allowed", "entry provenance"
        ),
    )
    validate_entry_provenance_v120(provenance)
    return provenance


def validate_entry_provenance_v120(provenance: BybitDemoEntryDecisionProvenanceV120) -> None:
    _demo_order_link(provenance.entry_order_link_id, "entry provenance")
    if not provenance.symbol or provenance.symbol != provenance.symbol.upper():
        raise ValueError("entry provenance symbol must be uppercase")
    _side(provenance.side, "entry provenance")
    if provenance.selected_signal_rank < 1 or provenance.executable_candidate_count < 1:
        raise ValueError("entry provenance selection rank/count are invalid")
    if provenance.candidate_audit_count < provenance.executable_candidate_count:
        raise ValueError("entry provenance candidate audit cannot be smaller than executable set")
    for attempt in provenance.fallback_attempts:
        _validate_fallback(attempt)
    for label, value in _entry_decimal_values(provenance):
        _finite_decimal(value, label)
    if provenance.actual_average_entry_price <= 0 or provenance.actual_filled_quantity <= 0:
        raise ValueError("entry provenance actual fill must be positive")
    if provenance.actual_fill_notional_usdt <= 0:
        raise ValueError("entry provenance actual notional must be positive")
    if provenance.account_taker_fee_rate < 0:
        raise ValueError("entry provenance account taker fee cannot be negative")
    if provenance.exit_mode not in _EXIT_MODES:
        raise ValueError("entry provenance exit mode is unsupported")
    retention = provenance.pre_entry_quantity_retention_fraction
    if retention is not None and not Decimal("0") < retention <= Decimal("1"):
        raise ValueError("entry provenance quantity retention must be within (0, 1]")
    if provenance.realized_pnl_used_for_selection:
        raise ValueError("entry provenance cannot use realized PnL for selection")
    if not provenance.diagnostics_only or provenance.automatic_selector_retuning_allowed:
        raise ValueError("entry provenance must remain diagnostics-only")
    if provenance.strategy_promotion_allowed:
        raise ValueError("entry provenance cannot authorize strategy promotion")
    if provenance.live_mainnet_order_routing_allowed:
        raise ValueError("entry provenance cannot permit live routing")


def encode_terminal_evidence_v120(
    terminal: BybitDemoTerminalEvidenceV120,
) -> tuple[str, str]:
    validate_terminal_evidence_v120(terminal)
    evidence = terminal.evidence
    payload = {
        "entry_order_link_id": terminal.entry_order_link_id,
        "checkpoint_revision": terminal.checkpoint_revision,
        "evidence": {
            "symbol": evidence.symbol,
            "side": evidence.side,
            "observation_count": evidence.observation_count,
            "observed_peak_favorable_r": str(evidence.observed_peak_favorable_r),
            "observed_max_adverse_r": str(evidence.observed_max_adverse_r),
            "realized_gross_exit_r": str(evidence.realized_gross_exit_r),
            "observed_peak_capture_fraction": _decimal_text(
                evidence.observed_peak_capture_fraction
            ),
            "giveback_from_observed_peak_to_exit_r": str(
                evidence.giveback_from_observed_peak_to_exit_r
            ),
            "exit_exceeded_observed_peak": evidence.exit_exceeded_observed_peak,
            "partial_close_seen": evidence.partial_close_seen,
            "realized_gross_pnl_usdt": str(evidence.realized_gross_pnl_usdt),
            "realized_net_after_execution_fees_usdt": str(
                evidence.realized_net_after_execution_fees_usdt
            ),
            "execution_fees_usdt": str(evidence.execution_fees_usdt),
            "account_closed_pnl_usdt": _decimal_text(evidence.account_closed_pnl_usdt),
            "funding_net_usdt": _decimal_text(evidence.funding_net_usdt),
            "all_in_net_pnl_usdt": _decimal_text(evidence.all_in_net_pnl_usdt),
            "profit_outcome_status": evidence.profit_outcome_status,
            "positive_peak_nonpositive_gross_exit": (
                evidence.positive_peak_nonpositive_gross_exit
            ),
            "gross_positive_fill_nonpositive": evidence.gross_positive_fill_nonpositive,
            "fill_positive_account_nonpositive": evidence.fill_positive_account_nonpositive,
            "account_positive_all_in_nonpositive": (
                evidence.account_positive_all_in_nonpositive
            ),
            "positive_peak_nonpositive_all_in": evidence.positive_peak_nonpositive_all_in,
            "fully_reconciled_all_in": evidence.fully_reconciled_all_in,
            "diagnostics_only": evidence.diagnostics_only,
            "exit_threshold_retuning_allowed": evidence.exit_threshold_retuning_allowed,
            "strategy_promotion_allowed": evidence.strategy_promotion_allowed,
            "live_mainnet_order_routing_allowed": evidence.live_mainnet_order_routing_allowed,
        },
    }
    canonical = _canonical_json(payload)
    return canonical, canonical_sha256(canonical)


def decode_terminal_evidence_v120(canonical_record: str) -> BybitDemoTerminalEvidenceV120:
    payload = _json_object(canonical_record, "terminal evidence")
    _require_exact_keys(payload, _TERMINAL_WRAPPER_KEYS, "terminal evidence")
    evidence_raw = payload.get("evidence")
    if not isinstance(evidence_raw, Mapping):
        raise ValueError("terminal evidence payload is missing evidence object")
    _require_exact_keys(evidence_raw, _TERMINAL_EVIDENCE_KEYS, "terminal evidence facts")
    evidence = BybitDemoTerminalEvidenceFactsV120(
        symbol=_text(evidence_raw, "symbol", "terminal evidence facts"),
        side=_text(evidence_raw, "side", "terminal evidence facts"),
        observation_count=_integer(
            evidence_raw, "observation_count", "terminal evidence facts"
        ),
        observed_peak_favorable_r=_decimal(
            evidence_raw, "observed_peak_favorable_r", "terminal evidence facts"
        ),
        observed_max_adverse_r=_decimal(
            evidence_raw, "observed_max_adverse_r", "terminal evidence facts"
        ),
        realized_gross_exit_r=_decimal(
            evidence_raw, "realized_gross_exit_r", "terminal evidence facts"
        ),
        observed_peak_capture_fraction=_optional_decimal(
            evidence_raw, "observed_peak_capture_fraction", "terminal evidence facts"
        ),
        giveback_from_observed_peak_to_exit_r=_decimal(
            evidence_raw,
            "giveback_from_observed_peak_to_exit_r",
            "terminal evidence facts",
        ),
        exit_exceeded_observed_peak=_boolean(
            evidence_raw, "exit_exceeded_observed_peak", "terminal evidence facts"
        ),
        partial_close_seen=_boolean(
            evidence_raw, "partial_close_seen", "terminal evidence facts"
        ),
        realized_gross_pnl_usdt=_decimal(
            evidence_raw, "realized_gross_pnl_usdt", "terminal evidence facts"
        ),
        realized_net_after_execution_fees_usdt=_decimal(
            evidence_raw,
            "realized_net_after_execution_fees_usdt",
            "terminal evidence facts",
        ),
        execution_fees_usdt=_decimal(
            evidence_raw, "execution_fees_usdt", "terminal evidence facts"
        ),
        account_closed_pnl_usdt=_optional_decimal(
            evidence_raw, "account_closed_pnl_usdt", "terminal evidence facts"
        ),
        funding_net_usdt=_optional_decimal(
            evidence_raw, "funding_net_usdt", "terminal evidence facts"
        ),
        all_in_net_pnl_usdt=_optional_decimal(
            evidence_raw, "all_in_net_pnl_usdt", "terminal evidence facts"
        ),
        profit_outcome_status=_text(
            evidence_raw, "profit_outcome_status", "terminal evidence facts"
        ),
        positive_peak_nonpositive_gross_exit=_boolean(
            evidence_raw,
            "positive_peak_nonpositive_gross_exit",
            "terminal evidence facts",
        ),
        gross_positive_fill_nonpositive=_boolean(
            evidence_raw, "gross_positive_fill_nonpositive", "terminal evidence facts"
        ),
        fill_positive_account_nonpositive=_boolean(
            evidence_raw, "fill_positive_account_nonpositive", "terminal evidence facts"
        ),
        account_positive_all_in_nonpositive=_boolean(
            evidence_raw,
            "account_positive_all_in_nonpositive",
            "terminal evidence facts",
        ),
        positive_peak_nonpositive_all_in=_optional_boolean(
            evidence_raw,
            "positive_peak_nonpositive_all_in",
            "terminal evidence facts",
        ),
        fully_reconciled_all_in=_boolean(
            evidence_raw, "fully_reconciled_all_in", "terminal evidence facts"
        ),
        diagnostics_only=_boolean(
            evidence_raw, "diagnostics_only", "terminal evidence facts"
        ),
        exit_threshold_retuning_allowed=_boolean(
            evidence_raw,
            "exit_threshold_retuning_allowed",
            "terminal evidence facts",
        ),
        strategy_promotion_allowed=_boolean(
            evidence_raw, "strategy_promotion_allowed", "terminal evidence facts"
        ),
        live_mainnet_order_routing_allowed=_boolean(
            evidence_raw,
            "live_mainnet_order_routing_allowed",
            "terminal evidence facts",
        ),
    )
    terminal = BybitDemoTerminalEvidenceV120(
        entry_order_link_id=_text(payload, "entry_order_link_id", "terminal evidence"),
        checkpoint_revision=_text(payload, "checkpoint_revision", "terminal evidence"),
        evidence=evidence,
    )
    validate_terminal_evidence_v120(terminal)
    return terminal


def validate_terminal_evidence_v120(terminal: BybitDemoTerminalEvidenceV120) -> None:
    _demo_order_link(terminal.entry_order_link_id, "terminal evidence")
    _sha256_text(terminal.checkpoint_revision, "terminal evidence checkpoint revision")
    evidence = terminal.evidence
    if not evidence.symbol or evidence.symbol != evidence.symbol.upper():
        raise ValueError("terminal evidence symbol must be uppercase")
    _side(evidence.side, "terminal evidence")
    if evidence.observation_count < 1:
        raise ValueError("terminal evidence observation count must be positive")
    for label, value in _terminal_decimal_values(evidence):
        _finite_decimal(value, label)
    if evidence.execution_fees_usdt < 0:
        raise ValueError("terminal evidence execution fees cannot be negative")
    if not evidence.fully_reconciled_all_in or evidence.all_in_net_pnl_usdt is None:
        raise ValueError("terminal evidence requires fully reconciled all-in evidence")
    if evidence.profit_outcome_status not in _FULLY_RECONCILED_OUTCOMES:
        raise ValueError("terminal evidence requires a fully reconciled outcome status")
    if not evidence.diagnostics_only or evidence.exit_threshold_retuning_allowed:
        raise ValueError("terminal evidence must remain diagnostics-only")
    if evidence.strategy_promotion_allowed:
        raise ValueError("terminal evidence cannot authorize strategy promotion")
    if evidence.live_mainnet_order_routing_allowed:
        raise ValueError("terminal evidence cannot permit live routing")


def _fallback_from_payload(item: object) -> BybitDemoFallbackAttemptV120:
    if not isinstance(item, Mapping):
        raise ValueError("entry provenance fallback attempt must be an object")
    _require_exact_keys(item, _FALLBACK_KEYS, "entry provenance fallback attempt")
    attempt = BybitDemoFallbackAttemptV120(
        symbol=_text(item, "symbol", "entry provenance fallback attempt"),
        side=_text(item, "side", "entry provenance fallback attempt"),
        stage=_text(item, "stage", "entry provenance fallback attempt"),
        reasons=_text_tuple(item, "reasons", "entry provenance fallback attempt"),
        quote_price=_optional_decimal(
            item, "quote_price", "entry provenance fallback attempt"
        ),
        modeled_entry_price=_optional_decimal(
            item, "modeled_entry_price", "entry provenance fallback attempt"
        ),
    )
    _validate_fallback(attempt)
    return attempt


def _validate_fallback(attempt: BybitDemoFallbackAttemptV120) -> None:
    if not attempt.symbol or attempt.symbol != attempt.symbol.upper():
        raise ValueError("entry provenance fallback symbol must be uppercase")
    _side(attempt.side, "entry provenance fallback")
    if attempt.stage not in _FALLBACK_STAGES:
        raise ValueError("entry provenance fallback stage is unsupported")
    for value in (attempt.quote_price, attempt.modeled_entry_price):
        if value is not None:
            _finite_decimal(value, "entry provenance fallback decimal")


def _entry_decimal_values(
    provenance: BybitDemoEntryDecisionProvenanceV120,
) -> tuple[tuple[str, Decimal | None], ...]:
    return (
        ("expected_net_edge_usd", provenance.expected_net_edge_usd),
        ("risk_budget_usdt", provenance.risk_budget_usdt),
        ("quality_score", provenance.quality_score),
        ("target_net_profit_usd", provenance.target_net_profit_usd),
        ("planned_reference_price", provenance.planned_reference_price),
        ("planned_reference_quantity", provenance.planned_reference_quantity),
        ("planned_notional_usdt", provenance.planned_notional_usdt),
        ("modeled_round_trip_cost_usdt", provenance.modeled_round_trip_cost_usdt),
        ("pre_entry_quote_price", provenance.pre_entry_quote_price),
        ("pre_entry_modeled_entry_price", provenance.pre_entry_modeled_entry_price),
        ("pre_entry_original_quantity", provenance.pre_entry_original_quantity),
        ("pre_entry_adjusted_quantity", provenance.pre_entry_adjusted_quantity),
        (
            "pre_entry_quantity_retention_fraction",
            provenance.pre_entry_quantity_retention_fraction,
        ),
        ("actual_average_entry_price", provenance.actual_average_entry_price),
        ("actual_filled_quantity", provenance.actual_filled_quantity),
        ("actual_fill_notional_usdt", provenance.actual_fill_notional_usdt),
        (
            "actual_fill_adverse_slippage_bps_vs_modeled_entry",
            provenance.actual_fill_adverse_slippage_bps_vs_modeled_entry,
        ),
        ("account_taker_fee_rate", provenance.account_taker_fee_rate),
        ("stop_to_liquidation_r", provenance.stop_to_liquidation_r),
        ("effective_account_equity_usdt", provenance.effective_account_equity_usdt),
        ("effective_peak_equity_usdt", provenance.effective_peak_equity_usdt),
    )


def _terminal_decimal_values(
    evidence: BybitDemoTerminalEvidenceFactsV120,
) -> tuple[tuple[str, Decimal | None], ...]:
    return (
        ("observed_peak_favorable_r", evidence.observed_peak_favorable_r),
        ("observed_max_adverse_r", evidence.observed_max_adverse_r),
        ("realized_gross_exit_r", evidence.realized_gross_exit_r),
        ("observed_peak_capture_fraction", evidence.observed_peak_capture_fraction),
        (
            "giveback_from_observed_peak_to_exit_r",
            evidence.giveback_from_observed_peak_to_exit_r,
        ),
        ("realized_gross_pnl_usdt", evidence.realized_gross_pnl_usdt),
        (
            "realized_net_after_execution_fees_usdt",
            evidence.realized_net_after_execution_fees_usdt,
        ),
        ("execution_fees_usdt", evidence.execution_fees_usdt),
        ("account_closed_pnl_usdt", evidence.account_closed_pnl_usdt),
        ("funding_net_usdt", evidence.funding_net_usdt),
        ("all_in_net_pnl_usdt", evidence.all_in_net_pnl_usdt),
    )


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _json_object(raw: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} canonical record is invalid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} canonical record must be an object")
    return value


def _require_exact_keys(payload: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    actual = set(payload)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError(f"{label} key set mismatch:missing={missing}:extra={extra}")


def _text(payload: Mapping[str, Any], field: str, label: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} missing text field:{field}")
    return value


def _optional_text(payload: Mapping[str, Any], field: str, label: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} invalid optional text field:{field}")
    return value


def _boolean(payload: Mapping[str, Any], field: str, label: str) -> bool:
    value = payload.get(field)
    if not isinstance(value, bool):
        raise ValueError(f"{label} invalid bool field:{field}")
    return value


def _optional_boolean(payload: Mapping[str, Any], field: str, label: str) -> bool | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ValueError(f"{label} invalid optional bool field:{field}")
    return value


def _integer(payload: Mapping[str, Any], field: str, label: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} invalid integer field:{field}")
    return value


def _decimal(payload: Mapping[str, Any], field: str, label: str) -> Decimal:
    value = _optional_decimal(payload, field, label)
    if value is None:
        raise ValueError(f"{label} missing decimal field:{field}")
    return value


def _optional_decimal(payload: Mapping[str, Any], field: str, label: str) -> Decimal | None:
    value = payload.get(field)
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError(f"{label} invalid decimal field:{field}") from exc
    _finite_decimal(parsed, f"{label} {field}")
    return parsed


def _text_tuple(payload: Mapping[str, Any], field: str, label: str) -> tuple[str, ...]:
    value = payload.get(field)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{label} invalid text list field:{field}")
    return tuple(value)


def _decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _finite_decimal(value: Decimal | None, label: str) -> None:
    if value is not None and not value.is_finite():
        raise ValueError(f"{label} must be finite")


def _sha256_text(value: str, label: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"{label} must be lowercase sha256")


def _demo_order_link(value: str, label: str) -> None:
    if not value.startswith("ASTRA-DEMO-"):
        raise ValueError(f"{label} requires ASTRA-DEMO orderLinkId")


def _side(value: str, label: str) -> None:
    if value not in _SIDES:
        raise ValueError(f"{label} side must be LONG or SHORT")


def _aware_iso(value: str, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{label} is required")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")


__all__ = [
    "BybitDemoApprovedEntryAuthorizationV120",
    "BybitDemoEntryDecisionProvenanceV120",
    "BybitDemoFallbackAttemptV120",
    "BybitDemoTerminalEvidenceFactsV120",
    "BybitDemoTerminalEvidenceV120",
    "canonical_sha256",
    "decode_approved_entry_authorization_v120",
    "decode_entry_provenance_v120",
    "decode_terminal_evidence_v120",
    "encode_approved_entry_authorization_v120",
    "encode_entry_provenance_v120",
    "encode_terminal_evidence_v120",
    "validate_approved_entry_authorization_v120",
    "validate_entry_provenance_v120",
    "validate_terminal_evidence_v120",
]
