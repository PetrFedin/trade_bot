"""Publish completed ASTRA research receipts atomically and without replacement.

A checked exists() followed by write_text() or os.replace() is not safe:
another writer can create a destination in between. A temporary same-directory
file plus a hard-link create makes refusal of existing destinations atomic.
This only establishes local receipt integrity, not historical-data provenance.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path


class ReceiptPublicationError(ValueError):
    """The receipt could not be safely published as a new immutable file."""


def publish_new_json_receipt(destination: Path, receipt: dict) -> None:
    """Persist full JSON then create destination only if it does not exist.

    Requires a filesystem supporting same-directory hard links and directory
    fsync. Never silently falls back to check-then-write or replacement.
    """
    destination = Path(destination)
    parent = destination.parent
    if any(item.is_symlink() for item in (parent, *parent.parents)):
        raise ReceiptPublicationError("symlinked receipt parent forbidden")
    parent.mkdir(parents=True, exist_ok=True)
    if destination.exists() or destination.is_symlink():
        raise ReceiptPublicationError("receipt destination already exists; no overwrite")
    try:
        serialized = json.dumps(receipt, indent=2, sort_keys=True, allow_nan=False)
        raw = (serialized + "\n").encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ReceiptPublicationError("invalid JSON receipt") from exc
    descriptor, temporary = tempfile.mkstemp(prefix=".astra-receipt-", suffix=".json", dir=parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError as exc:
            raise ReceiptPublicationError("receipt destination appeared; no overwrite") from exc
        except OSError as exc:
            raise ReceiptPublicationError("atomic receipt publication unavailable") from exc
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    # Persist the new final name and the removal of the temporary name.
    descriptor = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
