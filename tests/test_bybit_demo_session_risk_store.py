import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path

import pytest

from app.execution.bybit_demo_session_risk_ledger import (
    BybitDemoSessionRiskLedger,
    BybitDemoSessionTradeOutcome,
    observe_bybit_demo_session_equity,
)
from app.execution.bybit_demo_session_risk_store import (
    JsonFileBybitDemoSessionRiskLedgerStore,
)


def _ledger(*, pnl: str = "-5", peak: str | None = None) -> BybitDemoSessionRiskLedger:
    return BybitDemoSessionRiskLedger(
        opening_equity_usdt=Decimal("1000"),
        outcomes=(
            BybitDemoSessionTradeOutcome(
                entry_order_link_id="ASTRA-DEMO-E-CHECKPOINT",
                symbol="BTCUSDT",
                created_time_ms=100,
                updated_time_ms=150,
                all_in_net_pnl_usdt=Decimal(pnl),
                execution_fees_usdt=Decimal("1.25"),
            ),
        ),
        peak_equity_usdt=None if peak is None else Decimal(peak),
    )


def test_session_risk_store_round_trips_and_updates_by_revision(tmp_path: Path) -> None:
    path = tmp_path / "session-risk.json"
    store = JsonFileBybitDemoSessionRiskLedgerStore(path)

    initial = store.initialize(_ledger())
    loaded = store.load(expected_opening_equity_usdt=Decimal("1000"))

    assert loaded == initial
    assert len(initial.revision) == 64
    assert store.live_mainnet_order_routing_allowed is False
    assert store.order_writes_supported is False
    assert path.stat().st_mode & 0o777 == 0o600

    updated_ledger = _ledger(pnl="-3")
    updated = store.save(updated_ledger, expected_revision=loaded.revision)
    reloaded = store.load(expected_opening_equity_usdt=Decimal("1000"))
    assert reloaded == updated
    assert updated.revision != initial.revision
    assert reloaded.ledger.outcomes[0].all_in_net_pnl_usdt == Decimal("-3")
    assert reloaded.ledger.peak_equity_usdt == Decimal("1000")


def test_session_risk_store_persists_wallet_high_water_across_restart(tmp_path: Path) -> None:
    store = JsonFileBybitDemoSessionRiskLedgerStore(tmp_path / "session.json")
    initial = store.initialize(_ledger())
    observed = observe_bybit_demo_session_equity(
        initial.ledger,
        current_equity_usdt=Decimal("1125"),
    )
    store.save(observed, expected_revision=initial.revision)

    restarted = JsonFileBybitDemoSessionRiskLedgerStore(store.path)
    loaded = restarted.load(expected_opening_equity_usdt=Decimal("1000"))
    state = loaded.ledger.to_session_risk_state(current_equity_usdt=Decimal("1000"))

    assert loaded.ledger.peak_equity_usdt == Decimal("1125")
    assert state.peak_equity_usdt == Decimal("1125")


def test_session_risk_store_loads_legacy_schema_without_peak_field(tmp_path: Path) -> None:
    path = tmp_path / "legacy-session.json"
    ledger_payload = {
        "opening_equity_usdt": "1000",
        "outcomes": [],
    }
    canonical = json.dumps(
        ledger_payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    revision = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    envelope = {
        "schema_version": 1,
        "kind": "BYBIT_DEMO_SESSION_RISK_LEDGER",
        "demo_only": True,
        "live_mainnet_order_routing_allowed": False,
        "ledger_revision_sha256": revision,
        "ledger": ledger_payload,
    }
    path.write_text(
        json.dumps(envelope, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n",
        encoding="utf-8",
    )

    store = JsonFileBybitDemoSessionRiskLedgerStore(path)
    loaded = store.load(expected_opening_equity_usdt=Decimal("1000"))

    assert loaded.revision == revision
    assert loaded.ledger.peak_equity_usdt is None
    assert loaded.ledger.effective_peak_equity_usdt == Decimal("1000")


def test_session_risk_store_never_auto_initializes_missing_checkpoint(tmp_path: Path) -> None:
    store = JsonFileBybitDemoSessionRiskLedgerStore(tmp_path / "missing.json")
    with pytest.raises(FileNotFoundError):
        store.load(expected_opening_equity_usdt=Decimal("1000"))


def test_session_risk_store_rejects_opening_equity_mismatch(tmp_path: Path) -> None:
    store = JsonFileBybitDemoSessionRiskLedgerStore(tmp_path / "session.json")
    store.initialize(_ledger())
    with pytest.raises(ValueError, match="opening equity mismatch"):
        store.load(expected_opening_equity_usdt=Decimal("999"))


def test_session_risk_store_rejects_stale_revision(tmp_path: Path) -> None:
    store = JsonFileBybitDemoSessionRiskLedgerStore(tmp_path / "session.json")
    checkpoint = store.initialize(_ledger())
    store.save(_ledger(pnl="-4"), expected_revision=checkpoint.revision)

    with pytest.raises(RuntimeError, match="revision changed concurrently"):
        store.save(_ledger(pnl="-2"), expected_revision=checkpoint.revision)


def test_session_risk_store_rejects_checksum_tampering(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    store = JsonFileBybitDemoSessionRiskLedgerStore(path)
    store.initialize(_ledger())
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["ledger"]["outcomes"][0]["all_in_net_pnl_usdt"] = "1000000"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="checksum mismatch"):
        store.load(expected_opening_equity_usdt=Decimal("1000"))


def test_session_risk_store_rejects_existing_initialize_and_symlink(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    store = JsonFileBybitDemoSessionRiskLedgerStore(path)
    store.initialize(_ledger())
    with pytest.raises(FileExistsError):
        store.initialize(_ledger())

    target = tmp_path / "target.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "linked.json"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable")
    linked_store = JsonFileBybitDemoSessionRiskLedgerStore(link)
    with pytest.raises(ValueError, match="symlink"):
        linked_store.load(expected_opening_equity_usdt=Decimal("1000"))
