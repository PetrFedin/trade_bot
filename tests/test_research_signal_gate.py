"""Negative security and durability contracts for research-only signal intake."""

from __future__ import annotations

import ast
import hashlib
import hmac
import json
import sqlite3
import stat
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier

import pytest

from tools.research_signal_gate import (
    SCHEMA,
    SIGN_DOMAIN,
    ResearchSignalRejected,
    ResearchSigner,
    verify_and_record_research_signal,
)

NOW = datetime(2026, 10, 20, 16, 0, tzinfo=UTC)
KEY_ID = "authorized-research-source"
SECRET = b"only-test-key-not-a-real-trading-secret!"


def _body(**changes):
    base = {
        "schema_version": SCHEMA,
        "source": "research_tradingview",
        "signal_id": "signal_20261020_001",
        "strategy_id": "research_strategy_01",
        "symbol": "BTCUSDT",
        "observed_at_utc": "2026-10-20T15:59:00Z",
        "expires_at_utc": "2026-10-20T16:01:00Z",
        "bias": "BULLISH",
        "timeframe_minutes": 15,
    }
    base.update(changes)
    return base


def _canonical(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    ).encode("utf-8")


def _signed(value=None, *, key=SECRET, key_id=KEY_ID):
    raw = _canonical(_body() if value is None else value)
    signature = hmac.new(
        key, SIGN_DOMAIN + key_id.encode("ascii") + b"\n" + raw,
        hashlib.sha256,
    ).hexdigest()
    return raw, signature


def _trusted():
    return {
        KEY_ID: ResearchSigner(
            secret=SECRET,
            source="research_tradingview",
            symbols=frozenset({"BTCUSDT", "ETHUSDT"}),
            strategies=frozenset({"research_strategy_01"}),
        ),
    }


def _admit(tmp_path, *, body=None, raw=None, sig=None, now=NOW,
           journal=None, trusted=None, key_id=KEY_ID):
    if raw is None:
        raw, signed = _signed(body)
    else:
        signed = hmac.new(
            SECRET, SIGN_DOMAIN + KEY_ID.encode("ascii") + b"\n" + raw,
            hashlib.sha256,
        ).hexdigest()
    return verify_and_record_research_signal(
        raw_body=raw,
        key_id=key_id,
        signature_hex=signed if sig is None else sig,
        trusted_keys=_trusted() if trusted is None else trusted,
        now_utc=now,
        journal_path=tmp_path / "observations.sqlite3" if journal is None else journal,
    )


def test_accepts_authenticated_observation_but_never_grants_trading(tmp_path):
    result = _admit(tmp_path)
    assert result["decision"] == "RECORDED_RESEARCH_ONLY"
    assert result["source"] == "research_tradingview"
    assert result["signal_id"] == "signal_20261020_001"
    assert result["payload_sha256"] == hashlib.sha256(_signed()[0]).hexdigest()
    for name in (
        "broker_order_routing_allowed", "demo_trading_allowed",
        "live_trading_allowed", "strategy_promotion_allowed",
        "profitability_proven",
    ):
        assert result[name] is False
    assert "secret" not in json.dumps(result)
    with sqlite3.connect(tmp_path / "observations.sqlite3") as db:
        assert db.execute(
            "SELECT COUNT(*) FROM research_observations",
        ).fetchone()[0] == 1


def test_duplicate_rejected_persistently_across_connections(tmp_path):
    _admit(tmp_path)
    with pytest.raises(ResearchSignalRejected, match="duplicate"):
        _admit(tmp_path)
    with pytest.raises(ResearchSignalRejected, match="duplicate"):
        _admit(tmp_path, body=_body(bias="BEARISH"))
    with sqlite3.connect(tmp_path / "observations.sqlite3") as db:
        assert db.execute(
            "SELECT COUNT(*) FROM research_observations",
        ).fetchone()[0] == 1


@pytest.mark.parametrize("wrong", [
    "0" * 64, "INVALID", "", "abcd", "F" * 64,
])
def test_wrong_signatures_rejected_without_persistence(tmp_path, wrong):
    with pytest.raises(ResearchSignalRejected, match="signature"):
        _admit(tmp_path, sig=wrong)
    assert not (tmp_path / "observations.sqlite3").exists()


def test_wrong_operator_key_and_source_rejected(tmp_path):
    raw, sig = _signed()
    with pytest.raises(ResearchSignalRejected, match="signing key"):
        _admit(tmp_path, raw=raw, sig=sig, key_id="unknown-key")
    with pytest.raises(ResearchSignalRejected, match="scope"):
        _admit(tmp_path, body=_body(source="not_trusted"))
    assert not (tmp_path / "observations.sqlite3").exists()


@pytest.mark.parametrize("changes", [
    {"symbol": "SOLUSDT"},
    {"strategy_id": "untrusted_strategy_001"},
    {"bias": "BUY"},
    {"bias": ["BULLISH"]},
    {"timeframe_minutes": True},
    {"timeframe_minutes": 0},
    {"observed_at_utc": "2026-10-20T15:59:00+00:00"},
    {"observed_at_utc": "2026-02-30T15:59:00Z"},
    {"expires_at_utc": "2026-10-20T15:59:00Z"},
    {"expires_at_utc": "2026-10-20T16:20:00Z"},
    {"observed_at_utc": "2026-10-20T16:01:00Z"},
])
def test_bad_scope_schema_clock_or_window_rejected(tmp_path, changes):
    with pytest.raises(ResearchSignalRejected):
        _admit(tmp_path, body=_body(**changes))
    assert not (tmp_path / "observations.sqlite3").exists()


def test_expired_and_non_utc_clock_rejected(tmp_path):
    with pytest.raises(ResearchSignalRejected, match="window"):
        _admit(tmp_path, now=NOW + timedelta(minutes=2))
    with pytest.raises(ResearchSignalRejected, match="UTC clock"):
        _admit(tmp_path, now=NOW.replace(tzinfo=None))
    assert not (tmp_path / "observations.sqlite3").exists()


@pytest.mark.parametrize("raw", [
    b'{"schema_version":"x","schema_version":"y"}',
    b'{"value":NaN}',
    b'{"value":1.1}',
    b"{}",
    b"[]" ,
    b" " + _signed()[0],
    _signed()[0] + b"\n",
    b"\xff\xff",
    b"x" * 3000,
])
def test_bad_json_and_noncanonical_bytes_rejected(tmp_path, raw):
    with pytest.raises(ResearchSignalRejected):
        _admit(tmp_path, raw=raw)
    assert not (tmp_path / "observations.sqlite3").exists()


def test_symlink_journal_and_parent_refused(tmp_path):
    protected = tmp_path / "protected.db"
    protected.write_bytes(b"KEEP")
    link = tmp_path / "observations.sqlite3"
    link.symlink_to(protected)
    with pytest.raises(ResearchSignalRejected, match="symlinked"):
        _admit(tmp_path)
    assert protected.read_bytes() == b"KEEP"
    link.unlink()
    parent = tmp_path / "real"
    parent.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(parent, target_is_directory=True)
    with pytest.raises(ResearchSignalRejected, match="symlinked"):
        _admit(tmp_path, journal=alias / "observations.sqlite3")
    assert not list(parent.iterdir())


def test_corrupt_journal_fail_closed(tmp_path):
    path = tmp_path / "observations.sqlite3"
    path.write_bytes(b"not a valid sqlite database")
    path.chmod(0o600)
    with pytest.raises(ResearchSignalRejected, match="journal unavailable"):
        _admit(tmp_path)
    assert path.read_bytes() == b"not a valid sqlite database"


def test_journal_writer_contention_fails_without_double_acceptance(tmp_path):
    path = tmp_path / "observations.sqlite3"
    with sqlite3.connect(path) as owner:
        path.chmod(0o600)
        owner.execute("BEGIN IMMEDIATE")
        with pytest.raises(ResearchSignalRejected, match="journal unavailable"):
            _admit(tmp_path)
        owner.execute("ROLLBACK")
    result = _admit(tmp_path)
    assert result["decision"] == "RECORDED_RESEARCH_ONLY"


def test_research_gate_cannot_import_broker_oms_or_strategy():
    path = Path(__file__).resolve().parents[1] / "tools" / "research_signal_gate.py"
    tree = ast.parse(path.read_text())
    modules = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.append(node.module)
    assert not any(module.startswith("app.") for module in modules)
    assert not any(
        module.split(".")[0] in {"requests", "httpx", "socket", "websockets"}
        for module in modules
    )



def test_new_journal_is_private_and_durable(tmp_path):
    target = tmp_path / "private" / "signals.sqlite3"
    receipt = _admit(tmp_path, journal=target)
    assert receipt["decision"] == "RECORDED_RESEARCH_ONLY"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
    with pytest.raises(ResearchSignalRejected, match="duplicate"):
        _admit(tmp_path, journal=target)


def test_public_readable_journal_is_rejected(tmp_path):
    path = tmp_path / "observations.sqlite3"
    path.write_text("do not touch")
    path.chmod(0o644)
    with pytest.raises(ResearchSignalRejected, match="permissions"):
        _admit(tmp_path)
    assert path.read_text() == "do not touch"


def test_group_writable_storage_directory_is_rejected(tmp_path):
    folder = tmp_path / "unsafe"
    folder.mkdir()
    folder.chmod(0o770)
    path = folder / "signals.sqlite3"
    with pytest.raises(ResearchSignalRejected, match="not private"):
        _admit(tmp_path, journal=path)
    assert not path.exists()


def test_parallel_same_signal_can_never_be_accepted_twice(tmp_path):
    start = Barrier(2)

    def worker():
        start.wait()
        try:
            return _admit(tmp_path)["decision"]
        except ResearchSignalRejected as exc:
            return str(exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _i: worker(), (0, 1)))
    accepted = sum(result == "RECORDED_RESEARCH_ONLY" for result in results)
    assert accepted <= 1
    if accepted == 0:
        assert _admit(tmp_path)["decision"] == "RECORDED_RESEARCH_ONLY"
    with pytest.raises(ResearchSignalRejected, match="duplicate"):
        _admit(tmp_path)
    with sqlite3.connect(tmp_path / "observations.sqlite3") as db:
        assert db.execute(
            "SELECT COUNT(*) FROM research_observations",
        ).fetchone()[0] == 1


def test_key_rotation_cannot_replay_an_existing_signal(tmp_path):
    _admit(tmp_path)
    rotated_id = "research-source-rotated"
    rotated_secret = b"distinct-test-only-key-for-research-signals"
    trusted = _trusted()
    trusted[rotated_id] = ResearchSigner(
        secret=rotated_secret,
        source="research_tradingview",
        symbols=frozenset({"BTCUSDT"}),
        strategies=frozenset({"research_strategy_01"}),
    )
    raw, signature = _signed(key=rotated_secret, key_id=rotated_id)
    with pytest.raises(ResearchSignalRejected, match="duplicate"):
        _admit(
            tmp_path, raw=raw, sig=signature, trusted=trusted, key_id=rotated_id,
        )


def test_revoked_signer_rejected_without_writing(tmp_path):
    raw, signature = _signed()
    with pytest.raises(ResearchSignalRejected, match="signing key unavailable"):
        _admit(tmp_path, raw=raw, sig=signature, trusted={})
    assert not (tmp_path / "observations.sqlite3").exists()


def test_signed_order_command_is_not_part_of_research_signal_schema(tmp_path):
    message = _body()
    message["order"] = "BUY"
    with pytest.raises(ResearchSignalRejected, match="field set rejected"):
        _admit(tmp_path, body=message)
    assert not (tmp_path / "observations.sqlite3").exists()
