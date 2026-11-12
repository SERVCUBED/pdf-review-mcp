"""Ephemeral inode-validated lock: real processes, stale-inode waiters, crashes, hostile paths
and cleanup after success / no-op / errors."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from pdf_review_mcp import pdfstore, posixlock
from pdf_review_mcp.pdfstore import LockBusyError, ReviewError, StaleRevisionError, WriteBlockedError
from pdf_review_mcp.service import ReviewService

from conftest import make_plain_pdf

PY = sys.executable


def run_py(code: str, *args: str, **kw) -> subprocess.Popen:
    return subprocess.Popen([PY, "-c", textwrap.dedent(code), *args], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, **kw)


def lock_files(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir() if p.name.endswith(".review.lock"))


def rev(svc, f):
    return svc.review_summary(f)["revision"]


# ----------------------------------------------------------------- basic semantics


def test_lock_removed_after_success_noop_and_errors(svc, plain, root, monkeypatch):
    f = str(plain)
    r = svc.add_comment(f, 1, "x", rev(svc, f))
    assert lock_files(root) == []
    svc.sync_comment_appearance(f, r["id"], r["revision"])  # no-op path
    assert lock_files(root) == []
    with pytest.raises(StaleRevisionError):
        svc.add_comment(f, 1, "y", "sha256:" + "0" * 64)
    assert lock_files(root) == []
    with pytest.raises(ReviewError):
        svc.append_agent_response(f, "no-such-id", "z", "q", r["revision"])  # mutate raises
    assert lock_files(root) == []
    monkeypatch.setattr(pdfstore.os, "replace", lambda *a: (_ for _ in ()).throw(OSError(28, "full")))
    with pytest.raises(OSError):
        svc.add_comment(f, 1, "w", r["revision"])
    assert lock_files(root) == [] and not [p for p in os.listdir(root) if p.endswith(".tmp")]


def test_read_only_calls_create_no_lock(svc, plain, root):
    f = str(plain)
    svc.review_summary(f)
    svc.list_comments(f)
    svc.read_page_text(f, 1)
    assert lock_files(root) == []


def test_new_lock_file_mode_0600_and_existing_file_untouched(plain, root):
    lp = pdfstore.PdfStore().lock_path(plain)
    h = posixlock.acquire(lp, 1)
    assert h.created_new and (os.stat(lp).st_mode & 0o777) == 0o600
    h.release()
    assert not lp.exists()
    lp.write_bytes(b"")
    os.chmod(lp, 0o644)  # pre-existing (e.g. crash orphan or created by another tool)
    h = posixlock.acquire(lp, 1)
    assert not h.created_new and (os.stat(lp).st_mode & 0o777) == 0o644  # never chmod'ed
    h.release()
    assert not lp.exists()  # pre-existing orphan cleaned by the next owner


def test_unsupported_platform_fails_closed(svc, plain, monkeypatch):
    monkeypatch.setattr(posixlock, "fcntl", None)
    before = plain.read_bytes()
    with pytest.raises(WriteBlockedError, match="not supported"):
        svc.add_comment(str(plain), 1, "x", rev(svc, str(plain)))
    assert plain.read_bytes() == before


# ----------------------------------------------------------------- hostile paths


def test_symlink_lock_path_fails_closed(svc, plain, root, tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    lp = pdfstore.PdfStore().lock_path(plain)
    lp.symlink_to(victim)
    before = plain.read_bytes()
    with pytest.raises(WriteBlockedError, match="cannot open lock file"):
        svc.add_comment(str(plain), 1, "x", rev(svc, str(plain)))
    assert plain.read_bytes() == before and victim.read_text() == "keep me" and lp.is_symlink()


def test_dangling_symlink_lock_path_fails_closed(svc, plain, tmp_path):
    lp = pdfstore.PdfStore().lock_path(plain)
    lp.symlink_to(tmp_path / "does-not-exist")
    with pytest.raises(WriteBlockedError):
        svc.add_comment(str(plain), 1, "x", rev(svc, str(plain)))
    assert not (tmp_path / "does-not-exist").exists()  # O_NOFOLLOW: never created through link


def test_fifo_and_hardlink_fail_closed(svc, plain, root, tmp_path):
    lp = pdfstore.PdfStore().lock_path(plain)
    os.mkfifo(lp)
    with pytest.raises(WriteBlockedError, match="not a regular file"):
        svc.add_comment(str(plain), 1, "x", rev(svc, str(plain)))
    assert lp.exists()
    lp.unlink()
    other = tmp_path / "other.txt"
    other.write_text("data")
    os.link(other, lp)
    with pytest.raises(WriteBlockedError, match="hard links"):
        svc.add_comment(str(plain), 1, "x", rev(svc, str(plain)))
    assert other.read_text() == "data" and lp.exists()  # never truncated or unlinked


# ----------------------------------------------------------------- real processes

HOLDER = """
import sys, time
from pathlib import Path
from pdf_review_mcp import posixlock
h = posixlock.acquire(Path(sys.argv[1]), 10)
print("locked", h.ident[1], time.time(), flush=True)
sys.stdin.readline()
print("releasing", time.time(), flush=True)
h.release()
"""


def test_timeout_contender_does_not_unlink_owner_lock(plain):
    lp = pdfstore.PdfStore().lock_path(plain)
    holder = run_py(HOLDER, str(lp))
    try:
        assert holder.stdout.readline().startswith("locked")
        ino = os.stat(lp).st_ino
        with pytest.raises(posixlock.LockTimeout):
            posixlock.acquire(lp, 0.3)
        assert os.stat(lp).st_ino == ino  # owner's file intact
    finally:
        holder.communicate("\n", timeout=10)
    assert not lp.exists()


def test_waiter_on_unlinked_inode_retries_and_no_overlap(plain, tmp_path):
    lp = pdfstore.PdfStore().lock_path(plain)
    log = tmp_path / "log.txt"
    holder = run_py(HOLDER, str(lp))
    assert holder.stdout.readline().startswith("locked")
    old_ino = os.stat(lp).st_ino
    waiter = run_py("""
        import sys, time
        from pathlib import Path
        from pdf_review_mcp import posixlock
        h = posixlock.acquire(Path(sys.argv[1]), 20)
        print("waiter", h.retries, h.ident[1], time.time(), flush=True)
        h.release()
    """, str(lp))
    time.sleep(0.5)  # waiter has opened the OLD inode and is polling flock
    t_release = time.time()
    holder.communicate("\n", timeout=10)
    out, err = waiter.communicate(timeout=20)
    _, retries, ino, t_acq = out.split()
    assert waiter.returncode == 0, err
    assert int(retries) >= 1 and int(ino) != old_ino  # detected stale inode, moved to a new file
    assert float(t_acq) >= t_release - 0.05
    assert not lp.exists()
    log.write_text(out)


def test_crashed_owner_orphan_reused_and_removed(svc, plain):
    lp = pdfstore.PdfStore().lock_path(plain)
    holder = run_py(HOLDER, str(lp))
    assert holder.stdout.readline().startswith("locked")
    os.kill(holder.pid, signal.SIGKILL)
    holder.wait(timeout=10)
    assert lp.exists()  # SIGKILL: orphan file persists (documented), kernel dropped the lock
    svc.add_comment(str(plain), 1, "after crash", rev(svc, str(plain)))
    assert not lp.exists()


WRITER = """
import sys, time
from pdf_review_mcp.service import ReviewService
from pdf_review_mcp.pdfstore import StaleRevisionError, LockBusyError
f, n, tag = sys.argv[1], int(sys.argv[2]), sys.argv[3]
svc = ReviewService()
done = 0
while done < n:
    try:
        svc.add_comment(f, 1, f"{tag}-{done}", svc.review_summary(f)["revision"])
        done += 1
    except (StaleRevisionError, LockBusyError):
        time.sleep(0.01)
print("ok", done, flush=True)
"""


def test_repeated_contenders_real_processes_no_lost_updates(plain, root):
    procs = [run_py(WRITER, str(plain), "4", f"w{i}") for i in range(5)]
    outs = [p.communicate(timeout=180) for p in procs]
    assert all(p.returncode == 0 for p in procs), [o[1][-500:] for o in outs]
    s = ReviewService().list_comments(str(plain), limit=100)
    texts = sorted(c["text"] for c in s["comments"])
    assert texts == sorted(f"w{i}-{j}" for i in range(5) for j in range(4))
    assert lock_files(root) == [] and not [p for p in os.listdir(root) if p.endswith(".tmp")]


def test_counter_mutual_exclusion_many_processes(plain, tmp_path):
    lp = pdfstore.PdfStore().lock_path(plain)
    counter = tmp_path / "counter"
    counter.write_text("0")
    code = """
        import sys, time
        from pathlib import Path
        from pdf_review_mcp import posixlock
        lp, c = Path(sys.argv[1]), Path(sys.argv[2])
        for _ in range(25):
            h = posixlock.acquire(lp, 30)
            try:
                v = int(c.read_text()); time.sleep(0.001); c.write_text(str(v + 1))
            finally:
                h.release()
    """
    procs = [run_py(code, str(lp), str(counter)) for _ in range(6)]
    for p in procs:
        _, err = p.communicate(timeout=120)
        assert p.returncode == 0, err
    assert counter.read_text() == "150" and not lp.exists()
