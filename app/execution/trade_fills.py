from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Protocol

from app.domain.trading import Fill, Side
from app.oms.protocols import OmsStore
from app.oms.store import OrderRecord, OrderState
from app.portfolio.ledger import PortfolioLedger
from app.portfolio.protocols import PortfolioStore


class TradeFillProtocolError(ValueError):
    """Raised when a broker fill frame is incomplete or internally inconsistent."""


@dataclass(frozen=True)
class ExactBrokerFill:
    execution_id: str
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: Side
    order_quantity: Decimal
    cumulative_quantity: Decimal
    quantity: Decimal
    price: Decimal
    occurred_at: datetime

    def validate(self) -> None:
        for field, value in (
            ("execution_id", self.execution_id),
            ("broker_order_id", self.broker_order_id),
            ("client_order_id", self.client_order_id),
            ("symbol", self.symbol),
        ):
            if not value.strip():
                raise TradeFillProtocolError(f"{field} is required")
        if self.symbol != self.symbol.upper():
            raise TradeFillProtocolError("symbol must be uppercase")
        for field, value in (
            ("order_quantity", self.order_quantity),
            ("cumulative_quantity", self.cumulative_quantity),
            ("quantity", self.quantity),
            ("price", self.price),
        ):
            if not value.is_finite() or value <= 0:
                raise TradeFillProtocolError(f"{field} must be positive and finite")
        if self.quantity > self.cumulative_quantity:
            raise TradeFillProtocolError("fill quantity exceeds cumulative quantity")
        if self.cumulative_quantity > self.order_quantity:
            raise TradeFillProtocolError("cumulative quantity exceeds order quantity")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise TradeFillProtocolError("occurred_at must be timezone-aware")


class PaperFillFeeProvider(Protocol):
    def fee_for(self, fill: ExactBrokerFill) -> Decimal: ...


class ExplicitZeroPaperFeeModel:
    """Explicit zero-fee model for controlled paper validation only.

    The name is intentionally explicit so production code cannot silently interpret a
    missing broker fee as zero. A real fee/activity provider can implement the same port.
    """

    def fee_for(self, fill: ExactBrokerFill) -> Decimal:
        fill.validate()
        return Decimal("0")


@dataclass(frozen=True)
class FillAccountingResult:
    record: OrderRecord
    fill: Fill
    portfolio_event_appended: bool
    oms_advanced: bool


def _decimal(value: object, field: str) -> Decimal:
    if value is None or value == "":
        raise TradeFillProtocolError(f"missing decimal field: {field}")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise TradeFillProtocolError(f"invalid decimal field: {field}") from exc
    if not result.is_finite():
        raise TradeFillProtocolError(f"non-finite decimal field: {field}")
    return result


def _mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TradeFillProtocolError(f"{field} must be an object")
    return value


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise TradeFillProtocolError("fill timestamp is required")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise TradeFillProtocolError("invalid fill timestamp") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise TradeFillProtocolError("fill timestamp must be timezone-aware")
    return result.astimezone(UTC)


def parse_alpaca_trade_fill(raw_frame: bytes | str) -> ExactBrokerFill | None:
    """Parse exact fill economics from one Alpaca ``trade_updates`` frame.

    Non-fill messages return ``None``. Fill messages fail closed when execution identity,
    exact price/quantity, order identity, or broker timestamp is missing. This parser is
    deliberately independent of the legacy V100 stream state machine so portfolio
    accounting never has to infer execution price from an order limit.
    """

    if isinstance(raw_frame, bytes):
        try:
            text = raw_frame.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise TradeFillProtocolError("trade update is not UTF-8") from exc
    else:
        text = raw_frame
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise TradeFillProtocolError("trade update is not valid JSON") from exc
    root = _mapping(document, "trade update")
    if str(root.get("stream", "")) != "trade_updates":
        return None
    data = _mapping(root.get("data"), "trade update data")
    event = str(data.get("event", "")).lower()
    if event not in {"partial_fill", "fill"}:
        return None
    order = _mapping(data.get("order"), "trade update order")
    try:
        side = Side(str(order.get("side", "")).upper())
    except ValueError as exc:
        raise TradeFillProtocolError("unsupported fill side") from exc
    fill = ExactBrokerFill(
        execution_id=str(data.get("execution_id", "")).strip(),
        broker_order_id=str(order.get("id", "")).strip(),
        client_order_id=str(order.get("client_order_id", "")).strip(),
        symbol=str(order.get("symbol", "")).strip().upper(),
        side=side,
        order_quantity=_decimal(order.get("qty"), "order.qty"),
        cumulative_quantity=_decimal(order.get("filled_qty"), "order.filled_qty"),
        quantity=_decimal(data.get("qty"), "data.qty"),
        price=_decimal(data.get("price"), "data.price"),
        occurred_at=_timestamp(data.get("timestamp")),
    )
    fill.validate()
    return fill


class PaperTradeFillAccounting:
    """Apply exact broker fill events to durable portfolio state and OMS quantity.

    Portfolio persistence happens first. Its event id is the broker execution id, so a
    retry after a crash is idempotent. When a runtime ledger is supplied, a newly
    persisted event is applied to that same replayed ledger exactly once so strategy and
    risk see the broker fill immediately without waiting for a process restart.
    """

    _DIRECT_STATES = frozenset(
        {
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIALLY_FILLED,
            OrderState.CANCEL_REQUESTED,
        }
    )
    _LATE_EVENT_STATES = frozenset(
        {
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.UNCERTAIN,
            OrderState.RECONCILING,
            OrderState.RECONCILED,
            OrderState.MANUAL,
        }
    )

    def __init__(
        self,
        *,
        oms: OmsStore,
        portfolio: PortfolioStore,
        fee_provider: PaperFillFeeProvider,
        runtime_ledger: PortfolioLedger | None = None,
    ) -> None:
        self.oms = oms
        self.portfolio = portfolio
        self.fee_provider = fee_provider
        self.runtime_ledger = runtime_ledger

    def apply(self, intent_id: str, broker_fill: ExactBrokerFill) -> FillAccountingResult:
        broker_fill.validate()
        record = self.oms.get(intent_id)
        if record is None:
            raise KeyError(intent_id)
        self._validate_identity(record, broker_fill)

        if record.state is OrderState.SUBMIT_STARTED:
            record = self.oms.transition(
                intent_id,
                OrderState.ACKNOWLEDGED,
                event_id=f"fill-ack:{broker_fill.execution_id}",
                occurred_at=broker_fill.occurred_at,
                broker_order_id=broker_fill.broker_order_id,
            )
        elif record.state not in self._DIRECT_STATES | self._LATE_EVENT_STATES:
            raise ValueError(f"FILL_NOT_ADMISSIBLE:{record.state.value}")

        if (
            record.state in self._LATE_EVENT_STATES
            and broker_fill.cumulative_quantity > record.filled_quantity
        ):
            raise ValueError(f"FILL_REQUIRES_RECONCILIATION:{record.state.value}")

        fee = self.fee_provider.fee_for(broker_fill)
        if not fee.is_finite() or fee < 0:
            raise ValueError("fill fee must be finite and non-negative")
        domain_fill = Fill(
            fill_id=f"broker:{broker_fill.execution_id}",
            order_intent_id=intent_id,
            symbol=broker_fill.symbol,
            side=broker_fill.side,
            quantity=broker_fill.quantity,
            price=broker_fill.price,
            fee=fee,
            occurred_at=broker_fill.occurred_at,
        )
        appended = self.portfolio.append_fill(domain_fill)
        if appended and self.runtime_ledger is not None:
            self.runtime_ledger.apply_fill(domain_fill)

        advanced = broker_fill.cumulative_quantity > record.filled_quantity
        if advanced:
            record = self.oms.apply_cumulative_fill(
                intent_id,
                event_id=f"broker-fill:{broker_fill.execution_id}",
                cumulative_filled=broker_fill.cumulative_quantity,
                occurred_at=broker_fill.occurred_at,
                broker_order_id=broker_fill.broker_order_id,
            )
        return FillAccountingResult(
            record=record,
            fill=domain_fill,
            portfolio_event_appended=appended,
            oms_advanced=advanced,
        )

    @staticmethod
    def _validate_identity(record: OrderRecord, broker_fill: ExactBrokerFill) -> None:
        if record.client_order_id != broker_fill.client_order_id:
            raise ValueError("BROKER_CLIENT_ORDER_ID_MISMATCH")
        if record.broker_order_id and record.broker_order_id != broker_fill.broker_order_id:
            raise ValueError("BROKER_ORDER_ID_MISMATCH")
        if record.symbol != broker_fill.symbol:
            raise ValueError("BROKER_SYMBOL_MISMATCH")
        if record.side is not broker_fill.side:
            raise ValueError("BROKER_SIDE_MISMATCH")
        if record.quantity != broker_fill.order_quantity:
            raise ValueError("BROKER_QUANTITY_MISMATCH")
