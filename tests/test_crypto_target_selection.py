from decimal import Decimal
from types import SimpleNamespace
from typing import cast

import pytest

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


def test_selector_requires_20_net_then_leaves_profit_uncapped(monkeypatch) -> None:
    attempted: list[Decimal] = []

    def fake_build(_signal, *, equity_usdt, config):
        assert equity_usdt == Decimal("1000")
        attempted.append(config.target_net_profit_usd)
        return SimpleNamespace(
            eligible=True,
            plan=_plan(config.target_net_profit_usd),
            reasons=(),
        )

    monkeypatch.setattr(target_selection, "build_trade_plan", fake_build)

    selected = select_highest_feasible_crypto_target(
        _signal_placeholder(),
        equity_usdt=Decimal("1000"),
        config=CryptoPerpStrategyConfig(),
    )

    assert attempted == [Decimal("20")]
    assert selected.eligible is True
    assert selected.selected_target_net_profit_usd == Decimal("20")
    assert selected.selected_plan is not None
    assert selected.open_ended_profit_runner is True
    assert selected.profit_cap_net_profit_usd is None
    assert selected.fallback_protected_net_profit_usd == Decimal("15")
    assert selected.normal_exit_band_low_usd == Decimal("18")
    assert selected.normal_exit_band_high_usd == Decimal("22")
    assert selected.strategy_promotion_allowed is False
    assert selected.live_activation_allowed is False


def test_selector_never_falls_back_to_15_for_a_new_entry(monkeypatch) -> None:
    attempted: list[Decimal] = []

    def fake_build(_signal, *, equity_usdt, config):
        del equity_usdt
        attempted.append(config.target_net_profit_usd)
        return SimpleNamespace(
            eligible=False,
            plan=None,
            reasons=("EXPECTED_NET_PROFIT_BELOW_TARGET",),
        )

    monkeypatch.setattr(target_selection, "build_trade_plan", fake_build)

    selected = select_highest_feasible_crypto_target(
        _signal_placeholder(),
        equity_usdt=Decimal("1000"),
        config=CryptoPerpStrategyConfig(),
    )

    assert attempted == [Decimal("20")]
    assert selected.eligible is False
    assert selected.selected_target_net_profit_usd is None
    assert len(selected.attempts) == 1
    assert selected.attempts[0].minimum_net_profit_usd == Decimal("20")
    assert selected.fallback_protected_net_profit_usd == Decimal("15")
    assert selected.normal_exit_band_low_usd == Decimal("18")
    assert selected.normal_exit_band_high_usd == Decimal("22")


def test_optional_economics_gate_can_block_thin_20_edge(monkeypatch) -> None:
    def fake_build(_signal, *, equity_usdt, config):
        del equity_usdt
        assert config.target_net_profit_usd == Decimal("20")
        return SimpleNamespace(
            eligible=True,
            plan=_plan(Decimal("20"), expected_edge=Decimal("21")),
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

    assert selected.eligible is False
    assert selected.shadow_economics_enabled is True
    assert selected.attempts[0].entry_economics is not None
    assert selected.attempts[0].entry_economics.eligible is False


def test_protected_fallback_must_remain_below_normal_target_band() -> None:
    with pytest.raises(ValueError, match="protected fallback"):
        CryptoTargetSelectionPolicy(
            minimum_entry_net_profit_usd=Decimal("20"),
            normal_exit_target_net_profit_usd=Decimal("20"),
            normal_exit_tolerance_usd=Decimal("5"),
            fallback_protected_net_profit_usd=Decimal("18"),
        ).validate()
