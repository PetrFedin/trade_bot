"""Acceptance coverage for #145: session identity bound before risk.

Test names carry the acceptance number from the issue so the mapping is checkable.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.domain.operational_identity import (
    DEFAULT_MAXIMUM_OBSERVATION_AGE,
    CredentialPurpose,
    ExpectedAccountIdentity,
    IdentityRejection,
    ObservedAccountIdentity,
    OperationalIdentityError,
    TradingEnvironment,
    account_digest,
    verify_identity,
)

NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
VENUE = "alpaca"
ACCOUNT = "PA3XYZ01"
ENDPOINT = "https://paper-api.alpaca.markets"
RELEASE = "e27825c32261ce073dd074d975ef4ef27bc0a1a7"


def expected(**overrides) -> ExpectedAccountIdentity:
    base = {
        "venue": VENUE,
        "environment": TradingEnvironment.PAPER,
        "account_digest": account_digest(
            ACCOUNT, venue=VENUE, environment=TradingEnvironment.PAPER
        ),
        "allowed_rest_endpoint": ENDPOINT,
        "credential_fingerprint": "a1b2c3d4e5f60718",
        "credential_generation": 3,
        "release_sha": RELEASE,
        "currency": "USD",
    }
    base.update(overrides)
    built = ExpectedAccountIdentity(**base)
    built.validate()
    return built


def observed(**overrides) -> ObservedAccountIdentity:
    base = {
        "venue": VENUE,
        "environment": TradingEnvironment.PAPER,
        "account_id": ACCOUNT,
        "status": "ACTIVE",
        "currency": "USD",
        "trading_blocked": False,
        "rest_endpoint": ENDPOINT,
        "credential_fingerprint": "a1b2c3d4e5f60718",
        "credential_generation": 3,
        "credential_purpose": CredentialPurpose.TRADING,
        "release_sha": RELEASE,
        "observed_at": NOW,
    }
    base.update(overrides)
    return ObservedAccountIdentity(**base)


def verify(**overrides):
    call = {"expected": expected(), "observed": observed(), "now": NOW}
    call.update(overrides)
    return verify_identity(call["expected"], call["observed"], now=call["now"],
                           paired=call.get("paired"))


def test_a_matching_session_verifies() -> None:
    result = verify()
    assert result.environment is TradingEnvironment.PAPER
    assert result.credential_generation == 3


# --- acceptance 1: right endpoint, wrong account ----------------------------------


def test_acceptance_1_right_endpoint_wrong_account_is_refused() -> None:
    """Authentication succeeding says nothing about which account answered."""
    with pytest.raises(OperationalIdentityError) as caught:
        verify(observed=observed(account_id="PA9OTHER"))
    assert caught.value.reason is IdentityRejection.ACCOUNT_MISMATCH


# --- acceptance 2: credential fingerprint and generation --------------------------


def test_acceptance_2_wrong_fingerprint_is_refused() -> None:
    with pytest.raises(OperationalIdentityError) as caught:
        verify(observed=observed(credential_fingerprint="0000000000000000"))
    assert caught.value.reason is IdentityRejection.CREDENTIAL_FINGERPRINT_MISMATCH


def test_acceptance_2_an_older_generation_is_refused() -> None:
    with pytest.raises(OperationalIdentityError) as caught:
        verify(observed=observed(credential_generation=2))
    assert caught.value.reason is IdentityRejection.CREDENTIAL_GENERATION_ROTATED


def test_acceptance_2_a_newer_generation_is_also_refused() -> None:
    """A rotated-in credential was never approved; newer is not automatically better."""
    with pytest.raises(OperationalIdentityError) as caught:
        verify(observed=observed(credential_generation=4))
    assert caught.value.reason is IdentityRejection.CREDENTIAL_GENERATION_AHEAD


# --- acceptance 3: rotation invalidates a prior approval --------------------------


def test_acceptance_3_rotation_changes_the_binding() -> None:
    """An approval carries the binding; after rotation it no longer describes the session."""
    before = expected(credential_generation=3).binding
    after = expected(credential_generation=4).binding
    assert before != after


def test_acceptance_3_binding_is_stable_for_an_unchanged_contract() -> None:
    assert expected().binding == expected().binding


# --- acceptance 4: paired credentials must be one account -------------------------


def test_acceptance_4_paired_credentials_for_different_accounts_are_refused() -> None:
    """A trading key for another account authenticates perfectly well on its own."""
    read_only = observed(
        account_id="PA9OTHER", credential_purpose=CredentialPurpose.READ_ONLY
    )
    with pytest.raises(OperationalIdentityError) as caught:
        verify(paired=read_only)
    assert caught.value.reason is IdentityRejection.PAIRED_CREDENTIAL_ACCOUNT_MISMATCH


def test_acceptance_4_paired_credentials_for_one_account_pass() -> None:
    read_only = observed(credential_purpose=CredentialPurpose.READ_ONLY)
    assert verify(paired=read_only).credential_generation == 3


def test_acceptance_4_a_read_only_credential_cannot_satisfy_trading() -> None:
    with pytest.raises(OperationalIdentityError) as caught:
        verify(observed=observed(credential_purpose=CredentialPurpose.READ_ONLY))
    assert caught.value.reason is IdentityRejection.CREDENTIAL_PURPOSE_INSUFFICIENT


# --- acceptance 5: mainnet cannot satisfy a paper contract ------------------------


def test_acceptance_5_a_mainnet_environment_is_refused() -> None:
    with pytest.raises(OperationalIdentityError) as caught:
        verify(observed=observed(environment=TradingEnvironment.MAINNET))
    assert caught.value.reason is IdentityRejection.ENVIRONMENT_MISMATCH


def test_acceptance_5_a_mainnet_endpoint_is_refused() -> None:
    with pytest.raises(OperationalIdentityError) as caught:
        verify(observed=observed(rest_endpoint="https://api.alpaca.markets"))
    assert caught.value.reason is IdentityRejection.ENDPOINT_NOT_ALLOWED


def test_acceptance_5_the_same_account_number_on_mainnet_digests_differently() -> None:
    """Environment is folded into the digest so numbering cannot collide across venues."""
    paper = account_digest(ACCOUNT, venue=VENUE, environment=TradingEnvironment.PAPER)
    mainnet = account_digest(ACCOUNT, venue=VENUE, environment=TradingEnvironment.MAINNET)
    assert paper != mainnet


# --- acceptance 6: account state after planning -----------------------------------


def test_acceptance_6_a_trading_blocked_account_is_refused() -> None:
    with pytest.raises(OperationalIdentityError) as caught:
        verify(observed=observed(trading_blocked=True))
    assert caught.value.reason is IdentityRejection.ACCOUNT_TRADING_BLOCKED


def test_acceptance_6_an_inactive_account_is_refused() -> None:
    for status in ("SUSPENDED", "CLOSED", "ACCOUNT_UPDATED"):
        with pytest.raises(OperationalIdentityError) as caught:
            verify(observed=observed(status=status))
        assert caught.value.reason is IdentityRejection.ACCOUNT_NOT_ACTIVE


def test_acceptance_6_a_stale_observation_cannot_support_new_risk() -> None:
    """An account can be frozen between a preflight and a submit."""
    with pytest.raises(OperationalIdentityError) as caught:
        verify(now=NOW + DEFAULT_MAXIMUM_OBSERVATION_AGE + timedelta(seconds=1))
    assert caught.value.reason is IdentityRejection.OBSERVATION_STALE


def test_acceptance_6_a_fresh_observation_is_accepted() -> None:
    assert verify(now=NOW + timedelta(minutes=1)).credential_generation == 3


# --- acceptance 7: identity change across restart ---------------------------------


def test_acceptance_7_a_release_change_is_refused() -> None:
    with pytest.raises(OperationalIdentityError) as caught:
        verify(observed=observed(release_sha="0" * 40))
    assert caught.value.reason is IdentityRejection.RELEASE_MISMATCH


def test_acceptance_7_a_venue_change_is_refused() -> None:
    with pytest.raises(OperationalIdentityError) as caught:
        verify(observed=observed(venue="bybit"))
    assert caught.value.reason is IdentityRejection.VENUE_MISMATCH


# --- acceptance 8 and 9: no raw secrets or identifiers in the binding -------------


def test_acceptance_8_the_contract_refuses_a_raw_account_identifier() -> None:
    """Storing the identifier instead of its digest must not be expressible."""
    with pytest.raises(ValueError, match="not a raw identifier"):
        expected(account_digest=ACCOUNT)


def test_acceptance_9_the_binding_exposes_no_identifier_or_secret() -> None:
    contract = expected()
    binding = contract.binding
    assert ACCOUNT not in binding
    assert contract.credential_fingerprint not in binding
    assert len(binding) == 64


def test_acceptance_9_a_rejection_message_carries_no_raw_account_id() -> None:
    with pytest.raises(OperationalIdentityError) as caught:
        verify(observed=observed(account_id="PA9SECRET"))
    assert "PA9SECRET" not in str(caught.value)


# --- contract validation ----------------------------------------------------------


def test_a_non_https_endpoint_is_refused() -> None:
    with pytest.raises(ValueError, match="https"):
        expected(allowed_rest_endpoint="http://paper-api.alpaca.markets")


def test_a_non_positive_generation_is_refused() -> None:
    with pytest.raises(ValueError, match="credential_generation"):
        expected(credential_generation=0)


def test_a_currency_mismatch_is_refused() -> None:
    with pytest.raises(OperationalIdentityError) as caught:
        verify(observed=observed(currency="EUR"))
    assert caught.value.reason is IdentityRejection.CURRENCY_MISMATCH


def test_an_unspecified_currency_is_not_compared() -> None:
    assert verify(expected=expected(currency="UNSPECIFIED"),
                  observed=observed(currency="EUR")).credential_generation == 3


def test_a_naive_now_is_refused() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        verify(now=datetime(2026, 9, 20, 12, 0))


def test_an_empty_account_id_cannot_be_digested() -> None:
    with pytest.raises(ValueError, match="account_id"):
        account_digest("  ", venue=VENUE, environment=TradingEnvironment.PAPER)
