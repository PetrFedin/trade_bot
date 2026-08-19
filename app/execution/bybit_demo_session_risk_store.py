from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from app.execution.bybit_demo_session_risk_ledger import (
    BybitDemoSessionRiskLedger,
    BybitDemoSessionTradeOutcome,
)

_SCHEMA_VERSION = 1
_KIND = "BYBIT_DEMO_SESSION_RISK_LEDGER"


@dataclass(frozen=True)
class BybitDemoSessionRiskLedgerCheckpoint:
    ledger: BybitDemoSessionRiskLedger
    revision: str

    def validate(self) -> None:
        self.ledger.validate()
        if len(self.revision) != 64 or any(
            character not in "0123456789abcdef" for character in self.revision
        ):
            raise ValueError("demo session ledger checkpoint revision must be sha256 hex")


class BybitDemoSessionRiskLedgerStore(Protocol):
    live_mainnet_order_routing_allowed: bool
    order_writes_supported: bool

    def load(
        self,
        *,
        expected_opening_equity_usdt: Decimal,
    ) -> BybitDemoSessionRiskLedgerCheckpoint: ...

    def initialize(
        self,
        ledger: BybitDemoSessionRiskLedger,
    ) -> BybitDemoSessionRiskLedgerCheckpoint: ...

    def save(
        self,
        ledger: BybitDemoSessionRiskLedger,
        *,
        expected_revision: str,
    ) -> BybitDemoSessionRiskLedgerCheckpoint: ...


class JsonFileBybitDemoSessionRiskLedgerStore:
    """Atomic, checksummed demo-risk checkpoint with optimistic concurrency.

    The file contains no credentials or exchange secrets. Missing checkpoints never auto-create
    during ``load``: starting a new trading session must call ``initialize`` explicitly, so a
    restart cannot silently reset the loss streak, realized-cost counters, or equity high-water.
    """

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        if not self._path.name:
            raise ValueError("demo session ledger path must name a file")

    @property
    def path(self) -> Path:
        return self._path

    def load(
        self,
        *,
        expected_opening_equity_usdt: Decimal,
    ) -> BybitDemoSessionRiskLedgerCheckpoint:
        _validate_expected_opening_equity(expected_opening_equity_usdt)
        self._reject_symlink()
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise RuntimeError("demo session ledger checkpoint could not be read") from exc
        checkpoint = _decode_checkpoint(raw)
        if checkpoint.ledger.opening_equity_usdt != expected_opening_equity_usdt:
            raise ValueError("demo session ledger checkpoint opening equity mismatch")
        return checkpoint

    def initialize(
        self,
        ledger: BybitDemoSessionRiskLedger,
    ) -> BybitDemoSessionRiskLedgerCheckpoint:
        ledger.validate()
        self._reject_symlink()
        if self._path.exists():
            raise FileExistsError("demo session ledger checkpoint already exists")
        return self._atomic_write(ledger, expected_revision=None)

    def save(
        self,
        ledger: BybitDemoSessionRiskLedger,
        *,
        expected_revision: str,
    ) -> BybitDemoSessionRiskLedgerCheckpoint:
        ledger.validate()
        _validate_revision(expected_revision)
        current = self.load(expected_opening_equity_usdt=ledger.opening_equity_usdt)
        if current.revision != expected_revision:
            raise RuntimeError("demo session ledger checkpoint revision changed concurrently")
        return self._atomic_write(ledger, expected_revision=expected_revision)

    def _atomic_write(
        self,
        ledger: BybitDemoSessionRiskLedger,
        *,
        expected_revision: str | None,
    ) -> BybitDemoSessionRiskLedgerCheckpoint:
        payload, revision = _encode_checkpoint(ledger)
        parent = self._path.parent
        parent.mkdir(parents=True, exist_ok=True)
        self._reject_symlink()
        temporary = parent / f".{self._path.name}.{os.getpid()}.{revision[:12]}.tmp"
        if temporary.exists() or temporary.is_symlink():
            raise RuntimeError("demo session ledger temporary checkpoint already exists")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        descriptor = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if expected_revision is not None:
                latest = self.load(
                    expected_opening_equity_usdt=ledger.opening_equity_usdt
                )
                if latest.revision != expected_revision:
                    raise RuntimeError(
                        "demo session ledger checkpoint revision changed before replace"
                    )
            elif self._path.exists():
                raise FileExistsError("demo session ledger checkpoint appeared concurrently")
            os.replace(temporary, self._path)
            try:
                directory_fd = os.open(parent, os.O_RDONLY)
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        checkpoint = _decode_checkpoint(payload)
        if checkpoint.revision != revision:
            raise RuntimeError("demo session ledger persisted revision mismatch")
        return checkpoint

    def _reject_symlink(self) -> None:
        if self._path.is_symlink():
            raise ValueError("demo session ledger checkpoint cannot be a symlink")


def _encode_checkpoint(ledger: BybitDemoSessionRiskLedger) -> tuple[str, str]:
    ledger.validate()
    ledger_payload = {
        "opening_equity_usdt": str(ledger.opening_equity_usdt),
        "peak_equity_usdt": str(ledger.effective_peak_equity_usdt),
        "outcomes": [
            {
                "entry_order_link_id": outcome.entry_order_link_id,
                "symbol": outcome.symbol,
                "created_time_ms": outcome.created_time_ms,
                "updated_time_ms": outcome.updated_time_ms,
                "all_in_net_pnl_usdt": str(outcome.all_in_net_pnl_usdt),
                "execution_fees_usdt": str(outcome.execution_fees_usdt),
            }
            for outcome in ledger.outcomes
        ],
    }
    canonical_ledger = json.dumps(
        ledger_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    revision = hashlib.sha256(canonical_ledger.encode("utf-8")).hexdigest()
    envelope = {
        "schema_version": _SCHEMA_VERSION,
        "kind": _KIND,
        "demo_only": True,
        "live_mainnet_order_routing_allowed": False,
        "ledger_revision_sha256": revision,
        "ledger": ledger_payload,
    }
    return (
        json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n",
        revision,
    )


def _decode_checkpoint(raw: str) -> BybitDemoSessionRiskLedgerCheckpoint:
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("demo session ledger checkpoint is invalid JSON") from exc
    if not isinstance(envelope, dict):
        raise ValueError("demo session ledger checkpoint must be an object")
    if envelope.get("schema_version") != _SCHEMA_VERSION or envelope.get("kind") != _KIND:
        raise ValueError("demo session ledger checkpoint schema is unsupported")
    if envelope.get("demo_only") is not True:
        raise ValueError("demo session ledger checkpoint lost demo-only marker")
    if envelope.get("live_mainnet_order_routing_allowed") is not False:
        raise ValueError("demo session ledger checkpoint cannot permit live routing")
    ledger_payload = envelope.get("ledger")
    if not isinstance(ledger_payload, dict):
        raise ValueError("demo session ledger checkpoint is missing ledger object")
    canonical_ledger = json.dumps(
        ledger_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    revision = hashlib.sha256(canonical_ledger.encode("utf-8")).hexdigest()
    stored_revision = envelope.get("ledger_revision_sha256")
    if stored_revision != revision:
        raise ValueError("demo session ledger checkpoint checksum mismatch")
    outcomes_raw = ledger_payload.get("outcomes")
    if not isinstance(outcomes_raw, list):
        raise ValueError("demo session ledger checkpoint outcomes must be an array")
    opening_equity = _decimal_field(ledger_payload, "opening_equity_usdt")
    peak_equity = _optional_decimal_field(ledger_payload, "peak_equity_usdt")
    ledger = BybitDemoSessionRiskLedger(
        opening_equity_usdt=opening_equity,
        outcomes=tuple(_decode_outcome(value) for value in outcomes_raw),
        peak_equity_usdt=peak_equity,
    )
    ledger.validate()
    checkpoint = BybitDemoSessionRiskLedgerCheckpoint(ledger=ledger, revision=revision)
    checkpoint.validate()
    return checkpoint


def _decode_outcome(value: Any) -> BybitDemoSessionTradeOutcome:
    if not isinstance(value, dict):
        raise ValueError("demo session ledger outcome must be an object")
    outcome = BybitDemoSessionTradeOutcome(
        entry_order_link_id=_text_field(value, "entry_order_link_id"),
        symbol=_text_field(value, "symbol"),
        created_time_ms=_int_field(value, "created_time_ms"),
        updated_time_ms=_int_field(value, "updated_time_ms"),
        all_in_net_pnl_usdt=_decimal_field(value, "all_in_net_pnl_usdt"),
        execution_fees_usdt=_decimal_field(value, "execution_fees_usdt"),
    )
    outcome.validate()
    return outcome


def _text_field(value: dict[str, Any], field: str) -> str:
    raw = value.get(field)
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"demo session ledger checkpoint missing {field}")
    return raw


def _int_field(value: dict[str, Any], field: str) -> int:
    raw = value.get(field)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f"demo session ledger checkpoint invalid {field}")
    return raw


def _decimal_field(value: dict[str, Any], field: str) -> Decimal:
    raw = value.get(field)
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"demo session ledger checkpoint missing {field}")
    try:
        parsed = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"demo session ledger checkpoint invalid {field}") from exc
    if not parsed.is_finite():
        raise ValueError(f"demo session ledger checkpoint non-finite {field}")
    return parsed


def _optional_decimal_field(value: dict[str, Any], field: str) -> Decimal | None:
    if field not in value:
        return None
    return _decimal_field(value, field)


def _validate_expected_opening_equity(value: Decimal) -> None:
    if not value.is_finite() or value <= 0:
        raise ValueError("expected demo session opening equity must be positive and finite")


def _validate_revision(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("expected demo session ledger revision must be sha256 hex")
