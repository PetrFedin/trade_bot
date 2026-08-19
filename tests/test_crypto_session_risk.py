from decimal import Decimal

from app.strategy.crypto_session_risk import (
    CryptoSessionRiskAction,
    CryptoSessionRiskState,
    evaluate_crypto_session_risk,
)


def test_clean_crypto_session_allows_new_entries() -> None:
    decision = evaluate_crypto_session_risk(
        CryptoSessionRiskState(
            opening_equity_usdt=Decimal("1000"),
            current_equity_usdt=Decimal("1015"),
            peak_equity_usdt=Decimal("1020"),
            realized_pnl_usdt=Decimal("15"),
            execution_cost_usdt=Decimal("4"),
            consecutive_losses=0,
        )
    )

    assert decision.action is CryptoSessionRiskAction.ALLOW_NEW_ENTRY
    assert decision.new_entries_allowed is True
    assert decision.flatten_required is False


def test_execution_cost_budget_blocks_overtrading_without_forcing_flatten() -> None:
    decision = evaluate_crypto_session_risk(
        CryptoSessionRiskState(
            opening_equity_usdt=Decimal("1000"),
            current_equity_usdt=Decimal("1005"),
            peak_equity_usdt=Decimal("1010"),
            realized_pnl_usdt=Decimal("25"),
            execution_cost_usdt=Decimal("20"),
            consecutive_losses=0,
        )
    )

    assert decision.action is CryptoSessionRiskAction.BLOCK_NEW_ENTRIES
    assert decision.reasons == ("SESSION_EXECUTION_COST_BUDGET_EXHAUSTED",)
    assert decision.new_entries_allowed is False
    assert decision.flatten_required is False


def test_three_consecutive_losses_block_new_entries() -> None:
    decision = evaluate_crypto_session_risk(
        CryptoSessionRiskState(
            opening_equity_usdt=Decimal("1000"),
            current_equity_usdt=Decimal("980"),
            peak_equity_usdt=Decimal("1000"),
            realized_pnl_usdt=Decimal("-20"),
            execution_cost_usdt=Decimal("8"),
            consecutive_losses=3,
        )
    )

    assert decision.action is CryptoSessionRiskAction.BLOCK_NEW_ENTRIES
    assert "SESSION_CONSECUTIVE_LOSS_LIMIT_REACHED" in decision.reasons


def test_drawdown_breach_requires_flatten_and_blocks_reentry() -> None:
    decision = evaluate_crypto_session_risk(
        CryptoSessionRiskState(
            opening_equity_usdt=Decimal("1000"),
            current_equity_usdt=Decimal("950"),
            peak_equity_usdt=Decimal("1000"),
            realized_pnl_usdt=Decimal("-20"),
            execution_cost_usdt=Decimal("5"),
            consecutive_losses=2,
        )
    )

    assert decision.action is CryptoSessionRiskAction.FLATTEN_AND_BLOCK
    assert "SESSION_DRAWDOWN_LIMIT_BREACHED" in decision.reasons
    assert decision.new_entries_allowed is False
    assert decision.flatten_required is True


def test_ten_percent_equity_floor_is_hard_flatten_boundary() -> None:
    decision = evaluate_crypto_session_risk(
        CryptoSessionRiskState(
            opening_equity_usdt=Decimal("1000"),
            current_equity_usdt=Decimal("900"),
            peak_equity_usdt=Decimal("1000"),
            realized_pnl_usdt=Decimal("-100"),
            execution_cost_usdt=Decimal("12"),
            consecutive_losses=3,
        )
    )

    assert decision.action is CryptoSessionRiskAction.FLATTEN_AND_BLOCK
    assert "SESSION_EQUITY_FLOOR_BREACHED" in decision.reasons
    assert "SESSION_DRAWDOWN_LIMIT_BREACHED" in decision.reasons
