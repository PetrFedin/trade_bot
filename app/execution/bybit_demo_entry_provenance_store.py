from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from app.execution.bybit_demo_entry_provenance import BybitDemoEntryDecisionProvenance

_SCHEMA_VERSION = 1
_KIND = "BYBIT_DEMO_ENTRY_DECISION_PROVENANCE"


@dataclass(frozen=True)
class BybitDemoEntryProvenanceReceipt:
    entry_order_link_id: str
    record_sha256: str
    idempotent_existing_record: bool
    live_mainnet_order_routing_allowed: bool = False


class JsonFileBybitDemoEntryProvenanceStore:
    """Immutable outcome-free provenance for each protected demo entry."""

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    immutable_records = True
    realized_pnl_storage_allowed = False

    def __init__(self, root: str | os.PathLike[str]) -> None:
        self._root = Path(root)
        if not self._root.name:
            raise ValueError("entry provenance root must name a directory")

    @property
    def root(self) -> Path:
        return self._root

    def persist(
        self,
        provenance: BybitDemoEntryDecisionProvenance,
    ) -> BybitDemoEntryProvenanceReceipt:
        _validate_provenance(provenance)
        self._reject_unsafe_root()
        canonical, envelope, record_sha = _encode_record(provenance)
        self._root.mkdir(parents=True, exist_ok=True)
        self._reject_unsafe_root()
        target = self._record_path(provenance.entry_order_link_id)
        if target.is_symlink():
            raise ValueError("entry provenance record cannot be a symlink")
        if target.exists():
            return self._load_and_compare(
                target,
                expected_canonical=canonical,
                entry_order_link_id=provenance.entry_order_link_id,
                idempotent=True,
            )

        temporary = self._root / f".{target.name}.{os.getpid()}.{record_sha[:12]}.tmp"
        if temporary.exists() or temporary.is_symlink():
            raise RuntimeError("entry provenance temporary record already exists")
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
                    entry_order_link_id=provenance.entry_order_link_id,
                    idempotent=True,
                )
            self._fsync_root()
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

        return BybitDemoEntryProvenanceReceipt(
            entry_order_link_id=provenance.entry_order_link_id,
            record_sha256=record_sha,
            idempotent_existing_record=False,
        )

    def _load_and_compare(
        self,
        path: Path,
        *,
        expected_canonical: str,
        entry_order_link_id: str,
        idempotent: bool,
    ) -> BybitDemoEntryProvenanceReceipt:
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RuntimeError("entry provenance record could not be read") from exc
        canonical, stored_sha = _decode_record(raw)
        if canonical != expected_canonical:
            raise RuntimeError("entry provenance conflict for existing entry orderLinkId")
        return BybitDemoEntryProvenanceReceipt(
            entry_order_link_id=entry_order_link_id,
            record_sha256=stored_sha,
            idempotent_existing_record=idempotent,
        )

    def _record_path(self, entry_order_link_id: str) -> Path:
        digest = hashlib.sha256(entry_order_link_id.encode()).hexdigest()
        return self._root / f"{digest}.json"

    def _reject_unsafe_root(self) -> None:
        if self._root.is_symlink():
            raise ValueError("entry provenance root cannot be a symlink")

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
    provenance: BybitDemoEntryDecisionProvenance,
) -> tuple[str, str, str]:
    record = _provenance_payload(provenance)
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    record_sha = hashlib.sha256(canonical.encode()).hexdigest()
    envelope = json.dumps(
        {
            "schema_version": _SCHEMA_VERSION,
            "kind": _KIND,
            "demo_only": True,
            "outcome_free": True,
            "immutable": True,
            "realized_pnl_storage_allowed": False,
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
        raise ValueError("entry provenance record is invalid JSON") from exc
    if not isinstance(envelope, dict):
        raise ValueError("entry provenance record must be an object")
    if envelope.get("schema_version") != _SCHEMA_VERSION or envelope.get("kind") != _KIND:
        raise ValueError("entry provenance record schema is unsupported")
    if envelope.get("demo_only") is not True or envelope.get("outcome_free") is not True:
        raise ValueError("entry provenance record lost outcome-free demo markers")
    if envelope.get("immutable") is not True:
        raise ValueError("entry provenance record must remain immutable")
    if envelope.get("realized_pnl_storage_allowed") is not False:
        raise ValueError("entry provenance record cannot store realized PnL")
    if envelope.get("live_mainnet_order_routing_allowed") is not False:
        raise ValueError("entry provenance record cannot permit live routing")
    record = envelope.get("record")
    if not isinstance(record, dict):
        raise ValueError("entry provenance record payload is missing")
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    record_sha = hashlib.sha256(canonical.encode()).hexdigest()
    if envelope.get("record_sha256") != record_sha:
        raise ValueError("entry provenance record checksum mismatch")
    return canonical, record_sha


def _validate_provenance(provenance: BybitDemoEntryDecisionProvenance) -> None:
    if not provenance.entry_order_link_id.startswith("ASTRA-DEMO-"):
        raise ValueError("entry provenance requires ASTRA-DEMO orderLinkId")
    if provenance.live_mainnet_order_routing_allowed:
        raise ValueError("entry provenance store rejected live-capable provenance")
    if provenance.realized_pnl_used_for_selection:
        raise ValueError("entry provenance cannot use realized PnL for selection")
    if not provenance.diagnostics_only or provenance.automatic_selector_retuning_allowed:
        raise ValueError("entry provenance must remain diagnostics-only")
    if provenance.strategy_promotion_allowed:
        raise ValueError("entry provenance cannot authorize strategy promotion")
    if provenance.selected_signal_rank < 1 or provenance.executable_candidate_count < 1:
        raise ValueError("entry provenance selection rank/count are invalid")
    if provenance.actual_average_entry_price <= 0 or provenance.actual_filled_quantity <= 0:
        raise ValueError("entry provenance actual fill must be positive")
    if provenance.actual_fill_notional_usdt <= 0:
        raise ValueError("entry provenance actual notional must be positive")


def _provenance_payload(
    provenance: BybitDemoEntryDecisionProvenance,
) -> dict[str, object]:
    return {
        "entry_order_link_id": provenance.entry_order_link_id,
        "symbol": provenance.symbol,
        "side": provenance.side.value,
        "decision_time": provenance.decision_time,
        "selected_signal_rank": provenance.selected_signal_rank,
        "executable_candidate_count": provenance.executable_candidate_count,
        "candidate_audit_count": provenance.candidate_audit_count,
        "economic_shadow_selected_symbol": provenance.economic_shadow_selected_symbol,
        "economic_shadow_selected_side": provenance.economic_shadow_selected_side,
        "economic_shadow_differs_from_current": (
            provenance.economic_shadow_differs_from_current
        ),
        "selected_after_fallback": provenance.selected_after_fallback,
        "fallback_attempts": [
            {
                "symbol": attempt.symbol,
                "side": attempt.side,
                "stage": attempt.stage.value,
                "reasons": list(attempt.reasons),
                "quote_price": _optional_decimal(attempt.quote_price),
                "modeled_entry_price": _optional_decimal(attempt.modeled_entry_price),
            }
            for attempt in provenance.fallback_attempts
        ],
        "expected_net_edge_usd": str(provenance.expected_net_edge_usd),
        "risk_budget_usdt": str(provenance.risk_budget_usdt),
        "quality_score": str(provenance.quality_score),
        "target_net_profit_usd": str(provenance.target_net_profit_usd),
        "planned_reference_price": str(provenance.planned_reference_price),
        "planned_reference_quantity": str(provenance.planned_reference_quantity),
        "planned_notional_usdt": str(provenance.planned_notional_usdt),
        "modeled_round_trip_cost_usdt": str(provenance.modeled_round_trip_cost_usdt),
        "pre_entry_quote_price": _optional_decimal(provenance.pre_entry_quote_price),
        "pre_entry_modeled_entry_price": _optional_decimal(
            provenance.pre_entry_modeled_entry_price
        ),
        "pre_entry_original_quantity": _optional_decimal(
            provenance.pre_entry_original_quantity
        ),
        "pre_entry_adjusted_quantity": _optional_decimal(
            provenance.pre_entry_adjusted_quantity
        ),
        "pre_entry_quote_resized": provenance.pre_entry_quote_resized,
        "pre_entry_quantity_retention_fraction": _optional_decimal(
            provenance.pre_entry_quantity_retention_fraction
        ),
        "actual_average_entry_price": str(provenance.actual_average_entry_price),
        "actual_filled_quantity": str(provenance.actual_filled_quantity),
        "actual_fill_notional_usdt": str(provenance.actual_fill_notional_usdt),
        "actual_fill_adverse_slippage_bps_vs_modeled_entry": _optional_decimal(
            provenance.actual_fill_adverse_slippage_bps_vs_modeled_entry
        ),
        "account_taker_fee_rate": str(provenance.account_taker_fee_rate),
        "exit_mode": provenance.exit_mode,
        "runner_admission_reasons": list(provenance.runner_admission_reasons),
        "liquidation_safety_reason": provenance.liquidation_safety_reason,
        "stop_to_liquidation_r": _optional_decimal(provenance.stop_to_liquidation_r),
        "effective_account_equity_usdt": str(provenance.effective_account_equity_usdt),
        "effective_peak_equity_usdt": str(provenance.effective_peak_equity_usdt),
        "margin_mode": provenance.margin_mode,
        "realized_pnl_used_for_selection": provenance.realized_pnl_used_for_selection,
        "diagnostics_only": provenance.diagnostics_only,
        "automatic_selector_retuning_allowed": provenance.automatic_selector_retuning_allowed,
        "strategy_promotion_allowed": provenance.strategy_promotion_allowed,
        "live_mainnet_order_routing_allowed": provenance.live_mainnet_order_routing_allowed,
    }


def _optional_decimal(value: object | None) -> str | None:
    return None if value is None else str(value)
