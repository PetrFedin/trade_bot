from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.execution.bybit_demo_runtime_lease import JsonFileBybitDemoRuntimeLease


def test_runtime_lease_is_exclusive_until_exact_owner_releases(tmp_path: Path) -> None:
    path = tmp_path / "runtime.lock"
    first = JsonFileBybitDemoRuntimeLease(path, clock_ms=lambda: 123)
    second = JsonFileBybitDemoRuntimeLease(path, clock_ms=lambda: 456)

    lease = first.acquire()

    with pytest.raises(FileExistsError):
        second.acquire()
    observed = second.inspect()
    assert observed.owner_token == lease.owner_token
    assert observed.created_time_ms == 123
    assert first.automatic_stale_takeover_allowed is False
    assert first.live_mainnet_order_routing_allowed is False
    assert first.order_writes_supported is False

    first.release(owner_token=lease.owner_token)
    replacement = second.acquire()
    assert replacement.owner_token != lease.owner_token
    second.release(owner_token=replacement.owner_token)


def test_wrong_owner_cannot_release_runtime_lease(tmp_path: Path) -> None:
    store = JsonFileBybitDemoRuntimeLease(tmp_path / "runtime.lock", clock_ms=lambda: 123)
    lease = store.acquire()

    with pytest.raises(RuntimeError, match="ownership changed"):
        store.release(owner_token="b" * 64)

    assert store.inspect().owner_token == lease.owner_token
    store.release(owner_token=lease.owner_token)


def test_malformed_existing_lease_blocks_takeover(tmp_path: Path) -> None:
    path = tmp_path / "runtime.lock"
    path.write_text("not-json", encoding="utf-8")
    store = JsonFileBybitDemoRuntimeLease(path, clock_ms=lambda: 123)

    with pytest.raises(ValueError, match="invalid JSON"):
        store.acquire()

    assert path.exists()


def test_existing_lease_cannot_enable_automatic_stale_takeover(tmp_path: Path) -> None:
    path = tmp_path / "runtime.lock"
    payload = {
        "schema_version": 1,
        "kind": "BYBIT_DEMO_TRADING_RUNTIME_LEASE",
        "demo_only": True,
        "automatic_stale_takeover_allowed": True,
        "live_mainnet_order_routing_allowed": False,
        "owner_token": "a" * 64,
        "created_time_ms": 1,
        "process_id": 1,
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    store = JsonFileBybitDemoRuntimeLease(path, clock_ms=lambda: 123)

    with pytest.raises(ValueError, match="cannot allow automatic stale takeover"):
        store.acquire()


def test_symlink_runtime_lease_is_rejected(tmp_path: Path) -> None:
    target = tmp_path / "target.lock"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "runtime.lock"
    link.symlink_to(target)
    store = JsonFileBybitDemoRuntimeLease(link, clock_ms=lambda: 123)

    with pytest.raises(ValueError, match="cannot be a symlink"):
        store.acquire()


def test_clock_must_return_non_negative_integer(tmp_path: Path) -> None:
    for value in (True, -1, 1.5):
        store = JsonFileBybitDemoRuntimeLease(
            tmp_path / f"runtime-{value}.lock",
            clock_ms=lambda value=value: value,  # type: ignore[return-value]
        )
        with pytest.raises(ValueError, match="non-negative integer"):
            store.acquire()


def test_release_after_external_disappearance_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "runtime.lock"
    store = JsonFileBybitDemoRuntimeLease(path, clock_ms=lambda: 123)
    lease = store.acquire()
    path.unlink()

    with pytest.raises(FileNotFoundError):
        store.release(owner_token=lease.owner_token)
