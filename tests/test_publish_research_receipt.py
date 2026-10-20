"""Negative concurrency and consistency regressions for immutable receipts."""
import errno
import json

import pytest

import tools.publish_research_receipt as publisher
from tools.publish_research_receipt import ReceiptPublicationError, publish_new_json_receipt


def test_publish_complete_receipt_and_cleanup_stage(tmp_path):
    destination = tmp_path / "audit" / "receipt.json"
    publish_new_json_receipt(destination, {"models": 36, "profitability_proven": False})
    assert json.loads(destination.read_text()) == {"models": 36, "profitability_proven": False}
    assert destination.read_text().endswith("\n")
    assert not list(destination.parent.glob(".astra-receipt-*"))


def test_existing_empty_file_not_overwritten(tmp_path):
    target = tmp_path / "receipt.json"
    target.write_bytes(b"")
    with pytest.raises(ReceiptPublicationError, match="already exists"):
        publish_new_json_receipt(target, {"models": 36})
    assert target.read_bytes() == b""


def test_existing_symlink_not_followed(tmp_path):
    protected = tmp_path / "protected.json"
    protected.write_text("leave me")
    target = tmp_path / "receipt.json"
    target.symlink_to(protected)
    with pytest.raises(ReceiptPublicationError, match="already exists"):
        publish_new_json_receipt(target, {"models": 36})
    assert protected.read_text() == "leave me"
    assert target.is_symlink()


def test_competitor_arrives_during_commit(tmp_path, monkeypatch):
    target = tmp_path / "receipt.json"

    def competing_writer(source, destination, *, follow_symlinks):
        assert follow_symlinks is False
        destination.write_text("competitor")
        raise FileExistsError(errno.EEXIST, "File exists")

    monkeypatch.setattr(publisher.os, "link", competing_writer)
    with pytest.raises(ReceiptPublicationError, match="appeared"):
        publish_new_json_receipt(target, {"models": 36})
    assert target.read_text() == "competitor"
    assert not list(tmp_path.glob(".astra-receipt-*"))


def test_unsupported_hardlink_fails_closed(tmp_path, monkeypatch):
    target = tmp_path / "receipt.json"

    def unsupported(source, destination, *, follow_symlinks):
        raise OSError(errno.EOPNOTSUPP, "Operation not supported")

    monkeypatch.setattr(publisher.os, "link", unsupported)
    with pytest.raises(ReceiptPublicationError, match="unavailable"):
        publish_new_json_receipt(target, {"models": 36})
    assert not target.exists()
    assert not list(tmp_path.glob(".astra-receipt-*"))


def test_parent_symlink_refused(tmp_path):
    protected = tmp_path / "protected"
    protected.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(protected, target_is_directory=True)
    with pytest.raises(ReceiptPublicationError, match="symlinked receipt parent"):
        publish_new_json_receipt(alias / "receipt.json", {"models": 36})
    assert not list(protected.iterdir())


def test_nan_receipt_rejected(tmp_path):
    target = tmp_path / "receipt.json"
    with pytest.raises(ReceiptPublicationError, match="invalid JSON"):
        publish_new_json_receipt(target, {"result": float("nan")})
    assert not target.exists()
