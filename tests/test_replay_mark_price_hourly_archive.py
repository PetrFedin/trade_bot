"""Offline research replay: synthetic fixtures only; no network or trading."""
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from tests.test_verify_mark_price_hourly_archive import SYMBOL, _fixture
from tools.mark_price_hourly_sensitivity import collect_symbol
from tools.replay_mark_price_hourly_archive import (
    ArchiveReplayError,
    replay_archived_symbol,
)

START = datetime(2026, 1, 1, tzinfo=UTC)


def _case(tmp_path):
    fixture_root, funding, _ = _fixture(tmp_path)
    page_file = fixture_root / SYMBOL / "provider_pages" / (
        f"{SYMBOL}_mark_60m_page_000.json"
    )
    response = json.loads(page_file.read_text())

    class CapturedResponse:
        status_code = 200

        def raise_for_status(self):
            pass

        def json(self):
            return response

    def no_network(url, *, params, timeout):
        return CapturedResponse()

    root = tmp_path / "captured"
    collect_symbol(
        symbol=SYMBOL,
        start=START, end=START + timedelta(hours=23),
        funding_csv=funding / f"{SYMBOL}_funding.csv",
        funding_manifest=funding / f"{SYMBOL}_funding.manifest.json",
        output_dir=root / SYMBOL,
        get=no_network, pause_seconds=0,
    )
    manifest = root / SYMBOL / f"{SYMBOL}_mark_60m.manifest.json"
    return root, funding, hashlib.sha256(manifest.read_bytes()).hexdigest()


def _run(root, funding, target, sha):
    return replay_archived_symbol(
        archived_root=root,
        funding_root=funding,
        output_root=target,
        symbol=SYMBOL,
        start=START,
        end=START + timedelta(hours=23),
        expected_manifest_sha256=sha,
    )


def test_reproduces_complete_archive_without_network(tmp_path):
    root, funding, pin = _case(tmp_path)
    target = tmp_path / "output"
    result = _run(root, funding, target, pin)
    assert result["hourly_rows"] == 24
    assert result["funding_settlements"] == 3
    assert result["provider_pages"] == 1
    assert result["profitability_proven"] is False
    assert (target / SYMBOL / "offline-replay-receipt.json").is_file()
    assert hashlib.sha256(
        (target / SYMBOL / f"{SYMBOL}_mark_60m.manifest.json").read_bytes()
    ).hexdigest() == pin


def test_detached_pin_mismatch_fails_without_output(tmp_path):
    root, funding, pin = _case(tmp_path)
    target = tmp_path / "output"
    with pytest.raises(ArchiveReplayError, match="detached source manifest"):
        _run(root, funding, target, "0" * 64)
    assert not target.exists()


def test_missing_response_page_fails_without_output(tmp_path):
    root, funding, pin = _case(tmp_path)
    (root / SYMBOL / "provider_pages" / f"{SYMBOL}_mark_60m_page_000.json").unlink()
    target = tmp_path / "output"
    with pytest.raises(ArchiveReplayError, match="source archive rejected"):
        _run(root, funding, target, pin)
    assert not target.exists()


def test_existing_target_never_overwritten(tmp_path):
    root, funding, pin = _case(tmp_path)
    target = tmp_path / "output"
    _run(root, funding, target, pin)
    with pytest.raises(ArchiveReplayError, match="already exists"):
        _run(root, funding, target, pin)


def test_wrong_window_rejected(tmp_path):
    root, funding, pin = _case(tmp_path)
    with pytest.raises(ArchiveReplayError, match="fixed window mismatch"):
        replay_archived_symbol(
            archived_root=root, funding_root=funding,
            output_root=tmp_path / "output", symbol=SYMBOL,
            start=START, end=START + timedelta(hours=22),
            expected_manifest_sha256=pin,
        )
