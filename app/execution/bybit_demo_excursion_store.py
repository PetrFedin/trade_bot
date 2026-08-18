from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from app.execution.bybit_demo_excursion_tracker import BybitDemoTradeExcursionState
from app.strategy.crypto_perp import CryptoSide

_SCHEMA_VERSION = 1
_KIND = "BYBIT_DEMO_TRADE_EXCURSION"


@dataclass(frozen=True)
class BybitDemoExcursionCheckpoint:
    entry_order_link_id: str
    state: BybitDemoTradeExcursionState
    revision: str

    def validate(self) -> None:
        if not self.entry_order_link_id.startswith("ASTRA-DEMO-"):
            raise ValueError("demo excursion checkpoint requires ASTRA-DEMO orderLinkId")
        if len(self.revision) != 64 or any(
            character not in "0123456789abcdef" for character in self.revision
        ):
            raise ValueError("demo excursion checkpoint revision must be sha256 hex")
        if self.state.live_mainnet_order_routing_allowed:
            raise ValueError("demo excursion checkpoint cannot permit live routing")
        if not self.state.diagnostics_only or self.state.exit_threshold_retuning_allowed:
            raise ValueError("demo excursion checkpoint lost diagnostics-only contract")


class BybitDemoExcursionStore(Protocol):
    live_mainnet_order_routing_allowed: bool
    order_writes_supported: bool

    def load(self) -> BybitDemoExcursionCheckpoint: ...

    def initialize(
        self,
        *,
        entry_order_link_id: str,
        state: BybitDemoTradeExcursionState,
    ) -> BybitDemoExcursionCheckpoint: ...

    def save(
        self,
        *,
        entry_order_link_id: str,
        state: BybitDemoTradeExcursionState,
        expected_revision: str,
    ) -> BybitDemoExcursionCheckpoint: ...

    def clear(self, *, expected_revision: str) -> None: ...


class JsonFileBybitDemoExcursionStore:
    """Atomic checksummed active-trade excursion checkpoint.

    ``load`` never creates state. A missing checkpoint is explicit evidence that no persisted MFE/
    MAE history is available; callers must not silently reconstruct a zero-peak state for an
    already-open trade. This prevents restarts from making profit capture look better than it was.
    """

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self._path = Path(path)
        if not self._path.name:
            raise ValueError("demo excursion checkpoint path must name a file")

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> BybitDemoExcursionCheckpoint:
        self._reject_symlink()
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise RuntimeError("demo excursion checkpoint could not be read") from exc
        return _decode_checkpoint(raw)

    def initialize(
        self,
        *,
        entry_order_link_id: str,
        state: BybitDemoTradeExcursionState,
    ) -> BybitDemoExcursionCheckpoint:
        _validate_identity(entry_order_link_id, state)
        self._reject_symlink()
        if self._path.exists():
            raise FileExistsError("demo excursion checkpoint already exists")
        return self._atomic_write(
            entry_order_link_id=entry_order_link_id,
            state=state,
            expected_revision=None,
        )

    def save(
        self,
        *,
        entry_order_link_id: str,
        state: BybitDemoTradeExcursionState,
        expected_revision: str,
    ) -> BybitDemoExcursionCheckpoint:
        _validate_identity(entry_order_link_id, state)
        _validate_revision(expected_revision)
        current = self.load()
        if current.entry_order_link_id != entry_order_link_id:
            raise ValueError("demo excursion checkpoint orderLinkId mismatch")
        if current.revision != expected_revision:
            raise RuntimeError("demo excursion checkpoint revision changed concurrently")
        return self._atomic_write(
            entry_order_link_id=entry_order_link_id,
            state=state,
            expected_revision=expected_revision,
        )

    def clear(self, *, expected_revision: str) -> None:
        _validate_revision(expected_revision)
        self._reject_symlink()
        current = self.load()
        if current.revision != expected_revision:
            raise RuntimeError("demo excursion checkpoint revision changed before clear")
        try:
            self._path.unlink()
        except FileNotFoundError as exc:
            raise RuntimeError("demo excursion checkpoint disappeared before clear") from exc
        except OSError as exc:
            raise RuntimeError("demo excursion checkpoint could not be cleared") from exc
        self._fsync_parent()

    def _atomic_write(
        self,
        *,
        entry_order_link_id: str,
        state: BybitDemoTradeExcursionState,
        expected_revision: str | None,
    ) -> BybitDemoExcursionCheckpoint:
        payload, revision = _encode_checkpoint(
            entry_order_link_id=entry_order_link_id,
            state=state,
        )
        parent = self._path.parent
        parent.mkdir(parents=True, exist_ok=True)
        self._reject_symlink()
        temporary = parent / f".{self._path.name}.{os.getpid()}.{revision[:12]}.tmp"
        if temporary.exists() or temporary.is_symlink():
            raise RuntimeError("demo excursion temporary checkpoint already exists")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            if expected_revision is not None:
                latest = self.load()
                if latest.revision != expected_revision:
                    raise RuntimeError(
                        "demo excursion checkpoint revision changed before replace"
                    )
            elif self._path.exists():
                raise FileExistsError("demo excursion checkpoint appeared concurrently")
            os.replace(temporary, self._path)
            self._fsync_parent()
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
        checkpoint = BybitDemoExcursionCheckpoint(
            entry_order_link_id=entry_order_link_id,
            state=state,
            revision=revision,
        )
        checkpoint.validate()
        return checkpoint

    def _reject_symlink(self) -> None:
        if self._path.is_symlink():
            raise ValueError("demo excursion checkpoint cannot be a symlink")

    def _fsync_parent(self) -> None:
        try:
            directory_fd = os.open(self._path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _encode_checkpoint(
    *,
    entry_order_link_id: str,
    state: BybitDemoTradeExcursionState,
) -> tuple[str, str]:
    _validate_identity(entry_order_link_id, state)
    state_payload = _state_payload(state)
    canonical = json.dumps(
        {
            "entry_order_link_id": entry_order_link_id,
            "state": state_payload,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    revision = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    envelope = {
        "schema_version": _SCHEMA_VERSION,
        "kind": _KIND,
        "demo_only": True,
        "diagnostics_only": True,
        "exit_threshold_retuning_allowed": False,
        "live_mainnet_order_routing_allowed": False,
        "revision_sha256": revision,
        "entry_order_link_id": entry_order_link_id,
        "state": state_payload,
    }
    return (
        json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        + "\n",
        revision,
    )


def _decode_checkpoint(raw: str) -> BybitDemoExcursionCheckpoint:
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("demo excursion checkpoint is invalid JSON") from exc
    if not isinstance(envelope, dict):
        raise ValueError("demo excursion checkpoint must be an object")
    if envelope.get("schema_version") != _SCHEMA_VERSION or envelope.get("kind") != _KIND:
        raise ValueError("demo excursion checkpoint schema is unsupported")
    if envelope.get("demo_only") is not True or envelope.get("diagnostics_only") is not True:
        raise ValueError("demo excursion checkpoint lost demo diagnostics markers")
    if envelope.get("exit_threshold_retuning_allowed") is not False:
        raise ValueError("demo excursion checkpoint cannot authorize exit retuning")
    if envelope.get("live_mainnet_order_routing_allowed") is not False:
        raise ValueError("demo excursion checkpoint cannot permit live routing")
    entry_order_link_id = _text_field(envelope, "entry_order_link_id")
    state_raw = envelope.get("state")
    if not isinstance(state_raw, dict):
        raise ValueError("demo excursion checkpoint is missing state object")
    canonical = json.dumps(
        {
            "entry_order_link_id": entry_order_link_id,
            "state": state_raw,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    revision = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if envelope.get("revision_sha256") != revision:
        raise ValueError("demo excursion checkpoint checksum mismatch")
    state = _decode_state(state_raw)
    checkpoint = BybitDemoExcursionCheckpoint(
        entry_order_link_id=entry_order_link_id,
        state=state,
        revision=revision,
    )
    checkpoint.validate()
    return checkpoint


def _state_payload(state: BybitDemoTradeExcursionState) -> dict[str, Any]:
    return {
        "symbol": state.symbol,
        "side": state.side.value,
        "entry_price": str(state.entry_price),
        "initial_quantity": str(state.initial_quantity),
        "stop_fraction": str(state.stop_fraction),
        "observation_count": state.observation_count,
        "latest_server_time_ms": state.latest_server_time_ms,
        "latest_mark_price": _optional_decimal_text(state.latest_mark_price),
        "latest_gross_r": str(state.latest_gross_r),
        "observed_peak_favorable_r": str(state.observed_peak_favorable_r),
        "observed_trough_r": str(state.observed_trough_r),
        "latest_giveback_from_peak_r": str(state.latest_giveback_from_peak_r),
        "current_quantity": _optional_decimal_text(state.current_quantity),
        "partial_close_seen": state.partial_close_seen,
        "exchange_unrealised_pnl_usdt": _optional_decimal_text(
            state.exchange_unrealised_pnl_usdt
        ),
        "projected_initial_quantity_gross_pnl_usdt": str(
            state.projected_initial_quantity_gross_pnl_usdt
        ),
        "current_quantity_gross_pnl_usdt": str(state.current_quantity_gross_pnl_usdt),
    }


def _decode_state(value: dict[str, Any]) -> BybitDemoTradeExcursionState:
    side_raw = _text_field(value, "side")
    try:
        side = CryptoSide(side_raw)
    except ValueError as exc:
        raise ValueError("demo excursion checkpoint has invalid side") from exc
    state = BybitDemoTradeExcursionState(
        symbol=_text_field(value, "symbol"),
        side=side,
        entry_price=_decimal_field(value, "entry_price"),
        initial_quantity=_decimal_field(value, "initial_quantity"),
        stop_fraction=_decimal_field(value, "stop_fraction"),
        observation_count=_int_field(value, "observation_count"),
        latest_server_time_ms=_optional_int_field(value, "latest_server_time_ms"),
        latest_mark_price=_optional_decimal_field(value, "latest_mark_price"),
        latest_gross_r=_decimal_field(value, "latest_gross_r"),
        observed_peak_favorable_r=_decimal_field(value, "observed_peak_favorable_r"),
        observed_trough_r=_decimal_field(value, "observed_trough_r"),
        latest_giveback_from_peak_r=_decimal_field(value, "latest_giveback_from_peak_r"),
        current_quantity=_optional_decimal_field(value, "current_quantity"),
        partial_close_seen=_bool_field(value, "partial_close_seen"),
        exchange_unrealised_pnl_usdt=_optional_decimal_field(
            value,
            "exchange_unrealised_pnl_usdt",
        ),
        projected_initial_quantity_gross_pnl_usdt=_decimal_field(
            value,
            "projected_initial_quantity_gross_pnl_usdt",
        ),
        current_quantity_gross_pnl_usdt=_decimal_field(
            value,
            "current_quantity_gross_pnl_usdt",
        ),
    )
    _validate_identity("ASTRA-DEMO-STATE-DECODE", state, validate_link=False)
    return state


def _validate_identity(
    entry_order_link_id: str,
    state: BybitDemoTradeExcursionState,
    *,
    validate_link: bool = True,
) -> None:
    if validate_link and not entry_order_link_id.startswith("ASTRA-DEMO-"):
        raise ValueError("demo excursion store requires ASTRA-DEMO orderLinkId")
    if state.symbol != state.symbol.strip().upper() or not state.symbol.endswith("USDT"):
        raise ValueError("demo excursion state symbol must be normalized USDT")
    if state.entry_price <= 0 or state.initial_quantity <= 0 or state.stop_fraction <= 0:
        raise ValueError("demo excursion state has invalid positive fields")
    if state.observation_count < 0:
        raise ValueError("demo excursion state observation count cannot be negative")
    if state.live_mainnet_order_routing_allowed:
        raise ValueError("demo excursion state cannot permit live routing")
    if not state.diagnostics_only or state.exit_threshold_retuning_allowed:
        raise ValueError("demo excursion state must remain diagnostics only")


def _optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _text_field(value: dict[str, Any], field: str) -> str:
    raw = value.get(field)
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"demo excursion checkpoint missing {field}")
    return raw


def _int_field(value: dict[str, Any], field: str) -> int:
    raw = value.get(field)
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError(f"demo excursion checkpoint invalid {field}")
    return raw


def _optional_int_field(value: dict[str, Any], field: str) -> int | None:
    raw = value.get(field)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError(f"demo excursion checkpoint invalid {field}")
    return raw


def _decimal_field(value: dict[str, Any], field: str) -> Decimal:
    raw = value.get(field)
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"demo excursion checkpoint missing {field}")
    return _parse_decimal(raw, field)


def _optional_decimal_field(value: dict[str, Any], field: str) -> Decimal | None:
    raw = value.get(field)
    if raw is None:
        return None
    if not isinstance(raw, str) or not raw:
        raise ValueError(f"demo excursion checkpoint invalid {field}")
    return _parse_decimal(raw, field)


def _parse_decimal(raw: str, field: str) -> Decimal:
    try:
        parsed = Decimal(raw)
    except InvalidOperation as exc:
        raise ValueError(f"demo excursion checkpoint invalid {field}") from exc
    if not parsed.is_finite():
        raise ValueError(f"demo excursion checkpoint non-finite {field}")
    return parsed


def _bool_field(value: dict[str, Any], field: str) -> bool:
    raw = value.get(field)
    if not isinstance(raw, bool):
        raise ValueError(f"demo excursion checkpoint invalid {field}")
    return raw


def _validate_revision(value: str) -> None:
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError("expected demo excursion revision must be sha256 hex")
