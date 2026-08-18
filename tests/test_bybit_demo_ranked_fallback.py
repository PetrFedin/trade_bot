from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.execution.bybit_demo_cycle import BybitDemoCycleStatus
from app.execution.bybit_demo_orchestrator import BybitDemoOrchestratorStatus
from app.execution.bybit_demo_ranked_fallback import (
    BybitDemoCandidateFallbackStage,
    execute_resilient_account_sized_reconciled_guarded_bybit_demo_cycle,
    run_ranked_fallback_bybit_demo_strategy_cycle,
    summarize_bybit_demo_ranked_fallback_quality,
)
from app.execution.bybit_demo_strategy_selector import BybitDemoStrategyCycleStatus
from app.strategy.crypto_perp import CryptoSide


def _plan(symbol: str) -> SimpleNamespace:
    return SimpleNamespace(symbol=symbol, side=CryptoSide.LONG)


def _selection(symbol: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        selected_trade_plan=None if symbol is None else _plan(symbol),
    )


def _quote_block(symbol: str, *reasons: str) -> SimpleNamespace:
    return SimpleNamespace(
        status=BybitDemoStrategyCycleStatus.PRE_ENTRY_QUOTE_BLOCKED,
        selection=_selection(symbol),
        orchestrator_result=None,
        pre_entry_quote_reasons=tuple(reasons),
        pre_entry_quote_price=Decimal("101"),
        pre_entry_modeled_entry_price=Decimal("101.02"),
        live_mainnet_order_routing_allowed=False,
    )


def _fee_block(symbol: str, *reasons: str) -> SimpleNamespace:
    cycle = SimpleNamespace(
        status=BybitDemoCycleStatus.ENTRY_BLOCKED,
        reasons=tuple(reasons),
        entry_ack=None,
    )
    orchestrator = SimpleNamespace(
        status=BybitDemoOrchestratorStatus.CYCLE_EXECUTED,
        cycle_result=cycle,
    )
    return SimpleNamespace(
        status=BybitDemoStrategyCycleStatus.GUARDED_ORCHESTRATOR_CALLED,
        selection=_selection(symbol),
        orchestrator_result=orchestrator,
        pre_entry_quote_reasons=(),
        pre_entry_quote_price=Decimal("101"),
        pre_entry_modeled_entry_price=Decimal("101.02"),
        live_mainnet_order_routing_allowed=False,
    )


def _success(symbol: str) -> SimpleNamespace:
    cycle = SimpleNamespace(
        status=BybitDemoCycleStatus.PROTECTED,
        reasons=(),
        entry_ack=SimpleNamespace(order_id="demo-entry"),
    )
    orchestrator = SimpleNamespace(
        status=BybitDemoOrchestratorStatus.CYCLE_EXECUTED,
        cycle_result=cycle,
    )
    return SimpleNamespace(
        status=BybitDemoStrategyCycleStatus.GUARDED_ORCHESTRATOR_CALLED,
        selection=_selection(symbol),
        orchestrator_result=orchestrator,
        pre_entry_quote_reasons=(),
        pre_entry_quote_price=Decimal("101"),
        pre_entry_modeled_entry_price=Decimal("101.02"),
        live_mainnet_order_routing_allowed=False,
    )


def _no_trade() -> SimpleNamespace:
    return SimpleNamespace(
        status=BybitDemoStrategyCycleStatus.NO_TRADE,
        selection=_selection(None),
        orchestrator_result=None,
        pre_entry_quote_reasons=(),
        pre_entry_quote_price=None,
        pre_entry_modeled_entry_price=None,
        live_mainnet_order_routing_allowed=False,
    )


def _run(sequence: list[object]):
    calls: list[tuple[str, ...]] = []

    def executor(*_args: object, **kwargs: object):
        calls.append(tuple(sorted(kwargs["instruments"])))
        return sequence.pop(0)

    result = run_ranked_fallback_bybit_demo_strategy_cycle(
        {},
        instruments={"BTCUSDT": object(), "ETHUSDT": object()},
        strategy_config=object(),
        session_state=object(),
        now=object(),
        client=object(),
        base_executor=executor,
    )
    return result, calls


def test_quote_economics_block_falls_back_to_next_ranked_symbol() -> None:
    result, calls = _run(
        [
            _quote_block("BTCUSDT", "NEXT_OPEN_EXPECTED_NET_PROFIT_BELOW_TARGET"),
            _success("ETHUSDT"),
        ]
    )

    assert calls == [("BTCUSDT", "ETHUSDT"), ("ETHUSDT",)]
    assert result.selected_after_fallback is True
    assert result.candidates_exhausted is False
    assert result.final_selected_symbol == "ETHUSDT"
    assert len(result.fallback_attempts) == 1
    attempt = result.fallback_attempts[0]
    assert attempt.symbol == "BTCUSDT"
    assert attempt.stage is BybitDemoCandidateFallbackStage.PRE_ENTRY_QUOTE


def test_actual_fee_economics_block_falls_back_before_entry_ack() -> None:
    result, calls = _run(
        [
            _fee_block("BTCUSDT", "ACCOUNT_FEE_EXPECTED_NET_PROFIT_BELOW_TARGET"),
            _success("ETHUSDT"),
        ]
    )

    assert calls == [("BTCUSDT", "ETHUSDT"), ("ETHUSDT",)]
    assert result.selected_after_fallback is True
    assert result.final_selected_symbol == "ETHUSDT"
    assert result.fallback_attempts[0].stage is BybitDemoCandidateFallbackStage.ACCOUNT_FEE_ECONOMICS


def test_quote_read_failure_never_retries_another_symbol() -> None:
    result, calls = _run(
        [_quote_block("BTCUSDT", "PRE_ENTRY_QUOTE_READ_FAILED:TimeoutError")]
    )

    assert calls == [("BTCUSDT", "ETHUSDT")]
    assert result.fallback_attempts == ()
    assert result.selected_after_fallback is False
    assert result.cycle_result.status is BybitDemoStrategyCycleStatus.PRE_ENTRY_QUOTE_BLOCKED


def test_fee_read_failure_never_retries_another_symbol() -> None:
    result, calls = _run(
        [_fee_block("BTCUSDT", "ACCOUNT_FEE_RATE_RECONCILIATION_FAILED:TimeoutError")]
    )

    assert calls == [("BTCUSDT", "ETHUSDT")]
    assert result.fallback_attempts == ()
    assert result.selected_after_fallback is False


def test_post_entry_or_protection_state_never_retries() -> None:
    result, calls = _run([_success("BTCUSDT")])

    assert calls == [("BTCUSDT", "ETHUSDT")]
    assert result.fallback_attempts == ()
    assert result.selected_after_fallback is False
    assert result.final_selected_symbol == "BTCUSDT"


def test_exhausted_candidates_stop_without_relaxing_thresholds() -> None:
    result, calls = _run(
        [
            _quote_block("BTCUSDT", "NEXT_OPEN_EXPECTED_NET_PROFIT_BELOW_TARGET"),
            _quote_block("ETHUSDT", "NEXT_OPEN_EXPECTED_NET_PROFIT_BELOW_TARGET"),
        ]
    )

    assert calls == [("BTCUSDT", "ETHUSDT"), ("ETHUSDT",)]
    assert result.selected_after_fallback is False
    assert result.candidates_exhausted is True
    assert len(result.fallback_attempts) == 2


def test_non_retryable_fee_risk_reason_does_not_broaden_retry_surface() -> None:
    result, calls = _run([_fee_block("BTCUSDT", "SOME_OTHER_ENTRY_BLOCK")])

    assert calls == [("BTCUSDT", "ETHUSDT")]
    assert result.fallback_attempts == ()
    assert result.selected_after_fallback is False


def test_resilient_account_wrapper_preserves_account_refresh_boundary() -> None:
    observed: dict[str, object] = {}

    def base_strategy(*_args: object, **kwargs: object):
        observed["strategy_instruments"] = tuple(sorted(kwargs["instruments"]))
        return _success("BTCUSDT")

    def account_executor(*args: object, **kwargs: object):
        observed["account_strategy_executor"] = kwargs["strategy_cycle_executor"]
        cycle = kwargs["strategy_cycle_executor"](
            args[0],
            instruments=kwargs["instruments"],
            strategy_config=kwargs["strategy_config"],
            session_state=kwargs["session_state"],
            now=kwargs["now"],
            client=kwargs["client"],
        )
        return SimpleNamespace(
            strategy_cycle_result=cycle,
            live_mainnet_order_routing_allowed=False,
        )

    result = execute_resilient_account_sized_reconciled_guarded_bybit_demo_cycle(
        {},
        instruments={"BTCUSDT": object()},
        strategy_config=object(),
        session_state=object(),
        now=object(),
        client=object(),
        accounting_client=object(),
        account_sized_executor=account_executor,
        base_strategy_executor=base_strategy,
    )

    assert callable(observed["account_strategy_executor"])
    assert observed["strategy_instruments"] == ("BTCUSDT",)
    assert result.selected_after_fallback is False
    assert result.final_selected_symbol == "BTCUSDT"
    assert result.live_mainnet_order_routing_allowed is False


def test_resilient_account_wrapper_owns_strategy_executor_boundary() -> None:
    with pytest.raises(ValueError, match="owns the strategy_cycle_executor boundary"):
        execute_resilient_account_sized_reconciled_guarded_bybit_demo_cycle(
            {},
            instruments={},
            strategy_config=object(),
            session_state=object(),
            now=object(),
            client=object(),
            accounting_client=object(),
            strategy_cycle_executor=object(),
        )


def test_ranked_fallback_quality_tracks_rejections_without_calling_them_profit() -> None:
    ranked, _calls = _run(
        [
            _quote_block("BTCUSDT", "NEXT_OPEN_EXPECTED_NET_PROFIT_BELOW_TARGET"),
            _success("ETHUSDT"),
        ]
    )
    wrapper = SimpleNamespace(
        account_sized_result=SimpleNamespace(live_mainnet_order_routing_allowed=False),
        fallback_attempts=ranked.fallback_attempts,
        selected_after_fallback=ranked.selected_after_fallback,
        candidates_exhausted=ranked.candidates_exhausted,
        final_selected_symbol=ranked.final_selected_symbol,
        live_mainnet_order_routing_allowed=False,
    )

    quality = summarize_bybit_demo_ranked_fallback_quality([wrapper])

    assert quality["fallback_attempt_count"] == 1
    assert quality["selected_after_fallback_count"] == 1
    assert quality["fallback_stage_counts"] == {"PRE_ENTRY_QUOTE": 1}
    assert quality["fallback_never_relaxes_entry_thresholds"] is True
    assert quality["fallback_occurs_before_entry_ack_only"] is True
    assert quality["strategy_promotion_allowed"] is False
