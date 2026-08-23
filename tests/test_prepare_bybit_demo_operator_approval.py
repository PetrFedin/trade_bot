from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.marketdata.bybit_v5 import BybitKlineAcquisition, BybitKlineBar, BybitKlineRequest
from tools import prepare_bybit_demo_operator_approval as prepare_tool

_DECISION = datetime(2026, 8, 24, 12, tzinfo=UTC)
_APPROVED = datetime(2026, 8, 24, 12, 6, tzinfo=UTC)


def _row(rank: int, symbol: str) -> dict[str, Any]:
    return {
        "snapshot_id": "a" * 64,
        "evidence_rank": rank,
        "market_rank": rank,
        "symbol": symbol,
        "qualification_state": "QUALIFIED_POSITIVE_EVIDENCE",
        "signal_side": "LONG",
        "decision_time": _DECISION.isoformat(),
        "signal_quality_score": "1.5",
        "expected_net_edge_usd": "25",
        "planned_notional_usdt": "500",
        "risk_budget_usdt": "10",
        "estimated_round_trip_cost_usdt": "1",
        "evidence_sample_sufficient": True,
        "positive_historical_evidence": True,
        "operator_review_required": True,
        "trade_actionable": False,
        "strategy_promotion_allowed": False,
        "demo_activation_allowed": False,
        "live_activation_allowed": False,
        "bybit_live_order_routing_allowed": False,
    }


class _Reader:
    def __init__(self) -> None:
        self.calls: list[tuple[int, bool]] = []

    def latest_review_queue(self, *, limit: int = 10, include_mixed: bool = False):
        self.calls.append((limit, include_mixed))
        return (_row(1, "BTCUSDT"), _row(2, "ETHUSDT"))


class _KlineClient:
    def __init__(self) -> None:
        self.request: BybitKlineRequest | None = None

    def fetch(self, request: BybitKlineRequest) -> BybitKlineAcquisition:
        self.request = request
        bar = BybitKlineBar(
            symbol=request.symbols[0],
            start_time=_DECISION,
            open=prepare_tool.Decimal("100"),
            high=prepare_tool.Decimal("101"),
            low=prepare_tool.Decimal("99"),
            close=prepare_tool.Decimal("100.5"),
            volume=prepare_tool.Decimal("1"),
            turnover=prepare_tool.Decimal("100"),
        )
        # The real approval builder is mocked in these orchestration tests, but acquisition
        # validation still needs the fixed strategy minimum history count.
        count = prepare_tool.minimum_history_bars(prepare_tool.CryptoPerpStrategyConfig())
        bars = tuple(
            BybitKlineBar(
                symbol=bar.symbol,
                start_time=_DECISION - prepare_tool.timedelta(minutes=5 * (count - 1 - index)),
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
                turnover=bar.turnover,
            )
            for index in range(count)
        )
        return BybitKlineAcquisition(bars=bars, pages_by_symbol={bar.symbol: 1})


def test_prepare_command_selects_exact_rank_and_never_writes_order(monkeypatch) -> None:
    reader = _Reader()
    klines = _KlineClient()
    captured: dict[str, Any] = {}

    class _Approval:
        def to_payload(self):
            return {"approval_id": "b" * 64, "environment": "BYBIT_DEMO"}

    def _create(row, bars, **kwargs):
        captured["row"] = row
        captured["bars"] = bars
        captured.update(kwargs)
        return _Approval()

    monkeypatch.setattr(prepare_tool, "create_bybit_demo_operator_approval", _create)
    report = prepare_tool.prepare_bybit_demo_operator_approval(
        reader,
        klines,
        evidence_rank=2,
        expected_symbol="ETHUSDT",
        approved_at=_APPROVED,
        confirmation_phrase="APPROVE_BYBIT_DEMO_EXECUTION",
        ttl_seconds=90,
    )

    assert reader.calls == [(50, False)]
    assert captured["row"]["symbol"] == "ETHUSDT"
    assert captured["approved_at"] == _APPROVED
    assert captured["confirmation_phrase"] == "APPROVE_BYBIT_DEMO_EXECUTION"
    assert captured["ttl_seconds"] == 90
    assert klines.request is not None
    assert klines.request.symbols == ("ETHUSDT",)
    assert klines.request.end_ms == int(_DECISION.timestamp() * 1000)
    assert report["source_evidence_rank"] == 2
    assert report["source_symbol"] == "ETHUSDT"
    assert report["prepared_only"] is True
    assert report["order_write_performed"] is False
    assert report["environment"] == "BYBIT_DEMO"
    assert report["live_mainnet_order_routing_allowed"] is False


def test_prepare_command_blocks_symbol_drift_before_approval_creation(monkeypatch) -> None:
    called = False

    def _create(*args: Any, **kwargs: Any):
        nonlocal called
        called = True
        return SimpleNamespace(to_payload=lambda: {})

    monkeypatch.setattr(prepare_tool, "create_bybit_demo_operator_approval", _create)
    with pytest.raises(ValueError, match="no longer matches expected symbol"):
        prepare_tool.prepare_bybit_demo_operator_approval(
            _Reader(),
            _KlineClient(),
            evidence_rank=1,
            expected_symbol="ETHUSDT",
            approved_at=_APPROVED,
            confirmation_phrase="APPROVE_BYBIT_DEMO_EXECUTION",
        )
    assert called is False


def test_prepare_command_requires_unique_latest_positive_rank() -> None:
    class _DuplicateReader:
        def latest_review_queue(self, *, limit: int = 10, include_mixed: bool = False):
            return (_row(1, "BTCUSDT"), _row(1, "ETHUSDT"))

    with pytest.raises(RuntimeError, match="exactly one latest positive-evidence row"):
        prepare_tool.prepare_bybit_demo_operator_approval(
            _DuplicateReader(),
            _KlineClient(),
            evidence_rank=1,
            approved_at=_APPROVED,
            confirmation_phrase="APPROVE_BYBIT_DEMO_EXECUTION",
        )
