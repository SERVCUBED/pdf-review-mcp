"""Single-original-comment workflow: root-only private state, [DONE] prefix ownership, zero
annotation delta, native-marker precedence, concurrency."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap

import pymupdf
import pytest

from pdf_review_mcp import reviewstate as rs
from pdf_review_mcp.pdfstore import ReviewError, StaleRevisionError
from pdf_review_mcp.service import ReviewService

from conftest import _add_raw_annot, add_native_marker, add_native_reply, annots_snapshot

LABEL = "review-agent"


def rev(svc, f):
    return svc.review_summary(f)["revision"]


def th(svc, f, cid):
    return svc.get_comment(f, cid, 0)["thread"]


def raw(f, x, k):
    d = pymupdf.open(f)
    try:
        return d.xref_get_key(x, k)
    finally:
        d.close()


def set_raw(f, x, k, v):
    d = pymupdf.open(f)
    d.xref_set_key(x, k, v)
    d.saveIncr()
    d.close()


def new_pdf(root, name, bodies):
    """bodies: list of raw annotation dict sources for page 1; returns (path, [xrefs])."""
    f = root / name
    d = pymupdf.open()
    pg = d.new_page()
    pg.insert_text((72, 100), "The quick brown fox jumps over the lazy dog.", fontsize=14)
    xs = [_add_raw_annot(d, pg, b) for b in bodies]
    d.save(f)
    d.close()
    return str(f), xs


NOTE = "<< /Type /Annot /Subtype /Text /Rect [100 700 124 724] /T (Human) /Contents ({t}) /C [1 1 0] >>"


# ----------------------------------------------------------------- core lifecycle


def test_done_reopen_zero_annotation_delta_root_without_nm_and_with_popup(svc, root):
    f, (x,) = new_pdf(root, "a.pdf", [NOTE.format(t="Fix this")])
    pop = _add_popup(f, x)
    assert raw(f, x, "NM")[0] == "null"
    a0 = annots_snapshot(f)
    d = svc.mark_comment_done(f, f"xref:{x}", "fixed in source", rev(svc, f))
    assert d["annotations_created"] == 0 and annots_snapshot(f) == a0
    t = th(svc, f, f"xref:{x}")
    assert t["text"] == "[DONE] Fix this" and t["done"] and t["effective_source"] == "private"
    assert raw(f, x, "State")[0] == "null" and raw(f, x, "NM")[0] == "null"  # no /State, no /NM added
    assert raw(f, pop, "Parent") == ("xref", f"{x} 0 R")  # popup untouched
    assert t["state_history"][-1] == {"source": "server", "seq": 1, "state": "Completed", "actor": "test-agent",
                                      "note": "fixed in source", "at": t["state_history"][-1]["at"]}
    o = svc.reopen_comment(f, f"xref:{x}", "not yet", d["revision"])
    assert o["text"] == "Fix this" and annots_snapshot(f) == a0
    s = svc.sync_comment_appearance(f, f"xref:{x}", o["revision"])
    assert s["changed"] is False and annots_snapshot(f) == a0


def _add_popup(f, parent):
    d = pymupdf.open(f)
    pop = _add_raw_annot(d, d[0], f"<< /Type /Annot /Subtype /Popup /Rect [130 600 330 700] /Parent {parent} 0 R >>")
    d.xref_set_key(parent, "Popup", f"{pop} 0 R")
    d.saveIncr()
    d.close()
    return pop


def test_empty_highlight_becomes_DONE_and_back(svc, root):
    f, (x,) = new_pdf(root, "h.pdf", ["<< /Type /Annot /Subtype /Highlight /Rect [90 80 200 100] "
                                      "/QuadPoints [90 100 200 100 90 80 200 80] /T (U) /C [1 1 0] >>"])
    d = svc.mark_comment_done(f, f"xref:{x}", "n", rev(svc, f))
    assert d["text"] == "[DONE]" and d["prefix"] == {"owned": True, "form": "empty", "reason": None}
    # answer appended while done, then reopen keeps the answer and removes only "[DONE]"
    a = svc.append_agent_response(f, f"xref:{x}", "clarified", "[DONE]", d["revision"], actor=LABEL)
    assert a["final_text"] == "[DONE]\n\n[review-agent] clarified"
    o = svc.reopen_comment(f, f"xref:{x}", "x", a["revision"])
    assert o["text"] == "[review-agent] clarified"
    d2 = svc.mark_comment_done(f, f"xref:{x}", "n", o["revision"])
    o2 = svc.reopen_comment(f, f"xref:{x}", "x", d2["revision"])
    assert o2["text"] == "[review-agent] clarified"  # never "[DONE]" leftovers, no duplication


def test_literal_reviewer_DONE_is_not_done_not_duplicated_and_survives_reopen(svc, root):
    f, (x,) = new_pdf(root, "l.pdf", [NOTE.format(t="[DONE] means finished? please define")])
    t = th(svc, f, f"xref:{x}")
    assert not t["done"] and t["status"] == "None"  # literal text is not state
    d = svc.mark_comment_done(f, f"xref:{x}", "defined", rev(svc, f))
    assert d["text"] == "[DONE] means finished? please define"  # no "[DONE] [DONE]"
    assert d["prefix"]["owned"] is False and "literal" in d["prefix"]["reason"]
    o = svc.reopen_comment(f, f"xref:{x}", "x", d["revision"])
    assert o["text"] == "[DONE] means finished? please define"  # reviewer's text untouched
    empty_literal, (y,) = new_pdf(root, "l2.pdf", [NOTE.format(t="[DONE]")])
    d = svc.mark_comment_done(empty_literal, f"xref:{y}", "n", rev(svc, empty_literal))
    o = svc.reopen_comment(empty_literal, f"xref:{y}", "x", d["revision"])
    assert o["text"] == "[DONE]"


def test_repeated_cycles_and_answers_keep_original_and_history(svc, root):
    f, (x,) = new_pdf(root, "c.pdf", [NOTE.format(t="Q")])
    r = rev(svc, f)
    text = "Q"
    for i in range(3):
        a = svc.append_agent_response(f, f"xref:{x}", f"a{i}", th(svc, f, f"xref:{x}")["text"], r, actor=LABEL)
        text = text + f"\n\n[review-agent] a{i}"
        d = svc.mark_comment_done(f, f"xref:{x}", f"done {i}", a["revision"])
        assert d["text"] == "[DONE] " + text
        o = svc.reopen_comment(f, f"xref:{x}", f"again {i}", d["revision"])
        assert o["text"] == text
        r = o["revision"]
    t = th(svc, f, f"xref:{x}")
    assert [h["state"] for h in t["state_history"]] == ["Completed", "None"] * 3
    assert t["author"] == "Human" and len(t["edit_history"]) == 3  # only the 3 explicit answers


def test_edit_while_done_must_keep_managed_prefix(svc, root):
    f, (x,) = new_pdf(root, "e.pdf", [NOTE.format(t="Old wording")])
    d = svc.mark_comment_done(f, f"xref:{x}", "n", rev(svc, f))
    with pytest.raises(ReviewError, match="keep that prefix"):
        svc.edit_comment(f, f"xref:{x}", "New wording", "[DONE] Old wording", d["revision"])
    e = svc.edit_comment(f, f"xref:{x}", "[DONE] New wording", "[DONE] Old wording", d["revision"])
    o = svc.reopen_comment(f, f"xref:{x}", "x", e["revision"])
    assert o["text"] == "New wording"


def test_manual_prefix_removal_or_edit_never_strips_arbitrary_content(svc, root):
    f, (x,) = new_pdf(root, "m.pdf", [NOTE.format(t="Keep me")])
    d = svc.mark_comment_done(f, f"xref:{x}", "n", rev(svc, f))
    set_raw(f, x, "Contents", "(Keep me, edited in a viewer)")  # human removed the prefix
    o = svc.reopen_comment(f, f"xref:{x}", "x", rev(svc, f))
    assert o["text"] == "Keep me, edited in a viewer" and "no longer present" in o["text_note"]
    # human edits after the prefix but keeps it: only the prefix is removed
    d2 = svc.mark_comment_done(f, f"xref:{x}", "n", o["revision"])
    set_raw(f, x, "Contents", "([DONE] Keep me (viewer tweak))")
    o2 = svc.reopen_comment(f, f"xref:{x}", "x", rev(svc, f))
    assert o2["text"] == "Keep me (viewer tweak)"
    del d, d2


def test_rich_text_root_gets_no_prefix_but_state_and_grey(svc, root):
    f, (x,) = new_pdf(root, "rc.pdf", [NOTE.format(t="plain")])
    set_raw(f, x, "RC", pymupdf.get_pdf_str("<body><p>rich</p></body>"))
    d = svc.mark_comment_done(f, f"xref:{x}", "n", rev(svc, f))
    assert d["text"] == "plain" and "rich text" in d["text_note"] and d["appearance"]["action"] == "apply"
    assert th(svc, f, f"xref:{x}")["done"]
    o = svc.reopen_comment(f, f"xref:{x}", "x", d["revision"])
    assert o["text"] == "plain" and raw(f, x, "RC")[0] == "string"  # rich text never touched


# ----------------------------------------------------------------- private record validity


@pytest.mark.parametrize("bad", [
    "garbage", json.dumps({"schema": rs.SCHEMA, "v": 2}), json.dumps({"schema": "x", "v": 1}),
    json.dumps({"schema": rs.SCHEMA, "v": 1, "events": [], "baseline": [], "prefix": {}}),
    json.dumps({"schema": rs.SCHEMA, "v": 1, "baseline": [], "prefix": {"owned": True, "form": None, "reason": None},
                "events": [{"seq": 1, "state": "Completed", "actor": "a", "note": "", "at": "", "source": "server"}]}),
    json.dumps({"schema": rs.SCHEMA, "v": 1, "baseline": [], "prefix": {"owned": False, "form": None, "reason": None},
                "events": [{"seq": 2, "state": "Completed", "actor": "a", "note": "", "at": "", "source": "server"}]}),
    json.dumps({"schema": rs.SCHEMA, "v": 1, "baseline": [], "prefix": {"owned": False, "form": None, "reason": None},
                "events": [{"seq": 1, "state": "Done", "actor": "a", "note": "", "at": "", "source": "server"}]}),
])
def test_malformed_private_record_fails_safe(svc, root, bad):
    f, (x,) = new_pdf(root, "b.pdf", [NOTE.format(t="[DONE] text")])
    set_raw(f, x, rs.KEY_STATE, pymupdf.get_pdf_str(bad))
    s = svc.review_summary(f)
    assert any(dg["kind"] == "invalid_review_state_record" for dg in s["diagnostics"])
    t = th(svc, f, f"xref:{x}")
    assert not t["done"] and "review_state_record_error" in t  # falls back to native (none)
    data = open(f, "rb").read()
    for call in (lambda: svc.mark_comment_done(f, f"xref:{x}", "n", rev(svc, f)),
                 lambda: svc.sync_comment_appearance(f, f"xref:{x}", rev(svc, f))):
        with pytest.raises(ReviewError, match="invalid"):
            call()
    assert open(f, "rb").read() == data


# ----------------------------------------------------------------- native interoperability


def test_native_viewer_state_without_record_is_read_and_reopen_accounts_it(svc, root):
    f, (x,) = new_pdf(root, "n.pdf", [NOTE.format(t="Viewer completed me")])
    m = add_native_marker(f, x, "Completed", "done in viewer", author="Alice")
    t = th(svc, f, f"xref:{x}")
    assert t["done"] and t["effective_source"] == "native" and t["text"] == "Viewer completed me"
    with pytest.raises(ReviewError, match="already done"):
        svc.mark_comment_done(f, f"xref:{x}", "n", rev(svc, f))
    a0 = annots_snapshot(f)
    o = svc.reopen_comment(f, f"xref:{x}", "needs work", rev(svc, f))
    t = th(svc, f, f"xref:{x}")
    assert not t["done"] and t["effective_source"] == "private" and annots_snapshot(f) == a0
    assert [(h["source"], h["state"]) for h in t["state_history"]] == [("native", "Completed"), ("server", "None")]
    assert t["text"] == "Viewer completed me"  # nothing to strip (prefix never owned)
    assert raw(f, m, "State") == ("name", "/Completed")  # native marker untouched
    del o


def test_multiple_native_markers_baseline_then_new_events(svc, root):
    f, (x,) = new_pdf(root, "mm.pdf", [NOTE.format(t="T")])
    add_native_marker(f, x, "Accepted", "a", author="Alice")
    add_native_marker(f, x, "None", "b", author="Bob")
    d = svc.mark_comment_done(f, f"xref:{x}", "done", rev(svc, f))
    t = th(svc, f, f"xref:{x}")
    assert t["done"] and [h["source"] for h in t["state_history"]] == ["native", "native", "server"]
    n1 = add_native_marker(f, x, "None", "c", author="Carol")
    n2 = add_native_marker(f, x, "Accepted", "d", author="Dan")
    t = th(svc, f, f"xref:{x}")
    assert t["effective_marker_id"] == f"xref:{n2}" and t["status"] == "Accepted" and not t["done"]
    assert t["per_reviewer_state"]["Dan"] == "Accepted"
    s = svc.sync_comment_appearance(f, f"xref:{x}", rev(svc, f))
    assert s["adopted"] == f"xref:{n2}" and s["text"] == "T"
    del d, n1


# ----------------------------------------------------------------- concurrency


WRITER = """
import sys, time
from pdf_review_mcp.service import ReviewService
from pdf_review_mcp.pdfstore import StaleRevisionError, LockBusyError
f, cid = sys.argv[1], sys.argv[2]
svc = ReviewService(default_actor="w-" + cid)
for _ in range(400):
    try:
        svc.mark_comment_done(f, cid, "done by " + cid, svc.review_summary(f)["revision"])
        print("ok"); break
    except (StaleRevisionError, LockBusyError):
        time.sleep(0.01)
"""


def test_concurrent_processes_complete_different_roots(root, isolated_style_key):
    bodies = [NOTE.format(t=f"T{i}").replace("100 700 124 724", f"{100 + 40 * i} 700 {124 + 40 * i} 724")
              for i in range(4)]
    f, xs = new_pdf(root, "cc.pdf", bodies)
    a0 = annots_snapshot(f)
    env = {"XDG_CONFIG_HOME": str(isolated_style_key), "PATH": "/usr/bin:/bin"}
    procs = [subprocess.Popen([sys.executable, "-c", textwrap.dedent(WRITER), f, f"xref:{x}"],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env) for x in xs]
    outs = [p.communicate(timeout=180) for p in procs]
    assert all(p.returncode == 0 and o[0].strip() == "ok" for p, o in zip(procs, outs)), outs
    svc = ReviewService()
    assert svc.review_summary(f)["done"] == 4 and annots_snapshot(f) == a0
    assert {th(svc, f, f"xref:{x}")["text"] for x in xs} == {f"[DONE] T{i}" for i in range(4)}
    assert not [p.name for p in root.iterdir() if p.name.endswith(".review.lock")]


def test_stale_revision_still_enforced(svc, root):
    f, (x,) = new_pdf(root, "s.pdf", [NOTE.format(t="T")])
    r0 = rev(svc, f)
    svc.mark_comment_done(f, f"xref:{x}", "n", r0)
    with pytest.raises(StaleRevisionError):
        svc.reopen_comment(f, f"xref:{x}", "n", r0)
