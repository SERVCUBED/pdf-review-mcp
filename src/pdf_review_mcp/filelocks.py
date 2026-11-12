"""Transient write lock beside the PDF, provided by ``portalocker.TemporaryFileLock``.

The lock file ``.<name>.pdf.review.lock`` exists only while a write is in progress.
portalocker (BSD-3-Clause, https://github.com/wolph/portalocker) supplies the whole locking
protocol:

* ``flock(LOCK_EX|LOCK_NB)``, polled until the timeout;
* after locking, a check that the open handle is still the file at the path, with a retry
  if a previous owner removed it in the meantime;
* on release, an unlink while the lock is still held, and only if the path still names the
  held file.

A contender that times out never unlinks. A killed owner leaves an empty file, which the
kernel has already unlocked; the next write reuses it and removes it.

This module adds only an **open guard**, passed to ``open()`` as its ``opener``. It opens with
``O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC``, mode 0600 and never ``O_TRUNC``, then checks the opened
descriptor with ``fstat``: it must be a regular file with no extra hard links, owned by the
current user.
Because the check is made on the descriptor that is locked afterwards, it is race-free for
that file. Symlinks, dangling symlinks, FIFOs, devices, hard links and foreign files fail
closed before portalocker can lock or truncate anything.

``TemporaryFileLock`` does not accept ``open()`` keyword arguments in its constructor. The
guard is therefore set through the public ``file_open_kwargs`` attribute it inherits from
``portalocker.Lock`` (documented there as "passed straight through to open"). That hook is
reviewed only for **portalocker 4.4.2**. The dependency is pinned exactly (``==4.4.2``), and
``acquire`` refuses any other installed version, a missing or failed install, *before*
constructing the lock or opening any file, so an unreviewed release cannot open or truncate
anything. The check reads installed package metadata. It guards against version drift, not
against a tampered copy of 4.4.2. Upgrading needs a review and a rerun of the lock tests.
After acquiring, the adapter also checks that the held descriptor is one the guard opened.
That check is only defence in depth: it can detect a bypass, but only after the open.

Limits: the lock is cooperative, and covers only local POSIX filesystems on one host (no NFS
claim) in a stable directory controlled by the PDF's owner. Paths are resolved by name, not
through a pinned directory descriptor. On release, portalocker compares the path with ``stat``,
which follows symlinks, so a symlink to the held file swapped in during the hold would have
the link removed. Safety against other processes renaming or replacing entries in the
directory during a write is therefore **not** promised. Non-POSIX platforms are refused.
"""

from __future__ import annotations

import importlib.metadata
import os
import stat
from pathlib import Path

try:
    import portalocker
except ImportError:  # missing or broken install: writes are refused, reads keep working
    portalocker = None  # type: ignore[assignment]

#: The only portalocker release whose open path (``Lock._get_fh`` consulting
#: ``file_open_kwargs``, truncation only after locking) has been reviewed and tested.
REVIEWED_PORTALOCKER = "4.4.2"

_CHECK_INTERVAL_S = 0.02


class LockError(Exception):
    """Base class (the store maps these to user-facing ReviewErrors)."""


class LockUnsupported(LockError):
    pass


class LockUnsafe(LockError):
    pass


class LockTimeout(LockError):
    pass


def supported() -> bool:
    return os.name == "posix" and hasattr(os, "O_NOFOLLOW") and hasattr(os, "geteuid")


def installed_portalocker_version() -> str | None:
    try:
        return importlib.metadata.version("portalocker")
    except importlib.metadata.PackageNotFoundError:
        return None


def check_portalocker() -> None:
    """Refuse (no side effects) unless exactly the reviewed portalocker is installed."""
    installed = installed_portalocker_version()
    if portalocker is None or installed != REVIEWED_PORTALOCKER:
        found = "not importable" if portalocker is None else f"version {installed or 'unknown'}"
        raise LockUnsupported(f"portalocker {found}; this build is reviewed only for "
                              f"portalocker=={REVIEWED_PORTALOCKER}; writes are refused")


def _guarded_open(path: str, flags: int) -> int:
    if flags & os.O_TRUNC:
        raise LockUnsafe("refusing a truncating open of the lock file")
    try:
        fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, 0o600)
    except OSError as exc:
        raise LockUnsafe(f"cannot open lock file ({exc.strerror}); refusing to write "
                         "(a symlink or special file at the lock path?)") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise LockUnsafe("lock path is not a regular file; refusing to write")
        # nlink 0 = an owner removed it after our open: portalocker's post-lock path check
        # detects that and retries, so only extra links are refused here.
        if st.st_nlink > 1:
            raise LockUnsafe("lock file has extra hard links; refusing to write")
        if st.st_uid != os.geteuid():
            raise LockUnsafe("lock file is owned by another user; refusing to write")
    except BaseException:
        os.close(fd)
        raise
    return fd


class LockHandle:
    """A held lock. ``guarded_opens`` counts the opens made through the guard (more than one
    means portalocker retried after finding the file it had opened was stale)."""

    def __init__(self, lock: portalocker.TemporaryFileLock, guarded_opens: list) -> None:
        self._lock = lock
        self._opens = guarded_opens

    @property
    def guarded_opens(self) -> int:
        return len(self._opens)

    def release(self) -> None:
        self._lock.release()  # unlinks (only its own file, while locked), then unlocks

    def __enter__(self) -> "LockHandle":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def acquire(lock_path: Path, timeout: float) -> LockHandle:
    if not supported():
        raise LockUnsupported("this platform is not POSIX (no O_NOFOLLOW/geteuid); writes are refused")
    check_portalocker()  # before any construction or file access
    lock = portalocker.TemporaryFileLock(str(lock_path), timeout=max(0.0, timeout),
                                         check_interval=_CHECK_INTERVAL_S, fail_when_locked=False)
    open_kwargs = getattr(lock, "file_open_kwargs", None)
    if not isinstance(open_kwargs, dict):
        raise LockUnsupported("the installed portalocker has no open hook; writes are refused")
    opened: list[tuple[int, int]] = []

    def opener(path: str, flags: int) -> int:
        fd = _guarded_open(path, flags)
        st = os.fstat(fd)
        opened.append((st.st_dev, st.st_ino))
        return fd

    open_kwargs["opener"] = opener
    try:
        fh = lock.acquire()
    except portalocker.AlreadyLocked:
        raise LockTimeout(f"PDF is locked by another writer (waited {timeout}s)") from None
    except portalocker.LockException as exc:
        raise LockUnsafe(f"cannot lock ({exc})") from None
    st = os.fstat(fh.fileno())
    if (st.st_dev, st.st_ino) not in opened:  # defence in depth only (detects, cannot prevent)
        lock.release()
        raise LockUnsupported("the lock file was not opened through the safety guard; writes are refused")
    return LockHandle(lock, opened)
