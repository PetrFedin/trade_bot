from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.execution.bybit_demo_approval_lineage import (
    BybitDemoApprovedEntryAuthorization,
    validate_bybit_demo_approved_entry_authorization,
)

_SCHEMA_VERSION = 1
_KIND = "BYBIT_DEMO_APPROVED_ENTRY_AUTHORIZATION"


@dataclass(frozen=True)
class BybitDemoApprovedEntryAuthorizationReceipt:
    entry_order_link_id: str
    approval_id: str
    record_sha256: str
    idempotent_existing_record: bool
    live_mainnet_order_routing_allowed: bool = False


@dataclass(frozen=True)
class BybitDemoApprovedEntryAuthorizationRecord:
    authorization: BybitDemoApprovedEntryAuthorization
    record_sha256: str
    live_mainnet_order_routing_allowed: bool = False


class JsonFileBybitDemoApprovedEntryAuthorizationStore:
    """Immutable pre-submit evidence approval lineage keyed by Demo entry orderLinkId."""

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    order_submission_supported = False
    immutable_records = True
    outcome_storage_allowed = False
    realized_pnl_storage_allowed = False

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root)
        if not self._root.name:
            raise ValueError("approved entry authorization root must name a directory")

    @property
    def root(self) -> Path:
        return self._root

    def persist(
        self,
        authorization: BybitDemoApprovedEntryAuthorization,
    ) -> BybitDemoApprovedEntryAuthorizationReceipt:
        validate_bybit_demo_approved_entry_authorization(authorization)
        self._reject_unsafe_root()
        canonical, envelope, record_sha = _encode_record(authorization)
        self._root.mkdir(parents=True, exist_ok=True)
        self._reject_unsafe_root()
        target = self._record_path(authorization.expected_entry_order_link_id)
        if target.is_symlink():
            raise ValueError("approved entry authorization record cannot be a symlink")
        if target.exists():
            return self._load_and_compare(
                target,
                expected_canonical=canonical,
                authorization=authorization,
                idempotent=True,
            )

        temporary = self._root / f".{target.name}.{os.getpid()}.{record_sha[:12]}.tmp"
        if temporary.exists() or temporary.is_symlink():
            raise RuntimeError("approved entry authorization temporary record already exists")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as handle:
                handle.write(envelope)
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(temporary, target)
            except FileExistsError:
                return self._load_and_compare(
                    target,
                    expected_canonical=canonical,
                    authorization=authorization,
                    idempotent=True,
                )
            self._fsync_root()
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

        return BybitDemoApprovedEntryAuthorizationReceipt(
            entry_order_link_id=authorization.expected_entry_order_link_id,
            approval_id=authorization.approval_id,
            record_sha256=record_sha,
            idempotent_existing_record=False,
        )

    def load(
        self,
        *,
        entry_order_link_id: str,
    ) -> BybitDemoApprovedEntryAuthorizationRecord:
        _validate_entry_order_link_id(entry_order_link_id)
        self._reject_unsafe_root()
        target = self._record_path(entry_order_link_id)
        if target.is_symlink():
            raise ValueError("approved entry authorization record cannot be a symlink")
        raw = target.read_text(encoding="utf-8")
        payload, _canonical, record_sha = _decode_record(raw)
        authorization = _authorization_from_payload(payload)
        if authorization.expected_entry_order_link_id != entry_order_link_id:
            raise ValueError("approved entry authorization orderLinkId mismatch")
        return BybitDemoApprovedEntryAuthorizationRecord(
            authorization=authorization,
            record_sha256=record_sha,
        )

    def _load_and_compare(
        self,
        path: Path,
        *,
        expected_canonical: str,
        authorization: BybitDemoApprovedEntryAuthorization,
        idempotent: bool,
    ) -> BybitDemoApprovedEntryAuthorizationReceipt:
        raw = path.read_text(encoding="utf-8")
        _payload, canonical, stored_sha = _decode_record(raw)
        if canonical != expected_canonical:
            raise RuntimeError(
                "approved entry authorization conflict for existing entry orderLinkId"
            )
        return BybitDemoApprovedEntryAuthorizationReceipt(
            entry_order_link_id=authorization.expected_entry_order_link_id,
            approval_id=authorization.approval_id,
            record_sha256=stored_sha,
            idempotent_existing_record=idempotent,
        )

    def _record_path(self, entry_order_link_id: str) -> Path:
        digest = hashlib.sha256(entry_order_link_id.encode("utf-8")).hexdigest()
        return self._root / f"{digest}.json"

    def _reject_unsafe_root(self) -> None:
        if self._root.is_symlink():
            raise ValueError("approved entry authorization root cannot be a symlink")

    def _fsync_root(self) -> None:
        try:
            descriptor = os.open(self._root, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _encode_record(
    authorization: BybitDemoApprovedEntryAuthorization,
) -> tuple[str, str, str]:
    payload = asdict(authorization)
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    record_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    envelope = json.dumps(
        {
            "schema_version": _SCHEMA_VERSION,
            "kind": _KIND,
            "demo_only": True,
            "pre_submit_authorization": True,
            "outcome_free": True,
            "immutable": True,
            "order_submission_supported": False,
            "realized_pnl_storage_allowed": False,
            "live_mainnet_order_routing_allowed": False,
            "record_sha256": record_sha,
            "record": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return canonical, envelope + "\n", record_sha


def _decode_record(raw: str) -> tuple[dict[str, Any], str, str]:
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("approved entry authorization record is invalid JSON") from exc
    if not isinstance(envelope, dict):
        raise ValueError("approved entry authorization record must be an object")
    if envelope.get("schema_version") != _SCHEMA_VERSION or envelope.get("kind") != _KIND:
        raise ValueError("approved entry authorization schema is unsupported")
    if envelope.get("demo_only") is not True or envelope.get("pre_submit_authorization") is not True:
        raise ValueError("approved entry authorization lost Demo pre-submit markers")
    if envelope.get("outcome_free") is not True or envelope.get("immutable") is not True:
        raise ValueError("approved entry authorization must remain immutable/outcome-free")
    if envelope.get("order_submission_supported") is not False:
        raise ValueError("approved entry authorization store cannot submit orders")
    if envelope.get("realized_pnl_storage_allowed") is not False:
        raise ValueError("approved entry authorization cannot store realized PnL")
    if envelope.get("live_mainnet_order_routing_allowed") is not False:
        raise ValueError("approved entry authorization cannot permit mainnet routing")
    payload = envelope.get("record")
    if not isinstance(payload, dict):
        raise ValueError("approved entry authorization payload is missing")
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    record_sha = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if envelope.get("record_sha256") != record_sha:
        raise ValueError("approved entry authorization checksum mismatch")
    return payload, canonical, record_sha


def _authorization_from_payload(payload: dict[str, Any]) -> BybitDemoApprovedEntryAuthorization:
    authorization = BybitDemoApprovedEntryAuthorization(
        approval_id=_text(payload, "approval_id"),
        source_snapshot_id=_text(payload, "source_snapshot_id"),
        source_evidence_rank=_integer(payload, "source_evidence_rank"),
        source_market_rank=_integer(payload, "source_market_rank"),
        symbol=_text(payload, "symbol"),
        side=_text(payload, "side"),
        decision_time=_text(payload, "decision_time"),
        signal_available_at=_text(payload, "signal_available_at"),
        approved_at=_text(payload, "approved_at"),
        expires_at=_text(payload, "expires_at"),
        expected_entry_order_link_id=_text(payload, "expected_entry_order_link_id"),
        expected_close_order_link_id=_text(payload, "expected_close_order_link_id"),
        authorized_at=_text(payload, "authorized_at"),
        operator_confirmed=_boolean(payload, "operator_confirmed"),
        environment=_text(payload, "environment"),
        single_use_entry_required=_boolean(payload, "single_use_entry_required"),
        outcome_free=_boolean(payload, "outcome_free"),
        diagnostics_only=_boolean(payload, "diagnostics_only"),
        trade_actionable=_boolean(payload, "trade_actionable"),
        automatic_selector_retuning_allowed=_boolean(
            payload,
            "automatic_selector_retuning_allowed",
        ),
        strategy_promotion_allowed=_boolean(payload, "strategy_promotion_allowed"),
        live_mainnet_order_routing_allowed=_boolean(
            payload,
            "live_mainnet_order_routing_allowed",
        ),
    )
    validate_bybit_demo_approved_entry_authorization(authorization)
    return authorization


def _validate_entry_order_link_id(value: str) -> None:
    if not value.startswith("ASTRA-DEMO-"):
        raise ValueError("approved entry authorization requires ASTRA-DEMO orderLinkId")


def _text(payload: dict[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(f"approved entry authorization missing {field}")
    return value


def _integer(payload: dict[str, Any], field: str) -> int:
    value = payload.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"approved entry authorization invalid {field}")
    return value


def _boolean(payload: dict[str, Any], field: str) -> bool:
    value = payload.get(field)
    if not isinstance(value, bool):
        raise ValueError(f"approved entry authorization invalid {field}")
    return value


__all__ = [
    "BybitDemoApprovedEntryAuthorizationReceipt",
    "BybitDemoApprovedEntryAuthorizationRecord",
    "JsonFileBybitDemoApprovedEntryAuthorizationStore",
]
