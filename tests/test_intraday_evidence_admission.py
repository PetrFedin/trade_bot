"""Negative and positive admission evidence; fixture is explicitly fake."""
import csv
import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest

from tools.intraday_evidence_admission import (
    IntradayAdmissionError,
    validate_intraday_fixture,
)


def fixture(tmp_path, *, gap=False):
    path = tmp_path / "BTCUSDT_60m.csv"
    start = datetime(2026, 10, 1, tzinfo=UTC)
    with path.open("w", newline="") as fp:
        out = csv.writer(fp)
        out.writerow(("timestamp", "open", "high", "low", "close", "volume"))
        for hour in range(24):
            if gap and hour == 14:
                continue
            out.writerow((
                (start + timedelta(hours=hour)).isoformat(),
                "100", "101", "99", "100", "10",
            ))
    manifest = {
        "data_origin": "TEST_FIXTURE_ONLY",
        "interval_minutes": 60,
        "sha256": {path.name: hashlib.sha256(path.read_bytes()).hexdigest()},
    }
    (tmp_path / "provenance.json").write_text(json.dumps(manifest))
    return path


def run(tmp_path):
    return validate_intraday_fixture(
        tmp_path, symbols=("BTCUSDT",), days=("2026-10-01",),
    )


def test_fixture_contract_admitted_for_research_only(tmp_path):
    fixture(tmp_path)
    report = run(tmp_path)
    assert report["admitted_for_research_only"] is True
    assert report["strategy_promotion_allowed"] is False
    assert report["live_trading_allowed"] is False
    assert report["profitability_proven"] is False
    assert report["replay_completed"] is False


def test_refuses_missing_hour_even_with_unchanged_sha(tmp_path):
    fixture(tmp_path, gap=True)
    with pytest.raises(IntradayAdmissionError, match="incomplete"):
        run(tmp_path)


def test_refuses_modified_source(tmp_path):
    path = fixture(tmp_path)
    path.write_text(path.read_text().replace("100,101", "100,102"))
    with pytest.raises(IntradayAdmissionError, match="SHA"):
        run(tmp_path)


def test_refuses_synthetic_derived_daily_bars(tmp_path):
    fixture(tmp_path)
    manifest_path = tmp_path / "provenance.json"
    record = json.loads(manifest_path.read_text())
    record["data_origin"] = "DAILY_RESAMPLED"
    manifest_path.write_text(json.dumps(record))
    with pytest.raises(IntradayAdmissionError, match="provenance"):
        run(tmp_path)


def test_absence_of_data_is_not_proof(tmp_path):
    with pytest.raises(IntradayAdmissionError, match="provenance"):
        run(tmp_path)
