from app.strategy.selection_exit_confirmation import (
    SelectionExitConfirmationPolicy,
    SelectionExitConfirmationState,
    evaluate_selection_exit_confirmation,
)


def policy() -> SelectionExitConfirmationPolicy:
    return SelectionExitConfirmationPolicy(
        minimum_consecutive_deselected_bars=2,
        exit_profitable_positions_immediately=True,
        reset_on_reselection=True,
    )


def test_profitable_deselection_exits_immediately() -> None:
    decision = evaluate_selection_exit_confirmation(
        selected=False,
        profitable_at_decision=True,
        state=SelectionExitConfirmationState(),
        policy=policy(),
    )

    assert decision.allow_selection_exit is True
    assert decision.reason == "PROFITABLE_DESELECTION_IMMEDIATE_EXIT"
    assert decision.state.consecutive_deselected_bars == 0


def test_losing_deselection_requires_two_consecutive_completed_decisions() -> None:
    first = evaluate_selection_exit_confirmation(
        selected=False,
        profitable_at_decision=False,
        state=SelectionExitConfirmationState(),
        policy=policy(),
    )
    assert first.allow_selection_exit is False
    assert first.reason == "DESELECTION_CONFIRMATION_PENDING"
    assert first.state.consecutive_deselected_bars == 1

    second = evaluate_selection_exit_confirmation(
        selected=False,
        profitable_at_decision=False,
        state=first.state,
        policy=policy(),
    )
    assert second.allow_selection_exit is True
    assert second.reason == "DESELECTION_CONFIRMED"
    assert second.state.consecutive_deselected_bars == 0


def test_reselection_resets_pending_streak() -> None:
    pending = evaluate_selection_exit_confirmation(
        selected=False,
        profitable_at_decision=False,
        state=SelectionExitConfirmationState(),
        policy=policy(),
    )
    reset = evaluate_selection_exit_confirmation(
        selected=True,
        profitable_at_decision=False,
        state=pending.state,
        policy=policy(),
    )

    assert reset.allow_selection_exit is False
    assert reset.reason == "SELECTED"
    assert reset.state.consecutive_deselected_bars == 0


def test_policy_validation_rejects_zero_confirmation_bars() -> None:
    invalid = SelectionExitConfirmationPolicy(
        minimum_consecutive_deselected_bars=0,
    )

    try:
        invalid.validate()
    except ValueError as exc:
        assert "must be positive" in str(exc)
    else:
        raise AssertionError("invalid selection-exit policy was accepted")
