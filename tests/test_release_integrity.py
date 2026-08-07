from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tools.release_integrity import generate_release_evidence, parse_lock

NOW = datetime(2026, 8, 7, 21, 0, tzinfo=UTC)
COMMIT = "a" * 40


def write_project(root: Path) -> None:
    (root / "pyproject.toml").write_text(
        '[project]\nname = "astra-trade-bot"\nversion = "7.38.0"\n',
        encoding="utf-8",
    )


def write_lock(path: Path) -> None:
    path.write_text(
        """cryptography==50.0.0 \\
    --hash=sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa
pytest==9.0.2 \\
    --hash=sha256:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
""",
        encoding="utf-8",
    )


def test_parse_lock_requires_pins_and_hashes(tmp_path: Path) -> None:
    lock = tmp_path / "requirements.lock"
    write_lock(lock)
    dependencies = parse_lock(lock)
    assert [(item.name, item.version) for item in dependencies] == [
        ("cryptography", "50.0.0"),
        ("pytest", "9.0.2"),
    ]
    assert all(item.hashes for item in dependencies)

    lock.write_text("cryptography==50.0.0\n", encoding="utf-8")
    with pytest.raises(ValueError, match="no SHA-256 hashes"):
        parse_lock(lock)


def test_release_evidence_binds_commit_lock_and_artifact_hashes(tmp_path: Path) -> None:
    write_project(tmp_path)
    lock = tmp_path / "requirements.lock"
    write_lock(lock)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "astra_trade_bot-7.38.0-py3-none-any.whl").write_bytes(b"wheel-bytes")
    output = tmp_path / "evidence"

    manifest_path, sbom_path = generate_release_evidence(
        repository_root=tmp_path,
        lock_path=lock,
        dist_directory=dist,
        output_directory=output,
        commit_sha=COMMIT,
        created_at=NOW,
    )
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    sbom = json.loads(sbom_path.read_text(encoding="utf-8"))

    assert manifest["package"] == {"name": "astra-trade-bot", "version": "7.38.0"}
    assert manifest["commit_sha"] == COMMIT
    assert manifest["locked_dependency_count"] == 2
    assert len(manifest["artifacts"][0]["sha256"]) == 64
    assert manifest["live_trading_allowed"] is False
    assert sbom["spdxVersion"] == "SPDX-2.3"
    assert len(sbom["packages"]) == 2
    assert sbom["documentNamespace"].endswith(COMMIT)


def test_release_evidence_rejects_short_or_non_hex_commit(tmp_path: Path) -> None:
    write_project(tmp_path)
    lock = tmp_path / "requirements.lock"
    write_lock(lock)
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "artifact.whl").write_bytes(b"x")
    with pytest.raises(ValueError, match="full lowercase Git SHA"):
        generate_release_evidence(
            repository_root=tmp_path,
            lock_path=lock,
            dist_directory=dist,
            output_directory=tmp_path / "out",
            commit_sha="deadbeef",
            created_at=NOW,
        )
