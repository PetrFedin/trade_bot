from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

_ADAPTER_CONFORMANCE_SCHEMA = "astra-adapter-conformance-result-v1"


class AdapterConformanceStatus(StrEnum):
    QUALIFIED = "QUALIFIED"
    REJECTED = "REJECTED"


def _canonical_json(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def evidence_sha256(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _digest(value: str, name: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(\n        character not in "0123456789abcdef" for character in normalized\n    ):
        raise ValueError(f"{name} must be a sha256 digest")
    return normalized


@dataclass(frozen=True)
class AdapterConformanceCheck:
    check_id: str
    passed: bool
    evidence_sha256: str
    reason: str | None = None

    def validate(self) -> None:
        if not self.check_id.strip():
            raise ValueError("adapter conformance check_id is required")
        _digest(self.evidence_sha256, "evidence_sha256")
        if self.passed and self.reason is not None:
            raise ValueError("passing adapter conformance check cannot carry a failure reason")
        if not self.passed and (self.reason is None or not self.reason.strip()):
            raise ValueError("failed adapter conformance check requires a reason")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "check_id": self.check_id,
            "passed": self.passed,
            "evidence_sha256": self.evidence_sha256,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class AdapterConformanceResult:
    profile_id: str
    profile_version: str
    provider: str
    subject: str
    subject_version: str
    scope: str
    environment: str
    checks: tuple[AdapterConformanceCheck, ...]
    limitations: tuple[str, ...]
    schema_version: str = _ADAPTER_CONFORMANCE_SCHEMA

    def validate(self) -> None:
        if self.schema_version != _ADAPTER_CONFORMANCE_SCHEMA:
            raise ValueError("adapter conformance schema mismatch")
        for name, value in (
            ("profile_id", self.profile_id),
            ("profile_version", self.profile_version),
            ("provider", self.provider),
            ("subject", self.subject),
            ("subject_version", self.subject_version),
            ("scope", self.scope),
            ("environment", self.environment),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")
        if not self.checks:
            raise ValueError("adapter conformance result requires checks")
        ids: set[str] = set()
        for check in self.checks:
            check.validate()
            if check.check_id in ids:
                raise ValueError("adapter conformance check IDs must be unique")
            ids.add(check.check_id)
        if len(set(self.limitations)) != len(self.limitations):
            raise ValueError("adapter conformance limitations must be unique")
        if any(not item.strip() for item in self.limitations):
            raise ValueError("adapter conformance limitations cannot be blank")

    @property
    def qualified(self) -> bool:
        self.validate()
        return all(check.passed for check in self.checks)

    @property
    def status(self) -> AdapterConformanceStatus:
        return (
            AdapterConformanceStatus.QUALIFIED
            if self.qualified
            else AdapterConformanceStatus.REJECTED
        )

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "provider": self.provider,
            "subject": self.subject,
            "subject_version": self.subject_version,
            "scope": self.scope,
            "environment": self.environment,
            "status": self.status.value,
            "limitations": list(self.limitations),
            "checks": [check.payload() for check in self.checks],
        }

    @property
    def result_sha256(self) -> str:
        return evidence_sha256(self.payload())
