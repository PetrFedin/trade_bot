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
