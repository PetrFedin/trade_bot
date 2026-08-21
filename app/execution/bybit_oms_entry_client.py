from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

from app.domain.trading import OrderIntent, Side
from app.execution.bybit_demo import BybitDemoOrderAck, BybitDemoOrderRequest
from app.execution.bybit_observed_rest import ObservedBybitDemoStopRatchetClient
from app.execution.bybit_order_lookup import lookup_bybit_order_by_link_id
from app.execution.bybit_postgres_entry_recovery import PostgresBybitEntryRecoveryStore
from app.execution.bybit_rest_policy import (
    BybitRestRequestError,
    BybitRestTransportError,
)
from app.marketdata.bybit_entry_reference import BybitEntryReferenceStore
from app.oms.bybit_entry import (
    BybitEntrySubmissionClaim,
    bybit_entry_intent_id,
    bybit_reduce_only_intent_id,
)
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

    def claim_reduce_only_submission(
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
        broker_order_id: str | None = None,
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


class BybitEntryRecoveryEnvelopeError(RuntimeError):
    pass


class OmsAwareBybitDemoStopRatchetClient(ObservedBybitDemoStopRatchetClient):
    """Bybit demo client whose market-order mutations are owned by the canonical OMS.

    New entries and deterministic reduce-only risk reductions are durably moved to SUBMIT_STARTED
    before the single POST. Any ambiguous result is resolved by GET using orderLinkId; failure to
    prove broker truth becomes durable UNCERTAIN. Risk-reducing CLOSE claims deliberately bypass
    the operator new-entry gate so PAUSED/READ_ONLY/KILLED never prevents protection.

    The canonical client also owns the immutable pre-submit recovery store. A product cycle using
    this client must persist the exact fee-adjusted risk/protection envelope before the ENTRY POST.
    The client independently reloads that envelope immediately before the OMS claim, so a future
    direct canonical call cannot bypass the durable pre-submit recovery boundary.
    """

    entry_recovery_required = True

    def __init__(
        self,
        *,
        entry_oms: BybitEntryOmsPort,
        entry_reference_store: BybitEntryReferenceStore,
        entry_recovery_store: Any | None = None,
        **kwargs: Any,
    ) -> None:
        if entry_oms.live_mainnet_order_routing_allowed:
            raise ValueError("Bybit entry client rejected mainnet-capable OMS")
        if entry_oms.automatic_resubmit_after_submit_started_allowed:
            raise ValueError("Bybit entry client forbids automatic resubmit after SUBMIT_STARTED")
        oms_dsn = getattr(entry_oms, "dsn", None)
        self._postgres_backed_entry_oms = isinstance(oms_dsn, str) and bool(oms_dsn.strip())
        if entry_recovery_store is None and self._postgres_backed_entry_oms:
            entry_recovery_store = PostgresBybitEntryRecoveryStore(oms_dsn)
        if entry_recovery_store is not None:
            if (
                getattr(entry_recovery_store, "live_mainnet_order_routing_allowed", True)
                is not False
            ):
                raise ValueError("Bybit entry client rejected mainnet-capable recovery store")
            if getattr(entry_recovery_store, "order_writes_supported", True) is not False:
                raise ValueError("Bybit recovery store must not expose broker order writes")
            if getattr(entry_recovery_store, "immutable_records", False) is not True:
                raise ValueError("Bybit recovery store must preserve immutable records")
        super().__init__(**kwargs)
        self.entry_oms = entry_oms
        self.entry_reference_store = entry_reference_store
        self.entry_recovery_store = entry_recovery_store

    def place_market_order(self, request: BybitDemoOrderRequest) -> BybitDemoOrderAck:
        request.validate()
        if request.reduce_only:
            return self._place_reduce_only_market_order(request)
        if not request.order_link_id.startswith("ASTRA-DEMO-E-"):
            raise ValueError("Bybit OMS-aware entry requires deterministic ENTRY orderLinkId")

        observed_ms, occurred_at = self._observed_time()
        reference = self.entry_reference_store.consume(
            symbol=request.symbol,
            side=request.side,
            now_ms=observed_ms,
        )
        self._verify_entry_recovery_envelope(request)
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
        return self._submit_claimed_order(
            request,
            intent_id=intent_id,
            claim=claim,
            occurred_at=occurred_at,
        )

    def _verify_entry_recovery_envelope(self, request: BybitDemoOrderRequest) -> None:
        store = self.entry_recovery_store
        if store is None:
            if self._postgres_backed_entry_oms:
                raise BybitEntryRecoveryEnvelopeError(
                    "ENTRY_RECOVERY_ENVELOPE_STORE_REQUIRED_BEFORE_OMS_CLAIM"
                )
            return
        try:
            record = store.load(entry_order_link_id=request.order_link_id)
        except Exception as exc:
            raise BybitEntryRecoveryEnvelopeError(
                f"ENTRY_RECOVERY_ENVELOPE_LOAD_FAILED:{type(exc).__name__}"
            ) from exc
        if getattr(record, "live_mainnet_order_routing_allowed", True) is not False:
            raise BybitEntryRecoveryEnvelopeError(
                "ENTRY_RECOVERY_ENVELOPE_RECORD_REJECTED_MAINNET_CAPABILITY"
            )
        envelope = getattr(record, "envelope", None)
        if envelope is None:
            raise BybitEntryRecoveryEnvelopeError("ENTRY_RECOVERY_ENVELOPE_RECORD_MISSING")
        try:
            envelope.validate()
        except Exception as exc:
            raise BybitEntryRecoveryEnvelopeError(
                f"ENTRY_RECOVERY_ENVELOPE_VALIDATION_FAILED:{type(exc).__name__}"
            ) from exc
        mismatches: list[str] = []
        if envelope.entry_order_link_id != request.order_link_id:
            mismatches.append("ORDER_LINK_ID")
        if envelope.trade_plan.symbol != request.symbol:
            mismatches.append("SYMBOL")
        if envelope.order_side != request.side:
            mismatches.append("SIDE")
        if envelope.approved_order_quantity != request.quantity:
            mismatches.append("QUANTITY")
        if mismatches:
            raise BybitEntryRecoveryEnvelopeError(
                "ENTRY_RECOVERY_ENVELOPE_REQUEST_MISMATCH:" + ",".join(mismatches)
            )

    def _place_reduce_only_market_order(
        self,
        request: BybitDemoOrderRequest,
    ) -> BybitDemoOrderAck:
        if request.reference_price is None:
            raise ValueError("Bybit OMS-aware reduce-only close requires reference_price evidence")
        _observed_ms, occurred_at = self._observed_time()
        intent_id = bybit_reduce_only_intent_id(request.order_link_id)
        intent = OrderIntent(
            intent_id=intent_id,
            symbol=request.symbol,
            side=Side.BUY if request.side == "Buy" else Side.SELL,
            quantity=request.quantity,
            limit_price=request.reference_price,
            created_at=occurred_at,
            strategy_id="bybit-risk-reduction",
        )
        claim = self.entry_oms.claim_reduce_only_submission(
            intent,
            client_order_id=request.order_link_id,
            occurred_at=occurred_at,
        )
        return self._submit_claimed_order(
            request,
            intent_id=intent_id,
            claim=claim,
            occurred_at=occurred_at,
        )

    def _observed_time(self) -> tuple[int, datetime]:
        observed_ms = self._clock_ms()  # noqa: SLF001 - inherited signed request clock.
        if isinstance(observed_ms, bool) or not isinstance(observed_ms, int) or observed_ms < 0:
            raise ValueError("Bybit OMS clock must return a non-negative integer")
        return observed_ms, datetime.fromtimestamp(observed_ms / 1000, tz=UTC)

    def _submit_claimed_order(
        self,
        request: BybitDemoOrderRequest,
        *,
        intent_id: str,
        claim: BybitEntrySubmissionClaim,
        occurred_at: datetime,
    ) -> BybitDemoOrderAck:
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
                    "acknowledged Bybit OMS order is missing broker order id"
                )
            return BybitDemoOrderAck(
                record.broker_order_id,
                record.client_order_id,
                True,
            )
        if record.state is OrderState.REJECTED:
            raise BybitEntrySubmissionRejectedError("Bybit order is durably rejected")
        if record.state in {
            OrderState.OUTBOXED,
            OrderState.UNCERTAIN,
            OrderState.RECONCILING,
            OrderState.MANUAL,
        }:
            raise BybitEntrySubmissionUncertainError(
                f"Bybit order requires reconciliation:{record.state.value}"
            )
        raise BybitEntryOmsPersistenceError(
            f"Bybit order reached unexpected OMS state:{record.state.value}"
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
            truth = lookup_bybit_order_by_link_id(
                self._signed_get,  # noqa: SLF001 - authenticated GET-only broker recovery.
                symbol=request.symbol,
                order_link_id=request.order_link_id,
                expected_side=request.side,
                expected_quantity=request.quantity,
            )
        except Exception as exc:
            self._mark_uncertain(
                intent_id,
                occurred_at=occurred_at,
                reason=f"{ambiguity_reason};RECOVERY_READ_FAILED:{type(exc).__name__}",
            )
            raise BybitEntrySubmissionUncertainError(
                "Bybit order broker truth read failed after ambiguous submit"
            ) from exc
        if truth is None:
            self._mark_uncertain(
                intent_id,
                occurred_at=occurred_at,
                reason=f"{ambiguity_reason};ORDER_NOT_FOUND_BY_ORDER_LINK_ID",
            )
            raise BybitEntrySubmissionUncertainError(
                "Bybit order remains uncertain after GET-only recovery"
            )

        if truth.safely_rejected_without_execution:
            self.entry_oms.mark_rejected(
                intent_id,
                occurred_at=occurred_at,
                reason=f"BROKER_TRUTH_REJECTED_AFTER_AMBIGUITY:{ambiguity_reason}",
                broker_order_id=truth.order_id,
            )
            raise BybitEntrySubmissionRejectedError(
                "Bybit broker truth reports rejected order without execution"
            )
        if truth.status in {"Rejected", "Cancelled"}:
            self._mark_uncertain(
                intent_id,
                occurred_at=occurred_at,
                reason=(
                    f"{ambiguity_reason};BROKER_STATUS_REQUIRES_LIFECYCLE_RECONCILIATION:"
                    f"{truth.status}:cumExecQty={truth.cumulative_executed_quantity}"
                ),
            )
            raise BybitEntrySubmissionUncertainError(
                "Bybit terminal broker status requires lifecycle reconciliation"
            )

        ack = BybitDemoOrderAck(truth.order_id, request.order_link_id, True)
        self._persist_ack(
            intent_id,
            ack=ack,
            occurred_at=occurred_at,
            recovered_by_read=True,
        )
        return ack

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
                "Bybit broker accepted order but canonical OMS acknowledgement failed"
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
                "Bybit ambiguous order could not persist canonical OMS uncertainty"
            ) from exc


def _error_reason(exc: BybitRestRequestError | BybitRestTransportError) -> str:
    details = [type(exc).__name__]
    ret_code = getattr(exc, "ret_code", None)
    http_status = getattr(exc, "http_status", None)
    if ret_code is not None:
        details.append(f"retCode={ret_code}")
    if http_status is not None:
        details.append(f"httpStatus={http_status}")
    return ":".join(details)
