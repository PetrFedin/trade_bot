from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.execution.bybit_demo_connected_preflight import (
    PostgresBybitDemoOperationalStateReader,
)

psycopg = pytest.importorskip("psycopg")

_DSN = os.environ.get("ASTRA_DEMO_PREFLIGHT_TEST_DSN", "")
pytestmark = pytest.mark.skipif(
    not _DSN,
    reason="ASTRA_DEMO_PREFLIGHT_TEST_DSN is not configured",
)


def _apply(path: str) -> None:
    sql = Path(path).read_text(encoding="utf-8")
    with psycopg.connect(_DSN, autocommit=True) as connection:
        connection.execute(sql)


def test_postgres_preflight_reads_v119_v120_guards_and_runtime_identity() -> None:
    _apply("migrations/v119/001_bybit_demo_durable_runtime.sql")
    _apply("migrations/v120/001_bybit_demo_durable_audit_lifecycle.sql")

    reader = PostgresBybitDemoOperationalStateReader(_DSN)
    clean = reader.read_state()

    assert clean.required_relations_present is True
    assert clean.append_only_triggers_present is True
    assert clean.runtime_lease_present is False
    assert clean.active_checkpoint_present is False
    assert clean.approval_record_count == 0
    assert clean.provenance_record_count == 0
    assert clean.terminal_record_count == 0
    assert reader.order_writes_supported is False
    assert reader.schema_mutation_supported is False
    assert reader.live_mainnet_order_routing_allowed is False

    state = {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry_price": "60000",
        "initial_quantity": "0.01",
        "current_quantity": "0.009",
        "stop_fraction": "0.01",
    }
    now = datetime.now(UTC)
    with psycopg.connect(_DSN, autocommit=True) as connection:
        connection.execute(
            """INSERT INTO astra_bybit_demo_runtime_lease_v119
            (lease_name, owner_token, created_time_ms, process_id,
             automatic_stale_takeover_allowed, live_mainnet_order_routing_allowed,
             created_at)
            VALUES ('CANONICAL_DEMO_TRADING_RUNTIME', %s, 1, 1, false, false, %s)""",
            ("a" * 64, now),
        )
        connection.execute(
            """INSERT INTO astra_bybit_demo_active_excursion_v119
            (checkpoint_name, entry_order_link_id, revision, state_json,
             diagnostics_only, exit_threshold_retuning_allowed,
             live_mainnet_order_routing_allowed, created_at, updated_at)
            VALUES ('ACTIVE', 'ASTRA-DEMO-E-TEST', %s, %s::jsonb,
                    true, false, false, %s, %s)""",
            ("b" * 64, json.dumps(state), now, now),
        )

    active = reader.read_state()
    assert active.runtime_lease_present is True
    assert active.active_checkpoint_present is True
    assert active.active_checkpoint_order_link_id == "ASTRA-DEMO-E-TEST"
    assert active.active_checkpoint_symbol == "BTCUSDT"
    assert active.active_checkpoint_side == "LONG"
    assert active.active_checkpoint_entry_price == Decimal("60000")
    assert active.active_checkpoint_current_quantity == Decimal("0.009")
