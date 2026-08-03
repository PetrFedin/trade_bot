from __future__ import annotations

from pathlib import Path

import pytest

from tools.architecture_audit_v103 import audit as architecture_audit
from tools.platform_v103 import demo_readiness, main as platform_main, truth_status
from tools.static_audit_v103 import audit as static_audit
from tools.stress_v103 import run as stress_run

ROOT = Path(__file__).resolve().parents[1]


def test_architecture_audit_passes():
    assert architecture_audit(ROOT)["status"] == "PASS"


def test_static_audit_passes():
    assert static_audit(ROOT)["status"] == "PASS"


def test_truth_status_is_fail_closed():
    result = truth_status()
    assert result["production_control_plane_implemented"] is True
    assert result["external_postgresql_cluster_verified"] is False
    assert result["external_order_routing_allowed"] is False
    assert result["live_trading_allowed"] is False


def test_demo_readiness_is_not_production_ready():
    result = demo_readiness()
    assert result["ready_for_read_only_probe"] is False
    assert result["backend_kind"] == "memory-reference"


@pytest.mark.parametrize("command", [["truth-status"], ["demo-readiness"], ["audit", str(ROOT)]])
def test_platform_cli_commands(command):
    assert platform_main(command) == 0


def test_stress_small_run():
    result = stress_run(32, 4)
    assert result["successes"] == 32
    assert result["failures"] == 0
    assert result["external_order_routing_allowed"] is False


@pytest.mark.parametrize("iterations,workers", [(0, 1), (1, 0)])
def test_stress_validates_arguments(iterations, workers):
    with pytest.raises(ValueError):
        stress_run(iterations, workers)


def test_migration_has_fail_closed_functions_and_privileges():
    sql = (ROOT / "migrations/v103/001_production_campaign_control_plane.sql").read_text(encoding="utf-8")
    for token in (
        "claim_campaign_lease",
        "record_worker_heartbeat",
        "append_control_plane_event",
        "FOR UPDATE",
        "SECURITY DEFINER",
        "REVOKE ALL",
        "mutation_count integer NOT NULL DEFAULT 0 CHECK (mutation_count = 0)",
    ):
        assert token in sql
