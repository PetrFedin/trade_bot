from app.strategy.reentry_confirmation import (
    EntryBlockReason,
    ReentryConfirmationPolicy,
    ReentryConfirmationState,
    arm_after_exit,
    evaluate_reentry_confirmation,
)


def test_first_eligible_bar_after_exit_is_blocked() -> None:
    policy = ReentryConfirmationPolicy(minimum_consecutive_eligible_bars=2)
    decision = evaluate_reentry_confirmation(
        signal_eligible=True,
        state=arm_after_exit(policy=policy),
        policy=policy,
    )
    assert decision.allow_entry is False
    assert decision.reason is EntryBlockReason.REENTRY_CONFIRMATION_PENDING
    assert decision.confirmation_streak == 1
    assert decision.state.blocked_after_exit is True


def test_second_consecutive_eligible_bar_releases_reentry() -> None:
    policy = ReentryConfirmationPolicy(minimum_consecutive_eligible_bars=2)
    first = evaluate_reentry_confirmation(
        signal_eligible=True,
        state=arm_after_exit(policy=policy),
        policy=policy,
    )
    second = evaluate_reentry_confirmation(
        signal_eligible=True,
        state=first.state,
        policy=policy,
    )
    assert second.allow_entry is True
    assert second.reason is None
    assert second.confirmation_streak == 2
    assert second.state == ReentryConfirmationState()


def test_ineligible_bar_resets_confirmation_streak() -> None:
    policy = ReentryConfirmationPolicy(minimum_consecutive_eligible_bars=2)
    first = evaluate_reentry_confirmation(
        signal_eligible=True,
        state=arm_after_exit(policy=policy),
        policy=policy,
    )
    reset = evaluate_reentry_confirmation(
        signal_eligible=False,
        state=first.state,
        policy=policy,
    )
    assert reset.allow_entry is False
    assert reset.confirmation_streak == 0
    assert reset.state.consecutive_eligible_bars == 0
    assert reset.state.blocked_after_exit is True


def test_initial_entry_is_not_delayed_when_not_armed_by_exit() -> None:
    policy = ReentryConfirmationPolicy(minimum_consecutive_eligible_bars=2)
    decision = evaluate_reentry_confirmation(
        signal_eligible=True,
        state=ReentryConfirmationState(),
        policy=policy,
    )
    assert decision.allow_entry is True
    assert decision.reason is None
    assert decision.confirmation_streak == 0
