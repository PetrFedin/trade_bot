from __future__ import annotations

import hashlib
import json
from pathlib import Path

from tools import architecture_audit_v107, platform_v106, platform_v107, static_audit_v107, stress_v107


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_platform_v107_verifies_temp_release(tmp_path):
    (tmp_path / "a.txt").write_text("a", encoding="utf-8")
    (tmp_path / "RELEASE_IDENTITY_V107.json").write_text(
        json.dumps({"version": "7.37.0", "files": {"a.txt": sha(tmp_path / "a.txt")}}),
        encoding="utf-8",
    )
    assert platform_v107.verify_release(tmp_path)["status"] == "PASS"
    (tmp_path / "a.txt").write_text("tampered", encoding="utf-8")
    result = platform_v107.verify_release(tmp_path)
    assert result["status"] == "FAIL" and result["findings"] == ["digest:a.txt"]


def test_platform_v107_handles_missing_invalid_identity_and_status(tmp_path):
    assert platform_v107.verify_release(tmp_path)["status"] == "FAIL"
    (tmp_path / "RELEASE_IDENTITY_V107.json").write_text("not-json", encoding="utf-8")
    assert platform_v107.verify_release(tmp_path)["findings"] == ["invalid:RELEASE_IDENTITY_V107.json"]
    assert platform_v107.live_status(tmp_path)["status"] == "UNKNOWN"
    (tmp_path / "LIVE_EXECUTION_STATUS_V107.json").write_text("not-json", encoding="utf-8")
    assert platform_v107.live_status(tmp_path)["status"] == "UNKNOWN"


def test_schema106_successor_verifier_ignores_only_shared_files(tmp_path):
    (tmp_path / "immutable.txt").write_text("immutable", encoding="utf-8")
    (tmp_path / "README.md").write_text("old", encoding="utf-8")
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "astra-schema107-production-rollout-actuator"\nversion = "7.37.0"\n',
        encoding="utf-8",
    )
    identity = {
        "files": {
            "immutable.txt": sha(tmp_path / "immutable.txt"),
            "README.md": "0" * 64,
            "pyproject.toml": "0" * 64,
        }
    }
    (tmp_path / "RELEASE_IDENTITY_V106.json").write_text(json.dumps(identity), encoding="utf-8")
    result = platform_v106.verify_release(tmp_path)
    assert result["status"] == "PASS"
    assert result["mode"] == "successor"
    assert result["files_checked"] == 3
    assert result["files_verified"] == 1
    assert result["files_ignored"] == ["README.md", "pyproject.toml"]
    (tmp_path / "immutable.txt").write_text("tampered", encoding="utf-8")
    assert platform_v106.verify_release(tmp_path)["findings"] == ["digest:immutable.txt"]


def test_schema106_successor_still_requires_shared_file(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "astra-schema107-production-rollout-actuator"\nversion = "7.37.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "RELEASE_IDENTITY_V106.json").write_text(
        json.dumps({"files": {"README.md": "0" * 64}}), encoding="utf-8"
    )
    assert platform_v106.verify_release(tmp_path)["findings"] == ["missing:README.md"]


def test_migration_copy_and_security_tokens():
    root = Path(__file__).resolve().parents[1]
    canonical = root / "migrations/v107/001_production_rollout_actuator.sql"
    packaged = root / "app/platform_assets/v107/migrations/001_production_rollout_actuator.sql"
    assert canonical.read_bytes() == packaged.read_bytes()
    text = canonical.read_text(encoding="utf-8")
    for token in (
        "astra_rollout_fence_v107",
        "fencing_token bigint",
        "mutation_attempts IN (0, 1)",
        "append-only",
        "REVOKE ALL",
    ):
        assert token in text


def test_stress_small_is_deterministically_unique():
    result = stress_v107.stress(iterations=100, workers=4)
    assert result["status"] == "PASS"
    assert result["failures"] == []
    assert result["replay_ledger_size"] == 100
    assert result["unique_command_digests"] == 100


def test_audits_pass_on_frozen_tree():
    root = Path(__file__).resolve().parents[1]
    assert architecture_audit_v107.audit(root)["status"] == "PASS"
    assert static_audit_v107.audit(root)["status"] == "PASS"
