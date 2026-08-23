from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.execution.bybit_demo import (
    BybitDemoOrderAck,
    BybitDemoOrderRequest,
    BybitDemoProtectionRequest,
    BybitDemoRunnerProtectionRequest,
)
from app.execution.bybit_demo_strategy_selector import (
    BybitDemoStrategySelection,
    select_bybit_demo_trade_plan,
)
from app.marketdata.bybit_instruments import BybitInstrumentSpec
from app.marketdata.bybit_v5 import BybitKlineBar
from app.strategy.crypto_perp import (
    CryptoPerpStrategyConfig,
    CryptoTradePlan,
    build_trade_plan,
    evaluate_crypto_signal,
)
from app.strategy.crypto_session_risk import CryptoSessionRiskState

_APPROVAL_PHRASE = "APPROVE_BYBIT_DEMO_EXECUTION"
_INTERVAL = timedelta(minutes=5)
_MAX_SIGNAL_AGE = timedelta(minutes=10)
_MAX_APPROVAL_TTL = timedelta(minutes=2)
_HEX = frozenset("0123456789abcdef")


@dataclass(frozen=True)
class BybitDemoOperatorApproval:
    source_snapshot_id: str
    source_evidence_rank: int
    source_market_rank: int
    symbol: str
    side: str
    decision_time: str
    signal_available_at: str
    signal_quality_score: Decimal
    source_planned_notional_usdt: Decimal
    source_risk_budget_usdt: Decimal
    source_modeled_round_trip_cost_usdt: Decimal
    maximum_entry_quantity: Decimal
    approved_at: str
    expires_at: str
    operator_confirmed: bool = True
    environment: str = "BYBIT_DEMO"
    single_use_entry_required: bool = True
    live_mainnet_order_routing_allowed: bool = False

    def validate(self, *, now: datetime | None = None) -> None:
        _validate_sha(self.source_snapshot_id, "source snapshot")
        if not 1 <= self.source_evidence_rank <= 50:
            raise ValueError("demo approval evidence rank must be within [1, 50]")
        if not 1 <= self.source_market_rank <= 50:
            raise ValueError("demo approval market rank must be within [1, 50]")
        _validate_symbol(self.symbol)
        if self.side not in {"LONG", "SHORT"}:
            raise ValueError("demo approval side must be LONG or SHORT")
        decision = _parse_time(self.decision_time)
        available = _parse_time(self.signal_available_at)
        approved = _parse_time(self.approved_at)
        expires = _parse_time(self.expires_at)
        if available != decision + _INTERVAL:
            raise ValueError("demo approval signal availability must follow the decision bar")
        if approved < available:
            raise ValueError("demo approval cannot precede signal availability")
        if approved - available > _MAX_SIGNAL_AGE:
            raise ValueError("demo approval rejected a stale evidence-ranked signal")
        if expires <= approved or expires - approved > _MAX_APPROVAL_TTL:
            raise ValueError("demo approval expiry exceeds the two-minute execution window")
        if now is not None:
            current = _utc(now)
            if current < approved or current > expires:
                raise ValueError("demo approval is not valid at the execution time")
        for name, value in (
            ("signal_quality_score", self.signal_quality_score),
            ("source_planned_notional_usdt", self.source_planned_notional_usdt),
            ("source_risk_budget_usdt", self.source_risk_budget_usdt),
            (
                "source_modeled_round_trip_cost_usdt",
                self.source_modeled_round_trip_cost_usdt,
            ),
            ("maximum_entry_quantity", self.maximum_entry_quantity),
        ):
            if not value.is_finite():
                raise ValueError(f"demo approval {name} must be finite")
        if self.signal_quality_score <= 0:
            raise ValueError("demo approval signal quality must be positive")
        if self.source_planned_notional_usdt <= 0 or self.source_risk_budget_usdt <= 0:
            raise ValueError("demo approval source notional/risk must be positive")
        if self.source_modeled_round_trip_cost_usdt < 0:
            raise ValueError("demo approval modeled cost cannot be negative")
        if self.maximum_entry_quantity <= 0:
            raise ValueError("demo approval maximum entry quantity must be positive")
        if not self.operator_confirmed:
            raise ValueError("demo approval requires explicit operator confirmation")
        if self.environment != "BYBIT_DEMO":
            raise ValueError("demo approval environment must be BYBIT_DEMO")
        if not self.single_use_entry_required:
            raise ValueError("demo approval must be single-use for entry")
        if self.live_mainnet_order_routing_allowed:
            raise ValueError("demo approval cannot enable mainnet order routing")

    @property
    def expected_entry_order_link_id(self) -> str:
        return _order_link_id(
            symbol=self.symbol,
            side=self.side,
            decision_time=self.decision_time,
            action="ENTRY",
        )

    @property
    def expected_close_order_link_id(self) -> str:
        return _order_link_id(
            symbol=self.symbol,
            side=self.side,
            decision_time=self.decision_time,
            action="CLOSE",
        )

    @property
    def approval_id(self) -> str:
        canonical = json.dumps(
            self.to_payload(include_approval_id=False),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def to_payload(self, *, include_approval_id: bool = True) -> dict[str, Any]:
        self.validate()
        payload: dict[str, Any] = {
            "schema": "BYBIT_OPERATOR_APPROVED_DEMO_EXECUTION_V1",
            "source_snapshot_id": self.source_snapshot_id,
            "source_evidence_rank": self.source_evidence_rank,
            "source_market_rank": self.source_market_rank,
            "symbol": self.symbol,
            "side": self.side,
            "decision_time": self.decision_time,
            "signal_available_at": self.signal_available_at,
            "signal_quality_score": str(self.signal_quality_score),
            "source_planned_notional_usdt": str(self.source_planned_notional_usdt),
            "source_risk_budget_usdt": str(self.source_risk_budget_usdt),
            "source_modeled_round_trip_cost_usdt": str(
                self.source_modeled_round_trip_cost_usdt
            ),
            "maximum_entry_quantity": str(self.maximum_entry_quantity),
            "expected_entry_order_link_id": self.expected_entry_order_link_id,
            "expected_close_order_link_id": self.expected_close_order_link_id,
            "approved_at": self.approved_at,
            "expires_at": self.expires_at,
            "operator_confirmed": self.operator_confirmed,
            "environment": self.environment,
            "single_use_entry_required": self.single_use_entry_required,
            "trade_actionable": False,
            "demo_order_write_requires_this_approval": True,
            "live_activation_allowed": False,
            "bybit_live_order_routing_allowed": False,
        }
        if include_approval_id:
            payload["approval_id"] = self.approval_id
        return payload


def create_bybit_demo_operator_approval(
    review_row: Mapping[str, Any],
    bars: Sequence[BybitKlineBar],
    *,
    approved_at: datetime,
    confirmation_phrase: str,
    ttl_seconds: int = 120,
    strategy_config: CryptoPerpStrategyConfig | None = None,
) -> BybitDemoOperatorApproval:
    """Create a short-lived approval for one exact positive-evidence demo signal."""

    if confirmation_phrase != _APPROVAL_PHRASE:
        raise ValueError("demo execution confirmation phrase is invalid")
    if isinstance(ttl_seconds, bool) or not 1 <= ttl_seconds <= 120:
        raise ValueError("demo approval TTL must be within [1, 120] seconds")
    approved = _utc(approved_at)
    _validate_review_row(review_row)
    config = CryptoPerpStrategyConfig() if strategy_config is None else strategy_config
    config.validate()
    if config != CryptoPerpStrategyConfig():
        raise ValueError("demo approval requires the qualified fixed strategy config")

    symbol = str(review_row["symbol"])
    decision_text = str(review_row["decision_time"])
    decision = _parse_time(decision_text)
    history = _history_through_decision(bars, symbol=symbol, decision=decision)
    evaluation = evaluate_crypto_signal(history, config)
    if not evaluation.eligible or evaluation.signal is None:
        raise ValueError("demo approval fixed signal no longer reproduces")
    signal = evaluation.signal
    if signal.side.value != review_row.get("signal_side"):
        raise ValueError("demo approval source side does not reproduce")
    if signal.decision_time != decision_text:
        raise ValueError("demo approval source decision time does not reproduce")
    source_quality = _decimal(review_row.get("signal_quality_score"), "signal_quality_score")
    if signal.quality_score != source_quality:
        raise ValueError("demo approval source signal quality does not reproduce")

    source_risk = _decimal(review_row.get("risk_budget_usdt"), "risk_budget_usdt")
    source_notional = _decimal(
        review_row.get("planned_notional_usdt"),
        "planned_notional_usdt",
    )
    source_cost = _decimal(
        review_row.get("estimated_round_trip_cost_usdt"),
        "estimated_round_trip_cost_usdt",
    )
    source_expected_edge = _decimal(
        review_row.get("expected_net_edge_usd"),
        "expected_net_edge_usd",
    )
    plan_evaluation = build_trade_plan(
        signal,
        equity_usdt=source_risk / config.risk_fraction_per_trade,
        config=config,
    )
    if not plan_evaluation.eligible or plan_evaluation.plan is None:
        raise ValueError("demo approval source trade plan no longer reproduces")
    plan = plan_evaluation.plan
    if plan.notional_usdt != source_notional:
        raise ValueError("demo approval source notional does not reproduce")
    if plan.risk_budget_usdt != source_risk:
        raise ValueError("demo approval source risk does not reproduce")
    if plan.estimated_round_trip_cost_usdt != source_cost:
        raise ValueError("demo approval source modeled cost does not reproduce")
    if plan.expected_net_edge_usd != source_expected_edge:
        raise ValueError("demo approval source expected edge does not reproduce")

    approval = BybitDemoOperatorApproval(
        source_snapshot_id=str(review_row["snapshot_id"]),
        source_evidence_rank=_integer(review_row.get("evidence_rank"), "evidence_rank"),
        source_market_rank=_integer(review_row.get("market_rank"), "market_rank"),
        symbol=symbol,
        side=signal.side.value,
        decision_time=decision_text,
        signal_available_at=(decision + _INTERVAL).isoformat(),
        signal_quality_score=signal.quality_score,
        source_planned_notional_usdt=plan.notional_usdt,
        source_risk_budget_usdt=plan.risk_budget_usdt,
        source_modeled_round_trip_cost_usdt=plan.estimated_round_trip_cost_usdt,
        maximum_entry_quantity=plan.reference_quantity,
        approved_at=approved.isoformat(),
        expires_at=(approved + timedelta(seconds=ttl_seconds)).isoformat(),
    )
    approval.validate(now=approved)
    return approval


def validate_demo_approval_against_latest_review_row(
    approval: BybitDemoOperatorApproval,
    review_row: Mapping[str, Any],
    *,
    now: datetime,
) -> None:
    approval.validate(now=now)
    _validate_review_row(review_row)
    expected = {
        "snapshot_id": approval.source_snapshot_id,
        "evidence_rank": approval.source_evidence_rank,
        "market_rank": approval.source_market_rank,
        "symbol": approval.symbol,
        "signal_side": approval.side,
        "decision_time": approval.decision_time,
    }
    for field, value in expected.items():
        if review_row.get(field) != value:
            raise ValueError(f"demo approval latest review row changed:{field}")
    if _decimal(review_row.get("signal_quality_score"), "signal_quality_score") != (
        approval.signal_quality_score
    ):
        raise ValueError("demo approval latest review row changed:signal_quality_score")
    if _decimal(review_row.get("planned_notional_usdt"), "planned_notional_usdt") != (
        approval.source_planned_notional_usdt
    ):
        raise ValueError("demo approval latest review row changed:planned_notional_usdt")
    if _decimal(review_row.get("risk_budget_usdt"), "risk_budget_usdt") != (
        approval.source_risk_budget_usdt
    ):
        raise ValueError("demo approval latest review row changed:risk_budget_usdt")
    if _decimal(
        review_row.get("estimated_round_trip_cost_usdt"),
        "estimated_round_trip_cost_usdt",
    ) != approval.source_modeled_round_trip_cost_usdt:
        raise ValueError("demo approval latest review row changed:modeled_cost")


def dry_check_approved_opportunity_matches_demo_selector(
    approval: BybitDemoOperatorApproval,
    review_row: Mapping[str, Any],
    bars_by_symbol: Mapping[str, Sequence[BybitKlineBar]],
    *,
    instruments: Mapping[str, BybitInstrumentSpec],
    strategy_config: CryptoPerpStrategyConfig,
    session_state: CryptoSessionRiskState,
    now: datetime,
) -> BybitDemoStrategySelection:
    """Require the current demo selector to independently choose the approved signal."""

    validate_demo_approval_against_latest_review_row(approval, review_row, now=now)
    selection = select_bybit_demo_trade_plan(
        bars_by_symbol,
        instruments=instruments,
        strategy_config=strategy_config,
        session_state=session_state,
        now=now,
    )
    plan = selection.selected_trade_plan
    if plan is None:
        raise ValueError("demo approval current selector has no executable plan")
    _validate_selected_plan_against_approval(plan, approval)
    return selection


class OperatorApprovedBybitDemoClient:
    """Last-line demo client guard for one exact approved entry identity.

    Read methods are delegated. The first non-reduce-only order must match the approved
    symbol/side/decision-derived orderLinkId and cannot exceed the source approved quantity.
    The entry authorization is consumed before the network mutation call, so an ambiguous
    transport outcome cannot be retried through the same approval. Protective writes and an
    emergency reduce-only close are restricted to the same approved symbol.
    """

    def __init__(self, client: Any, approval: BybitDemoOperatorApproval, *, now: datetime) -> None:
        approval.validate(now=now)
        if getattr(client, "environment", None) != "BYBIT_DEMO":
            raise ValueError("operator approval wrapper requires a BYBIT_DEMO client")
        if getattr(client, "live_mainnet_order_routing_allowed", True) is not False:
            raise ValueError("operator approval wrapper rejected a mainnet-capable client")
        self._client = client
        self._approval = approval
        self._entry_consumed = False

    @property
    def environment(self) -> str:
        return "BYBIT_DEMO"

    @property
    def live_mainnet_order_routing_allowed(self) -> bool:
        return False

    @property
    def entry_approval_consumed(self) -> bool:
        return self._entry_consumed

    def get_fee_rate(self, *, symbol: str):
        _require_approved_symbol(symbol, self._approval)
        return self._client.get_fee_rate(symbol=symbol)

    def get_positions(self, *, settle_coin: str = "USDT"):
        return self._client.get_positions(settle_coin=settle_coin)

    def get_executions(
        self,
        *,
        symbol: str,
        order_link_id: str | None = None,
        limit: int = 50,
    ):
        _require_approved_symbol(symbol, self._approval)
        if order_link_id is not None and order_link_id not in {
            self._approval.expected_entry_order_link_id,
            self._approval.expected_close_order_link_id,
        }:
            raise ValueError("operator approval rejected execution query for another order")
        return self._client.get_executions(
            symbol=symbol,
            order_link_id=order_link_id,
            limit=limit,
        )

    def place_market_order(self, request: BybitDemoOrderRequest) -> BybitDemoOrderAck:
        request.validate()
        if request.reduce_only:
            self._validate_reduce_only_close(request)
            return self._client.place_market_order(request)
        if self._entry_consumed:
            raise ValueError("operator demo entry approval has already been consumed")
        self._validate_entry(request)
        self._entry_consumed = True
        return self._client.place_market_order(request)

    def set_full_position_protection(self, request: BybitDemoProtectionRequest):
        request.validate()
        self._require_entry_before_protection(request.symbol)
        return self._client.set_full_position_protection(request)

    def set_open_ended_position_protection(
        self,
        request: BybitDemoRunnerProtectionRequest,
    ):
        request.validate()
        self._require_entry_before_protection(request.symbol)
        return self._client.set_open_ended_position_protection(request)

    def cancel_order(self, *, symbol: str, order_link_id: str) -> BybitDemoOrderAck:
        _require_approved_symbol(symbol, self._approval)
        if order_link_id not in {
            self._approval.expected_entry_order_link_id,
            self._approval.expected_close_order_link_id,
        }:
            raise ValueError("operator approval rejected cancel for another order")
        return self._client.cancel_order(symbol=symbol, order_link_id=order_link_id)

    def _validate_entry(self, request: BybitDemoOrderRequest) -> None:
        _require_approved_symbol(request.symbol, self._approval)
        expected_side = "Buy" if self._approval.side == "LONG" else "Sell"
        if request.side != expected_side:
            raise ValueError("operator approval rejected entry side mismatch")
        if request.order_link_id != self._approval.expected_entry_order_link_id:
            raise ValueError("operator approval rejected entry decision identity mismatch")
        if request.quantity > self._approval.maximum_entry_quantity:
            raise ValueError("operator approval rejected entry quantity above approved cap")

    def _validate_reduce_only_close(self, request: BybitDemoOrderRequest) -> None:
        _require_approved_symbol(request.symbol, self._approval)
        expected_side = "Sell" if self._approval.side == "LONG" else "Buy"
        if request.side != expected_side:
            raise ValueError("operator approval rejected reduce-only close side mismatch")
        if request.order_link_id != self._approval.expected_close_order_link_id:
            raise ValueError("operator approval rejected close decision identity mismatch")
        if request.quantity > self._approval.maximum_entry_quantity:
            raise ValueError("operator approval rejected close quantity above approved cap")

    def _require_entry_before_protection(self, symbol: str) -> None:
        _require_approved_symbol(symbol, self._approval)
        if not self._entry_consumed:
            raise ValueError("operator approval rejected protection before approved entry")


def _validate_selected_plan_against_approval(
    plan: CryptoTradePlan,
    approval: BybitDemoOperatorApproval,
) -> None:
    if plan.symbol != approval.symbol:
        raise ValueError("demo approval selector chose another symbol")
    if plan.side.value != approval.side:
        raise ValueError("demo approval selector chose another side")
    if plan.decision_time != approval.decision_time:
        raise ValueError("demo approval selector chose another decision time")
    if plan.quality_score != approval.signal_quality_score:
        raise ValueError("demo approval selector signal quality changed")
    if plan.notional_usdt > approval.source_planned_notional_usdt:
        raise ValueError("demo approval selector notional exceeds source approval")
    if plan.risk_budget_usdt > approval.source_risk_budget_usdt:
        raise ValueError("demo approval selector risk exceeds source approval")
    if plan.estimated_round_trip_cost_usdt > approval.source_modeled_round_trip_cost_usdt:
        raise ValueError("demo approval selector modeled cost exceeds source approval")
    if plan.reference_quantity > approval.maximum_entry_quantity:
        raise ValueError("demo approval selector quantity exceeds source approval")


def _validate_review_row(row: Mapping[str, Any]) -> None:
    if row.get("qualification_state") != "QUALIFIED_POSITIVE_EVIDENCE":
        raise ValueError("demo approval requires QUALIFIED_POSITIVE_EVIDENCE")
    if row.get("evidence_sample_sufficient") is not True:
        raise ValueError("demo approval requires sample-sufficient historical evidence")
    if row.get("positive_historical_evidence") is not True:
        raise ValueError("demo approval requires positive historical evidence")
    if row.get("operator_review_required") is not True:
        raise ValueError("demo approval requires operator review")
    for field in (
        "trade_actionable",
        "strategy_promotion_allowed",
        "demo_activation_allowed",
        "live_activation_allowed",
        "bybit_live_order_routing_allowed",
    ):
        if row.get(field) is not False:
            raise ValueError(f"demo approval source row violates safety flag:{field}")
    _validate_sha(str(row.get("snapshot_id", "")), "source snapshot")
    _validate_symbol(str(row.get("symbol", "")))
    if row.get("signal_side") not in {"LONG", "SHORT"}:
        raise ValueError("demo approval source row side is invalid")
    decision = row.get("decision_time")
    if not isinstance(decision, str):
        raise ValueError("demo approval source row decision time is missing")
    _parse_time(decision)
    for field in (
        "evidence_rank",
        "market_rank",
        "signal_quality_score",
        "expected_net_edge_usd",
        "planned_notional_usdt",
        "risk_budget_usdt",
        "estimated_round_trip_cost_usdt",
    ):
        if row.get(field) is None:
            raise ValueError(f"demo approval source row missing {field}")


def _history_through_decision(
    bars: Sequence[BybitKlineBar],
    *,
    symbol: str,
    decision: datetime,
) -> tuple[BybitKlineBar, ...]:
    ordered = tuple(sorted(bars, key=lambda item: item.start_time))
    if tuple(bars) != ordered:
        raise ValueError("demo approval bars must be chronological")
    if not ordered or any(bar.symbol != symbol for bar in ordered):
        raise ValueError("demo approval bars must belong to the approved symbol")
    for bar in ordered:
        bar.validate()
    history = tuple(bar for bar in ordered if bar.start_time.astimezone(UTC) <= decision)
    if not history or history[-1].start_time.astimezone(UTC) != decision:
        raise ValueError("demo approval requires the exact decision bar")
    return history


def _order_link_id(*, symbol: str, side: str, decision_time: str, action: str) -> str:
    if action not in {"ENTRY", "CLOSE"}:
        raise ValueError("demo approval order action is unsupported")
    payload = "|".join((symbol, side, decision_time, action))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16].upper()
    suffix = "E" if action == "ENTRY" else "C"
    return f"ASTRA-DEMO-{suffix}-{digest}"


def _require_approved_symbol(symbol: str, approval: BybitDemoOperatorApproval) -> None:
    if symbol != approval.symbol:
        raise ValueError("operator approval rejected another symbol")


def _validate_symbol(symbol: str) -> None:
    if (
        not symbol
        or symbol != symbol.strip().upper()
        or not symbol.endswith("USDT")
        or not symbol.isalnum()
    ):
        raise ValueError("demo approval symbol must be normalized USDT")


def _validate_sha(value: str, name: str) -> None:
    if len(value) != 64 or any(char not in _HEX for char in value):
        raise ValueError(f"demo approval {name} must be lowercase sha256")


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("demo approval timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("demo approval timestamp must be timezone-aware")
    return value.astimezone(UTC)


def _decimal(value: Any, field: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError(f"demo approval {field} is missing")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"demo approval {field} is invalid") from exc
    if not parsed.is_finite():
        raise ValueError(f"demo approval {field} must be finite")
    return parsed


def _integer(value: Any, field: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"demo approval {field} is invalid")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"demo approval {field} is invalid") from exc


__all__ = [
    "BybitDemoOperatorApproval",
    "OperatorApprovedBybitDemoClient",
    "create_bybit_demo_operator_approval",
    "dry_check_approved_opportunity_matches_demo_selector",
    "validate_demo_approval_against_latest_review_row",
]
