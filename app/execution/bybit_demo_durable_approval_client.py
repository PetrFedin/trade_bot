from __future__ import annotations

from collections.abc import Callable
from typing import Any

from app.execution.bybit_demo import BybitDemoOrderRequest
from app.execution.bybit_demo_approval_lineage import BybitDemoApprovedEntryAuthorization
from app.execution.bybit_demo_approval_lineage_store import (
    BybitDemoApprovedEntryAuthorizationReceipt,
)
from app.execution.bybit_demo_operator_approval import BybitDemoOperatorApproval


class DurableApprovalLineageBybitDemoClient:
    """Persist durable approval lineage immediately before the underlying Demo entry mutation.

    This wrapper sits *under* ``OperatorApprovedBybitDemoClient``. All account, fee, quote and
    strategy checks therefore complete before this layer is reached. For the single non-reduce-only
    entry, the immutable authorization is written and burned before the real Demo client's network
    mutation. If persistence fails or an existing authorization is found, the network call is not
    made. Reduce-only recovery/protection operations do not create a second entry authorization.
    """

    environment = "BYBIT_DEMO"
    live_mainnet_order_routing_allowed = False

    def __init__(
        self,
        client: Any,
        approval: BybitDemoOperatorApproval,
        authorization: BybitDemoApprovedEntryAuthorization,
        *,
        store: Any,
        on_persisted: Callable[[BybitDemoApprovedEntryAuthorizationReceipt], None] | None = None,
    ) -> None:
        if getattr(client, "environment", None) != "BYBIT_DEMO":
            raise ValueError("durable approval lineage wrapper requires BYBIT_DEMO client")
        if getattr(client, "live_mainnet_order_routing_allowed", True) is not False:
            raise ValueError("durable approval lineage wrapper rejected mainnet-capable client")
        _validate_store(store)
        if authorization.approval_id != approval.approval_id:
            raise ValueError("durable approval lineage authorization approval id mismatch")
        if authorization.expected_entry_order_link_id != approval.expected_entry_order_link_id:
            raise ValueError("durable approval lineage entry orderLinkId mismatch")
        self._client = client
        self._approval = approval
        self._authorization = authorization
        self._store = store
        self._on_persisted = on_persisted
        self._entry_authorization_recorded = False

    @property
    def entry_authorization_recorded(self) -> bool:
        return self._entry_authorization_recorded

    @property
    def protection_state_read_supported(self) -> bool:
        return getattr(self._client, "protection_state_read_supported", False) is True

    def get_fee_rate(self, *, symbol: str):
        self._require_symbol(symbol)
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
        self._require_symbol(symbol)
        if order_link_id is not None:
            self._require_order_identity(order_link_id)
        return self._client.get_executions(
            symbol=symbol,
            order_link_id=order_link_id,
            limit=limit,
        )

    def place_market_order(self, request: BybitDemoOrderRequest):
        request.validate()
        self._require_symbol(request.symbol)
        if request.reduce_only:
            self._validate_reduce_only_close(request)
            return self._client.place_market_order(request)
        if self._entry_authorization_recorded:
            raise ValueError("durable approval lineage entry authorization already recorded")
        self._validate_entry(request)
        receipt = self._store.persist(self._authorization)
        _validate_receipt(receipt, approval=self._approval)
        if receipt.idempotent_existing_record:
            raise ValueError(
                "durable approval lineage already exists; reconcile before any resubmit"
            )
        self._entry_authorization_recorded = True
        if self._on_persisted is not None:
            self._on_persisted(receipt)
        return self._client.place_market_order(request)

    def set_full_position_protection(self, request):
        request.validate()
        self._require_symbol(request.symbol)
        return self._client.set_full_position_protection(request)

    def set_open_ended_position_protection(self, request):
        request.validate()
        self._require_symbol(request.symbol)
        return self._client.set_open_ended_position_protection(request)

    def cancel_order(self, *, symbol: str, order_link_id: str):
        self._require_symbol(symbol)
        self._require_order_identity(order_link_id)
        return self._client.cancel_order(symbol=symbol, order_link_id=order_link_id)

    def _validate_entry(self, request: BybitDemoOrderRequest) -> None:
        expected_side = "Buy" if self._approval.side == "LONG" else "Sell"
        if request.side != expected_side:
            raise ValueError("durable approval lineage rejected entry side mismatch")
        if request.order_link_id != self._approval.expected_entry_order_link_id:
            raise ValueError("durable approval lineage rejected entry decision identity mismatch")
        if request.quantity > self._approval.maximum_entry_quantity:
            raise ValueError("durable approval lineage rejected quantity above approved cap")

    def _validate_reduce_only_close(self, request: BybitDemoOrderRequest) -> None:
        expected_side = "Sell" if self._approval.side == "LONG" else "Buy"
        if request.side != expected_side:
            raise ValueError("durable approval lineage rejected close side mismatch")
        if request.order_link_id != self._approval.expected_close_order_link_id:
            raise ValueError("durable approval lineage rejected close order identity mismatch")
        if request.quantity > self._approval.maximum_entry_quantity:
            raise ValueError("durable approval lineage rejected close quantity above approved cap")

    def _require_symbol(self, symbol: str) -> None:
        if symbol != self._approval.symbol:
            raise ValueError("durable approval lineage rejected another symbol")

    def _require_order_identity(self, order_link_id: str) -> None:
        if order_link_id not in {
            self._approval.expected_entry_order_link_id,
            self._approval.expected_close_order_link_id,
        }:
            raise ValueError("durable approval lineage rejected another order identity")


def _validate_store(store: Any) -> None:
    if getattr(store, "live_mainnet_order_routing_allowed", True) is not False:
        raise ValueError("durable approval lineage rejected mainnet-capable store")
    if getattr(store, "order_writes_supported", True) is not False:
        raise ValueError("durable approval lineage store cannot write orders")
    if getattr(store, "order_submission_supported", True) is not False:
        raise ValueError("durable approval lineage store cannot submit orders")
    if getattr(store, "immutable_records", False) is not True:
        raise ValueError("durable approval lineage store must be immutable")
    if getattr(store, "outcome_storage_allowed", True) is not False:
        raise ValueError("durable approval lineage store must be outcome-free")
    if getattr(store, "realized_pnl_storage_allowed", True) is not False:
        raise ValueError("durable approval lineage store cannot store realized PnL")


def _validate_receipt(
    receipt: BybitDemoApprovedEntryAuthorizationReceipt,
    *,
    approval: BybitDemoOperatorApproval,
) -> None:
    if receipt.live_mainnet_order_routing_allowed:
        raise ValueError("durable approval lineage receipt enabled mainnet routing")
    if receipt.entry_order_link_id != approval.expected_entry_order_link_id:
        raise ValueError("durable approval lineage receipt orderLinkId mismatch")
    if receipt.approval_id != approval.approval_id:
        raise ValueError("durable approval lineage receipt approval id mismatch")
    value = receipt.record_sha256
    if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
        raise ValueError("durable approval lineage receipt checksum is invalid")


__all__ = ["DurableApprovalLineageBybitDemoClient"]
