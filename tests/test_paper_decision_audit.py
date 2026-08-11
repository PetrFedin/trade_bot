from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.application.paper_decision_audit import (
    SQLitePaperDecisionAuditStore,
    audit_cross_sectional_paper_result,
)
from app.domain.trading import OrderIntent, Side, TargetPosition
from app.strategy.cross_sectional_portfolio import (
    PortfolioEntryBlockReason,
    PortfolioExitReason,
)

NOW = datetime(2026, 8, 12, 7, 0, tzinfo=UTC)
STRATEGY = "cross-sectional-quality-v2-paper-shadow"


def result_fixture():
    exit_target = TargetPosition(
        symbol="NVDA",
        quantity=Decimal("0"),
        reference_price=Decimal("105"),
        generated_at=NOW,
        strategy_id=STRATEGY,
    )
    entry_target = TargetPosition(
        symbol="AAPL",
        quantity=Decimal("29"),
        reference_price=Decimal("100"),
        generated_at=NOW,
        strategy_id=STRATEGY,
    )
    exit_intent = OrderIntent(
        intent_id="exit-nvda",
        symbol="NVDA",
        side=Side.SELL,
        quantity=Decimal("10"),
        limit_price=Decimal("105"),
        created_at=NOW,
        strategy_id=STRATEGY,
    )
    entry_intent = OrderIntent(
        intent_id="entry-aapl",
        symbol="AAPL",
        side=Side.BUY,
        quantity=Decimal("29"),
        limit_price=Decimal("100"),
        created_at=NOW,
        strategy_id=STRATEGY,
    )
    target_plan = SimpleNamespace(
        selected_symbols=("AAPL", "MSFT"),
        targets=(exit_target, entry_target),
        entry_blocks=(("MSFT", PortfolioEntryBlockReason.GROSS_EXPOSURE_CAP),),
        exit_reasons=(("NVDA", PortfolioExitReason.SELECTION_EXIT),),
    )
    order_plan = SimpleNamespace(
        items=(
            SimpleNamespace(
                target=exit_target,
                approved=True,
                reasons=(),
                intent=exit_intent,
            ),
            SimpleNamespace(
                target=entry_target,
                approved=False,
                reasons=("QUALITY_GATE_PAUSE_ENTRIES",),
                intent=entry_intent,
            ),
        )
    )
    prepared_orders=(
        SimpleNamespace(record=SimpleNamespace(intent_id="exit-nvda")),
    )
    return SimpleNamespace(
        target_plan=target_plan,
        order_plan=order_plan,
        prepared_orders=prepared_orders,
    )


def test_decision_audit_persists_selection_blocks_exits_and_order_reasons(
    tmp_path: Path,
) -> None:
    store = SQLitePaperDecisionAuditStore(tmp_path / "decision-audit.sqlite")
    result = result_fixture()

    first = audit_cross_sectional_paper_result(
        store=store,
        strategy_id=STRATEGY,
        generated_at=NOW,
        result=result,
    )
    replay = audit_cross_sectional_paper_result(
        store=store,
        strategy_id=STRATEGY,
        generated_at=NOW,
        result=result,
    )

    assert replay == first
    records = store.records(strategy_id=STRATEGY)
    assert len(records) == 1
    record = records[0]
    assert record.selected_symbols == ("AAPL", "MSFT")
    assert record.entry_blocks == (("MSFT", "GROSS_EXPOSURE_CAP"),)
    assert record.exit_reasons == (("NVDA", "SELECTION_EXIT"),)
    assert record.prepared_intent_ids == ("exit-nvda",)
    assert record.order_decisions[0]["approved"] is True
    assert record.order_decisions[0]["side"] == "SELL"
    assert record.order_decisions[1]["approved"] is False
    assert record.order_decisions[1]["reasons"] == [
        "QUALITY_GATE_PAUSE_ENTRIES"
    ]


def test_same_decision_identity_with_changed_payload_fails_closed(tmp_path: Path) -> None:
    store = SQLitePaperDecisionAuditStore(tmp_path / "decision-audit.sqlite")
    result = result_fixture()
    record = audit_cross_sectional_paper_result(
        store=store,
        strategy_id=STRATEGY,
        generated_at=NOW,
        result=result,
    )
    conflicting = record.__class__(
        decision_id=record.decision_id,
        strategy_id=record.strategy_id,
        generated_at=record.generated_at,
        selected_symbols=record.selected_symbols,
        targets=record.targets,
        entry_blocks=record.entry_blocks,
        exit_reasons=record.exit_reasons,
        order_decisions=record.order_decisions,
        prepared_intent_ids=("different-intent",),
    )

    with pytest.raises(ValueError, match="PAPER_DECISION_AUDIT_CONFLICT"):
        store.append(conflicting)
