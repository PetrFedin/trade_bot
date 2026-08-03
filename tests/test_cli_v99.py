import json
from datetime import datetime, timezone

from app.runtime.paper_broker_roundtrip_v99 import FileRoundTripJournalV99, RoundTripState
from tools.platform_v99 import main


def test_verify_journal_cli(tmp_path, capsys) -> None:
    path = tmp_path / "events.jsonl"
    FileRoundTripJournalV99(path).append(
        round_trip_id="rt",
        state=RoundTripState.BLOCKED,
        occurred_at=datetime(2026, 8, 3, tzinfo=timezone.utc),
        generation=1,
        attributes={"reason": "test"},
    )
    assert main(["verify-journal", str(path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "PASS"
    assert payload["events"] == 1
    assert not payload["live_trading_allowed"]
