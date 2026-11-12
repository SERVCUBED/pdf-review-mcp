"""Transient write lock (portalocker TemporaryFileLock + open guard): real processes, stale-inode
waiters, crashes, hostile lock paths, and cleanup after success / no-op / errors.

These are integration tests of the behaviour the store relies on; they do not re-test
portalocker internals."""

from __future__ import annotations

import os
import queue
import signal
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from pdf_review_mcp import filelocks, pdfstore
from pdf_review_mcp.pdfstore import LockBusyError, ReviewError, StaleRevisionError, WriteBlockedError
from pdf_review_mcp.service import ReviewService

PY = sys.executable


def run_py(code: str, *args: str) -> subprocess.Popen:
    return subprocess.Popen([PY, "-c", textwrap.dedent(code), *args], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def lock_files(d: Path) -> list[str]:
    return sorted(p.name for p in d.iterdir() if p.name.endswith(".review.lock"))


def rev(svc, f):
    return svc.review_summary(f)["revision"]


def _lines(proc):
    """Background reader so a test can wait for the next stdout line with a timeout."""
    q: queue.Queue = queue.Queue()

    def pump():
        for line in proc.stdout:
            q.put(line)
        q.put(None)

    threading.Thread(target=pump, daemon=True).start()

    def next_line(timeout=30):
        line = q.get(timeout=timeout)
        assert line is not None, f"process exited early: {proc.stderr.read()}"
        return line.split()

    return next_line


def _holds_inode(pid: int, ident: tuple[int, int]) -> bool:
    """True once process ``pid`` has a descriptor open on inode ``ident`` (black-box, via /proc)."""
    fd_dir = Path(f"/proc/{pid}/fd")
    for entry in fd_dir.iterdir():
        try:
            st = os.stat(entry)
        except OSError:
            continue
        if (st.st_dev, st.st_ino) == ident:
            return True
    return False


def _wait_holds(pid, ident, timeout=20.0):
    import time
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if _holds_inode(pid, ident):
            return
        time.sleep(0.01)
    raise AssertionError("waiter never opened the old lock inode")


def _finish(*procs):
    for p in procs:
        if p is not None and p.poll() is None:
            try:
                p.communicate("\n", timeout=15)
            except Exception:
                p.kill()


needs_proc = pytest.mark.skipif(not Path("/proc/self/fd").is_dir(), reason="needs /proc/<pid>/fd")


# ----------------------------------------------------------------- cleanup and modes


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


def test_new_file_0600_existing_mode_kept_and_legacy_empty_orphan_removed(svc, plain):
    lp = pdfstore.PdfStore().lock_path(plain)
    with filelocks.acquire(lp, 1) as h:
        assert h.guarded_opens == 1 and (os.stat(lp).st_mode & 0o777) == 0o600
    assert not lp.exists()
    lp.write_bytes(b"")  # empty orphan as left by older versions or a crash
    os.chmod(lp, 0o644)
    with filelocks.acquire(lp, 1):
        assert (os.stat(lp).st_mode & 0o777) == 0o644  # never chmod'ed
    assert not lp.exists()
    lp.write_bytes(b"")
    svc.add_comment(str(plain), 1, "after legacy orphan", rev(svc, str(plain)))
    assert not lp.exists()


def test_unsupported_platform_fails_closed(svc, plain, monkeypatch):
    monkeypatch.setattr(filelocks, "supported", lambda: False)
    before = plain.read_bytes()
    with pytest.raises(WriteBlockedError, match="not supported"):
        svc.add_comment(str(plain), 1, "x", rev(svc, str(plain)))
    assert plain.read_bytes() == before


# ----------------------------------------------------------------- hostile lock paths


@pytest.mark.parametrize("kind", ["symlink", "dangling", "fifo", "hardlink", "directory", "foreign"])
def test_hostile_lock_path_fails_closed_without_truncation(svc, plain, tmp_path, monkeypatch, kind):
    lp = pdfstore.PdfStore().lock_path(plain)
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me")
    missing = tmp_path / "does-not-exist"
    if kind == "symlink":
        lp.symlink_to(victim)
    elif kind == "dangling":
        lp.symlink_to(missing)
    elif kind == "fifo":
        os.mkfifo(lp)
    elif kind == "hardlink":
        os.link(victim, lp)
    elif kind == "directory":
        lp.mkdir()
    elif kind == "foreign":
        # A real foreign-owned file needs root; simulate "owner != us" for the guard's fstat check.
        lp.write_text("someone else's")
        real = os.geteuid()
        monkeypatch.setattr(filelocks.os, "geteuid", lambda: real + 1)
    before = plain.read_bytes()
    f = str(plain)
    r0 = rev(svc, f)
    with pytest.raises(WriteBlockedError, match="Refusing to write"):
        svc.add_comment(f, 1, "x", r0)
    assert plain.read_bytes() == before
    assert victim.read_text() == "keep me"               # never truncated through a link
    assert not missing.exists()                          # never created through a dangling link
    assert os.path.lexists(lp)                           # never unlinked by the refused writer
    if kind == "foreign":
        assert lp.read_text() == "someone else's"


# ----------------------------------------------------------------- real processes

HOLDER = """
import sys
from pathlib import Path
from pdf_review_mcp import filelocks
lp = Path(sys.argv[1])
h = filelocks.acquire(lp, 10)
with open(lp, "w") as other:          # holder's own data in its lock file (another descriptor)
    other.write("holder-data")
st = lp.stat()
print("locked", st.st_dev, st.st_ino, flush=True)
sys.stdin.readline()
h.release()
print("released", flush=True)
"""


def test_timeout_contender_preserves_holder_file_and_pdf(svc, plain, monkeypatch):
    lp = pdfstore.PdfStore().lock_path(plain)
    holder = run_py(HOLDER, str(lp))
    try:
        nxt = _lines(holder)
        tag, dev, ino = nxt()
        assert tag == "locked"
        monkeypatch.setattr(pdfstore, "LOCK_TIMEOUT_S", 0.3)
        before = plain.read_bytes()
        with pytest.raises(LockBusyError, match="locked by another writer"):
            svc.add_comment(str(plain), 1, "blocked", rev(svc, str(plain)))
        assert plain.read_bytes() == before
        st = os.stat(lp, follow_symlinks=False)
        assert (st.st_dev, st.st_ino) == (int(dev), int(ino))  # owner's file not unlinked
        assert lp.read_text() == "holder-data"                 # nor truncated
        holder.stdin.write("\n")
        holder.stdin.flush()
        assert nxt() == ["released"]
    finally:
        _finish(holder)
    assert not lp.exists()


def test_pathname_replaced_during_hold_is_not_removed(plain, tmp_path):
    lp = pdfstore.PdfStore().lock_path(plain)
    h = filelocks.acquire(lp, 1)
    repl = tmp_path / "replacement"
    repl.write_text("replacement")
    os.replace(repl, lp)  # a third party swaps the pathname while we hold the lock
    h.release()
    assert lp.read_text() == "replacement"  # release never removes a file it does not hold


def test_killed_holder_orphan_recovered(svc, plain):
    lp = pdfstore.PdfStore().lock_path(plain)
    holder = run_py(HOLDER, str(lp))
    assert holder.stdout.readline().startswith("locked")
    os.kill(holder.pid, signal.SIGKILL)
    holder.wait(timeout=10)
    assert lp.exists()  # SIGKILL: the file stays (documented); the kernel dropped the lock
    svc.add_comment(str(plain), 1, "after crash", rev(svc, str(plain)))
    assert not lp.exists()


CS_WAITER = """
import os, sys, time
from pathlib import Path
from pdf_review_mcp import filelocks
lp, marker = Path(sys.argv[1]), Path(sys.argv[2])
h = filelocks.acquire(lp, 30)
now = os.stat(lp, follow_symlinks=False)
overlap = marker.exists()
marker.write_text(str(os.getpid()))
time.sleep(0.05)
marker.unlink()
print("acquired", h.guarded_opens, now.st_dev, now.st_ino, now.st_nlink, int(overlap), flush=True)
h.release()
"""


@needs_proc
def test_two_waiters_on_unlinked_inode_retry_without_overlap(plain, tmp_path):
    """Two contenders that opened the lock file before the owner removed it must each detect
    the stale inode, retry on a fresh file and never overlap. The old inode is pinned so
    "new inode != old inode" is meaningful on ext4 (immediate inode-number reuse)."""
    lp = pdfstore.PdfStore().lock_path(plain)
    marker = tmp_path / "critical-section"
    holder = run_py(HOLDER, str(lp))
    waiters: list = []
    pin = None
    try:
        h_next = _lines(holder)
        tag, dev, ino = h_next()
        assert tag == "locked"
        old = (int(dev), int(ino))
        pin = os.open(lp, os.O_RDONLY)  # keeps the old inode allocated; no flock
        waiters = [run_py(CS_WAITER, str(lp), str(marker)) for _ in range(2)]
        for w in waiters:
            _wait_holds(w.pid, old)  # each waiter has the OLD inode open and is polling it
        holder.stdin.write("\n")
        holder.stdin.flush()
        assert h_next() == ["released"]
        assert os.fstat(pin).st_nlink == 0  # removed by the owner
        results = []
        for w in waiters:
            out, err = w.communicate(timeout=30)
            assert w.returncode == 0, err
            results.append(out.split())
        for tag, opens, ndev, nino, nlink, overlap in results:
            assert tag == "acquired"
            assert int(opens) >= 2                      # stale descriptor detected, reopened
            assert int(overlap) == 0
            assert int(nlink) == 1 and (int(ndev), int(nino)) != old
    finally:
        _finish(holder, *waiters)
        if pin is not None:
            os.close(pin)
    assert not lp.exists() and not marker.exists()


COUNTER = """
import sys, time
from pathlib import Path
from pdf_review_mcp import filelocks
lp, c, marker = Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3])
for _ in range(20):
    with filelocks.acquire(lp, 60):
        if marker.exists():
            print("OVERLAP", flush=True)
        marker.touch()
        v = int(c.read_text()); time.sleep(0.001); c.write_text(str(v + 1))
        marker.unlink()
"""


def test_counter_mutual_exclusion_8_processes_x20(plain, tmp_path):
    lp = pdfstore.PdfStore().lock_path(plain)
    counter, marker = tmp_path / "counter", tmp_path / "in-critical-section"
    counter.write_text("0")
    procs = [run_py(COUNTER, str(lp), str(counter), str(marker)) for _ in range(8)]
    for p in procs:
        out, err = p.communicate(timeout=180)
        assert p.returncode == 0, err
        assert "OVERLAP" not in out
    assert counter.read_text() == "160" and not lp.exists() and not marker.exists()


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


def test_concurrent_service_writers_no_lost_updates(plain, root):
    procs = [run_py(WRITER, str(plain), "4", f"w{i}") for i in range(5)]
    outs = [p.communicate(timeout=180) for p in procs]
    assert all(p.returncode == 0 for p in procs), [o[1][-500:] for o in outs]
    s = ReviewService().list_comments(str(plain), limit=100)
    texts = sorted(c["text"] for c in s["comments"])
    assert texts == sorted(f"w{i}-{j}" for i in range(5) for j in range(4))
    assert lock_files(root) == [] and not [p for p in os.listdir(root) if p.endswith(".tmp")]


# ----------------------------------------------------------------- reviewed-version gate


class _UnreviewedPortalocker:
    """Stand-in for an unreviewed release whose lock ignores the open guard and truncates the
    lock path through any symlink: constructing or acquiring it would damage the victim."""

    constructed = 0

    class AlreadyLocked(Exception):
        pass

    LockException = AlreadyLocked

    class TemporaryFileLock:
        def __init__(self, filename, *a, **k):
            _UnreviewedPortalocker.constructed += 1
            self.filename = filename

        def acquire(self):
            return open(self.filename, "w")  # plain truncating open, follows symlinks


@pytest.mark.parametrize("installed", ["4.4.1", "4.4.3", "4.5.0", "5.0.0", "4.4.2.post1", None, "module-missing"])
def test_unreviewed_portalocker_refused_before_any_lock_construction_or_open(
        svc, plain, root, tmp_path, monkeypatch, installed):
    import builtins

    lp = pdfstore.PdfStore().lock_path(plain)
    victim = tmp_path / "victim.txt"
    victim.write_text("DO NOT TRUNCATE")
    lp.symlink_to(victim)
    f = str(plain)
    r0 = rev(svc, f)
    before = plain.read_bytes()
    _UnreviewedPortalocker.constructed = 0
    if installed == "module-missing":
        monkeypatch.setattr(filelocks, "portalocker", None)
        monkeypatch.setattr(filelocks, "installed_portalocker_version", lambda: None)
    else:
        monkeypatch.setattr(filelocks, "portalocker", _UnreviewedPortalocker)
        monkeypatch.setattr(filelocks, "installed_portalocker_version", lambda: installed)
    touched: list[str] = []
    real_os_open, real_open = os.open, builtins.open

    def spy_os_open(path, *a, **k):
        if ".review.lock" in str(path):
            touched.append(f"os.open {path}")
        return real_os_open(path, *a, **k)

    def spy_open(file, *a, **k):
        if ".review.lock" in str(file):
            touched.append(f"open {file}")
        return real_open(file, *a, **k)

    monkeypatch.setattr(os, "open", spy_os_open)
    monkeypatch.setattr(builtins, "open", spy_open)
    with pytest.raises(WriteBlockedError, match=r"reviewed only for portalocker==4\.4\.2"):
        svc.add_comment(f, 1, "x", r0)
    monkeypatch.undo()
    assert touched == [] and _UnreviewedPortalocker.constructed == 0
    assert victim.read_text() == "DO NOT TRUNCATE" and lp.is_symlink()
    assert plain.read_bytes() == before
    assert lock_files(root) == [lp.name]  # only the pre-existing symlink; nothing created


def test_reviewed_portalocker_is_what_is_installed():
    assert filelocks.REVIEWED_PORTALOCKER == "4.4.2"
    assert filelocks.installed_portalocker_version() == "4.4.2"
    filelocks.check_portalocker()  # no exception


def test_version_gate_without_symlink_leaves_no_lock_artifact(svc, plain, root, monkeypatch):
    monkeypatch.setattr(filelocks, "installed_portalocker_version", lambda: "4.4.3")
    before = plain.read_bytes()
    with pytest.raises(WriteBlockedError, match="writes are refused"):
        svc.add_comment(str(plain), 1, "x", rev(svc, str(plain)))
    assert plain.read_bytes() == before and lock_files(root) == []
