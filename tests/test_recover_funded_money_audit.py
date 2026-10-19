"""Checkpoint corruption, interruption, and clean-replay parity tests."""
import json

import pytest

import tools.recover_funded_money_audit as recovery
from tests.test_verify_funded_replay_integration import _fixture
from tools.verify_funded_replay_integration import HistoricalMoneyIntegrationError


def _run(tmp_path, monkeypatch, interrupt=None):
    monkeypatch.setattr(
        recovery, "audit_portfolio_model",
        lambda model, **params: {"scenario": params["scenario"]},
    )
    import tools.verify_funded_replay_integration as integration
    monkeypatch.setattr(
        integration, "audit_portfolio_model",
        lambda model, **params: {"scenario": params["scenario"]},
    )
    report, verifier = _fixture()
    return recovery.audit_with_recovery(
        report=report, verifier=verifier, report_bytes=b"report",
        verifier_bytes=b"verifier",
        historical_head_sha="6f6c3c42ef3b33a1fbeb2a45131b014ee15810f8",
        checkpoint_path=tmp_path / "checkpoint.json",
        code_root=__import__("pathlib").Path(recovery.__file__).resolve().parent.parent,
        interrupt_after=interrupt,
    )


def test_checkpoint_resume_equals_clean_audit(tmp_path, monkeypatch):
    with pytest.raises(InterruptedError):
        _run(tmp_path, monkeypatch, interrupt=4)
    assert len(json.loads((tmp_path / "checkpoint.json").read_text())["models"]) == 4
    result = _run(tmp_path, monkeypatch)
    assert result["completed_models"] == 36
    assert result["clean_replay_identical"] is True
    assert result["profitability_proven"] is False


def test_checkpoint_tampering_rejected(tmp_path, monkeypatch):
    with pytest.raises(InterruptedError):
        _run(tmp_path, monkeypatch, interrupt=1)
    path = tmp_path / "checkpoint.json"
    state = json.loads(path.read_text())
    state["identity"]["historical_head_sha"] = "f" * 40
    path.write_text(json.dumps(state))
    with pytest.raises(HistoricalMoneyIntegrationError, match="checkpoint"):
        _run(tmp_path, monkeypatch)


def test_mismatched_code_fingerprint_rejected(tmp_path, monkeypatch):
    with pytest.raises(InterruptedError):
        _run(tmp_path, monkeypatch, interrupt=1)
    path = tmp_path / "checkpoint.json"
    state = json.loads(path.read_text())
    state["identity"]["code_sha256"] = "0" * 64
    state["checkpoint_sha256"] = recovery._digest({
        k: v for k, v in state.items() if k != "checkpoint_sha256"
    })
    path.write_text(json.dumps(state))
    with pytest.raises(HistoricalMoneyIntegrationError, match="checkpoint"):
        _run(tmp_path, monkeypatch)


def test_checkpoint_concurrent_audit_fails_closed(tmp_path, monkeypatch):
    from tools.recover_funded_money_audit import _exclusive_checkpoint_lock

    checkpoint = tmp_path / "checkpoint.json"
    with _exclusive_checkpoint_lock(checkpoint):
        with pytest.raises(HistoricalMoneyIntegrationError, match="already being audited"):
            _run(tmp_path, monkeypatch, interrupt=1)
        assert not checkpoint.exists()
    with pytest.raises(InterruptedError):
        _run(tmp_path, monkeypatch, interrupt=1)
    assert checkpoint.is_file()


def test_symlinked_checkpoint_parent_rejected(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)
    from tools.recover_funded_money_audit import _exclusive_checkpoint_lock

    with pytest.raises(HistoricalMoneyIntegrationError, match="symlinked checkpoint parent"):
        with _exclusive_checkpoint_lock(alias / "checkpoint.json"):
            pass
    assert not list(real.iterdir())


def test_symlinked_checkpoint_lock_rejected(tmp_path):
    from tools.recover_funded_money_audit import _exclusive_checkpoint_lock

    protected = tmp_path / "protected"
    protected.write_text("do not change")
    lock = tmp_path / "checkpoint.json.lock"
    lock.symlink_to(protected)
    with pytest.raises(HistoricalMoneyIntegrationError, match="checkpoint lock unavailable"):
        with _exclusive_checkpoint_lock(tmp_path / "checkpoint.json"):
            pass
    assert lock.is_symlink()
    assert protected.read_text() == "do not change"
