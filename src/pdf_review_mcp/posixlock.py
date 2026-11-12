"""Ephemeral, inode-validated POSIX write lock.

The lock file ``.<name>.pdf.review.lock`` lives beside the PDF only while a write is in
progress: the OWNER removes it while still holding the lock.

Protocol (all paths relative to a pinned directory fd, symlinks never followed):

1. ``openat(dir, name, O_RDWR|O_CREAT|O_NOFOLLOW|O_NONBLOCK|O_CLOEXEC, 0600)``; never O_TRUNC.
2. ``fstat``: must be a regular file with link count 1, owned by the current user,
   else fail closed (hard link, FIFO, device, foreign file => no write).
3. Poll ``flock(LOCK_EX|LOCK_NB)`` with backoff until the timeout.
4. With the lock held: the inode must still be linked (``st_nlink`` > 0) and the name must
   still refer to the SAME (dev, ino). Otherwise a previous owner removed it while we waited:
   close and retry on the new file. This check is what keeps a REMOVABLE lock file mutually
   exclusive between concurrent writers of this very version.
5. Release: while still holding the lock, unlink the name only if it still refers to our
   inode, then close (which drops the lock). Waiters on the old inode see ``st_nlink == 0``
   and retry. A contender that times out or fails validation never unlinks anything.

A crashed owner (SIGKILL, power loss) leaves only an empty file; the kernel drops the lock, and
the next operation reuses and removes it. No signal handlers are installed. Concurrent writers
are supported only if they all use this protocol.

Platforms without ``fcntl.flock`` or ``dir_fd`` support are refused (fail closed); there is no
untested fallback.
"""

from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX
    fcntl = None  # type: ignore[assignment]


class LockError(Exception):
    """Base class (the store maps these to user-facing ReviewErrors)."""


class LockUnsupported(LockError):
    pass


class LockUnsafe(LockError):
    pass


class LockTimeout(LockError):
    pass


def supported() -> bool:
    return (fcntl is not None and hasattr(fcntl, "flock") and os.open in os.supports_dir_fd
            and os.unlink in os.supports_dir_fd and os.stat in os.supports_dir_fd
            and hasattr(os, "O_NOFOLLOW") and hasattr(os, "O_DIRECTORY"))


@dataclass
class LockHandle:
    lock_path: Path
    dfd: int
    fd: int
    ident: tuple[int, int]
    retries: int = 0
    created_new: bool = False
    removed: bool = field(default=False)

    def release(self) -> None:
        """Unlink (only our inode, while holding the lock) then close. Never raises for cleanup."""
        name = self.lock_path.name
        try:
            try:
                st = os.stat(name, dir_fd=self.dfd, follow_symlinks=False)
                if stat.S_ISREG(st.st_mode) and (st.st_dev, st.st_ino) == self.ident:
                    os.unlink(name, dir_fd=self.dfd)
                    self.removed = True
            except OSError:
                pass  # e.g. read-only directory: the empty file stays, the lock is still dropped
        finally:
            try:
                os.close(self.fd)  # drops the flock
            finally:
                os.close(self.dfd)

    def __enter__(self) -> "LockHandle":
        return self

    def __exit__(self, *exc) -> None:
        self.release()


def acquire(lock_path: Path, timeout: float, *, poll: float = 0.005, max_poll: float = 0.1) -> LockHandle:
    if not supported():
        raise LockUnsupported("this platform lacks flock/dir_fd support; writes are refused")
    lock_path = Path(lock_path)
    name = lock_path.name
    try:
        dfd = os.open(lock_path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError as exc:
        raise LockUnsafe(f"cannot open the PDF directory for locking ({exc.strerror})") from None
    deadline = time.monotonic() + max(0.0, timeout)
    retries = 0
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        while True:
            try:
                existed = True
                try:
                    os.stat(name, dir_fd=dfd, follow_symlinks=False)
                except FileNotFoundError:
                    existed = False
                fd = os.open(name, flags, 0o600, dir_fd=dfd)
            except OSError as exc:
                raise LockUnsafe(f"cannot open lock file ({exc.strerror}); refusing to write "
                                 "(a symlink or special file at the lock path?)") from None
            try:
                st = os.fstat(fd)
                if not stat.S_ISREG(st.st_mode):
                    raise LockUnsafe("lock path is not a regular file; refusing to write")
                if st.st_nlink > 1:
                    raise LockUnsafe("lock file has extra hard links; refusing to write")
                if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
                    raise LockUnsafe("lock file is owned by another user; refusing to write")
                delay = poll
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise LockTimeout(f"PDF is locked by another writer (waited {timeout}s)") from None
                        time.sleep(delay)
                        delay = min(max_poll, delay * 2)
                held = os.fstat(fd)
                try:
                    now = os.stat(name, dir_fd=dfd, follow_symlinks=False)
                    same = (stat.S_ISREG(now.st_mode)
                            and (now.st_dev, now.st_ino) == (held.st_dev, held.st_ino))
                except FileNotFoundError:
                    same = False
                if held.st_nlink == 0 or not same:
                    os.close(fd)  # stale inode (a previous owner removed it); start over
                    retries += 1
                    if time.monotonic() >= deadline:
                        raise LockTimeout(f"PDF is locked by another writer (waited {timeout}s)")
                    continue
                return LockHandle(lock_path, dfd, fd, (held.st_dev, held.st_ino), retries,
                                  created_new=not existed)
            except BaseException:
                try:
                    os.close(fd)
                except OSError:
                    pass
                raise
    except BaseException:
        os.close(dfd)
        raise
