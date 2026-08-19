from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from app.domain.trading import OrderIntent, Side
from app.execution.bybit_demo import BybitDemoOrderAck, BybitDemoOrderRequest
from app.execution.bybit_observed_rest import ObservedBybitDemoStopRatchetClient
from app.execution.bybit_rest_policy import (
    BybitRestRequestError,
    BybitRestTransportError,
)
from app.marketdata.bybit_entry_reference import BybitEntryReferenceStore
from app.oms.bybit_entry import BybitEntrySubmissionClaim, bybit_entry_intent_id
from app.oms.store import OrderRecord, OrderState


class BybitEntryOmsPort(Protocol):
    live_mainnet_order_routing_allowed: bool
    automatic_resubmit_after_submit_started_allowed: bool

    def claim_entry_submission(
        self,
        intent: OrderIntent,
        *,
        client_order_id: str,
        occurred_at: datetime,
    ) -> BybitEntrySubmissionClaim: ...

    def mark_acknowledged(
        self,
        intent_id: str,
        *,
        broker_order_id: str,
        occurred_at: datetime,
        recovered_by_read: bool,
    ) -> OrderRecord: ...

    def mark_rejected(
        self,
        intent_id: str,
        *,
        occurred_at: datetime,
        reason: str,
    ) -> OrderRecord: ...

    def mark_uncertain(
        self,
        intent_id: str,
        *,
        occurred_at: datetime,
        reason: str,
    ) -> OrderRecord: ...


class BybitEntrySubmissionUncertainError(RuntimeError):
    pass


class BybitEntrySubmissionRejectedError(RuntimeError):
    pass


class BybitEntryOmsPersistenceError(RuntimeError):
    pass


class OmsAwareBybitDemoStopRatchetClient(ObservedBybitDemoStopRatchetClient):
    """Bybit demo client whose ENTRY mutation is owned by the canonical PostgreSQL OMS.

    Reduce-only closes keep their existing lifecycle for now. New entries are durably moved to
    SUBMIT_STARTED before the single POST. Any ambiguous result is resolved by GET using the
    deterministic orderLinkId; failure to prove broker truth becomes durable UNCERTAIN.
    """

    def __init__(
        self,
        *,
        entry_oms: BybitEntryOmsPort,
        entry_reference_store: BybitEntryReferenceStore,
        **kwargs: Any,
    ) -> None:
        if entry_oms.live_mainnet_order_routing_allowed:
            raise ValueError("Bybit entry client rejected mainnet-capable OMS")
        if entry_oms.automatic_resubmit_after_submit_started_allowed:
            raise ValueError("Bybit entry client forbids automatic resubmit after SUBMIT_STARTED")
        super().__init__(**kwargs)
        self.entry_oms = entry_oms
        self.entry_reference_store = entry_reference_store

    def place_market_order(self, request: BybitDemoOrderRequest) -> BybitDemoOrderAck:
        request.validate()
        if request.reduce_only:
            return super().place_market_order(request)
        if not request.order_link_id.startswith("ASTRA-DEMO-E-"):
            raise ValueError("Bybit OMS-aware entry requires deterministic ENTRY orderLinkId")

        now_ms = self._clock_ms  # noqa: SLF001 - inherited signed clock is the request clock.
        observed_ms = now_ms()
        if isinstance(observed_ms, bool) or not isinstance(observed_ms, int) or observed_ms < 0:
            raise ValueError("Bybit OMS entry clock must return a non-negative integer")
        occurred_at = datetime.fromtimestamp(observed_ms / 1000, tz=UTC)
        reference = self.entry_reference_store.consume(
            symbol=request.symbol,
            side=request.side,
            now_ms=observed_ms,
        )
        intent_id = bybit_entry_intent_id(request.order_link_id)
        intent = OrderIntent(
            intent_id=intent_id,
            symbol=request.symbol,
            side=Side.BUY if request.side == "Buy" else Side.SELL,
            quantity=request.quantity,
            limit_price=reference.price,
            created_at=occurred_at,
            strategy_id="bybit-crypto-perp-v2",
        )
        claim = self.entry_oms.claim_entry_submission(
            intent,
            client_order_id=request.order_link_id,
            occurred_at=occurred_at,
        )
        if not claim.mutation_allowed:
            return self._resume_from_durable_state(
                request,
                claim.record,
                occurred_at=occurred_at,
            )

        try:
            ack = super().place_market_order(request)
        except (BybitRestRequestError, BybitRestTransportError) as exc:
            if exc.ambiguous_mutation:
                return self._recover_after_ambiguous_submit(
                    request,
                    intent_id=intent_id,
                    occurred_at=occurred_at,
                    ambiguity_reason=_error_reason(exc),
                )
            self.entry_oms.mark_rejected(
                intent_id,
                occurred_at=occurred_at,
                reason=_error_reason(exc),
            )
            raise
        except Exception as exc:
            return self._recover_after_ambiguous_submit(
                request,
                intent_id=intent_id,
                occurred_at=occurred_at,
                ambiguity_reason=f"POST_ACK_OR_PROTOCOL_AMBIGUITY:{type(exc).__name__}",
            )

        self._persist_ack(
            intent_id,
            ack=ack,
            occurred_at=occurred_at,
            recovered_by_read=False,
        )
        return ack

    def _resume_from_durable_state(
        self,
        request: BybitDemoOrderRequest,
        record: OrderRecord,
        *,
        occurred_at: datetime,
    ) -> BybitDemoOrderAck:
        if record.state is OrderState.SUBMIT_STARTED:
            return self._recover_after_ambiguous_submit(
                request,
                intent_id=record.intent_id,
                occurred_at=occurred_at,
                ambiguity_reason="RESUME_AFTER_DURABLE_SUBMIT_STARTED",
            )
        if record.state in {
            OrderState.ACKNOWLEDGED,
            OrderState.PARTIALLY_FILLED,
            OrderState.FILLED,
        }:
            if not record.broker_order_id:
                raise BybitEntryOmsPersistenceError(
                    "acknowledged Bybit OMS entry is missing broker order id"
                )
            return BybitDemoOrderAck(
                record.broker_order_id,
                record.client_order_id,
                True,
            )
        if record.state is OrderState.REJECTED:
            raise BybitEntrySubmissionRejectedError("Bybit entry is durably rejected")
        if record.state in {
            OrderState.OUTBOXED,
            OrderState.UNCERTAIN,
            OrderState.RECONCILING,
            OrderState.MANUAL,
        }:
            raise BybitEntrySubmissionUncertainError(
                f"Bybit entry requires reconciliation:{record.state.value}"
            )
        raise BybitEntryOmsPersistenceError(
            f"Bybit entry reached unexpected OMS state:{record.state.value}"
        )

    def _recover_after_ambiguous_submit(
        self,
        request: BybitDemoOrderRequest,
        *,
        intent_id: str,
        occurred_at: datetime,
        ambiguity_reason: str,
    ) -> BybitDemoOrderAck:
        try:
            order = self._lookup_order_by_link_id(request)
        except Exception as exc:
            self._mark_uncertain(
                intent_id,
                occurred_at=occurred_at,
                reason=f"{ambiguity_reason};RECOVERY_READ_FAILED:{type(exc).__name__}",
            )
            raise BybitEntrySubmissionUncertainError(
                "Bybit entry broker truth read failed after ambiguous submit"
            ) from exc
        if order is None:
            self._mark_uncertain(
                intent_id,
                occurred_at=occurred_at,
                reason=f"{ambiguity_reason};ORDER_NOT_FOUND_BY_ORDER_LINK_ID",
            )
            raise BybitEntrySubmissionUncertainError(
                "Bybit entry remains uncertain after GET-only recovery"
            )

        try:
            order_id, status = _validate_recovered_order(order, request=request)
        except Exception as exc:
            self._mark_uncertain(
                intent_id,
                occurred_at=occurred_at,
                reason=f"{ambiguity_reason};RECOVERED_ORDER_INVALID:{type(exc).__name__}",
            )
            raise BybitEntrySubmissionUncertainError(
                "Bybit recovered order does not match durable entry intent"
            ) from exc

        if status == "Rejected":
            self.entry_oms.mark_rejected(
                intent_id,
                occurred_at=occurred_at,
                reason=f"BROKER_TRUTH_REJECTED_AFTER_AMBIGUITY:{ambiguity_reason}",
            )
            raise BybitEntrySubmissionRejectedError(
                "Bybit broker truth reports rejected entry"
            )

        ack = BybitDemoOrderAck(order_id, request.order_link_id, True)
        self._persist_ack(
            intent_id,
            ack=ack,
            occurred_at=occurred_at,
            recovered_by_read=True,
        )
        return ack

    def _lookup_order_by_link_id(
        self,
        request: BybitDemoOrderRequest,
    ) -> Mapping[str, Any] | None:
        params = {
            "category": "linear",
            "symbol": request.symbol,
            "orderLinkId": request.order_link_id,
            "limit": "1",
        }
        for path in ("/v5/order/realtime", "/v5/order/history"):
            response = self._signed_get(path, params)  # noqa: SLF001 - broker recovery read.
            result = response.payload.get("result")
            if not isinstance(result, Mapping):
                raise ValueError("Bybit order recovery response missing result")
            rows = result.get("list")
            if not isinstance(rows, list) or not all(isinstance(row, Mapping) for row in rows):
                raise ValueError("Bybit order recovery response missing list")
            exact = [row for row in rows if row.get("orderLinkId") == request.order_link_id]
            if len(exact) > 1:
                raise ValueError("Bybit order recovery returned duplicate orderLinkId")
            if exact:
                return exact[0]
        return None

    def _persist_ack(
        self,
        intent_id: str,
        *,
        ack: BybitDemoOrderAck,
        occurred_at: datetime,
        recovered_by_read: bool,
    ) -> None:
        try:
            self.entry_oms.mark_acknowledged(
                intent_id,
                broker_order_id=ack.order_id,
                occurred_at=occurred_at,
                recovered_by_read=recovered_by_read,
            )
        except Exception as exc:
            raise BybitEntryOmsPersistenceError(
                "Bybit broker accepted entry but canonical OMS acknowledgement failed"
            ) from exc

    def _mark_uncertain(
        self,
        intent_id: str,
        *,
        occurred_at: datetime,
        reason: str,
    ) -> None:
        try:
            self.entry_oms.mark_uncertain(
                intent_id,
                occurred_at=occurred_at,
                reason=reason,
            )
        except Exception as exc:
            raise BybitEntryOmsPersistenceError(
                "Bybit ambiguous entry could not persist canonical OMS uncertainty"
            ) from exc


def _validate_recovered_order(
    order: Mapping[str, Any],
    *,
    request: BybitDemoOrderRequest,
) -> tuple[str, str]:
    order_id = order.get("orderId")
    order_link_id = order.get("orderLinkId")
    symbol = order.get("symbol")
    side = order.get("side")
    status = order.get("orderStatus")
    if not isinstance(order_id, str) or not order_id:
        raise ValueError("Bybit recovered order missing orderId")
    if order_link_id != request.order_link_id:
        raise ValueError("Bybit recovered orderLinkId mismatch")
    if symbol != request.symbol:
        raise ValueError("Bybit recovered symbol mismatch")
    if side != request.side:
        raise ValueError("Bybit recovered side mismatch")
    if not isinstance(status, str) or not status:
        raise ValueError("Bybit recovered order status missing")
    raw_qty = order.get("qty")
    try:
        quantity = Decimal(str(raw_qty))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Bybit recovered order quantity invalid") from exc
    if quantity != request.quantity:
        raise ValueError("Bybit recovered order quantity mismatch")
    return order_id, status


def _error_reason(exc: BybitRestRequestError | BybitRestTransportError) -> str:
    details = [type(exc).__name__]
    ret_code = getattr(exc, "ret_code", None)
    http_status = getattr(exc, "http_status", None)
    if ret_code is not None:
        details.append(f"retCode={ret_code}")
    if http_status is not None:
        details.append(f"httpStatus={http_status}")
    return ":".join(details)
