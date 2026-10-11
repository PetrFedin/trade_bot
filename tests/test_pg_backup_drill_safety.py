"""Safety regression for the backup-drill actuator (no PostgreSQL needed)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools import pg_backup_drill as drill

SOURCE = "postgresql://operator:pwd@127.0.0.1:5432/astra_prod"
ADMIN = "postgresql://operator:pwd@127.0.0.1:5432/postgres"
TARGET = "astra_drill_20261011_safe"


def test_target_must_have_disposable_name() -> None:
    for target in ("postgres", "astra_prod", "astra_drill_short", "astra_drill_abcdefg", 'astra_drill_12345678"; DROP DATABASE x;--'):
        with pytest.raises(drill.DrillError):
            drill._validate_target(SOURCE, ADMIN, target)


def test_same_database_refused() -> None:
    with pytest.raises(drill.DrillError):
        drill._validate_target(SOURCE, ADMIN, "astra_prod")


def test_cross_server_admin_refused() -> None:
    with pytest.raises(drill.DrillError):
        drill._validate_target(SOURCE, ADMIN.replace("127.0.0.1", "remote.example"), TARGET)


def test_target_with_strict_isolation_name_accepted() -> None:
    drill._validate_target(SOURCE, ADMIN, TARGET)


def test_source_mutation_requested_fails_before_any_io() -> None:
    with patch.object(drill, "assert_client_is_compatible") as compatibility:
        with pytest.raises(drill.DrillError, match="forbidden"):
            drill.run_drill(SOURCE, admin_dsn=ADMIN, target_database=TARGET, mutate=True)
    compatibility.assert_not_called()


def test_target_creation_is_create_only() -> None:
    connection = MagicMock()
    session = MagicMock()
    session.__enter__.return_value = connection
    with patch.object(drill, "_connect", return_value=session), patch.object(drill, "_run", return_value="") as run:
        drill.restore(ADMIN, TARGET, Path("/tmp/fake-dump"))
    assert connection.execute.call_count == 1
    assert connection.execute.call_args.args == (f'CREATE DATABASE "{TARGET}"',)
    command = run.call_args.args[0]
    assert command[0] == "pg_restore"
    assert "--exit-on-error" in command


def test_occupied_target_failure_never_runs_restore() -> None:
    connection = MagicMock()
    connection.execute.side_effect = RuntimeError("database already exists")
    session = MagicMock()
    session.__enter__.return_value = connection
    with patch.object(drill, "_connect", return_value=session), patch.object(drill, "_run") as run:
        with pytest.raises(RuntimeError, match="already exists"):
            drill.restore(ADMIN, TARGET, Path("/tmp/fake-dump"))
    run.assert_not_called()


def test_source_never_deleted_on_failed_drill() -> None:
    with patch.object(drill, "assert_client_is_compatible", side_effect=drill.DrillError("client mismatch")), patch.object(drill, "_connect") as connect:
        with pytest.raises(drill.DrillError, match="mismatch"):
            drill.run_drill(SOURCE, admin_dsn=ADMIN, target_database=TARGET)
    connect.assert_not_called()
