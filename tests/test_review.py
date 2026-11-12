from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pymupdf
import pytest

from pdf_review_mcp import pdfstore
from pdf_review_mcp.pdfstore import (
    LockBusyError,
    ReviewError,
    StaleRevisionError,
    WriteBlockedError,
)
from pdf_review_mcp.access import AccessError
from pdf_review_mcp.service import ReviewService

from conftest import add_native_reply, annots_snapshot, make_plain_pdf


def rev(svc, path):
    return svc.review_summary(path)["revision"]


def thread(svc, cid, path):
    return svc.get_comment(path, cid, context_chars=0)["thread"]


# ----------------------------------------------------------------- native parsing


def test_native_parsing_roots_replies_states(svc, native, root):
    _, ids = native
    s = svc.review_summary(str(root / "native.pdf"))
    # Roots: A (Accepted->open), D (native Completed), E (bare square), F; popup/replies/states/
    # group member/link are not root tasks.
    assert s["root_comments"] == 4
    assert s["open"] == 3 and s["done"] == 1
    assert s["replies"] == 2 and s["status_markers"] == 3
    comments = {c["id"]: c for c in svc.list_comments(str(root / "native.pdf"))["comments"]}
    a = comments[f"xref:{ids['A']}"]
    assert a["text"] == "Fix the typo — señor"  # UTF-16BE decoded
    assert a["author"] == "Human Alice"
    assert a["status"] == "Accepted" and a["done"] is False  # Marked model ignored
    assert [(r["author"], r["depth"]) for r in a["replies"]] == [("Bob", 1), ("Human Alice", 2)]
    assert a["replies"][1]["in_reply_to"] == f"xref:{ids['B']}"
    d = comments["human-nm-1"]
    assert d["status"] == "Completed" and d["done"] is True
    assert d["state_history"][0]["actor"] == "Carol"
    # duplicate /NM "dup" falls back to xref ids
    assert f"xref:{ids['E']}" in comments and f"xref:{ids['F']}" in comments
    assert f"xref:{ids['P']}" not in comments and f"xref:{ids['G']}" not in comments


def test_open_listing_and_filters(svc, native, root):
    open_ = svc.list_comments(str(root / "native.pdf"), status="open")
    assert open_["total_matching"] == 3
    assert all(not c["done"] for c in open_["comments"])
    assert svc.list_comments(str(root / "native.pdf"), status="done")["total_matching"] == 1
    assert svc.list_comments(str(root / "native.pdf"), author="dave")["total_matching"] == 2
    assert svc.list_comments(str(root / "native.pdf"), page=1)["total_matching"] == 2
    assert svc.list_comments(str(root / "native.pdf"), include_empty=False)["total_matching"] == 3
    page = svc.list_comments(str(root / "native.pdf"), limit=1, offset=1)
    assert len(page["comments"]) == 1 and page["has_more"] is True


def test_reply_to_reply_id_resolves_to_root_thread(svc, native, root):
    _, ids = native
    t = thread(svc, f"xref:{ids['C']}", str(root / "native.pdf"))
    assert t["id"] == f"xref:{ids['A']}"


def test_context_tied_to_rect(svc, native, root):
    ctx = svc.get_comment_context(str(root / "native.pdf"), "human-nm-1", margin=10, max_chars=500)["context"]
    assert ctx["anchored_text"] == "quick brown"
    assert "lazy dog" in ctx["surrounding_text"]
    assert "budgets" not in ctx["surrounding_text"]  # bounded band
    small = svc.get_comment_context(str(root / "native.pdf"), "human-nm-1", margin=500, max_chars=5)
    assert small["context"]["truncated"] and len(small["context"]["surrounding_text"]) <= 5


def test_read_page_text_bounded(svc, plain, root):
    r = svc.read_page_text(str(root / "plain.pdf"), 1, max_chars=50)
    assert len(r["text"]) == 50 and r["has_more"]
    r2 = svc.read_page_text(str(root / "plain.pdf"), 1, offset=50, max_chars=50)
    assert r2["offset"] == 50
    with pytest.raises(ReviewError):
        svc.read_page_text(str(root / "plain.pdf"), 3)


def test_read_tools_do_not_modify(svc, native, root):
    path, ids = native
    before = path.read_bytes()
    mtime = path.stat().st_mtime_ns
    listing_before = sorted(os.listdir(root))
    svc.review_summary(str(root / "native.pdf"))
    svc.list_comments(str(root / "native.pdf"))
    svc.get_comment(str(root / "native.pdf"), "human-nm-1")
    svc.get_comment_context(str(root / "native.pdf"), f"xref:{ids['A']}")
    svc.read_page_text(str(root / "native.pdf"), 1)
    svc.list_pdfs(str(root))
    assert path.read_bytes() == before
    assert path.stat().st_mtime_ns == mtime
    assert sorted(os.listdir(root)) == listing_before  # no lock/temp files


def test_untrusted_notice_present(svc, native, root):
    assert "untrusted" in svc.list_comments(str(root / "native.pdf"))["untrusted_content_notice"]


# ----------------------------------------------------------------- writes & persistence


def test_lifecycle_persisted_reopen_in_new_process_view(svc, native, root):
    """Answer inline, complete ON THE ROOT; nothing is created; state lives in the PDF."""
    path, ids = native
    f = str(root / "native.pdf")
    root_id = f"xref:{ids['A']}"
    before_annots = annots_snapshot(f)
    a = svc.append_agent_response(f, root_id, "Fixed typo on p1 line 1.", "Fix the typo — señor", rev(svc, f),
                                  actor="bot")
    d = svc.mark_comment_done(f, root_id, "Changed 'teh'->'the' on p1; verified.", a["revision"], actor="bot")
    assert d["state"] == "Completed" and d["previous_state"] == "Accepted" and d["annotations_created"] == 0
    assert annots_snapshot(f) == before_annots  # no reply, marker or popup created

    fresh = ReviewService()  # fresh instance + raw re-open: state lives in the PDF itself
    t = thread(fresh, root_id, f)
    assert t["status"] == "Completed" and t["done"] and t["effective_source"] == "private"
    assert t["text"] == "[DONE] Fix the typo — señor\n\n[bot] Fixed typo on p1 line 1."
    assert t["author"] == "Human Alice"
    # existing native replies (written by a viewer) are read, untouched
    assert [(r["text"], r["depth"]) for r in t["replies"]] == [("Which one?", 1), ("The fox one", 2)]
    # native history (Accepted marker + Marked marker ignored) is kept and accounted
    assert [h["source"] for h in t["state_history"]] == ["native", "server"]
    assert t["state_history"][-1]["actor"] == "bot" and "verified" in t["state_history"][-1]["note"]
    doc = pymupdf.open(path)
    assert doc.xref_get_key(ids["A"], "State")[0] == "null"  # never /State on the root
    rec = json.loads(doc.xref_get_key(ids["A"], "PDFReviewState")[1])
    assert rec["schema"] == "pdf-review-mcp/review-state" and rec["prefix"] == {"owned": True, "form": "space", "reason": None}
    doc.close()
    assert fresh.review_summary(f)["open"] == 2
    o = fresh.reopen_comment(f, root_id, "needs more", d["revision"])
    t = thread(fresh, root_id, f)
    assert t["text"] == "Fix the typo — señor\n\n[bot] Fixed typo on p1 line 1." and not t["done"]
    assert o["annotations_created"] == 0 and annots_snapshot(f) == before_annots


def test_state_roundtrip_and_guards(svc, plain, root):
    f = str(root / "plain.pdf")
    a = svc.add_comment(f, 1, "Please expand intro", rev(svc, f), actor="Human")
    cid = a["id"]
    with pytest.raises(ReviewError, match="already open"):
        svc.reopen_comment(f, cid, "x", a["revision"])
    with pytest.raises(ReviewError, match="must not be empty"):
        svc.mark_comment_done(f, cid, "   ", a["revision"])
    d1 = svc.mark_comment_done(f, cid, "Expanded intro para 2", a["revision"])
    with pytest.raises(ReviewError, match="already done"):
        svc.mark_comment_done(f, cid, "again", d1["revision"])
    o = svc.reopen_comment(f, cid, "Still too short", d1["revision"], actor="Human")
    assert thread(svc, cid, f)["status"] == "None" and thread(svc, cid, f)["text"] == "Please expand intro"
    assert svc.list_comments(f, status="open")["total_matching"] == 1
    d2 = svc.mark_comment_done(f, cid, "Added 3 sentences", o["revision"])
    t = thread(svc, cid, f)
    assert [h["state"] for h in t["state_history"]] == ["Completed", "None", "Completed"]
    assert [h["actor"] for h in t["state_history"]] == ["test-agent", "Human", "test-agent"]
    assert t["text"] == "[DONE] Please expand intro"  # no duplicated prefix after a cycle
    assert t["done"] and t["reply_count"] == 0
    # Status on a (viewer-written) reply is refused; replies are never created by the server.
    rx = add_native_reply(f, int(cid.split(":")[1]) if cid.startswith("xref:") else
                          pdfstore.parse_document(pymupdf.open(f)).find(cid).xref, "note")
    with pytest.raises(ReviewError, match="root comment"):
        svc.mark_comment_done(f, f"xref:{rx}", "x", rev(svc, f))
    s = svc.review_summary(f)
    assert s["root_comments"] == 1 and s["status_markers"] == 0 and s["replies"] == 1
    assert not hasattr(svc, "reply_to_comment")
    del d2


def test_add_highlight_comment_and_context(svc, plain, root):
    r = svc.add_comment(str(root / "plain.pdf"), 2, "Too informal", rev(svc, str(root / "plain.pdf")), highlight_text="lazy dog")
    t = thread(svc, r["id"], str(root / "plain.pdf"))
    assert t["type"] == "Highlight" and t["page"] == 2
    ctx = svc.get_comment_context(str(root / "plain.pdf"), r["id"])["context"]
    assert ctx["anchored_text"] == "lazy dog"
    with pytest.raises(ReviewError, match="not found"):
        svc.add_comment(str(root / "plain.pdf"), 1, "x", r["revision"], highlight_text="zebra")
    with pytest.raises(ReviewError, match="page must be"):
        svc.add_comment(str(root / "plain.pdf"), 9, "x", r["revision"])
    with pytest.raises(ReviewError, match="within the page"):
        svc.add_comment(str(root / "plain.pdf"), 1, "x", r["revision"], x=-5, y=10)


def test_edit_requires_exact_expected_content_and_keeps_history(svc, native, root):
    path, ids = native
    a = svc.add_comment(str(root / "native.pdf"), 1, "Draft v1", rev(svc, str(root / "native.pdf")), actor="bot")
    with pytest.raises(ReviewError, match="expected_content"):
        svc.edit_comment(str(root / "native.pdf"), a["id"], "Draft v2", "Draft v", a["revision"], actor="bot")
    e = svc.edit_comment(str(root / "native.pdf"), a["id"], "Draft v2", "Draft v1", a["revision"], actor="bot")
    assert e["previous_text"] == "Draft v1"
    t = ReviewService().get_comment(str(root / "native.pdf"), a["id"], 0)["thread"]
    assert t["text"] == "Draft v2"
    assert t["edit_history"][0]["previous_text"] == "Draft v1"
    assert t["edit_history"][0]["edited_by"] == "bot"


def test_explicit_edit_of_human_comment_works_and_retains_history(svc, native, root):
    path, ids = native
    hid = f"xref:{ids['A']}"
    original = "Fix the typo — señor"
    with pytest.raises(ReviewError, match="expected_content"):
        svc.edit_comment(str(root / "native.pdf"), hid, "x", "", rev(svc, str(root / "native.pdf")))
    e = svc.edit_comment(str(root / "native.pdf"), hid, "Fix the typo (clarified)", original, rev(svc, str(root / "native.pdf")), actor="editor")
    t = ReviewService().get_comment(str(root / "native.pdf"), hid, 0)["thread"]
    assert t["text"] == "Fix the typo (clarified)"
    assert t["author"] == "Human Alice"  # /T untouched
    h = t["edit_history"][-1]
    assert h["previous_text"] == original and h["previous_author"] == "Human Alice"
    assert h["previous_modified"] == "2025-01-01T12:00:00Z" and h["edited_by"] == "editor"
    assert [r["text"] for r in t["replies"]] == ["Which one?", "The fox one"]  # replies untouched
    assert t["status"] == "Accepted"
    # Second edit appends to history.
    svc.edit_comment(str(root / "native.pdf"), hid, "v3", "Fix the typo (clarified)", e["revision"])
    assert len(thread(svc, hid, str(root / "native.pdf"))["edit_history"]) == 2
    # Nested human reply is editable the same explicit way.
    r = svc.edit_comment(str(root / "native.pdf"), f"xref:{ids['C']}", "The fox one!", "The fox one", rev(svc, str(root / "native.pdf")))
    assert thread(svc, hid, str(root / "native.pdf"))["replies"][1]["text"] == "The fox one!"
    # Status markers and group members are not editable.
    with pytest.raises(ReviewError, match="Status markers"):
        svc.edit_comment(str(root / "native.pdf"), f"xref:{ids['S1']}", "x", "Accepted set by Human Alice", r["revision"])
    with pytest.raises(ReviewError, match="Group member"):
        svc.edit_comment(str(root / "native.pdf"), f"xref:{ids['G']}", "x", "", r["revision"])


def test_forged_tool_marker_cannot_bypass_expected_content(svc, plain, root):
    doc = pymupdf.open(plain)
    a = doc[0].add_text_annot((100, 100), "Human words")
    a.set_info(title="Human")
    a.update()
    doc.xref_set_key(a.xref, pdfstore.KEY_TOOL, pymupdf.get_pdf_str(pdfstore.TOOL_MARK))
    doc.saveIncr()
    doc.close()
    c = svc.list_comments(str(root / "plain.pdf"))["comments"][0]
    assert c["tool_marker"] is True  # reported, but grants nothing
    before = plain.read_bytes()
    for bad in ["", "Human", "human words", "OVERWRITTEN"]:
        with pytest.raises(ReviewError, match="expected_content"):
            svc.edit_comment(str(root / "plain.pdf"), c["id"], "OVERWRITTEN", bad, rev(svc, str(root / "plain.pdf")))
    assert plain.read_bytes() == before
    assert thread(svc, c["id"], str(root / "plain.pdf"))["text"] == "Human words"


def test_edit_moves_rich_text_to_history_and_refuses_freetext(svc, plain, root):
    doc = pymupdf.open(plain)
    a = doc[0].add_text_annot((100, 100), "plain body")
    doc.xref_set_key(a.xref, "RC", pymupdf.get_pdf_str("<body><p>rich body</p></body>"))
    doc[0].add_freetext_annot((100, 200, 300, 250), "free text")
    doc.saveIncr()
    doc.close()
    cs = {c["text"]: c for c in svc.list_comments(str(root / "plain.pdf"))["comments"]}
    assert cs["plain body"]["has_rich_text"]
    r = svc.edit_comment(str(root / "plain.pdf"), cs["plain body"]["id"], "new body", "plain body", rev(svc, str(root / "plain.pdf")))
    t = thread(svc, cs["plain body"]["id"], str(root / "plain.pdf"))
    assert "has_rich_text" not in t and "rich body" in t["edit_history"][0]["previous_rich_text"]
    with pytest.raises(ReviewError, match="FreeText"):
        svc.edit_comment(str(root / "plain.pdf"), cs["free text"]["id"], "x", "free text", r["revision"])


def test_preserves_unrelated_annotations_and_original_bytes(svc, native, root):
    path, ids = native
    original = path.read_bytes()
    doc = pymupdf.open(path)
    objs = {k: doc.xref_object(x, compressed=True) for k, x in ids.items()}
    style_keys = {"C", "AP", "PDFReviewStyle"}
    f_keys = {k: doc.xref_get_key(ids["F"], k) for k in doc.xref_get_keys(ids["F"]) if k not in style_keys}
    doc.close()
    r = svc.append_agent_response(str(root / "native.pdf"), "human-nm-1", "ack", "Reword this",
                                  rev(svc, str(root / "native.pdf")))
    svc.mark_comment_done(str(root / "native.pdf"), f"xref:{ids['F']}", "Checked conclusion, fixed p2", r["revision"])
    new = path.read_bytes()
    assert new.startswith(original)  # incremental update: original bytes untouched
    doc = pymupdf.open(path)
    # Every pre-existing annotation object is byte-for-byte unchanged, except the two explicit
    # targets: D (inline answer: /Contents, /M, history) and the completed root F, where ONLY
    # styling (/C, /AP, /PDFReviewStyle) and the managed state keys (/Contents gets "[DONE] ",
    # /M, /PDFReviewState) may differ.
    for k, x in ids.items():
        if k in ("D", "F"):
            continue
        assert doc.xref_object(x, compressed=True) == objs[k], k
    managed = style_keys | {"Contents", "M", "PDFReviewState"}
    assert {k: doc.xref_get_key(ids["F"], k) for k in doc.xref_get_keys(ids["F"])
            if k not in managed} == {k: v for k, v in f_keys.items() if k not in managed}
    assert doc.xref_get_key(ids["F"], "Contents")[1] == "[DONE] Check conclusion"
    assert doc.xref_get_key(ids["F"], "C") == ("array", "[.55 .55 .55]")
    assert any(t == pymupdf.PDF_ANNOT_LINK for _, t, _ in doc[0].annot_xrefs())
    doc.close()


def test_stale_revision_rejected_and_file_unchanged(svc, plain, root):
    r0 = rev(svc, str(root / "plain.pdf"))
    svc.add_comment(str(root / "plain.pdf"), 1, "first", r0)
    snapshot = plain.read_bytes()
    with pytest.raises(StaleRevisionError):
        svc.add_comment(str(root / "plain.pdf"), 1, "second", r0)
    with pytest.raises(ReviewError, match="sha256"):
        svc.add_comment(str(root / "plain.pdf"), 1, "second", "deadbeef")
    assert plain.read_bytes() == snapshot
    # External modification changes the revision too.
    cur = rev(svc, str(root / "plain.pdf"))
    plain.write_bytes(snapshot + b"\n%external\n")
    with pytest.raises(StaleRevisionError):
        svc.add_comment(str(root / "plain.pdf"), 1, "third", cur)


def test_revision_is_sha256_of_bytes(svc, plain, root):
    assert rev(svc, str(root / "plain.pdf")) == "sha256:" + hashlib.sha256(plain.read_bytes()).hexdigest()


def test_concurrent_writers_same_revision_one_wins(svc, plain, root):
    r0 = rev(svc, str(root / "plain.pdf"))
    results, errors = [], []

    def worker(i):
        try:
            results.append(ReviewService().add_comment(str(root / "plain.pdf"), 1, f"c{i}", r0))
        except StaleRevisionError as e:
            errors.append(e)

    ts = [threading.Thread(target=worker, args=(i,)) for i in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(results) == 1 and len(errors) == 3
    assert svc.review_summary(str(root / "plain.pdf"))["root_comments"] == 1


def test_cross_process_lock_busy(svc, plain, monkeypatch, root):
    lock_path = svc.store.lock_path(plain)
    holder = subprocess.Popen(
        [sys.executable, "-c", textwrap.dedent(f"""
            import sys
            from pathlib import Path
            from pdf_review_mcp import posixlock
            h = posixlock.acquire(Path({str(lock_path)!r}), 10)
            print('locked', flush=True); sys.stdin.readline(); h.release()
        """)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
    )
    try:
        assert holder.stdout.readline().strip() == "locked"
        monkeypatch.setattr(pdfstore, "LOCK_TIMEOUT_S", 0.3)
        before = plain.read_bytes()
        with pytest.raises(LockBusyError):
            svc.add_comment(str(root / "plain.pdf"), 1, "blocked", rev(svc, str(root / "plain.pdf")))
        assert plain.read_bytes() == before
    finally:
        holder.communicate("\n", timeout=10)
    svc.add_comment(str(root / "plain.pdf"), 1, "now ok", rev(svc, str(root / "plain.pdf")))
    assert not lock_path.exists()


def test_atomic_failure_leaves_original_and_no_temp(svc, plain, root, monkeypatch):
    before = plain.read_bytes()
    r0 = rev(svc, str(root / "plain.pdf"))

    def boom(*a, **k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(pdfstore.os, "replace", boom)
    with pytest.raises(OSError):
        svc.add_comment(str(root / "plain.pdf"), 1, "x", r0)
    assert plain.read_bytes() == before
    assert not [n for n in os.listdir(root) if n.endswith(".tmp")]


def test_failing_mutation_leaves_original(svc, native, root):
    path, _ = native
    before = path.read_bytes()
    with pytest.raises(ReviewError):
        svc.append_agent_response(str(root / "native.pdf"), "no-such-id", "x", "y", rev(svc, str(root / "native.pdf")))
    assert path.read_bytes() == before
    assert not [n for n in os.listdir(root) if n.endswith(".tmp")]


def test_written_file_keeps_permissions(svc, plain, root):
    os.chmod(plain, 0o640)
    svc.add_comment(str(root / "plain.pdf"), 1, "x", rev(svc, str(root / "plain.pdf")))
    assert (plain.stat().st_mode & 0o777) == 0o640


# ----------------------------------------------------------------- blocked files


def test_encrypted_pdf_write_blocked(svc, root):
    p = root / "enc.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "secret")
    doc.save(p, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="owner", user_pw="",
             permissions=pymupdf.PDF_PERM_PRINT)
    doc.close()
    s = svc.review_summary(str(root / "enc.pdf"))
    assert s["security"]["encrypted"] and not s["security"]["writable"]
    before = p.read_bytes()
    with pytest.raises(WriteBlockedError, match="encrypted"):
        svc.add_comment(str(root / "enc.pdf"), 1, "x", s["revision"])
    assert p.read_bytes() == before


def test_password_pdf_read_refused(svc, root):
    p = root / "pw.pdf"
    doc = pymupdf.open()
    doc.new_page()
    doc.save(p, encryption=pymupdf.PDF_ENCRYPT_AES_256, owner_pw="o", user_pw="u")
    doc.close()
    with pytest.raises(ReviewError, match="password"):
        svc.review_summary(str(root / "pw.pdf"))


def test_signed_pdf_write_blocked(svc, root):
    p = root / "signed.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    w = pymupdf.Widget()
    w.field_type = pymupdf.PDF_WIDGET_TYPE_SIGNATURE
    w.field_name = "Sig1"
    w.rect = pymupdf.Rect(72, 72, 200, 120)
    page.add_widget(w)
    cat = doc.pdf_catalog()
    af = doc.xref_get_key(cat, "AcroForm")
    if af[0] == "xref":
        doc.xref_set_key(int(af[1].split()[0]), "SigFlags", "3")
    else:
        doc.xref_set_key(cat, "AcroForm/SigFlags", "3")
    doc.save(p)
    doc.close()
    s = svc.review_summary(str(root / "signed.pdf"))
    assert s["security"]["signed"] and not s["security"]["writable"]
    before = p.read_bytes()
    with pytest.raises(WriteBlockedError, match="signed"):
        svc.add_comment(str(root / "signed.pdf"), 1, "x", s["revision"])
    assert p.read_bytes() == before


# ----------------------------------------------------------------- sandbox


def test_relative_and_traversal_paths_rejected(svc, root, tmp_path):
    outside = tmp_path / "outside.pdf"
    make_plain_pdf(outside)
    for bad in ["../outside.pdf", "sub/../../outside.pdf", "native.pdf", "a\x00.pdf", ""]:
        with pytest.raises(AccessError):
            svc.review_summary(bad)
    with pytest.raises(AccessError, match="Relative"):
        svc.add_comment("../outside.pdf", 1, "x", "sha256:" + "0" * 64)
    with pytest.raises(AccessError, match="Relative"):
        svc.list_pdfs("..")


def test_allowlist_traversal_and_absolute_escape(root, tmp_path):
    svc = ReviewService(allow_dirs=[str(root)])
    outside = tmp_path / "outside.pdf"
    make_plain_pdf(outside)
    for bad in [str(outside), str(root / ".." / "outside.pdf")]:
        with pytest.raises(AccessError, match="escapes the allowed directories"):
            svc.review_summary(bad)
        with pytest.raises(AccessError, match="escapes"):
            svc.add_comment(bad, 1, "x", "sha256:" + "0" * 64)
    with pytest.raises(AccessError, match="escapes"):
        svc.list_pdfs(str(tmp_path))


def test_allowlist_symlink_escape_blocked(root, tmp_path):
    svc = ReviewService(allow_dirs=[str(root)])
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    make_plain_pdf(outside_dir / "o.pdf")
    (root / "link.pdf").symlink_to(outside_dir / "o.pdf")
    (root / "linkdir").symlink_to(outside_dir, target_is_directory=True)
    before = (outside_dir / "o.pdf").read_bytes()
    for bad in [str(root / "link.pdf"), str(root / "linkdir" / "o.pdf")]:
        with pytest.raises(AccessError, match="escapes"):
            svc.review_summary(bad)
        with pytest.raises(AccessError):
            svc.add_comment(bad, 1, "x", "sha256:" + "0" * 64)
    assert (outside_dir / "o.pdf").read_bytes() == before
    assert all("link" not in f["path"] for f in svc.list_pdfs(str(root))["files"])


def test_symlink_alias_writes_real_target(svc, plain, root):
    (root / "alias.pdf").symlink_to(plain)
    alias = str(root / "alias.pdf")
    r = svc.add_comment(alias, 1, "via alias", rev(svc, alias))
    assert r["path"] == str(plain.resolve())
    assert (root / "alias.pdf").is_symlink()


def test_non_pdf_rejected(svc, root):
    (root / "notes.txt").write_text("hi")
    with pytest.raises(AccessError, match=".pdf"):
        svc.review_summary(str(root / "notes.txt"))


def test_list_pdfs(svc, native, plain, root):
    (root / "sub").mkdir()
    make_plain_pdf(root / "sub" / "x.PDF")
    base = str(root)
    files = [f["path"] for f in svc.list_pdfs(base)["files"]]
    assert files == [str(root / "native.pdf"), str(root / "plain.pdf"), str(root / "sub" / "x.PDF")]
    assert [f["path"] for f in svc.list_pdfs(base, recursive=False)["files"]] == [
        str(root / "native.pdf"), str(root / "plain.pdf")]
    assert svc.list_pdfs(base, limit=1)["truncated"]
    with pytest.raises(AccessError, match="explicit absolute"):
        svc.list_pdfs()
