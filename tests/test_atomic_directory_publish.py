"""Negative contracts for no-replace publication of ASTRA research evidence."""
from __future__ import annotations

import sys

import pytest

from tools.atomic_directory_publish import AtomicPublicationError, publish_new_directory


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux renameat2 required")
def test_atomic_publish_creates_new_directory_without_copy(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "evidence.json").write_text('{"research_only": true}')
    target = tmp_path / "approved"
    publish_new_directory(stage, target)
    assert not stage.exists()
    assert (target / "evidence.json").read_text() == '{"research_only": true}'


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux renameat2 required")
def test_preexisting_empty_directory_must_not_be_replaced(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "proof").write_text("new")
    target = tmp_path / "approved"
    target.mkdir()
    with pytest.raises(AtomicPublicationError, match="already exists"):
        publish_new_directory(stage, target)
    assert stage.exists()
    assert target.is_dir()
    assert not (target / "proof").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux renameat2 required")
def test_symlink_destination_must_not_be_followed_or_replaced(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    protected = tmp_path / "protected"
    protected.mkdir()
    target = tmp_path / "approved"
    target.symlink_to(protected, target_is_directory=True)
    with pytest.raises(AtomicPublicationError, match="already exists"):
        publish_new_directory(stage, target)
    assert target.is_symlink()
    assert stage.exists()
    assert not list(protected.iterdir())


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux renameat2 required")
def test_renameat2_closes_race_after_initial_existence_check(tmp_path, monkeypatch):
    """Simulate another publisher creating target before atomic kernel rename."""
    import tools.atomic_directory_publish as publisher

    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "proof").write_text("new")
    target = tmp_path / "approved"
    original_cdll = publisher.ctypes.CDLL

    class RacingLibrary:
        def __init__(self, real):
            self.real = real

        @property
        def renameat2(self):
            target.mkdir()
            return self.real.renameat2

    monkeypatch.setattr(
        publisher.ctypes, "CDLL",
        lambda *args, **kwargs: RacingLibrary(original_cdll(*args, **kwargs)),
    )
    with pytest.raises(AtomicPublicationError, match="appeared during publication"):
        publish_new_directory(stage, target)
    assert stage.exists()
    assert (stage / "proof").read_text() == "new"
    assert target.is_dir()
    assert not (target / "proof").exists()


def test_unsupported_platform_fails_closed(tmp_path, monkeypatch):
    import tools.atomic_directory_publish as publisher

    stage = tmp_path / "stage"
    stage.mkdir()
    target = tmp_path / "approved"
    monkeypatch.setattr(publisher.sys, "platform", "darwin")
    with pytest.raises(AtomicPublicationError, match="requires Linux"):
        publish_new_directory(stage, target)
    assert stage.exists()
    assert not target.exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux renameat2 required")
def test_staged_file_symlink_cannot_be_published(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    protected = tmp_path / "protected"
    protected.write_text("original")
    (stage / "linked.json").symlink_to(protected)
    target = tmp_path / "approved"

    with pytest.raises(AtomicPublicationError, match="durability sync failed"):
        publish_new_directory(stage, target)
    assert stage.is_dir()
    assert not target.exists()
    assert protected.read_text() == "original"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux renameat2 required")
def test_staged_directory_symlink_cannot_be_published(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    source = tmp_path / "elsewhere"
    source.mkdir()
    (stage / "linked").symlink_to(source, target_is_directory=True)

    with pytest.raises(AtomicPublicationError, match="symlinked directory"):
        publish_new_directory(stage, tmp_path / "approved")
    assert stage.is_dir()
    assert not (tmp_path / "approved").exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux renameat2 required")
def test_staged_file_sync_failure_does_not_publish(tmp_path, monkeypatch):
    import os
    import stat

    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "evidence.json").write_text("source")
    target = tmp_path / "approved"
    real_fsync = os.fsync

    def fail_file_sync(fd):
        if stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError("injected file durability failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_file_sync)
    with pytest.raises(AtomicPublicationError, match="durability sync failed"):
        publish_new_directory(stage, target)
    assert (stage / "evidence.json").read_text() == "source"
    assert not target.exists()


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux renameat2 required")
def test_published_parent_sync_failure_not_claimed_success(tmp_path, monkeypatch):
    import os
    import stat

    staging = tmp_path / "staging"
    staging.mkdir()
    stage = staging / "stage"
    stage.mkdir()
    (stage / "proof").write_text("source")
    target = tmp_path / "approved"
    expected_parent = os.stat(tmp_path)
    real_fsync = os.fsync

    def fail_destination_parent_sync(fd):
        meta = os.fstat(fd)
        if (
            stat.S_ISDIR(meta.st_mode)
            and (meta.st_ino, meta.st_dev)
            == (expected_parent.st_ino, expected_parent.st_dev)
        ):
            raise OSError("injected destination metadata durability failure")
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_destination_parent_sync)
    with pytest.raises(AtomicPublicationError, match="published directory durability sync failed"):
        publish_new_directory(stage, target)
    assert not stage.exists()
    assert (target / "proof").read_text() == "source"


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux renameat2 required")
def test_symlinked_target_parent_rejected_without_publication(tmp_path):
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "proof").write_text("source")
    protected = tmp_path / "protected"
    protected.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(protected, target_is_directory=True)
    with pytest.raises(AtomicPublicationError, match="symlinked publication parent"):
        publish_new_directory(stage, alias / "approved")
    assert stage.exists()
    assert not list(protected.iterdir())
