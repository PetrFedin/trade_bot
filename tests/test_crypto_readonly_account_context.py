from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.execution.bybit_mainnet_readonly import (
    BybitMainnetAccountInfo,
    BybitMainnetApiKeyInfo,
    BybitMainnetPosition,
    BybitMainnetWalletBalance,
)
from app.runtime.bybit_mainnet_readonly_probe import BybitMainnetReadOnlySnapshot
from app.strategy.crypto_live_evidence_ranking import (
    CryptoLiveOpportunity,
    CryptoLiveOpportunitySnapshot,
)
from app.strategy.crypto_readonly_account_context import (
    build_crypto_account_aware_registry_snapshot,
    build_crypto_readonly_account_context,
)

_NOW = datetime(2026, 8, 23, 18, tzinfo=UTC)


def _mainnet_snapshot(*, available: Decimal = Decimal("600")) -> BybitMainnetReadOnlySnapshot:
    snapshot = BybitMainnetReadOnlySnapshot(
        api_key=BybitMainnetApiKeyInfo(
            key_fingerprint_sha256="a" * 64,
            read_only=True,
            ip_bindings=("203.0.113.10",),
            key_type=1,
            note="trade-bot-readonly",
            permissions=("Contract:Order", "Contract:Position"),
        ),
        account=BybitMainnetAccountInfo(
            margin_mode="REGULAR_MARGIN",
            unified_margin_status=5,
            updated_time_ms=int(_NOW.timestamp() * 1000),
        ),
        wallet=BybitMainnetWalletBalance(
            total_equity_usd=Decimal("1000"),
            total_wallet_balance_usd=Decimal("950"),
            total_margin_balance_usd=Decimal("980"),
            total_available_balance_usd=available,
            total_perp_upl_usd=Decimal("30"),
            total_initial_margin_usd=Decimal("200"),
            total_maintenance_margin_usd=Decimal("50"),
            usdt_wallet_balance=Decimal("900"),
        ),
        positions=(
            BybitMainnetPosition(
                symbol="BTCUSDT",
                side="Buy",
                size=Decimal("0.01"),
                position_idx=0,
                average_price=Decimal("100000"),
                mark_price=Decimal("101000"),
                position_value=Decimal("300"),
                unrealised_pnl=Decimal("10"),
                liquidation_price=Decimal("50000"),
                leverage=Decimal("3"),
            ),
            BybitMainnetPosition(
                symbol="ETHUSDT",
                side="Sell",
                size=Decimal("0.10"),
                position_idx=0,
                average_price=Decimal("4500"),
                mark_price=Decimal("4400"),
                position_value=Decimal("150"),
                unrealised_pnl=Decimal("5"),
                liquidation_price=Decimal("7000"),
                leverage=Decimal("2"),
            ),
        ),
        api_host="api.bybit.eu",
    )
    snapshot.validate()
    return snapshot


def _opportunity(
    *,
    rank: int,
    symbol: str,
    side: str | None,
    notional: Decimal | None,
) -> CryptoLiveOpportunity:
    opportunity = CryptoLiveOpportunity(
        evidence_rank=rank,
        market_rank=rank,
        symbol=symbol,
        market_universe_score=Decimal("0.8"),
        qualification_state=(
            "QUALIFIED_POSITIVE_EVIDENCE" if side is not None else "NO_FIXED_STRATEGY_SIGNAL"
        ),
        qualification_reasons=("TEST",),
        signal_side=side,
        decision_time=None if side is None else _NOW.isoformat(),
        signal_quality_score=None if side is None else Decimal("1.2"),
        current_market_regime=None,
        current_open_interest_regime=None,
        current_crowding_regime=None,
        current_prior_funding_regime=None,
        current_stress_regime=None,
        current_stress_score=None,
        expected_net_edge_usd=None if side is None else Decimal("30"),
        planned_notional_usdt=notional,
        risk_budget_usdt=None if side is None else Decimal("6"),
        estimated_round_trip_cost_usdt=None if side is None else Decimal("1"),
        evidence_cell_key=None if side is None else "CELL",
        evidence_trade_count=None if side is None else 10,
        evidence_sample_sufficient=side is not None,
        evidence_profit_factor=None if side is None else Decimal("2"),
        evidence_win_rate=None if side is None else Decimal("0.6"),
        evidence_total_net_pnl_usdt=None if side is None else Decimal("100"),
        evidence_average_net_pnl_usdt=None if side is None else Decimal("10"),
        evidence_average_mfe_r=None if side is None else Decimal("1.5"),
        evidence_average_mae_r=None if side is None else Decimal("-0.4"),
        evidence_drawdown_usdt=None if side is None else Decimal("20"),
        positive_historical_evidence=side is not None,
    )
    opportunity.validate()
    return opportunity


def _ranking() -> CryptoLiveOpportunitySnapshot:
    ranking = CryptoLiveOpportunitySnapshot(
        observed_at_ms=int(_NOW.timestamp() * 1000),
        market_snapshot_id="b" * 64,
        evidence_snapshot_id="c" * 64,
        equity_usdt=Decimal("600"),
        equity_source="BYBIT_MAINNET_READONLY_AVAILABLE_CAPITAL_USD_EQUIVALENT",
        opportunities=(
            _opportunity(rank=1, symbol="BTCUSDT", side="LONG", notional=Decimal("100")),
            _opportunity(rank=2, symbol="ETHUSDT", side="LONG", notional=Decimal("80")),
            _opportunity(rank=3, symbol="SOLUSDT", side=None, notional=None),
        ),
        qualified_positive_count=2,
        qualified_mixed_count=0,
    )
    ranking.validate()
    return ranking


def test_readonly_account_context_uses_available_capital_and_existing_exposure() -> None:
    context = build_crypto_readonly_account_context(
        _mainnet_snapshot(),
        observed_at=_NOW,
    )

    assert context.total_equity_usd == Decimal("1000")
    assert context.total_available_balance_usd == Decimal("600")
    assert context.sizing_capital_usd_equivalent == Decimal("600")
    assert context.gross_position_value_usd == Decimal("450")
    assert context.long_position_value_usd == Decimal("300")
    assert context.short_position_value_usd == Decimal("150")
    assert context.net_position_value_usd == Decimal("150")
    assert context.initial_margin_to_equity == Decimal("0.2")
    assert context.maintenance_margin_to_equity == Decimal("0.05")
    assert context.available_balance_to_equity == Decimal("0.6")
    assert context.gross_position_value_to_equity == Decimal("0.45")
    assert context.open_position_count == 2
    assert context.position_exposure_complete is True
    assert context.trade_actionable is False
    assert context.live_mainnet_order_routing_allowed is False
    assert context.order_writes_supported is False


def test_account_overlay_preserves_evidence_order_and_exposes_position_relation() -> None:
    context = build_crypto_readonly_account_context(
        _mainnet_snapshot(),
        observed_at=_NOW,
    )
    account_aware = build_crypto_account_aware_registry_snapshot(
        _ranking(),
        context,
        observed_at=_NOW,
    )

    assert account_aware.ranking_order_changed is False
    assert [item.symbol for item in account_aware.candidate_overlays] == [
        "BTCUSDT",
        "ETHUSDT",
        "SOLUSDT",
    ]
    btc, eth, sol = account_aware.candidate_overlays
    assert btc.existing_position_relation == "SAME_DIRECTION_EXISTING_POSITION"
    assert btc.existing_gross_position_value_usd == Decimal("300")
    assert btc.planned_notional_to_sizing_capital == Decimal("100") / Decimal("600")
    assert btc.gross_plus_planned_upper_bound_usd == Decimal("550")
    assert eth.existing_position_relation == "OPPOSING_EXISTING_POSITION"
    assert eth.existing_gross_position_value_usd == Decimal("150")
    assert eth.gross_plus_planned_upper_bound_usd == Decimal("530")
    assert sol.existing_position_relation == "NO_SIGNAL"
    assert sol.planned_notional_usdt is None
    assert account_aware.trade_actionable is False
    assert account_aware.live_mainnet_order_routing_allowed is False
    assert account_aware.order_writes_supported is False


def test_account_aware_registry_fails_closed_without_positive_available_capital() -> None:
    context = build_crypto_readonly_account_context(
        _mainnet_snapshot(available=Decimal("0")),
        observed_at=_NOW,
    )
    assert context.sizing_capital_usd_equivalent is None
    with pytest.raises(ValueError, match="no positive available sizing capital"):
        build_crypto_account_aware_registry_snapshot(
            _ranking(),
            context,
            observed_at=_NOW,
        )
