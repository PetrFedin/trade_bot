from __future__ import annotations

from app.application.bybit_product_composition import BybitProductStartupReconciler
from app.execution.bybit_startup_reconciliation import BybitStartupReconciliationStatus


class _EntryOms:
    live_mainnet_order_routing_allowed = False

    def __init__(self, unresolved: int) -> None:
        self.unresolved = unresolved

    def count_unresolved_entry_submissions(self) -> int:
        return self.unresolved


class _MustNotReadBroker:
    live_mainnet_order_routing_allowed = False

    def get_positions(self, *, settle_coin="USDT"):
        raise AssertionError("broker truth must not be read before unresolved OMS is cleared")

    def get_open_orders(self, *, settle_coin="USDT", limit=50):
        raise AssertionError("broker truth must not be read before unresolved OMS is cleared")


class _CheckpointStore:
    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def load(self):
        raise FileNotFoundError


def test_unresolved_canonical_entry_oms_blocks_startup_before_new_entry() -> None:
    reconciler = BybitProductStartupReconciler(
        broker=_MustNotReadBroker(),
        checkpoint_store=_CheckpointStore(),
        entry_oms=_EntryOms(1),
    )

    result = reconciler.run()

    assert result.status is BybitStartupReconciliationStatus.BLOCKED
    assert result.next_entry_allowed is False
    assert result.broker_truth_complete is False
    assert result.reasons == ("UNRESOLVED_BYBIT_OMS_ENTRY_SUBMISSIONS:1",)
    assert result.live_mainnet_order_routing_allowed is False
