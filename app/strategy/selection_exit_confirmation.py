from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SelectionExitConfirmationPolicy:
    minimum_consecutive_deselected_bars: int = 2
    exit_profitable_positions_immediately: bool = True
    reset_on_reselection: bool = True

    def validate(self) -> None:
        if self.minimum_consecutive_deselected_bars < 1:
            raise ValueError("minimum_consecutive_deselected_bars must be positive")


@dataclass(frozen=True)
class SelectionExitConfirmationState:
    consecutive_deselected_bars: int = 0

    def validate(self) -> None:
        if self.consecutive_deselected_bars < 0:
            raise ValueError("consecutive_deselected_bars cannot be negative")


@dataclass(frozen=True)
class SelectionExitConfirmationDecision:
    allow_selection_exit: bool
    state: SelectionExitConfirmationState
    reason: str


def evaluate_selection_exit_confirmation(
    *,
    selected: bool,
    profitable_at_decision: bool,
    state: SelectionExitConfirmationState,
    policy: SelectionExitConfirmationPolicy,
) -> SelectionExitConfirmationDecision:
    """Confirm only non-risk selection exits; external risk exits remain independent."""

    policy.validate()
    state.validate()
    if selected:
        next_state = (
            SelectionExitConfirmationState()
            if policy.reset_on_reselection
            else state
        )
        return SelectionExitConfirmationDecision(
            allow_selection_exit=False,
            state=next_state,
            reason="SELECTED",
        )
    if profitable_at_decision and policy.exit_profitable_positions_immediately:
        return SelectionExitConfirmationDecision(
            allow_selection_exit=True,
            state=SelectionExitConfirmationState(),
            reason="PROFITABLE_DESELECTION_IMMEDIATE_EXIT",
        )
    streak = state.consecutive_deselected_bars + 1
    next_state = SelectionExitConfirmationState(
        consecutive_deselected_bars=streak,
    )
    if streak >= policy.minimum_consecutive_deselected_bars:
        return SelectionExitConfirmationDecision(
            allow_selection_exit=True,
            state=SelectionExitConfirmationState(),
            reason="DESELECTION_CONFIRMED",
        )
    return SelectionExitConfirmationDecision(
        allow_selection_exit=False,
        state=next_state,
        reason="DESELECTION_CONFIRMATION_PENDING",
    )
