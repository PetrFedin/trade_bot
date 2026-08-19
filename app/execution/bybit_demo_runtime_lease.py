from __future__ import annotations

import json
import os
import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

_SCHEMA_VERSION = 1
_KIND = "BYBIT_DEMO_TRADING_RUNTIME_LEASE"


@dataclass(frozen=True)
class BybitDemoRuntimeLease:
    owner_token: str
    created_time_ms: int
    process_id: int
    live_mainnet_order_routing_allowed: bool = False


ClockMs = Callable[[], int]


class JsonFileBybitDemoRuntimeLease:
    """Exclusive fail-closed lease for one canonical demo trading invocation.

    The lease uses O_EXCL so two processes cannot both pass the checkpoint/entry boundary. It is
    deliberately not auto-expired: silently taking over an old lease could overlap with a slow
    process and create duplicate exposure. A crashed/orphaned lease therefore blocks new trading
    until an operator independently verifies the runtime is no longer active and removes the file.
    This favors capital safety over unattended availability.
    """

    live_mainnet_order_routing_allowed = False
    order_writes_supported = False
    automatic_stale_takeover_allowed = False

    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        clock_ms: ClockMs | None = None,
    ) -> None:
        self._path = Path(path)
        if not self._path.name:
            raise ValueError("demo runtime lease path must name a file")
        self._clock_ms = (lambda: int(time.time() * 1000)) if clock_ms is None else clock_ms

    @property
    def path(self) -> Path:
        return self._path

    def acquire(self) -> BybitDemoRuntimeLease:
        self._reject_symlink()
        parent = self._path.parent
        parent.mkdir(parents=True, exist_ok=True)
        self._reject_symlink()
        owner_token = secrets.token_hex(32)
        created_time_ms = self._clock_ms()
        if isinstance(created_time_ms, bool) or created_time_ms < 0:
            raise ValueError("demo runtime lease clock must return a non-negative integer")
        lease = BybitDemoRuntimeLease(
            owner_token=owner_token,
            created_time_ms=created_time_ms,
            process_id=os.getpid(),
        )
        payload = json.dumps(
            {
                "schema_version": _SCHEMA_VERSION,
                "kind": _KIND,
                "demo_only": True,
                "automatic_stale_takeover_allowed": False,
                "live_mainnet_order_routing_allowed": False,
                "owner_token": lease.owner_token,
                "created_time_ms": lease.created_time_ms,
                "process_id": lease.process_id,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ) + "\n"
        try:
            descriptor = os.open(
                self._path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
            )
        except FileExistsError:
            self._validate_existing_lock()
            raise
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            self._fsync_parent()
        except Exception:
            try:
                self._path.unlink()
            except OSError:
                pass
            raise
        return lease

    def release(self, *, owner_token: str) -> None:
        if len(owner_token) != 64 or any(
            character not in "0123456789abcdef" for character in owner_token
        ):
            raise ValueError("demo runtime lease owner token must be 32-byte hex")
        self._reject_symlink()
        lease = self.inspect()
        if lease.owner_token != owner_token:
            raise RuntimeError("demo runtime lease ownership changed before release")
        try:
            self._path.unlink()
        except FileNotFoundError as exc:
            raise RuntimeError("demo runtime lease disappeared before release") from exc
        except OSError as exc:
            raise RuntimeError("demo runtime lease could not be released") from exc
        self._fsync_parent()

    def inspect(self) -> BybitDemoRuntimeLease:
        self._reject_symlink()
        try:
            raw = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise
        except OSError as exc:
            raise RuntimeError("demo runtime lease could not be read") from exc
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("demo runtime lease is invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("demo runtime lease must be an object")
        if payload.get("schema_version") != _SCHEMA_VERSION or payload.get("kind") != _KIND:
            raise ValueError("demo runtime lease schema is unsupported")
        if payload.get("demo_only") is not True:
            raise ValueError("demo runtime lease lost demo-only marker")
        if payload.get("automatic_stale_takeover_allowed") is not False:
            raise ValueError("demo runtime lease cannot allow automatic stale takeover")
        if payload.get("live_mainnet_order_routing_allowed") is not False:
            raise ValueError("demo runtime lease cannot permit live routing")
        owner = payload.get("owner_token")
        created = payload.get("created_time_ms")
        process_id = payload.get("process_id")
        if not isinstance(owner, str) or len(owner) != 64 or any(
            character not in "0123456789abcdef" for character in owner
        ):
            raise ValueError("demo runtime lease has invalid owner token")
        if isinstance(created, bool) or not isinstance(created, int) or created < 0:
            raise ValueError("demo runtime lease has invalid created time")
        if isinstance(process_id, bool) or not isinstance(process_id, int) or process_id <= 0:
            raise ValueError("demo runtime lease has invalid process id")
        return BybitDemoRuntimeLease(
            owner_token=owner,
            created_time_ms=created,
            process_id=process_id,
        )

    def _validate_existing_lock(self) -> None:
        self._reject_symlink()
        self.inspect()

    def _reject_symlink(self) -> None:
        if self._path.is_symlink():
            raise ValueError("demo runtime lease cannot be a symlink")

    def _fsync_parent(self) -> None:
        try:
            descriptor = os.open(self._path.parent, os.O_RDONLY)
        except OSError:
            return
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
