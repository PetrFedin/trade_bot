from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import app.strategy.crypto_target_selection as target_selection
from app.strategy.crypto_entry_economics import CryptoEntryEconomicsPolicy
from app.strategy.crypto_perp import (
    CryptoPerpStrategyConfig,
    CryptoSide,
    CryptoSignal,
    CryptoTradePlan,
)
from app.strategy.crypto_target_selection import (
    CryptoTargetSelectionPolicy,
    select_highest_feasible_crypto_target,
)


def _plan(target: Decimal, *, expected_edge: Decimal | None = None) -> CryptoTradePlan:
    return CryptoTradePlan(
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
        decision_time="2026-08-12T18:00:00+00:00",
        reference_price=Decimal("100000"),
        notional_usdt=Decimal("1500"),
        reference_quantity=Decimal("0.015"),
        risk_budget_usdt=Decimal("10"),
        stop_fraction=Decimal("0.004"),
        estimated_round_trip_cost_usdt=Decimal("1.5"),
        estimated_stop_loss_after_cost_usdt=Decimal("7.5"),
        target_net_profit_usd=target,
        required_move_fraction=Decimal("0.01"),
        expected_move_fraction=Decimal("0.015"),
        expected_net_edge_usd=expected_edge if expected_edge is not None else target + 5,
        quality_score=Decimal("2.5"),
    )


def _signal_placeholder() -> CryptoSignal:
    return cast(CryptoSignal, object())


def test_selector_tries_25_then_20_then_15_and_stops_at_highest_feasible(
    monkeypatch,
) -> None:
    attempted: list[Decimal] = []

    def fake_build(_signal, *, equity_usdt, config):
        assert equity_usdt == Decimal("1000")
        attempted.append(config.target_net_profit_usd)
        target = config.target_net_profit_usd
        if target == Decimal("25"):
            return SimpleNamespace(eligible=False, plan=None, reasons=("EDGE_TOO_SMALL",))
        return SimpleNamespace(eligible=True, plan=_plan(target), reasons=())

    monkeypatch.setattr(target_selection, "build_trade_plan", fake_build)

    selected = select_highest_feasible_crypto_target(
        _signal_placeholder(),
        equity_usdt=Decimal("1000"),
        config=CryptoPerpStrategyConfig(),
    )

    assert attempted == [Decimal("25"), Decimal("20")]
    assert selected.eligible is True
    assert selected.selected_target_net_profit_usd == Decimal("20")
    assert selected.selected_plan is not None
    assert selected.strategy_promotion_allowed is False
    assert selected.live_activation_allowed is False


def test_selector_returns_no_trade_when_even_15_has_no_cost_aware_edge(monkeypatch) -> None:
    def fake_build(_signal, *, equity_usdt, config):
        del equity_usdt
        return SimpleNamespace(
            eligible=False,
            plan=None,
            reasons=(f"TARGET_{config.target_net_profit_usd}_NOT_FEASIBLE",),
        )

    monkeypatch.setattr(target_selection, "build_trade_plan", fake_build)

    selected = select_highest_feasible_crypto_target(
        _signal_placeholder(),
        equity_usdt=Decimal("1000"),
        config=CryptoPerpStrategyConfig(),
    )

    assert selected.eligible is False
    assert selected.selected_target_net_profit_usd is None
    assert len(selected.attempts) == 3
    assert selected.strategy_promotion_allowed is False


def test_optional_economics_gate_can_reject_thin_25_edge_and_select_20(monkeypatch) -> None:
    def fake_build(_signal, *, equity_usdt, config):
        del equity_usdt
        target = config.target_net_profit_usd
        expected_edge = Decimal("26") if target == Decimal("25") else Decimal("30")
        return SimpleNamespace(
            eligible=True,
            plan=_plan(target, expected_edge=expected_edge),
            reasons=(),
        )

    monkeypatch.setattr(target_selection, "build_trade_plan", fake_build)
    policy = CryptoTargetSelectionPolicy(
        entry_economics_policy=CryptoEntryEconomicsPolicy(
            minimum_expected_edge_to_target=Decimal("1.20"),
            maximum_round_trip_cost_to_target=Decimal("0.15"),
            minimum_target_to_risk_budget=Decimal("1.50"),
        )
    )

    selected = select_highest_feasible_crypto_target(
        _signal_placeholder(),
        equity_usdt=Decimal("1000"),
        config=CryptoPerpStrategyConfig(),
        policy=policy,
    )

    assert selected.eligible is True
    assert selected.selected_target_net_profit_usd == Decimal("20")
    assert selected.shadow_economics_enabled is True
    assert selected.attempts[0].entry_economics is not None
    assert selected.attempts[0].entry_economics.eligible is False
    assert selected.attempts[1].entry_economics is not None
    assert selected.attempts[1].entry_economics.eligible is True
