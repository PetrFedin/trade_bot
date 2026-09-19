"""Coverage for #144 parts B-D: recoverability stated by comparison, not by claim."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.runtime.recovery_digest import (
    RecoveryDigestError,
    StateSnapshot,
    TableSnapshot,
    build_snapshot,
    compare_snapshots,
    row_digest,
    table_digest,
)

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


def snapshot(tables, *, schema_version: str = "16.4", captured_at: datetime = NOW) -> StateSnapshot:
    return build_snapshot(
        environment="drill",
        database="astra",
        schema_version=schema_version,
        captured_at=captured_at,
        tables=tables,
    )


# --- row and table digests --------------------------------------------------------


def test_column_order_does_not_change_a_row_digest() -> None:
    assert row_digest({"a": 1, "b": 2}) == row_digest({"b": 2, "a": 1})


def test_a_changed_value_changes_the_row_digest() -> None:
    assert row_digest({"a": 1}) != row_digest({"a": 2})


def test_equal_decimals_written_differently_are_the_same_content() -> None:
    """1.10 and 1.1 are one quantity; reporting them as a divergence would be noise."""
    assert row_digest({"q": Decimal("1.10")}) == row_digest({"q": Decimal("1.1")})


def test_timestamps_are_compared_in_utc() -> None:
    from datetime import timezone

    utc = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    shifted = datetime(2026, 1, 1, 15, 0, tzinfo=timezone(timedelta(hours=3)))
    assert row_digest({"t": utc}) == row_digest({"t": shifted})


def test_a_naive_timestamp_is_refused_rather_than_guessed() -> None:
    with pytest.raises(RecoveryDigestError, match="timezone-aware"):
        row_digest({"t": datetime(2026, 1, 1, 12, 0)})


def test_row_order_does_not_change_a_table_digest() -> None:
    """A restore may return rows in another physical order; that is not data loss."""
    rows = [{"id": 1}, {"id": 2}, {"id": 3}]
    assert table_digest(rows)[0] == table_digest(list(reversed(rows)))[0]


def test_one_missing_row_changes_a_table_digest() -> None:
    assert table_digest([{"id": 1}, {"id": 2}])[0] != table_digest([{"id": 1}])[0]


def test_table_digest_reports_the_row_count() -> None:
    assert table_digest([{"id": 1}, {"id": 2}])[1] == 2


def test_an_empty_table_still_has_a_digest() -> None:
    digest, count = table_digest([])
    assert count == 0
    assert len(digest) == 64


# --- acceptance 6: comparison at the recovery point -------------------------------


def test_acceptance_6_an_exact_restore_reports_identical() -> None:
    rows = [("astra_oms_orders", [{"id": 1}]), ("astra_risk_decisions", [{"id": 2}])]
    result = compare_snapshots(snapshot(rows), snapshot(rows))
    assert result.identical is True
    assert result.rows_lost == 0


def test_acceptance_6_a_dropped_row_is_reported_with_its_table() -> None:
    source = snapshot([("astra_oms_orders", [{"id": 1}, {"id": 2}])])
    restored = snapshot([("astra_oms_orders", [{"id": 1}])])
    result = compare_snapshots(source, restored)
    assert result.identical is False
    assert [item.table for item in result.divergences] == ["astra_oms_orders"]
    assert result.divergences[0].kind == "ROW_COUNT_DIFFERS"
    assert result.rows_lost == 1


def test_acceptance_6_a_changed_value_is_caught_at_equal_row_counts() -> None:
    """Counting rows would miss this, which is why the comparison hashes content."""
    source = snapshot([("astra_risk_decisions", [{"id": 1, "verdict": "ADMIT"}])])
    restored = snapshot([("astra_risk_decisions", [{"id": 1, "verdict": "REJECT"}])])
    result = compare_snapshots(source, restored)
    assert result.identical is False
    assert result.divergences[0].kind == "CONTENT_DIFFERS"
    assert result.rows_lost == 0


def test_acceptance_6_a_table_missing_from_the_restore_is_named() -> None:
    source = snapshot([("astra_oms_orders", [{"id": 1}]), ("astra_oms_events", [{"id": 1}])])
    restored = snapshot([("astra_oms_orders", [{"id": 1}])])
    result = compare_snapshots(source, restored)
    assert result.divergences[0].table == "astra_oms_events"
    assert result.divergences[0].kind == "MISSING_FROM_RESTORED"


def test_acceptance_6_an_unexpected_table_is_named_too() -> None:
    source = snapshot([("astra_oms_orders", [{"id": 1}])])
    restored = snapshot(
        [("astra_oms_orders", [{"id": 1}]), ("astra_oms_events", [{"id": 1}])]
    )
    result = compare_snapshots(source, restored)
    assert result.divergences[0].kind == "ONLY_IN_RESTORED"


# --- acceptance 5: schema identity ------------------------------------------------


def test_acceptance_5_a_schema_change_is_reported() -> None:
    rows = [("astra_oms_orders", [{"id": 1}])]
    result = compare_snapshots(snapshot(rows), snapshot(rows, schema_version="17.2"))
    assert result.schema_matches is False


def test_snapshot_digest_ignores_when_it_was_captured() -> None:
    """Two reads of an unchanged database describe one recovery point."""
    rows = [("astra_oms_orders", [{"id": 1}])]
    first = snapshot(rows, captured_at=NOW)
    second = snapshot(rows, captured_at=NOW + timedelta(hours=3))
    assert first.digest == second.digest


def test_snapshot_digest_changes_with_content() -> None:
    first = snapshot([("astra_oms_orders", [{"id": 1}])])
    second = snapshot([("astra_oms_orders", [{"id": 2}])])
    assert first.digest != second.digest


def test_snapshot_reports_total_rows() -> None:
    assert snapshot([("a_one", [{"id": 1}]), ("a_two", [{"id": 1}, {"id": 2}])]).total_rows == 3


# --- snapshot validation ----------------------------------------------------------


def test_a_snapshot_must_cover_something() -> None:
    with pytest.raises(RecoveryDigestError, match="at least one table"):
        snapshot([])


def test_a_duplicate_table_is_refused() -> None:
    duplicate = StateSnapshot(
        environment="drill",
        database="astra",
        schema_version="16.4",
        captured_at=NOW,
        tables=(
            TableSnapshot(table="astra_oms_orders", digest="0" * 64, row_count=1),
            TableSnapshot(table="astra_oms_orders", digest="1" * 64, row_count=1),
        ),
    )
    with pytest.raises(RecoveryDigestError, match="only once"):
        duplicate.validate()


def test_a_naive_capture_time_is_refused() -> None:
    with pytest.raises(RecoveryDigestError, match="timezone-aware"):
        snapshot([("astra_oms_orders", [{"id": 1}])], captured_at=datetime(2026, 9, 19, 12, 0))


def test_a_malformed_table_snapshot_is_refused() -> None:
    with pytest.raises(RecoveryDigestError, match="sha256"):
        TableSnapshot(table="astra_oms_orders", digest="short", row_count=0).validate()
    with pytest.raises(RecoveryDigestError, match="lowercase"):
        TableSnapshot(table="Astra_OMS", digest="0" * 64, row_count=0).validate()


def test_summary_is_serialisable_evidence() -> None:
    import json

    source = snapshot([("astra_oms_orders", [{"id": 1}, {"id": 2}])])
    restored = snapshot([("astra_oms_orders", [{"id": 1}])])
    payload = compare_snapshots(source, restored).summary()
    assert json.loads(json.dumps(payload))["rows_lost"] == 1
