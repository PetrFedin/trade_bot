from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from time import monotonic, time
from typing import Any, Protocol

from app.execution.bybit_entry_recovery_convergence import (
    BybitEntryRecoveryConvergenceStatus,
    converge_bybit_executed_entry_recovery,
)
from app.execution.bybit_startup_reconciliation import (
    BybitStartupReconciliationResult,
    BybitStartupReconciliationStatus,
)
from app.oms.store import OrderRecord, OrderState

ClockMs = Callable[[], int]
MonotonicFn = Callable[[], float]
_PRE_ACK_RECOVERY_STATES = frozenset(
    {OrderState.SUBMIT_STARTED, OrderState.UNCERTAIN, OrderState.RECONCILING}
)


class _BaseStartupReconciler(Protocol):
    live_mainnet_order_routing_allowed: bool

    def run(self) -> BybitStartupReconciliationResult: ...


class _CandidateReader(Protocol):
    live_mainnet_order_routing_allowed: bool
    order_writes_supported: bool

    def load_candidates(self, *, limit: int = 8) -> tuple[OrderRecord, ...]: ...


class _Broker(Protocol):
    live_mainnet_order_routing_allowed: bool

    def get_order_by_link_id(self, **kwargs): ...

    def get_positions(self, *, settle_coin: str = "USDT"): ...


class _CheckpointStore(Protocol):
    live_mainnet_order_routing_allowed: bool
    order_writes_supported: bool

    def load(self): ...


class _HealthRecorder(Protocol):
    def record(
        self,
        result: BybitStartupReconciliationResult,
        *,
        observed_monotonic: Decimal,
    ) -> None: ...


@dataclass(frozen=True)
class RecoveryAwareBybitProductStartupReconciler:
    """Recover one provable crash-after-ENTRY before delegating to normal startup truth checks."""

    base_reconciler: _BaseStartupReconciler
    broker: _Broker
    checkpoint_store: _CheckpointStore
    candidate_reader: _CandidateReader
    recovery_store: Any
    runtime_lease: Any
    excursion_store: Any
    entry_oms: Any
    recovery_client: Any
    reconciliation_health: _HealthRecorder | None = None
    clock_ms: ClockMs = lambda: int(time() * 1000)
    monotonic_fn: MonotonicFn = monotonic
    live_mainnet_order_routing_allowed: bool = False

    def run(self) -> BybitStartupReconciliationResult:
        try:
            self._validate_capabilities()
            candidates = self.candidate_reader.load_candidates(limit=8)
        except Exception as exc:  # noqa: BLE001 - discovery/capability failure is fail-closed.
            return self._record(_blocked(f"ENTRY_RECOVERY_DISCOVERY_FAILED:{type(exc).__name__}"))

        try:
            checkpoint = self.checkpoint_store.load()
        except FileNotFoundError:
            checkpoint = None
        except Exception as exc:  # noqa: BLE001 - unreadable checkpoint must not be repaired blindly.
            return self._record(
                _blocked(f"ENTRY_RECOVERY_CHECKPOINT_READ_FAILED:{type(exc).__name__}")
            )

        actionable = tuple(
            record
            for record in candidates
            if not (
                checkpoint is not None
                and record.client_order_id == checkpoint.entry_order_link_id
                and record.state is OrderState.FILLED
            )
        )
        if not actionable:
            return self._record(self.base_reconciler.run())
        if len(actionable) != 1:
            return self._record(
                _blocked(
                    "ENTRY_RECOVERY_MULTIPLE_UNHANDED_CANDIDATES:"
                    + ",".join(record.intent_id for record in actionable)
                )
            )

        record = actionable[0]
        if checkpoint is not None and checkpoint.entry_order_link_id != record.client_order_id:
            return self._record(
                _blocked(
                    f"ENTRY_RECOVERY_FOREIGN_CHECKPOINT:{record.intent_id}:"
                    f"{checkpoint.entry_order_link_id}"
                )
            )

        try:
            truth = self.broker.get_order_by_link_id(
                symbol=record.symbol,
                order_link_id=record.client_order_id,
                expected_side="Buy" if record.side.value == "BUY" else "Sell",
                expected_quantity=record.quantity,
            )
        except Exception as exc:  # noqa: BLE001 - incomplete broker truth cannot authorize recovery.
            return self._record(
                _blocked(f"ENTRY_RECOVERY_ORDER_READ_FAILED:{record.intent_id}:{type(exc).__name__}")
            )
        if truth is None:
            return self._record(
                _blocked(f"ENTRY_RECOVERY_ORDER_NOT_FOUND:{record.intent_id}")
            )

        if truth.safely_rejected_without_execution:
            if record.state in _PRE_ACK_RECOVERY_STATES:
                return self._record(self.base_reconciler.run())
            return self._record(
                _blocked(f"ENTRY_RECOVERY_BROKER_REJECTED_AFTER_ACK:{record.intent_id}")
            )
        if truth.cumulative_executed_quantity <= 0:
            return self._record(
                _blocked(
                    f"ENTRY_RECOVERY_EXECUTION_NOT_PROVEN:{record.intent_id}:{truth.status}"
                )
            )

        try:
            positions = tuple(self.broker.get_positions(settle_coin="USDT"))
            convergence = converge_bybit_executed_entry_recovery(
                record,
                order_truth=truth,
                positions=positions,
                recovery_store=self.recovery_store,
                runtime_lease=self.runtime_lease,
                excursion_store=self.excursion_store,
                entry_oms=self.entry_oms,
                client=self.recovery_client,
                occurred_at=_utc_from_ms(self.clock_ms()),
            )
        except Exception as exc:  # noqa: BLE001 - never fall through to normal startup on mutation error.
            return self._record(
                _blocked(f"ENTRY_RECOVERY_EXECUTION_FAILED:{record.intent_id}:{type(exc).__name__}")
            )
        if convergence.status is BybitEntryRecoveryConvergenceStatus.BLOCKED:
            reasons = tuple(
                f"ENTRY_RECOVERY_BLOCKED:{record.intent_id}:{reason}"
                for reason in convergence.reasons
            )
            return self._record(_blocked_many(reasons))

        final = self.base_reconciler.run()
        if convergence.status is BybitEntryRecoveryConvergenceStatus.ACTIVE_MANAGEMENT_READY:
            if final.status is not BybitStartupReconciliationStatus.RESUME_MANAGEMENT:
                return self._record(
                    replace(
                        final,
                        status=BybitStartupReconciliationStatus.BLOCKED,
                        reasons=_unique(
                            final.reasons
                            + ("ENTRY_RECOVERY_POST_CONVERGENCE_ACTIVE_STATE_MISMATCH",)
                        ),
                        next_entry_allowed=False,
                        management_allowed=False,
                        terminal_recovery_required=False,
                    )
                )
        elif convergence.status is BybitEntryRecoveryConvergenceStatus.TERMINAL_HANDOFF_REQUIRED:
            if final.status is not BybitStartupReconciliationStatus.TERMINAL_RECOVERY_REQUIRED:
                return self._record(
                    replace(
                        final,
                        status=BybitStartupReconciliationStatus.BLOCKED,
                        reasons=_unique(
                            final.reasons
                            + ("ENTRY_RECOVERY_POST_CONVERGENCE_TERMINAL_STATE_MISMATCH",)
                        ),
                        next_entry_allowed=False,
                        management_allowed=False,
                        terminal_recovery_required=False,
                    )
                )
        return self._record(final)

    def _record(
        self,
        result: BybitStartupReconciliationResult,
    ) -> BybitStartupReconciliationResult:
        if self.reconciliation_health is not None:
            self.reconciliation_health.record(
                result,
                observed_monotonic=Decimal(str(self.monotonic_fn())),
            )
        return result

    def _validate_capabilities(self) -> None:
        values = (
            ("base startup", self.base_reconciler),
            ("broker", self.broker),
            ("checkpoint store", self.checkpoint_store),
            ("candidate reader", self.candidate_reader),
            ("recovery store", self.recovery_store),
            ("runtime lease", self.runtime_lease),
            ("excursion store", self.excursion_store),
            ("entry OMS", self.entry_oms),
            ("recovery client", self.recovery_client),
        )
        for name, value in values:
            if getattr(value, "live_mainnet_order_routing_allowed", True) is not False:
                raise ValueError(f"recovery-aware startup rejected mainnet-capable {name}")
        if self.candidate_reader.order_writes_supported:
            raise ValueError("recovery candidate reader must be read-only")


def _blocked(reason: str) -> BybitStartupReconciliationResult:
    return _blocked_many((reason,))


def _blocked_many(reasons: tuple[str, ...]) -> BybitStartupReconciliationResult:
    return BybitStartupReconciliationResult(
        status=BybitStartupReconciliationStatus.BLOCKED,
        reasons=_unique(reasons),
        checkpoint=None,
        active_positions=(),
        open_orders=(),
        next_entry_allowed=False,
        management_allowed=False,
        terminal_recovery_required=False,
        broker_truth_complete=False,
    )


def _unique(reasons: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(reasons))


def _utc_from_ms(value: int) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("recovery-aware startup clock must return non-negative integer ms")
    return datetime.fromtimestamp(value / 1000, tz=UTC)
