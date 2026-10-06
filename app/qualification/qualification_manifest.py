from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime

from app.qualification.adapter_conformance import AdapterConformanceReport
from app.qualification.bybit_provider_replay import BybitProviderReplayEvidence
from app.qualification.marketdata_integrity import (
    MarketDataIntegrityDecision,
    MarketDataIntegrityStatus,
)
from app.qualification.qualification_job import (
    QualificationJobResult,
    QualificationJobStatus,
)

_SCHEMA_VERSION = "astra-qualification-manifest-v1"
_SCOPE = "PUBLIC_MARKET_DATA_ONLY"

_REQUIRED_ASSERTIONS = (
    "PROVIDER_COMPLETE_REPLAY_BOUND",
    "ADAPTER_CONFORMANCE_PASS",
    "MARKET_DATA_INTEGRITY_HEALTHY",
    "READ_ONLY_PUBLIC_MARKETDATA_SCOPE",
)
_REQUIRED_LIMITATIONS = (
    "EXECUTION_ORDER_SUBMIT_NOT_QUALIFIED",
    "EXECUTION_CANCEL_REPLACE_NOT_QUALIFIED",
    "EXECUTION_FILL_STATUS_MAPPING_NOT_QUALIFIED",
    "ACCOUNT_RECONCILIATION_NOT_QUALIFIED",
    "PROFITABILITY_NOT_PROVEN",
    "REGULATORY_CERTIFICATION_NOT_CLAIMED",
)


@dataclass(frozen=True)
class QualificationManifest:
    job_id: str
    job_result_sha256: str
    request_sha256: str
    organisation_id: str
    subject: str
    subject_version: str
    profile_id: str
    profile_version: str
    environment: str
    corpus_ids: tuple[str, ...]
    provider_replay_sha256: str
    adapter_conformance_sha256: str
    marketdata_integrity_sha256: str
    continuity_checkpoint_id: str
    provider: str
    venue: str
    symbol: str
    interval_seconds: int
    scope: str
    assertions: tuple[str, ...]
    limitations: tuple[str, ...]
    issued_at: datetime
    schema_version: str = _SCHEMA_VERSION

    def validate(self) -> None:
        if self.schema_version != _SCHEMA_VERSION:
            raise ValueError("qualification manifest schema mismatch")
        for name, value in (
            ("job_id", self.job_id),
            ("organisation_id", self.organisation_id),
            ("subject", self.subject),
            ("subject_version", self.subject_version),
            ("profile_id", self.profile_id),
            ("profile_version", self.profile_version),
            ("environment", self.environment),
            ("continuity_checkpoint_id", self.continuity_checkpoint_id),
            ("provider", self.provider),
            ("venue", self.venue),
            ("symbol", self.symbol),
            ("scope", self.scope),
        ):
            if not value.strip():
                raise ValueError(f"{name} is required")
        if not self.job_id.startswith("qjob_"):
            raise ValueError("qualification manifest requires a qualification job id")
        if self.scope != _SCOPE:
            raise ValueError("qualification manifest scope mismatch")
        if self.symbol != self.symbol.upper():
            raise ValueError("qualification manifest symbol must be uppercase")
        if self.interval_seconds < 1:
            raise ValueError("qualification manifest interval_seconds must be positive")
        for name, value in (
            ("job_result_sha256", self.job_result_sha256),
            ("request_sha256", self.request_sha256),
            ("provider_replay_sha256", self.provider_replay_sha256),
            ("adapter_conformance_sha256", self.adapter_conformance_sha256),
            ("marketdata_integrity_sha256", self.marketdata_integrity_sha256),
        ):
            _digest(value, name)
        if not self.corpus_ids:
            raise ValueError("qualification manifest requires corpus IDs")
        if len(set(self.corpus_ids)) != len(self.corpus_ids):
            raise ValueError("qualification manifest corpus IDs must be unique")
        if any(not item.strip() for item in self.corpus_ids):
            raise ValueError("qualification manifest corpus IDs cannot be blank")
        if self.assertions != _REQUIRED_ASSERTIONS:
            raise ValueError("qualification manifest assertions mismatch")
        if self.limitations != _REQUIRED_LIMITATIONS:
            raise ValueError("qualification manifest limitations mismatch")
        _aware(self.issued_at, "issued_at")

    def unsigned_payload(self) -> dict[str, object]:
        self.validate()
        return {
            "schema_version": self.schema_version,
            "job_id": self.job_id,
            "job_result_sha256": self.job_result_sha256,
            "request_sha256": self.request_sha256,
            "organisation_id": self.organisation_id,
            "subject": self.subject,
            "subject_version": self.subject_version,
            "profile_id": self.profile_id,
            "profile_version": self.profile_version,
            "environment": self.environment,
            "corpus_ids": list(self.corpus_ids),
            "provider_replay_sha256": self.provider_replay_sha256,
            "adapter_conformance_sha256": self.adapter_conformance_sha256,
            "marketdata_integrity_sha256": self.marketdata_integrity_sha256,
            "continuity_checkpoint_id": self.continuity_checkpoint_id,
            "provider": self.provider,
            "venue": self.venue,
            "symbol": self.symbol,
            "interval_seconds": self.interval_seconds,
            "scope": self.scope,
            "assertions": list(self.assertions),
            "limitations": list(self.limitations),
            "issued_at": _aware(self.issued_at, "issued_at").isoformat(),
        }

    @property
    def manifest_sha256(self) -> str:
        return _sha256(self.unsigned_payload())

    @property
    def manifest_id(self) -> str:
        return f"qmanifest_{self.manifest_sha256[:24]}"

    def payload(self) -> dict[str, object]:
        return {
            "manifest_id": self.manifest_id,
            **self.unsigned_payload(),
            "manifest_sha256": self.manifest_sha256,
        }


def build_qualification_manifest(
    *,
    job_result: QualificationJobResult,
    adapter_conformance: AdapterConformanceReport,
    marketdata_integrity: MarketDataIntegrityDecision,
    provider_replay: BybitProviderReplayEvidence,
) -> QualificationManifest:
    """Build a reproducible manifest only for an exact successful qualification chain."""

    job_result.validate()
    adapter_conformance.validate()
    marketdata_integrity.validate()
    provider_replay.validate()

    if job_result.status is not QualificationJobStatus.PASS:
        raise ValueError("qualification manifest requires a PASS job")
    if not adapter_conformance.qualified:
        raise ValueError("qualification manifest requires qualified adapter conformance")
    if marketdata_integrity.status is not MarketDataIntegrityStatus.HEALTHY:
        raise ValueError("qualification manifest requires HEALTHY market data integrity")

    request = job_result.request
    if adapter_conformance.profile_id != request.profile_id:
        raise ValueError("qualification manifest profile_id mismatch")
    if adapter_conformance.profile_version != request.profile_version:
        raise ValueError("qualification manifest profile_version mismatch")
    if adapter_conformance.subject != request.subject:
        raise ValueError("qualification manifest subject mismatch")
    if adapter_conformance.subject_version != request.subject_version:
        raise ValueError("qualification manifest subject_version mismatch")
    if job_result.adapter_conformance_sha256 != adapter_conformance.evidence_sha256:
        raise ValueError("qualification manifest adapter evidence mismatch")
    if job_result.marketdata_integrity_sha256 != marketdata_integrity.evidence_sha256:
        raise ValueError("qualification manifest integrity evidence mismatch")
    if marketdata_integrity.adapter_conformance_sha256 != adapter_conformance.evidence_sha256:
        raise ValueError("qualification manifest evidence chain mismatch")
    if marketdata_integrity.provider_replay_sha256 != provider_replay.evidence_sha256:
        raise ValueError("qualification manifest provider replay mismatch")
    if marketdata_integrity.continuity_checkpoint_id != provider_replay.continuity_checkpoint_id:
        raise ValueError("qualification manifest continuity mismatch")

    expected_corpus = (provider_replay.replay.dataset_id,)
    if request.corpus_ids != expected_corpus:
        raise ValueError("qualification manifest corpus does not match provider replay")

    manifest = QualificationManifest(
        job_id=job_result.job_id,
        job_result_sha256=job_result.result_sha256,
        request_sha256=request.request_sha256,
        organisation_id=request.organisation_id,
        subject=request.subject,
        subject_version=request.subject_version,
        profile_id=request.profile_id,
        profile_version=request.profile_version,
        environment=request.environment,
        corpus_ids=request.corpus_ids,
        provider_replay_sha256=provider_replay.evidence_sha256,
        adapter_conformance_sha256=adapter_conformance.evidence_sha256,
        marketdata_integrity_sha256=marketdata_integrity.evidence_sha256,
        continuity_checkpoint_id=marketdata_integrity.continuity_checkpoint_id,
        provider=marketdata_integrity.provider,
        venue=marketdata_integrity.venue,
        symbol=marketdata_integrity.symbol,
        interval_seconds=marketdata_integrity.interval_seconds,
        scope=_SCOPE,
        assertions=_REQUIRED_ASSERTIONS,
        limitations=_REQUIRED_LIMITATIONS,
        issued_at=job_result.completed_at,
    )
    manifest.validate()
    return manifest


def _aware(value: datetime, name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
    ).hexdigest()


def _digest(value: str, name: str) -> str:
    normalized = value.strip().lower()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ValueError(f"{name} must be a sha256 digest")
    return normalized
