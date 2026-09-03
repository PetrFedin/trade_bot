from __future__ import annotations

import ast
from pathlib import Path

import pytest

from app.execution.bybit_demo_postgres_runtime_lease import PostgresBybitDemoRuntimeLease
from app.execution.bybit_demo_runtime_lease import BybitDemoRuntimeLease


OWNER = "a" * 64


def test_runtime_lease_record_is_fail_closed() -> None:
    lease = BybitDemoRuntimeLease(
        owner_token=OWNER,
        created_time_ms=1,
        process_id=123,
    )

    assert lease.automatic_stale_takeover_allowed is False
    assert lease.live_mainnet_order_routing_allowed is False
    assert lease.order_writes_supported is False


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"owner_token": "bad"}, "owner token"),
        ({"created_time_ms": -1}, "created time"),
        ({"created_time_ms": True}, "created time"),
        ({"process_id": 0}, "process id"),
        ({"process_id": True}, "process id"),
        ({"automatic_stale_takeover_allowed": True}, "stale takeover"),
        ({"live_mainnet_order_routing_allowed": True}, "mainnet routing"),
        ({"order_writes_supported": True}, "order writes"),
    ],
)
def test_runtime_lease_record_rejects_unsafe_or_malformed_state(
    kwargs: dict[str, object], message: str
) -> None:
    values: dict[str, object] = {
        "owner_token": OWNER,
        "created_time_ms": 1,
        "process_id": 123,
    }
    values.update(kwargs)

    with pytest.raises(ValueError, match=message):
        BybitDemoRuntimeLease(**values)  # type: ignore[arg-type]


def test_postgres_lease_adapter_has_no_order_or_live_capability() -> None:
    assert PostgresBybitDemoRuntimeLease.automatic_stale_takeover_allowed is False
    assert PostgresBybitDemoRuntimeLease.live_mainnet_order_routing_allowed is False
    assert PostgresBybitDemoRuntimeLease.order_writes_supported is False

    public_names = {
        name
        for name in vars(PostgresBybitDemoRuntimeLease)
        if not name.startswith("_")
    }
    assert public_names == {
        "acquire",
        "automatic_stale_takeover_allowed",
        "inspect",
        "live_mainnet_order_routing_allowed",
        "migrate",
        "order_writes_supported",
        "release",
    }


def test_c2a0_modules_do_not_import_strategy_research_or_broker_network_code() -> None:
    paths = (
        Path("app/execution/bybit_demo_runtime_lease.py"),
        Path("app/execution/bybit_demo_postgres_runtime_lease.py"),
    )
    forbidden_prefixes = (
        "app.strategy",
        "app.marketdata",
        "app.research",
        "requests",
        "httpx",
        "websockets",
    )

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imported.add(node.module)

        assert not any(
            module == prefix or module.startswith(f"{prefix}.")
            for module in imported
            for prefix in forbidden_prefixes
        ), (path, imported)
