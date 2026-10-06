from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum


class ConformanceStatus(StrEnum):
    PASS = "PASS"  # nosec B105 - conformance status label, not a credential
    FAIL = "FAIL"
    BLOCKED = "BLOCKED"
    NOT_IN_SCOPE = "NOT_IN_SCOPE"


def conformance_evidence_sha256(value: object) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _digest(value: str, name: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{name} must be a sha256 digest")
    return normalized


@dataclass(frozen=True)
class AdapterConformanceCheck:
    check_id: str
    status: ConformanceStatus
    required: bool
    evidence_sha256: str | None
    reason: str | None = None

    def validate(self) -> None:
        if not self.check_id.strip():
            raise ValueError("conformance check_id is required")
        if self.status is ConformanceStatus.NOT_IN_SCOPE:
            if self.required:
                raise ValueError("required conformance checks cannot be NOT_IN_SCOPE")
            if self.evidence_sha256 is not None:
                _digest(self.evidence_sha256, "evidence_sha256")
            return
        if self.evidence_sha256 is None:
            raise ValueError("PASS/FAIL conformance checks require evidence_sha256")
        _digest(self.evidence_sha256, "evidence_sha256")
        if self.status in {ConformanceStatus.FAIL, ConformanceStatus.BLOCKED} and not (
            self.reason or ""
        ).strip():
            raise ValueError("failed or blocked conformance checks require a reason")

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "check_id": self.check_id,
            "status": self.status.value,
            "required": self.required,
            "evidence_sha256": self.evidence_sha256,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class AdapterConformanceReport:
    profile_id: str
    profile_version: str
    subject: str
    subject_version: str
    checks: tuple[AdapterConformanceCheck, ...]

    def validate(self) -> None:
        for name, value in (
            ("profile_id", self.profile_id),
            ("profile_version", self.profile_version),
            ("subject", self.subject),
            ("subject_version", self.subject_version),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")
        if not self.checks:
            raise ValueError("conformance report requires checks")
        ids = [check.check_id for check in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("conformance check ids must be unique")
        if not any(check.required for check in self.checks):
            raise ValueError("conformance report requires at least one mandatory check")
        for check in self.checks:
            check.validate()

    @property
    def qualified(self) -> bool:
        self.validate()
        return all(
            check.status is ConformanceStatus.PASS
            for check in self.checks
            if check.required
        )

    @property
    def failure_reasons(self) -> tuple[str, ...]:
        self.validate()
        return tuple(
            f"{check.check_id}:{check.reason}"
            for check in self.checks
            if check.required
            and check.status in {ConformanceStatus.FAIL, ConformanceStatus.BLOCKED}
        )

    def payload(self) -> dict[str, object]:
        self.validate()
        return {
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "subject": self.subject,
            "subject_version": self.subject_version,
            "qualified": self.qualified,
            "failure_reasons": list(self.failure_reasons),
            "checks": [check.payload() for check in self.checks],
        }

    @property
    def evidence_sha256(self) -> str:
        return conformance_evidence_sha256(self.payload())
