"""Fail-closed, no-replace directory publication for immutable research evidence.

On POSIX, os.rename/os.replace may replace an existing *empty* directory after
an earlier existence check. Linux RENAME_NOREPLACE makes refusal atomic.
No unsafe check-then-rename fallback is permitted.
"""

from __future__ import annotations

import ctypes
import errno
import os
import sys
from pathlib import Path


class AtomicPublicationError(ValueError):
    """A research evidence directory could not be published without replacement."""


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
        return
    error_number = ctypes.get_errno()
    if error_number in (errno.EEXIST, errno.ENOTEMPTY):
        raise AtomicPublicationError("destination appeared during publication; no overwrite")
    raise AtomicPublicationError(
        f"atomic no-replace publication refused: {os.strerror(error_number)}"
    )
