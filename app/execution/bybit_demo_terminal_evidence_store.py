from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from app.execution.bybit_demo_profit_preservation_evidence import (
    BybitDemoProfitPreservationEvidence,
)

_SCHEMA_VERSION = 1
_KIND = "BYBIT_DEMO_TERMINAL_PROFIT_EVIDENCE"


@dataclass(frozen=True)
class BybitDemoTerminalEvidenceReceipt:
    entry_order_link_id: str
    checkpoint_revision: str
    record_sha256: str
    idempotent_existing_record: bool
    live_mainnet_order_routing_allowed: bool = False


class JsonFileBybitDemoTerminalEvidenceStore:
    """Immutable per-trade terminal evidence records for crash-safe final handoff.

    A record is keyed by a hash of the demo entry orderLinkId. The payload includes the exact
    terminal excursion checkpoint revision plus the fully reconciled MFE-to-all-in evidence.
    Repeating the same write is idempotent. Reusing the same entry orderLinkId with different
    evidence or a different checkpoint revision is rejected rather than overwritten.
    """

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    immutable_records = True

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root)
        if not self._root.name:
            raise ValueError("terminal evidence root must name a directory")

    @property
    def root(self) -> Path:
        return self._root

    def persist(
        self,
        *,
        entry_order_link_id: str,
        checkpoint_revision: str,
        evidence: BybitDemoProfitPreservationEvidence,
    ) -> BybitDemoTerminalEvidenceReceipt:
        _validate_identity(entry_order_link_id, checkpoint_revision, evidence)
        self._reject_unsafe_root()
        canonical, envelope, record_sha = _encode_record(
            entry_order_link_id=entry_order_link_id,
            checkpoint_revision=checkpoint_revision,
            evidence=evidence,
        )
        self._root.mkdir(parents=True, exist_ok=True)
        self._reject_unsafe_root()
        target = self._record_path(entry_order_link_id)
        if target.is_symlink():
            raise ValueError("terminal evidence record cannot be a symlink")
        if target.exists():
            return self._load_and_compare(
                target,
                expected_canonical=canonical,
                entry_order_link_id=entry_order_link_id,
                checkpoint_revision=checkpoint_revision,
                idempotent=True,
            )

        temporary = self._root / f".{target.name}.{os.getpid()}.{record_sha[:12]}.tmp"
        if temporary.exists() or temporary.is_symlink():
            raise RuntimeError("terminal evidence temporary record already exists")
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
                    entry_order_link_id=entry_order_link_id,
                    checkpoint_revision=checkpoint_revision,
                    idempotent=True,
                )
            self._fsync_root()
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

        return BybitDemoTerminalEvidenceReceipt(
            entry_order_link_id=entry_order_link_id,
            checkpoint_revision=checkpoint_revision,
            record_sha256=record_sha,
            idempotent_existing_record=False,
        )

    def _load_and_compare(
        self,
        path: Path,
        *,
        expected_canonical: str,
        entry_order_link_id: str,
        checkpoint_revision: str,
        idempotent: bool,
    ) -> BybitDemoTerminalEvidenceReceipt:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError("terminal evidence record could not be read") from exc
        canonical, stored_sha = _decode_record(raw)
        if canonical != expected_canonical:
            raise RuntimeError("terminal evidence conflict for existing entry orderLinkId")
        return BybitDemoTerminalEvidenceReceipt(
            entry_order_link_id=entry_order_link_id,
            checkpoint_revision=checkpoint_revision,
            record_sha256=stored_sha,
            idempotent_existing_record=idempotent,
        )

    def _record_path(self, entry_order_link_id: str) -> Path:
        digest = hashlib.sha256(entry_order_link_id.encode()).hexdigest()
        return self._root / f"{digest}.json"

    def _reject_unsafe_root(self) -> None:
        if self._root.is_symlink():
            raise ValueError("terminal evidence root cannot be a symlink")

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
    *,
    entry_order_link_id: str,
    checkpoint_revision: str,
    evidence: BybitDemoProfitPreservationEvidence,
) -> tuple[str, str, str]:
    record = {
        "entry_order_link_id": entry_order_link_id,
        "checkpoint_revision": checkpoint_revision,
        "evidence": _evidence_payload(evidence),
    }
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    record_sha = hashlib.sha256(canonical.encode()).hexdigest()
    envelope = json.dumps(
        {
            "schema_version": _SCHEMA_VERSION,
            "kind": _KIND,
            "demo_only": True,
            "immutable": True,
            "live_mainnet_order_routing_allowed": False,
            "record_sha256": record_sha,
            "record": record,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return canonical, envelope + "\n", record_sha


def _decode_record(raw: str) -> tuple[str, str]:
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("terminal evidence record is invalid JSON") from exc
    if not isinstance(envelope, dict):
        raise ValueError("terminal evidence record must be an object")
    if envelope.get("schema_version") != _SCHEMA_VERSION or envelope.get("kind") != _KIND:
        raise ValueError("terminal evidence record schema is unsupported")
    if envelope.get("demo_only") is not True or envelope.get("immutable") is not True:
        raise ValueError("terminal evidence record lost immutable demo markers")
    if envelope.get("live_mainnet_order_routing_allowed") is not False:
        raise ValueError("terminal evidence record cannot permit live routing")
    record = envelope.get("record")
    if not isinstance(record, dict):
        raise ValueError("terminal evidence record payload is missing")
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    record_sha = hashlib.sha256(canonical.encode()).hexdigest()
    if envelope.get("record_sha256") != record_sha:
        raise ValueError("terminal evidence record checksum mismatch")
    return canonical, record_sha


def _validate_identity(
    entry_order_link_id: str,
    checkpoint_revision: str,
    evidence: BybitDemoProfitPreservationEvidence,
) -> None:
    if not entry_order_link_id.startswith("ASTRA-DEMO-"):
        raise ValueError("terminal evidence requires ASTRA-DEMO entry orderLinkId")
    if len(checkpoint_revision) != 64 or any(
        character not in "0123456789abcdef" for character in checkpoint_revision
    ):
        raise ValueError("terminal evidence requires sha256 excursion revision")
    if evidence.live_mainnet_order_routing_allowed:
        raise ValueError("terminal evidence cannot persist live-capable evidence")
    if not evidence.fully_reconciled_all_in or evidence.all_in_net_pnl_usdt is None:
        raise ValueError("terminal evidence store requires fully reconciled all-in evidence")
    if not evidence.diagnostics_only or evidence.exit_threshold_retuning_allowed:
        raise ValueError("terminal evidence store requires diagnostics-only evidence")


def _evidence_payload(evidence: BybitDemoProfitPreservationEvidence) -> dict[str, object]:
    return {
        "symbol": evidence.symbol,
        "side": evidence.side.value,
        "observation_count": evidence.observation_count,
        "observed_peak_favorable_r": str(evidence.observed_peak_favorable_r),
        "observed_max_adverse_r": str(evidence.observed_max_adverse_r),
        "realized_gross_exit_r": str(evidence.realized_gross_exit_r),
        "observed_peak_capture_fraction": _optional_decimal(
            evidence.observed_peak_capture_fraction
        ),
        "giveback_from_observed_peak_to_exit_r": str(
            evidence.giveback_from_observed_peak_to_exit_r
        ),
        "exit_exceeded_observed_peak": evidence.exit_exceeded_observed_peak,
        "partial_close_seen": evidence.partial_close_seen,
        "realized_gross_pnl_usdt": str(evidence.realized_gross_pnl_usdt),
        "realized_net_after_execution_fees_usdt": str(
            evidence.realized_net_after_execution_fees_usdt
        ),
        "execution_fees_usdt": str(evidence.execution_fees_usdt),
        "account_closed_pnl_usdt": _optional_decimal(evidence.account_closed_pnl_usdt),
        "funding_net_usdt": _optional_decimal(evidence.funding_net_usdt),
        "all_in_net_pnl_usdt": _optional_decimal(evidence.all_in_net_pnl_usdt),
        "profit_outcome_status": evidence.profit_outcome_status.value,
        "positive_peak_nonpositive_gross_exit": (
            evidence.positive_peak_nonpositive_gross_exit
        ),
        "gross_positive_fill_nonpositive": evidence.gross_positive_fill_nonpositive,
        "fill_positive_account_nonpositive": evidence.fill_positive_account_nonpositive,
        "account_positive_all_in_nonpositive": evidence.account_positive_all_in_nonpositive,
        "positive_peak_nonpositive_all_in": evidence.positive_peak_nonpositive_all_in,
        "fully_reconciled_all_in": evidence.fully_reconciled_all_in,
        "diagnostics_only": evidence.diagnostics_only,
        "exit_threshold_retuning_allowed": evidence.exit_threshold_retuning_allowed,
        "strategy_promotion_allowed": evidence.strategy_promotion_allowed,
        "live_mainnet_order_routing_allowed": evidence.live_mainnet_order_routing_allowed,
    }


def _optional_decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)
