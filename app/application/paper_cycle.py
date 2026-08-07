from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from app.application.composition import ProductRuntime
from app.application.order_lifecycle import PreparedPaperOrder
from app.application.trade_updates import PaperTradeUpdateProcessor, TradeUpdateProcessingResult
from app.domain.trading import Bar, OrderIntent, TargetPosition
from app.execution.paper_executor import ExecutionResult, PaperSubmitExecutor
from app.oms.reconciliation import (
    BrokerOrderTruth,
    BrokerPortfolioTruth,
    PortfolioReconciliationResult,
    reconcile_portfolio,
)
from app.oms.store import OrderRecord
from app.risk.pretrade import RiskContext, RiskDecision
from app.runtime.alpaca_paper_adapter_v100 import AlpacaTradeUpdateStreamV100
from app.runtime.paper_broker_contract_v99 import PaperBrokerV99


@dataclass(frozen=True)
class PaperPlanningResult:
    target: TargetPosition
    intent: OrderIntent | None
    risk: RiskDecision | None
    prepared: PreparedPaperOrder | None

    @property
    def order_ready(self) -> bool:
        return self.prepared is not None


class PaperCycleService:
    """Bounded application service for the stable paper-trading product graph.

    It intentionally exposes one durable external mutation per ``execute_next_submit``
    call. There is no hidden retry loop or daemon. Planning persists immutable risk and
    outbox state, submit execution uses the at-most-one executor, trade updates route by
    durable client-order identity, and reconciliation remains read-only.
    """

    def __init__(
        self,
        *,
        runtime: ProductRuntime,
        broker: PaperBrokerV99,
        trade_stream: AlpacaTradeUpdateStreamV100,
        stream_generation: int,
    ) -> None:
        if stream_generation < 1:
            raise ValueError("stream_generation must be positive")
        self.runtime = runtime
        self.broker = broker
        self.trade_stream = trade_stream
        self.stream_generation = stream_generation
        self.executor = PaperSubmitExecutor(store=runtime.oms_store, broker=broker)
        self.trade_updates = PaperTradeUpdateProcessor(
            stream=trade_stream,
            oms=runtime.oms_store,
            fill_accounting=runtime.require_fill_accounting(),
        )

    def plan_and_prepare(
        self,
        bars: Sequence[Bar],
        *,
        kill_switch_engaged: bool = False,
        risk_context: RiskContext | None = None,
    ) -> PaperPlanningResult:
        target, intent, decision = self.runtime.paper_pipeline.plan(
            bars,
            kill_switch_engaged=kill_switch_engaged,
            risk_context=risk_context,
        )
        if intent is None or decision is None or not decision.approved:
            return PaperPlanningResult(target, intent, decision, None)
        prepared = self.runtime.order_lifecycle.prepare(
            intent,
            decision,
            occurred_at=target.generated_at,
        )
        return PaperPlanningResult(target, intent, decision, prepared)

    def execute_next_submit(self, *, occurred_at: datetime) -> ExecutionResult | None:
        """Execute at most one durable submit outbox message."""

        pending = self.runtime.oms_store.pending_outbox(limit=1)
        if not pending:
            return None
        message = pending[0]
        if message.topic != "paper_order_submit":
            raise ValueError(f"UNSUPPORTED_OUTBOX_TOPIC:{message.topic}")
        return self.executor.execute(message, occurred_at=occurred_at)

    def process_trade_update(
        self,
        raw_frame: bytes | str,
        *,
        received_at: datetime,
    ) -> TradeUpdateProcessingResult:
        return self.trade_updates.process(
            raw_frame,
            received_at=received_at,
            expected_generation=self.stream_generation,
        )

    def reconcile_order(
        self,
        intent_id: str,
        broker_truth: BrokerOrderTruth | None,
        *,
        occurred_at: datetime,
        event_prefix: str,
    ) -> OrderRecord:
        return self.runtime.reconciler.reconcile_order(
            intent_id,
            broker_truth,
            occurred_at=occurred_at,
            event_prefix=event_prefix,
        )

    def reconcile_portfolio(
        self,
        broker_truth: BrokerPortfolioTruth,
    ) -> PortfolioReconciliationResult:
        return reconcile_portfolio(self.runtime.portfolio, broker_truth)
