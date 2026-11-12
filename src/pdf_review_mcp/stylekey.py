"""Local, owner-only HMAC key that authenticates completion-style records.

The key is NOT a comment database and is never written to a PDF, a log or a tool result. It
only lets this installation recognise /PDFReviewStyle records that it created itself, so that
a forged record inside a PDF can never make the server re-point an appearance or recolour.

Location: $XDG_CONFIG_HOME/pdf-review-mcp/completion-style.key (default ~/.config/...).
Created lazily (only when a styling write needs to sign), exclusively, mode 0600, in a 0700
directory. Any unsafe condition (symlink, wrong owner, group/other permissions, wrong size,
unreadable) makes the key unavailable: callers then skip styling (fail closed). Permissions of
existing paths are never changed.
"""

from __future__ import annotations

import errno
import hashlib
import hmac
import os
import secrets
import stat
from pathlib import Path

KEY_NAME = "completion-style.key"
KEY_BYTES = 32


class KeyUnavailable(Exception):
    """No usable key (missing, unsafe or unreadable). Message never contains key material."""


def key_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "")
    root = Path(base) if base and os.path.isabs(base) else Path.home() / ".config"
    return root / "pdf-review-mcp" / KEY_NAME


def _uid() -> int:
    if not hasattr(os, "getuid"):
        raise KeyUnavailable("owner checks are not supported on this platform")
    return os.getuid()


def _check_dir(d: Path) -> None:
    try:
        st = os.lstat(d)
    except FileNotFoundError:
        raise KeyUnavailable("key directory does not exist") from None
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise KeyUnavailable("key directory is a symlink or not a directory")
    if st.st_uid != _uid():
        raise KeyUnavailable("key directory is not owned by the current user")
    if st.st_mode & 0o077:
        raise KeyUnavailable("key directory is accessible by group/others (needs 0700)")


def _create(p: Path) -> None:
    tmp = p.with_name(f".{KEY_NAME}.{secrets.token_hex(8)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w") as fh:
            fh.write(secrets.token_hex(KEY_BYTES) + "\n")
            fh.flush()
            os.fsync(fh.fileno())
        try:
            os.link(tmp, p)  # atomic; never replaces an existing key (concurrent creators agree)
        except FileExistsError:
            pass
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
    try:
        dfd = os.open(p.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


def load_key(create: bool) -> tuple[bytes, str]:
    """Return (key, key_id). key_id is a public fingerprint (HMAC of a constant), not the key."""
    p = key_path()
    d = p.parent
    if not os.path.lexists(d):
        if not create:
            raise KeyUnavailable("no local completion-style key")
        try:
            os.makedirs(d, mode=0o700, exist_ok=True)
        except OSError as exc:
            raise KeyUnavailable(f"cannot create key directory ({exc.strerror})") from None
    _check_dir(d)
    flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(p, flags)
    except FileNotFoundError:
        if not create:
            raise KeyUnavailable("no local completion-style key") from None
        try:
            _create(p)
            fd = os.open(p, flags)
        except OSError as exc:
            raise KeyUnavailable(f"cannot create key ({exc.strerror})") from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise KeyUnavailable("key file is a symlink") from None
        raise KeyUnavailable(f"cannot open key ({exc.strerror})") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise KeyUnavailable("key file is not a regular file")
        if st.st_uid != _uid():
            raise KeyUnavailable("key file is not owned by the current user")
        if st.st_mode & 0o077:
            raise KeyUnavailable("key file is accessible by group/others (needs 0600)")
        if st.st_size != KEY_BYTES * 2 + 1:
            raise KeyUnavailable("key file has an unexpected size")
        raw = os.read(fd, KEY_BYTES * 2 + 2)
    finally:
        os.close(fd)
    try:
        key = bytes.fromhex(raw.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError):
        raise KeyUnavailable("key file is malformed") from None
    if len(key) != KEY_BYTES:
        raise KeyUnavailable("key file is malformed")
    kid = hmac.new(key, b"pdf-review-mcp key id", hashlib.sha256).hexdigest()[:16]
    return key, kid
