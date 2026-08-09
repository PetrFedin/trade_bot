from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.runtime.deployment_supervisor_v99 import (
    DeploymentLeaseConflict,
    DeploymentPolicyV99,
    DeploymentState,
    DeploymentSupervisorV99,
    FileDeploymentCheckpointStoreV99,
    StaleDeploymentGeneration,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)


def runtime(tmp_path: Path, maximum_crashes: int = 3) -> DeploymentSupervisorV99:
    return DeploymentSupervisorV99(
        service_name="paper-roundtrip",
        store=FileDeploymentCheckpointStoreV99(tmp_path / "deployment.json"),
        policy=DeploymentPolicyV99(maximum_crashes=maximum_crashes),
    )


def test_lease_run_drain_stop(tmp_path: Path) -> None:
    service = runtime(tmp_path)
    acquired = service.acquire(instance_id="worker-a", now=NOW)
    running = service.mark_running(instance_id="worker-a", generation=acquired.generation, now=NOW + timedelta(seconds=1))
    draining = service.request_drain(instance_id="worker-a", generation=running.generation, now=NOW + timedelta(seconds=2))
    stopped = service.stop(instance_id="worker-a", generation=draining.generation, now=NOW + timedelta(seconds=3), outstanding_work=0)
    assert stopped.state is DeploymentState.STOPPED


def test_stale_generation_is_fenced(tmp_path: Path) -> None:
    service = runtime(tmp_path)
    first = service.acquire(instance_id="worker-a", now=NOW)
    service.request_drain(instance_id="worker-a", generation=first.generation, now=NOW + timedelta(seconds=1))
    service.stop(instance_id="worker-a", generation=first.generation, now=NOW + timedelta(seconds=2), outstanding_work=0)
    second = service.acquire(instance_id="worker-b", now=NOW + timedelta(seconds=3))
    with pytest.raises(StaleDeploymentGeneration):
        service.heartbeat(instance_id="worker-a", generation=first.generation, now=NOW + timedelta(seconds=4))
    assert second.generation > first.generation


def test_live_lease_rejects_second_owner(tmp_path: Path) -> None:
    service = runtime(tmp_path)
    service.acquire(instance_id="worker-a", now=NOW)
    with pytest.raises(DeploymentLeaseConflict):
        service.acquire(instance_id="worker-b", now=NOW + timedelta(seconds=1))


def test_crash_budget_quarantines(tmp_path: Path) -> None:
    service = runtime(tmp_path, maximum_crashes=2)
    first = service.acquire(instance_id="worker-a", now=NOW)
    service.record_crash(instance_id="worker-a", generation=first.generation, now=NOW + timedelta(seconds=1), reason="one")
    second = service.acquire(instance_id="worker-b", now=NOW + timedelta(seconds=2))
    quarantined = service.record_crash(instance_id="worker-b", generation=second.generation, now=NOW + timedelta(seconds=3), reason="two")
    assert quarantined.state is DeploymentState.QUARANTINED
