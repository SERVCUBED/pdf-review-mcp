"""Visible completion styling: grey on done, exact restore on reopen, sync tool.

Render checks use PyMuPDF's renderer only (evidence of the PDF data; NOT a claim about any
specific viewer)."""

from __future__ import annotations

import json

import pymupdf
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from pdf_review_mcp import appearance as style
from pdf_review_mcp.pdfstore import ReviewError, StaleRevisionError
from pdf_review_mcp.server import build_server
from pdf_review_mcp.service import ReviewService

from conftest import _add_raw_annot, add_native_marker, add_native_reply, core_obj


def rev(svc, path):
    return svc.review_summary(path)["revision"]


def obj(path, x):
    """Style-relevant view of the annotation (everything except /M, /PDFReviewState and
    /Contents, which the review-state change manages and tests check separately)."""
    return core_obj(path, x)


def annot_count(path):
    """Visible annotations: excludes popups and Hidden-flag (F & 2) status markers."""
    d = pymupdf.open(path)
    try:
        n = 0
        for p in d:
            for x, t, _ in p.annot_xrefs():
                f = d.xref_get_key(x, "F")
                hidden = f[0] == "int" and int(f[1]) & 2
                if t != pymupdf.PDF_ANNOT_POPUP and not hidden:
                    n += 1
        return n
    finally:
        d.close()


def all_annot_count(path):
    d = pymupdf.open(path)
    try:
        return sum(len(p.annot_xrefs()) for p in d)
    finally:
        d.close()


def mean_rgb(path, rect, page=0):
    d = pymupdf.open(path)
    try:
        pix = d[page].get_pixmap(clip=pymupdf.Rect(rect), dpi=144, annots=True)
        s = pix.samples
        n = pix.width * pix.height
        return tuple(sum(s[i::pix.n]) / n for i in range(3)), bytes(s)
    finally:
        d.close()


def is_grey(rgb, tol=8):
    return max(rgb) - min(rgb) <= tol


@pytest.fixture
def styled_pdf(root):
    """Disposable fixture: yellow Highlight (opacity .6), blue Text note, Square, FreeText."""
    p = root / "style.pdf"
    d = pymupdf.open()
    pg = d.new_page()
    pg.insert_text((72, 100), "The quick brown fox jumps over the lazy dog.", fontsize=14)
    hl = pg.add_highlight_annot(pg.search_for("quick brown")[0])
    hl.set_opacity(0.6)
    hl.set_info(content="Reword this", title="Human")
    hl.update()
    tx = pg.add_text_annot((300, 200), "Cite source")
    tx.set_colors(stroke=(0, 0, 1))
    tx.set_info(title="Human")
    tx.update()
    sq = pg.add_rect_annot((72, 300, 200, 360))
    sq.set_info(content="Check box", title="Human")
    sq.update()
    ft = pg.add_freetext_annot((250, 300, 450, 340), "free text note")
    ids = {"hl": hl.xref, "tx": tx.xref, "sq": sq.xref, "ft": ft.xref}
    rects = {k: list(a.rect) for k, a in (("hl", hl), ("tx", tx))}
    d.save(p)
    d.close()
    return p, ids, rects


def test_done_greys_highlight_and_text_prefixes_done_and_reopen_restores(svc, styled_pdf, tmp_path):
    p, ids, rects = styled_pdf
    f = str(p)
    hl_rgb0, hl_px0 = mean_rgb(f, rects["hl"])
    tx_rgb0, tx_px0 = mean_rgb(f, rects["tx"])
    assert hl_rgb0[2] < hl_rgb0[0] - 40 and hl_rgb0[2] < hl_rgb0[1] - 40  # yellow-ish
    assert tx_rgb0[2] > tx_rgb0[0] + 40  # blue-ish
    o_hl, o_tx = obj(f, ids["hl"]), obj(f, ids["tx"])
    before = {k: svc.get_comment(f, f"xref:{ids[k]}", 0)["thread"] for k in ("hl", "tx")}
    n0, n_all0 = annot_count(f), all_annot_count(f)
    r = rev(svc, f)
    for k in ("hl", "tx"):
        d = svc.mark_comment_done(f, f"xref:{ids[k]}", "fixed, see source commit abc", r)
        assert d["appearance"]["action"] == "apply" and d["appearance"]["warning"] is None
        r = d["revision"]
    hl_rgb1, _ = mean_rgb(f, rects["hl"])
    tx_rgb1, _ = mean_rgb(f, rects["tx"])
    assert is_grey(hl_rgb1) and is_grey(tx_rgb1), (hl_rgb1, tx_rgb1)
    assert hl_rgb1[0] < 250  # visibly grey band, not vanished
    for k in ("hl", "tx"):
        t = svc.get_comment(f, f"xref:{ids[k]}", 0)["thread"]
        b = before[k]
        assert (t["text"], t["author"], t["rect"], t["reply_count"]) == (
            "[DONE] " + b["text"], b["author"], b["rect"], b["reply_count"])
        assert t["done"] and t["appearance"]["completion_style"] == "completed"
        assert t["appearance"]["color"] == [0.55, 0.55, 0.55] and t["appearance"]["in_sync"]
        assert "fixed, see source" not in t["text"]  # note stays hidden status evidence
    assert annot_count(f) == n0 and all_annot_count(f) == n_all0  # no annotation at all
    doc = pymupdf.open(f)
    assert doc.xref_get_key(ids["hl"], "CA") == ("float", ".6")  # opacity preserved
    doc.close()
    # Save render evidence (PyMuPDF rendering) next to the test's tmp dir.
    doc = pymupdf.open(f)
    doc[0].get_pixmap(dpi=100, annots=True).save(tmp_path / "done.png")
    doc.close()
    for k in ("hl", "tx"):
        o = svc.reopen_comment(f, f"xref:{ids[k]}", "not yet", r)
        assert o["appearance"]["action"] == "restore"
        r = o["revision"]
    assert obj(f, ids["hl"]) == o_hl and obj(f, ids["tx"]) == o_tx  # byte-exact objects
    assert mean_rgb(f, rects["hl"])[1] == hl_px0 and mean_rgb(f, rects["tx"])[1] == tx_px0  # pixel-exact


@pytest.mark.parametrize("c", ["/C [0.2]", "/C [0 0 1 0]", "", "/C []", "/C [1 0 0]"])
def test_exact_restore_gray_cmyk_missing_empty_colour(svc, root, c):
    p = root / "c.pdf"
    d = pymupdf.open()
    x = _add_raw_annot(d, d.new_page(), f"<< /Type /Annot /Subtype /Text /Rect [60 680 80 700] "
                                        f"/T (H) /Contents (c) {c} >>")
    d.save(p)
    d.close()
    f = str(p)
    o0 = obj(f, x)
    dn = svc.mark_comment_done(f, f"xref:{x}", "n", rev(svc, f))
    assert dn["appearance"]["action"] == "apply"
    assert "PDFReviewStyle" in obj(f, x)
    svc.reopen_comment(f, f"xref:{x}", "n", dn["revision"])
    assert obj(f, x) == o0


def test_multi_cycles_persisted_across_processes(styled_pdf, root):
    p, ids, rects = styled_pdf
    f = str(p)
    o0, px0 = obj(f, ids["hl"]), mean_rgb(f, rects["hl"])[1]
    for i in range(3):
        s1 = ReviewService(default_actor=f"cycle{i}")
        d = s1.mark_comment_done(f, f"xref:{ids['hl']}", "done", rev(s1, f))
        assert d["appearance"]["action"] == "apply"
        s2 = ReviewService()  # fresh "process": styling record read from the PDF itself
        t = s2.get_comment(f, f"xref:{ids['hl']}", 0)["thread"]
        assert t["appearance"]["completion_style"] == "completed" and is_grey(mean_rgb(f, rects["hl"])[0])
        o = s2.reopen_comment(f, f"xref:{ids['hl']}", "again", rev(s2, f))
        assert o["appearance"]["action"] == "restore"
        assert obj(f, ids["hl"]) == o0 and mean_rgb(f, rects["hl"])[1] == px0
    t = ReviewService().get_comment(f, f"xref:{ids['hl']}", 0)["thread"]
    assert [h["state"] for h in t["state_history"]] == ["Completed", "None"] * 3


def test_sync_existing_completed_no_new_status_idempotent(svc, native):
    path, ids = native
    f = str(path)
    s0 = svc.review_summary(f)
    assert "human-nm-1" in s0["appearance_out_of_sync_ids"]  # native Completed highlight, unstyled
    t0 = svc.get_comment(f, "human-nm-1", 0)["thread"]
    n0 = all_annot_count(f)
    assert t0["text"] == "Reword this" and t0["state_history"][-1]["source"] == "native"
    doc = pymupdf.open(f)
    rect0 = doc.xref_get_key(ids["D"], "Rect")
    doc.close()
    r = svc.sync_comment_appearance(f, "human-nm-1", s0["revision"])
    assert r["changed"] and r["appearance"]["action"] == "apply" and r["status"] == "Completed"
    t1 = svc.get_comment(f, "human-nm-1", 0)["thread"]
    # The native Completed marker is ADOPTED into the private record (not removed, no new marker):
    assert [h["source"] for h in t1["state_history"]] == ["native", "adopted_native"]
    assert t1["state_history"][0]["accounted_in_private_baseline"] is True
    assert t1["state_history"][1]["native_id"] == t0["state_history"][-1]["id"]
    assert t1["replies"] == t0["replies"] and t1["effective_source"] == "private"
    assert t1["text"] == "[DONE] " + t0["text"] and t1["author"] == t0["author"]
    assert all_annot_count(f) == n0  # no new annotation at all (not even hidden/popup)
    doc = pymupdf.open(f)
    assert doc.xref_get_key(ids["D"], "Rect") == rect0  # native geometry kept exactly
    doc.close()
    s1 = svc.review_summary(f)
    assert (s1["done"], s1["open"], s1["status_markers"], s1["replies"]) == (
        s0["done"], s0["open"], s0["status_markers"], s0["replies"])
    assert "human-nm-1" not in s1["appearance_out_of_sync_ids"]
    data = path.read_bytes()
    again = svc.sync_comment_appearance(f, "human-nm-1", r["revision"])
    assert again["changed"] is False and again["appearance"]["reason"] == "already_applied"
    assert again["revision"] == r["revision"] and path.read_bytes() == data
    # Open, unstyled root: no-op, no write.
    noop = svc.sync_comment_appearance(f, f"xref:{ids['F']}", r["revision"])
    assert noop["changed"] is False and noop["appearance"]["reason"] == "not_styled"
    assert path.read_bytes() == data
    with pytest.raises(ReviewError, match="not a root"):
        svc.sync_comment_appearance(f, f"xref:{ids['B']}", r["revision"])


def test_sync_stale_revision(svc, native):
    path, _ = native
    f = str(path)
    r0 = rev(svc, f)
    svc.sync_comment_appearance(f, "human-nm-1", r0)
    with pytest.raises(StaleRevisionError):
        svc.sync_comment_appearance(f, "human-nm-1", r0)


def test_sync_restores_after_reopen_in_viewer(svc, styled_pdf):
    p, ids, rects = styled_pdf
    f = str(p)
    o0 = obj(f, ids["tx"])
    d = svc.mark_comment_done(f, f"xref:{ids['tx']}", "done", rev(svc, f))
    # Simulate a viewer reopening by appending its own native /None marker (no style knowledge).
    doc = pymupdf.open(f)
    _add_raw_annot(doc, doc[0], f"<< /Type /Annot /Subtype /Text /Rect [0 0 0 0] /T (Viewer) "
                                f"/IRT {ids['tx']} 0 R /State /None /StateModel /Review /F 30 >>")
    doc.saveIncr()
    doc.close()
    s = svc.review_summary(f)
    tid = svc.get_comment(f, f"xref:{ids['tx']}", 0)["thread"]["id"]
    assert tid in s["appearance_out_of_sync_ids"]
    r = svc.sync_comment_appearance(f, f"xref:{ids['tx']}", s["revision"])
    assert r["appearance"]["action"] == "restore" and r["status"] == "None"
    assert obj(f, ids["tx"]) == o0


def test_mixed_unsupported_types_state_persists_with_warning(svc, styled_pdf):
    p, ids, _ = styled_pdf
    f = str(p)
    o_sq, o_ft = obj(f, ids["sq"]), obj(f, ids["ft"])
    r = rev(svc, f)
    for k in ("sq", "ft"):
        d = svc.mark_comment_done(f, f"xref:{ids[k]}", "done", r)
        a = d["appearance"]
        assert d["state"] == "Completed" and a["action"] == "skip" and a["reason"] == "unsupported_type"
        assert "authoritative" in a["warning"]
        r = d["revision"]
    assert obj(f, ids["sq"]) == o_sq and obj(f, ids["ft"]) == o_ft
    assert svc.review_summary(f)["done"] == 2
    data = p.read_bytes()
    s = svc.sync_comment_appearance(f, f"xref:{ids['sq']}", r)
    assert s["changed"] is False and s["appearance"]["reason"] == "unsupported_type"
    assert p.read_bytes() == data
    t = svc.get_comment(f, f"xref:{ids['sq']}", 0)["thread"]
    assert t["appearance"]["style_supported"] is False and t["appearance"]["in_sync"] is True


def test_manual_recolour_after_completion_respected(svc, styled_pdf):
    p, ids, _ = styled_pdf
    f = str(p)
    d = svc.mark_comment_done(f, f"xref:{ids['tx']}", "done", rev(svc, f))
    doc = pymupdf.open(f)  # human recolours the grey note red in a viewer
    doc.xref_set_key(ids["tx"], "C", "[1 0 0]")
    doc.saveIncr()
    doc.close()
    data = p.read_bytes()
    s = svc.sync_comment_appearance(f, f"xref:{ids['tx']}", rev(svc, f))
    assert s["changed"] is False and s["appearance"]["reason"] == "modified_since_applied"
    assert p.read_bytes() == data
    o = svc.reopen_comment(f, f"xref:{ids['tx']}", "reopen", rev(svc, f))
    assert o["appearance"]["action"] == "release"
    doc = pymupdf.open(f)
    assert doc.xref_get_key(ids["tx"], "C") == ("array", "[1 0 0]")  # user's colour kept
    assert doc.xref_get_key(ids["tx"], "PDFReviewStyle")[0] == "null"
    doc.close()
    # Re-completion after reopen greys again, and remembers red for the next reopen.
    d2 = svc.mark_comment_done(f, f"xref:{ids['tx']}", "done again", o["revision"])
    assert d2["appearance"]["action"] == "apply" and d2["appearance"]["previous_color"] == [1, 0, 0]
    svc.reopen_comment(f, f"xref:{ids['tx']}", "x", d2["revision"])
    doc = pymupdf.open(f)
    assert doc.xref_get_key(ids["tx"], "C") == ("array", "[1 0 0]")
    doc.close()


@pytest.mark.parametrize("meta", [
    "garbage", json.dumps({"schema": "other", "v": 1}), json.dumps([1, 2]),
    json.dumps({"schema": style.STYLE_SCHEMA, "v": 99, "state": "completed", "prev": {}, "applied": {}}),
])
def test_invalid_style_metadata_fails_safely(svc, styled_pdf, meta):
    p, ids, _ = styled_pdf
    f = str(p)
    d = svc.mark_comment_done(f, f"xref:{ids['tx']}", "done", rev(svc, f))
    doc = pymupdf.open(f)
    doc.xref_set_key(ids["tx"], style.KEY_STYLE, pymupdf.get_pdf_str(meta))
    doc.saveIncr()
    doc.close()
    o_before = obj(f, ids["tx"])
    data = p.read_bytes()
    s = svc.sync_comment_appearance(f, f"xref:{ids['tx']}", rev(svc, f))
    assert s["changed"] is False and s["appearance"]["reason"] == "invalid_metadata"
    assert p.read_bytes() == data
    o = svc.reopen_comment(f, f"xref:{ids['tx']}", "reopen", rev(svc, f))
    assert o["state"] == "None" and o["appearance"]["reason"] == "invalid_metadata"
    assert o["appearance"]["warning"]
    assert obj(f, ids["tx"]) == o_before  # nothing guessed or "restored"


def test_forged_valid_metadata_pointing_to_missing_stream_not_restored(svc, styled_pdf):
    p, ids, _ = styled_pdf
    f = str(p)
    svc.mark_comment_done(f, f"xref:{ids['tx']}", "done", rev(svc, f))
    doc = pymupdf.open(f)
    m = json.loads(doc.xref_get_key(ids["tx"], style.KEY_STYLE)[1])
    m["prev"]["AP"] = "<</N 99999 0 R>>"
    doc.xref_set_key(ids["tx"], style.KEY_STYLE, pymupdf.get_pdf_str(json.dumps(m)))
    doc.saveIncr()
    doc.close()
    o_before = obj(f, ids["tx"])
    o = svc.reopen_comment(f, f"xref:{ids['tx']}", "reopen", rev(svc, f))
    assert o["appearance"]["reason"] == "invalid_signature" and obj(f, ids["tx"]) == o_before


def test_complex_appearance_left_untouched(svc, root):
    p = root / "cx.pdf"
    d = pymupdf.open()
    pg = d.new_page()
    on = d.get_new_xref()
    d.update_object(on, "<< /Type /XObject /Subtype /Form /BBox [0 0 20 20] >>")
    d.update_stream(on, b"0 0 1 rg 0 0 20 20 re f")
    x = _add_raw_annot(d, pg, f"<< /Type /Annot /Subtype /Text /Rect [60 680 80 700] /T (H) "
                              f"/Contents (c) /C [0 0 1] /AP << /N << /On {on} 0 R >> >> /AS /On >>")
    d.save(p)
    d.close()
    f = str(p)
    o0 = obj(f, x)
    dn = svc.mark_comment_done(f, f"xref:{x}", "n", rev(svc, f))
    assert dn["appearance"]["reason"] == "complex_appearance" and obj(f, x) == o0


def test_appearance_error_rolls_back_root_but_keeps_state(svc, styled_pdf, monkeypatch):
    p, ids, _ = styled_pdf
    f = str(p)
    o0 = obj(f, ids["hl"])

    def boom(*a, **k):  # fails AFTER /C was greyed and a new /AP generated
        raise RuntimeError("simulated appearance failure")

    with monkeypatch.context() as mp:
        mp.setattr(style, "_keep_rect", boom)
        d = svc.mark_comment_done(f, f"xref:{ids['hl']}", "done", rev(svc, f))
    assert d["state"] == "Completed"
    assert d["appearance"]["reason"] == "appearance_error" and "simulated" in d["appearance"]["detail"]
    assert obj(f, ids["hl"]) == o0  # root rolled back exactly
    assert svc.get_comment(f, f"xref:{ids['hl']}", 0)["thread"]["done"]
    # Later sync can apply it once the problem is gone.
    s = svc.sync_comment_appearance(f, f"xref:{ids['hl']}", d["revision"])
    assert s["appearance"]["action"] == "apply"


def test_verification_failure_aborts_whole_write(svc, styled_pdf, monkeypatch):
    p, ids, _ = styled_pdf
    f = str(p)
    data = p.read_bytes()
    monkeypatch.setattr(style, "GREY_PDF", "[0.1 0.2 0.3]")  # applied colour won't verify as grey
    real_norm = style._norm_c
    monkeypatch.setattr(style, "_norm_c", lambda c: "X" if c.startswith("[0.1") else real_norm(c))
    with pytest.raises(ReviewError, match="verification failed"):
        svc.mark_comment_done(f, f"xref:{ids['tx']}", "done", rev(svc, f))
    assert p.read_bytes() == data  # nothing written: status AND styling rolled back together


def test_edit_and_append_keep_grey(svc, styled_pdf):
    p, ids, rects = styled_pdf
    f = str(p)
    d = svc.mark_comment_done(f, f"xref:{ids['hl']}", "done", rev(svc, f))
    a = svc.append_agent_response(f, f"xref:{ids['hl']}", "done", "[DONE] Reword this", d["revision"])
    t = svc.get_comment(f, f"xref:{ids['hl']}", 0)["thread"]
    assert t["text"] == "[DONE] Reword this\n\n[test-agent] done"
    assert t["done"] and t["appearance"]["completion_style"] == "completed"
    assert is_grey(mean_rgb(f, rects["hl"])[0])
    assert svc.sync_comment_appearance(f, f"xref:{ids['hl']}", a["revision"])["changed"] is False


async def test_sync_tool_over_mcp(root, native):
    async with Client(build_server()) as c:
        tools = {t.name: t for t in await c.list_tools()}
        assert len(tools) == 13
        t = tools["sync_comment_appearance"]
        assert t.annotations.read_only_hint is False and t.annotations.destructive_hint is True
        assert t.annotations.idempotent_hint is True
        assert set(t.input_schema["required"]) == {"path", "comment_id", "expected_revision"}
        assert "COMPLETION (single original comment)" in c.instructions
        s = (await c.call_tool("review_summary", {"path": str(root / "native.pdf")})).data
        r = (await c.call_tool("sync_comment_appearance", {
            "path": str(root / "native.pdf"), "comment_id": "human-nm-1", "expected_revision": s["revision"]})).data
        assert r["changed"] and r["appearance"]["action"] == "apply"
        r2 = (await c.call_tool("sync_comment_appearance", {
            "path": str(root / "native.pdf"), "comment_id": "human-nm-1", "expected_revision": r["revision"]})).data
        assert r2["changed"] is False
        with pytest.raises(ToolError, match="Stale"):
            await c.call_tool("sync_comment_appearance", {
                "path": str(root / "native.pdf"), "comment_id": "human-nm-1", "expected_revision": s["revision"]})
