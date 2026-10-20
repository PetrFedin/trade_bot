"""Fail-closed, no-replace directory publication for immutable research evidence.

On POSIX, os.rename/os.replace may replace an existing *empty* directory after
an earlier existence check. Linux RENAME_NOREPLACE makes refusal atomic.
No unsafe check-then-rename fallback is permitted.
"""

from __future__ import annotations

import ctypes
import errno
import os
import stat
import sys
from pathlib import Path


class AtomicPublicationError(ValueError):
    """A research evidence directory could not be published without replacement."""


def _sync_staged_tree(root: Path) -> None:
    """Persist staged files and directory entries before publishing a source set."""
    def reject_walk(error: OSError) -> None:
        raise AtomicPublicationError("staged tree traversal failed") from error

    def sync_path(path: Path, *, directory: bool) -> None:
        flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
        if directory:
            flags |= os.O_DIRECTORY
        try:
            descriptor = os.open(path, flags)
            try:
                metadata = os.fstat(descriptor)
                correct_type = (
                    stat.S_ISDIR(metadata.st_mode) if directory
                    else stat.S_ISREG(metadata.st_mode)
                )
                if not correct_type:
                    raise AtomicPublicationError("staged tree contains non-file evidence")
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            raise AtomicPublicationError("staged evidence durability sync failed") from exc

    for current, directories, files in os.walk(
        root, topdown=False, followlinks=False, onerror=reject_walk,
    ):
        parent = Path(current)
        for name in directories:
            if (parent / name).is_symlink():
                raise AtomicPublicationError("staged tree contains symlinked directory")
        for name in files:
            sync_path(parent / name, directory=False)
        sync_path(parent, directory=True)


def publish_new_directory(staged: Path, destination: Path) -> None:
    """Publish a fully validated staged directory only when target is absent.

    Caller must keep staging and destination on the same filesystem. Systems
    without Linux renameat2 fail closed, preserving unpublished staging data
    until caller cleanup. This operation does not attest provider authenticity.
    """
    staged = Path(staged)
    destination = Path(destination)
    if not staged.is_dir() or staged.is_symlink():
        raise AtomicPublicationError("staged source must be an existing real directory")
    if destination.exists() or destination.is_symlink():
        raise AtomicPublicationError("destination already exists; no overwrite")
    if not sys.platform.startswith("linux"):
        raise AtomicPublicationError("atomic no-replace directory publish requires Linux")
    if any(parent.is_symlink() for parent in (destination.parent, *destination.parent.parents)):
        raise AtomicPublicationError("symlinked publication parent forbidden")

    _sync_staged_tree(staged)
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise AtomicPublicationError("kernel no-replace rename interface unavailable")
    renameat2.argtypes = [
        ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    ctypes.set_errno(0)
    status = renameat2(
        -100, os.fsencode(staged), -100, os.fsencode(destination), 1,
    )
    if status == 0:
        # Rename durability requires both source and destination parent names.
        for parent in {staged.parent, destination.parent}:
            try:
                directory_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError as exc:
                raise AtomicPublicationError("published directory durability sync failed") from exc
        return
    error_number = ctypes.get_errno()
    if error_number in (errno.EEXIST, errno.ENOTEMPTY):
        raise AtomicPublicationError("destination appeared during publication; no overwrite")
    raise AtomicPublicationError(
        f"atomic no-replace publication refused: {os.strerror(error_number)}"
    )
