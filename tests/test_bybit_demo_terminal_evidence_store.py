from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.execution.bybit_demo_post_trade_accounting import BybitDemoProfitOutcomeStatus
from app.execution.bybit_demo_profit_preservation_evidence import (
    BybitDemoProfitPreservationEvidence,
)
from app.execution.bybit_demo_terminal_evidence_store import (
    JsonFileBybitDemoTerminalEvidenceStore,
)
from app.strategy.crypto_perp import CryptoSide

_ENTRY = "ASTRA-DEMO-E-TERMINALEVID"
_REVISION = "a" * 64


def _evidence() -> BybitDemoProfitPreservationEvidence:
    return BybitDemoProfitPreservationEvidence(
        symbol="BTCUSDT",
        side=CryptoSide.LONG,
        observation_count=12,
        observed_peak_favorable_r=Decimal("1.8"),
        observed_max_adverse_r=Decimal("0.4"),
        realized_gross_exit_r=Decimal("1.1"),
        observed_peak_capture_fraction=Decimal("0.6111111111"),
        giveback_from_observed_peak_to_exit_r=Decimal("0.7"),
        exit_exceeded_observed_peak=False,
        partial_close_seen=False,
        realized_gross_pnl_usdt=Decimal("11"),
        realized_net_after_execution_fees_usdt=Decimal("10.4"),
        execution_fees_usdt=Decimal("0.6"),
        account_closed_pnl_usdt=Decimal("10.35"),
        funding_net_usdt=Decimal("-0.05"),
        all_in_net_pnl_usdt=Decimal("10.30"),
        profit_outcome_status=BybitDemoProfitOutcomeStatus.FULLY_RECONCILED_PROFIT,
        positive_peak_nonpositive_gross_exit=False,
        gross_positive_fill_nonpositive=False,
        fill_positive_account_nonpositive=False,
        account_positive_all_in_nonpositive=False,
        positive_peak_nonpositive_all_in=False,
        fully_reconciled_all_in=True,
    )


def _record_file(root: Path) -> Path:
    files = list(root.glob("*.json"))
    assert len(files) == 1
    return files[0]


def test_terminal_evidence_persist_is_immutable_and_idempotent(tmp_path: Path) -> None:
    store = JsonFileBybitDemoTerminalEvidenceStore(tmp_path / "terminal-evidence")

    first = store.persist(
        entry_order_link_id=_ENTRY,
        checkpoint_revision=_REVISION,
        evidence=_evidence(),
    )
    second = store.persist(
        entry_order_link_id=_ENTRY,
        checkpoint_revision=_REVISION,
        evidence=_evidence(),
    )

    assert first.record_sha256 == second.record_sha256
    assert first.idempotent_existing_record is False
    assert second.idempotent_existing_record is True
    assert store.live_mainnet_order_routing_allowed is False
    assert store.order_writes_supported is False
    assert store.immutable_records is True


def test_conflicting_checkpoint_revision_cannot_replace_existing_record(tmp_path: Path) -> None:
    store = JsonFileBybitDemoTerminalEvidenceStore(tmp_path / "terminal-evidence")
    store.persist(
        entry_order_link_id=_ENTRY,
        checkpoint_revision=_REVISION,
        evidence=_evidence(),
    )

    with pytest.raises(RuntimeError, match="terminal evidence conflict"):
        store.persist(
            entry_order_link_id=_ENTRY,
            checkpoint_revision="b" * 64,
            evidence=_evidence(),
        )


def test_conflicting_all_in_pnl_cannot_replace_existing_record(tmp_path: Path) -> None:
    store = JsonFileBybitDemoTerminalEvidenceStore(tmp_path / "terminal-evidence")
    store.persist(
        entry_order_link_id=_ENTRY,
        checkpoint_revision=_REVISION,
        evidence=_evidence(),
    )
    changed = replace(_evidence(), all_in_net_pnl_usdt=Decimal("9.99"))

    with pytest.raises(RuntimeError, match="terminal evidence conflict"):
        store.persist(
            entry_order_link_id=_ENTRY,
            checkpoint_revision=_REVISION,
            evidence=changed,
        )


def test_tampered_terminal_record_is_rejected_instead_of_treated_idempotent(tmp_path: Path) -> None:
    store = JsonFileBybitDemoTerminalEvidenceStore(tmp_path / "terminal-evidence")
    store.persist(
        entry_order_link_id=_ENTRY,
        checkpoint_revision=_REVISION,
        evidence=_evidence(),
    )
    path = _record_file(store.root)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    envelope["record"]["evidence"]["all_in_net_pnl_usdt"] = "999"
    path.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(ValueError, match="checksum mismatch"):
        store.persist(
            entry_order_link_id=_ENTRY,
            checkpoint_revision=_REVISION,
            evidence=_evidence(),
        )


def test_pending_all_in_evidence_cannot_be_persisted_as_terminal(tmp_path: Path) -> None:
    store = JsonFileBybitDemoTerminalEvidenceStore(tmp_path / "terminal-evidence")
    pending = replace(
        _evidence(),
        all_in_net_pnl_usdt=None,
        fully_reconciled_all_in=False,
        profit_outcome_status=BybitDemoProfitOutcomeStatus.ALL_IN_ACCOUNTING_PENDING,
    )

    with pytest.raises(ValueError, match="fully reconciled all-in evidence"):
        store.persist(
            entry_order_link_id=_ENTRY,
            checkpoint_revision=_REVISION,
            evidence=pending,
        )


def test_live_capable_evidence_is_rejected(tmp_path: Path) -> None:
    store = JsonFileBybitDemoTerminalEvidenceStore(tmp_path / "terminal-evidence")
    unsafe = replace(_evidence(), live_mainnet_order_routing_allowed=True)

    with pytest.raises(ValueError, match="live-capable evidence"):
        store.persist(
            entry_order_link_id=_ENTRY,
            checkpoint_revision=_REVISION,
            evidence=unsafe,
        )


def test_symlink_root_is_rejected(tmp_path: Path) -> None:
    real_root = tmp_path / "real-evidence"
    real_root.mkdir()
    link_root = tmp_path / "linked-evidence"
    link_root.symlink_to(real_root, target_is_directory=True)
    store = JsonFileBybitDemoTerminalEvidenceStore(link_root)

    with pytest.raises(ValueError, match="root cannot be a symlink"):
        store.persist(
            entry_order_link_id=_ENTRY,
            checkpoint_revision=_REVISION,
            evidence=_evidence(),
        )
