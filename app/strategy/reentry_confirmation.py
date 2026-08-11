from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class EntryBlockReason(StrEnum):
    REENTRY_CONFIRMATION_PENDING = "REENTRY_CONFIRMATION_PENDING"


@dataclass(frozen=True)
class ReentryConfirmationPolicy:
    minimum_consecutive_eligible_bars: int = 2
    initial_entry_requires_confirmation: bool = False
    reset_streak_on_ineligible_signal: bool = True
    apply_after_any_exit: bool = True

    def validate(self) -> None:
        if self.minimum_consecutive_eligible_bars < 1:
            raise ValueError("minimum_consecutive_eligible_bars must be positive")
        if self.initial_entry_requires_confirmation:
            raise ValueError("initial-entry confirmation is not qualified in this policy")
        if not self.reset_streak_on_ineligible_signal:
            raise ValueError("re-entry confirmation must reset on ineligible signal")
        if not self.apply_after_any_exit:
            raise ValueError("re-entry confirmation must apply after any exit")


@dataclass(frozen=True)
class ReentryConfirmationState:
    blocked_after_exit: bool = False
    consecutive_eligible_bars: int = 0

    def validate(self) -> None:
        if self.consecutive_eligible_bars < 0:
            raise ValueError("consecutive_eligible_bars must be non-negative")
        if not self.blocked_after_exit and self.consecutive_eligible_bars != 0:
            raise ValueError("unblocked re-entry state cannot retain confirmation streak")


@dataclass(frozen=True)
class ReentryConfirmationDecision:
    allow_entry: bool
    reason: EntryBlockReason | None
    confirmation_streak: int
    state: ReentryConfirmationState


def arm_after_exit(
    *, policy: ReentryConfirmationPolicy
) -> ReentryConfirmationState:
    policy.validate()
    return ReentryConfirmationState(blocked_after_exit=True, consecutive_eligible_bars=0)


def clear_after_entry() -> ReentryConfirmationState:
    return ReentryConfirmationState(blocked_after_exit=False, consecutive_eligible_bars=0)


def evaluate_reentry_confirmation(
    *,
    signal_eligible: bool,
    state: ReentryConfirmationState,
    policy: ReentryConfirmationPolicy,
) -> ReentryConfirmationDecision:
    policy.validate()
    state.validate()
    if not state.blocked_after_exit:
        return ReentryConfirmationDecision(
            allow_entry=signal_eligible,
            reason=None,
            confirmation_streak=0,
            state=state,
        )
    if not signal_eligible:
        return ReentryConfirmationDecision(
            allow_entry=False,
            reason=None,
            confirmation_streak=0,
            state=ReentryConfirmationState(
                blocked_after_exit=True,
                consecutive_eligible_bars=0,
            ),
        )

    streak = state.consecutive_eligible_bars + 1
    if streak < policy.minimum_consecutive_eligible_bars:
        return ReentryConfirmationDecision(
            allow_entry=False,
            reason=EntryBlockReason.REENTRY_CONFIRMATION_PENDING,
            confirmation_streak=streak,
            state=ReentryConfirmationState(
                blocked_after_exit=True,
                consecutive_eligible_bars=streak,
            ),
        )
    return ReentryConfirmationDecision(
        allow_entry=True,
        reason=None,
        confirmation_streak=streak,
        state=clear_after_entry(),
    )
