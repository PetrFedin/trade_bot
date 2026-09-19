"""Prove which account, environment and credential generation the runtime is operating on.

F25 recorded that the canonical composition has no expected broker account. ProductConfig
carries cash, quantity, risk limits and an SLO; PaperSubmitExecutor checks only that paper
writes are enabled; the Alpaca adapter can report an account id that nothing compares
against anything. Authentication succeeding is treated as sufficient.

It is not. A valid credential may belong to the wrong account, or to a generation that has
since been rotated or revoked, and the endpoint being a Paper endpoint says nothing about
either. The property that matters before a risk-increasing action is

    expected environment + expected account + approved credential generation + exact release

and this module states that contract and checks an observation against it.

Two rules shape the model. Raw secrets are never held: the expected side stores a digest of
the account identifier, never the identifier itself, so evidence and logs can carry the
binding without carrying what it protects. And every refusal is named, because "identity
mismatch" tells an operator nothing about whether a key was rotated, an account was frozen,
or a mainnet credential was pointed at a Paper contract.

The semantics are taken from what V99 and V101 already understood - expected account id,
credential fingerprint, credential generation - rather than invented alongside them. What
is new is that they sit on the canonical path instead of in qualification code.

Nothing here contacts a venue. An observation is supplied by the caller, the way every
other transport in this codebase is.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

# An observation older than this cannot support a new risk-increasing action: an account
# can be frozen, or a credential rotated, between a preflight and a submit.
DEFAULT_MAXIMUM_OBSERVATION_AGE = timedelta(minutes=5)


class TradingEnvironment(StrEnum):
    """Which venue environment a session is contracted to."""

    PAPER = "PAPER"
    DEMO = "DEMO"
    MAINNET = "MAINNET"


class CredentialPurpose(StrEnum):
    """What a credential is permitted to do."""

    READ_ONLY = "READ_ONLY"
    TRADING = "TRADING"


class IdentityRejection(StrEnum):
    """Why an observed session does not satisfy the contracted identity."""

    VENUE_MISMATCH = "VENUE_MISMATCH"
    ENVIRONMENT_MISMATCH = "ENVIRONMENT_MISMATCH"
    ACCOUNT_MISMATCH = "ACCOUNT_MISMATCH"
    ENDPOINT_NOT_ALLOWED = "ENDPOINT_NOT_ALLOWED"
    CREDENTIAL_FINGERPRINT_MISMATCH = "CREDENTIAL_FINGERPRINT_MISMATCH"
    CREDENTIAL_GENERATION_ROTATED = "CREDENTIAL_GENERATION_ROTATED"
    CREDENTIAL_GENERATION_AHEAD = "CREDENTIAL_GENERATION_AHEAD"
    CREDENTIAL_PURPOSE_INSUFFICIENT = "CREDENTIAL_PURPOSE_INSUFFICIENT"
    ACCOUNT_NOT_ACTIVE = "ACCOUNT_NOT_ACTIVE"
    ACCOUNT_TRADING_BLOCKED = "ACCOUNT_TRADING_BLOCKED"
    CURRENCY_MISMATCH = "CURRENCY_MISMATCH"
    RELEASE_MISMATCH = "RELEASE_MISMATCH"
    OBSERVATION_STALE = "OBSERVATION_STALE"
    PAIRED_CREDENTIAL_ACCOUNT_MISMATCH = "PAIRED_CREDENTIAL_ACCOUNT_MISMATCH"


class OperationalIdentityError(ValueError):
    """Raised when an observed session cannot satisfy the contracted identity."""

    def __init__(self, reason: IdentityRejection, detail: str = "") -> None:
        self.reason = reason
        super().__init__(f"{reason.value}: {detail}" if detail else reason.value)


def account_digest(account_id: str, *, venue: str, environment: TradingEnvironment) -> str:
    """Digest an account identifier so a binding can be stored without the identifier.

    The venue and environment are folded in so the same identifier at a Paper and a
    mainnet venue cannot produce the same digest, which is what stops a mainnet account
    from satisfying a Paper contract by coincidence of numbering.
    """
    if not account_id.strip():
        raise ValueError("account_id is required")
    material = f"{venue.strip().lower()}|{environment.value}|{account_id.strip()}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ExpectedAccountIdentity:
    """The account, environment and credential generation a session is contracted to."""

    venue: str
    environment: TradingEnvironment
    account_digest: str
    allowed_rest_endpoint: str
    credential_fingerprint: str
    credential_generation: int
    release_sha: str
    account_type: str = "UNSPECIFIED"
    currency: str = "UNSPECIFIED"
    required_purpose: CredentialPurpose = CredentialPurpose.TRADING

    def validate(self) -> None:
        for name in ("venue", "allowed_rest_endpoint", "credential_fingerprint", "release_sha"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if len(self.account_digest) != 64:
            raise ValueError("account_digest must be a sha256 digest, not a raw identifier")
        if self.credential_generation <= 0:
            raise ValueError("credential_generation must be positive")
        if not self.allowed_rest_endpoint.startswith("https://"):
            raise ValueError("allowed_rest_endpoint must be an https endpoint")

    @property
    def binding(self) -> str:
        """Digest an authorization can carry, naming the identity without exposing it.

        A rotated credential produces a different binding, so an approval issued under
        the old generation no longer matches the session that would execute it.
        """
        material = {
            "venue": self.venue.strip().lower(),
            "environment": self.environment.value,
            "account_digest": self.account_digest,
            "allowed_rest_endpoint": self.allowed_rest_endpoint,
            "credential_fingerprint": self.credential_fingerprint,
            "credential_generation": self.credential_generation,
            "release_sha": self.release_sha,
            "required_purpose": self.required_purpose.value,
        }
        encoded = json.dumps(material, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ObservedAccountIdentity:
    """What a read-only preflight actually saw, before anything is compared."""

    venue: str
    environment: TradingEnvironment
    account_id: str
    status: str
    currency: str
    trading_blocked: bool
    rest_endpoint: str
    credential_fingerprint: str
    credential_generation: int
    credential_purpose: CredentialPurpose
    release_sha: str
    observed_at: datetime
    account_type: str = "UNSPECIFIED"

    def validate(self) -> None:
        for name in ("venue", "account_id", "status", "rest_endpoint", "release_sha"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be a non-empty string")
        if self.credential_generation <= 0:
            raise ValueError("credential_generation must be positive")
        if self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None:
            raise ValueError("observed_at must be timezone-aware")

    def digest_for(self, expected: ExpectedAccountIdentity) -> str:
        return account_digest(self.account_id, venue=expected.venue, environment=self.environment)


@dataclass(frozen=True)
class IdentityVerification:
    """What the comparison concluded, and the binding an authorization may carry."""

    binding: str
    verified_at: datetime
    environment: TradingEnvironment
    credential_generation: int

    def validate(self) -> None:
        if len(self.binding) != 64:
            raise ValueError("binding must be a sha256 digest")
        if self.verified_at.tzinfo is None or self.verified_at.utcoffset() is None:
            raise ValueError("verified_at must be timezone-aware")


_ACTIVE_STATUSES = frozenset({"ACTIVE", "ACTIVE_TRADING", "TRADING"})


def verify_identity(
    expected: ExpectedAccountIdentity,
    observed: ObservedAccountIdentity,
    *,
    now: datetime,
    maximum_observation_age: timedelta = DEFAULT_MAXIMUM_OBSERVATION_AGE,
    paired: ObservedAccountIdentity | None = None,
) -> IdentityVerification:
    """Refuse unless the observation satisfies the contracted identity in every respect.

    ``paired`` is the second credential where read-only and trading keys are separated.
    Both must resolve to the same account: a trading key for a different account than the
    one that was read is the failure this check exists to catch, and it authenticates
    perfectly well on its own.
    """
    expected.validate()
    observed.validate()

    if observed.venue.strip().lower() != expected.venue.strip().lower():
        raise OperationalIdentityError(
            IdentityRejection.VENUE_MISMATCH, f"{observed.venue} is not {expected.venue}"
        )
    if observed.environment is not expected.environment:
        raise OperationalIdentityError(
            IdentityRejection.ENVIRONMENT_MISMATCH,
            f"{observed.environment.value} is not {expected.environment.value}",
        )
    if observed.rest_endpoint.strip() != expected.allowed_rest_endpoint.strip():
        raise OperationalIdentityError(
            IdentityRejection.ENDPOINT_NOT_ALLOWED, observed.rest_endpoint
        )

    # Constant-time: the digest is not a secret, but comparing it this way costs nothing
    # and keeps the habit where it matters.
    if not hmac.compare_digest(observed.digest_for(expected), expected.account_digest):
        raise OperationalIdentityError(
            IdentityRejection.ACCOUNT_MISMATCH,
            "authenticated account is not the contracted account",
        )
    if not hmac.compare_digest(observed.credential_fingerprint, expected.credential_fingerprint):
        raise OperationalIdentityError(IdentityRejection.CREDENTIAL_FINGERPRINT_MISMATCH)

    if observed.credential_generation < expected.credential_generation:
        raise OperationalIdentityError(
            IdentityRejection.CREDENTIAL_GENERATION_ROTATED,
            f"observed {observed.credential_generation} < approved "
            f"{expected.credential_generation}",
        )
    if observed.credential_generation > expected.credential_generation:
        # A newer generation is not automatically better: it was never approved, and an
        # authorization issued under the old one does not describe it.
        raise OperationalIdentityError(
            IdentityRejection.CREDENTIAL_GENERATION_AHEAD,
            f"observed {observed.credential_generation} > approved "
            f"{expected.credential_generation}; re-approve before use",
        )

    if (
        expected.required_purpose is CredentialPurpose.TRADING
        and observed.credential_purpose is not CredentialPurpose.TRADING
    ):
        raise OperationalIdentityError(
            IdentityRejection.CREDENTIAL_PURPOSE_INSUFFICIENT,
            f"{observed.credential_purpose.value} cannot satisfy TRADING",
        )

    if observed.status.strip().upper() not in _ACTIVE_STATUSES:
        raise OperationalIdentityError(IdentityRejection.ACCOUNT_NOT_ACTIVE, observed.status)
    if observed.trading_blocked:
        raise OperationalIdentityError(IdentityRejection.ACCOUNT_TRADING_BLOCKED)

    if expected.currency != "UNSPECIFIED" and observed.currency.strip().upper() != (
        expected.currency.strip().upper()
    ):
        raise OperationalIdentityError(
            IdentityRejection.CURRENCY_MISMATCH, f"{observed.currency} is not {expected.currency}"
        )
    if observed.release_sha.strip() != expected.release_sha.strip():
        raise OperationalIdentityError(IdentityRejection.RELEASE_MISMATCH)

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    age = now.astimezone(UTC) - observed.observed_at.astimezone(UTC)
    if age > maximum_observation_age:
        raise OperationalIdentityError(IdentityRejection.OBSERVATION_STALE, f"observed {age} ago")

    if paired is not None:
        paired.validate()
        if not hmac.compare_digest(paired.digest_for(expected), expected.account_digest):
            raise OperationalIdentityError(
                IdentityRejection.PAIRED_CREDENTIAL_ACCOUNT_MISMATCH,
                "read-only and trading credentials resolve to different accounts",
            )

    verification = IdentityVerification(
        binding=expected.binding,
        verified_at=now.astimezone(UTC),
        environment=expected.environment,
        credential_generation=expected.credential_generation,
    )
    verification.validate()
    return verification
