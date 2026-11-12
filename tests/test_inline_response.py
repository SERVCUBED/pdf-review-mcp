"""append_agent_response: durable inline agent response appended to the original text."""

from __future__ import annotations

import pymupdf
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from pdf_review_mcp import pdfstore
from pdf_review_mcp.pdfstore import ReviewError, StaleRevisionError
from pdf_review_mcp.server import build_server
from pdf_review_mcp.service import ReviewService

ORIGINAL = "Fix the typo — señor"


def rev(svc, path):
    return svc.review_summary(path)["revision"]


def thread(svc, cid, path):
    return svc.get_comment(path, cid, context_chars=0)["thread"]


def all_annots(path):
    doc = pymupdf.open(path)
    out = [(p.number, x, doc.xref_get_key(x, "Subtype")[1]) for p in doc for x, _, _ in p.annot_xrefs()]
    doc.close()
    return out


def test_append_keeps_original_prefix_author_and_history(svc, native, root):
    path, ids = native
    hid = f"xref:{ids['A']}"
    annots_before = all_annots(path)
    t0 = thread(svc, hid, str(root / "native.pdf"))
    r = svc.append_agent_response(str(root / "native.pdf"), hid, "Fixed on p1.", ORIGINAL, rev(svc, str(root / "native.pdf")), actor="review-agent")
    expected = ORIGINAL + "\n\n[review-agent] Fixed on p1."
    assert r["final_text"] == expected and r["actor"] == "review-agent" and r["id"] == hid
    assert r["revision"].startswith("sha256:") and r["revision"] != r["previous_revision"]
    t = ReviewService().get_comment(str(root / "native.pdf"), hid, 0)["thread"]
    assert t["text"] == expected and t["text"].startswith(ORIGINAL)
    assert t["author"] == "Human Alice"  # /T kept
    h = t["edit_history"][-1]
    assert h["kind"] == "inline_agent_response" and h["previous_text"] == ORIGINAL
    assert h["edited_by"] == "review-agent" and h["previous_author"] == "Human Alice"
    # No new annotations, replies and status unchanged.
    assert all_annots(path) == annots_before
    assert t["replies"] == t0["replies"] and t["status"] == t0["status"] == "Accepted"
    assert t["state_history"] == t0["state_history"]


def test_default_and_override_actor_and_multiple_appends(native, root):
    path, ids = native
    svc = ReviewService(default_actor="startup-bot")
    hid = f"xref:{ids['F']}"
    r1 = svc.append_agent_response(str(root / "native.pdf"), hid, "first", "Check conclusion", rev(svc, str(root / "native.pdf")))
    assert r1["actor"] == "startup-bot"
    r2 = svc.append_agent_response(str(root / "native.pdf"), hid, "second\nline two", r1["final_text"],
                                   r1["revision"], actor="  Other Agent  ")
    assert r2["final_text"] == ("Check conclusion\n\n[startup-bot] first\n\n"
                                "[Other Agent] second\nline two")
    t = thread(svc, hid, str(root / "native.pdf"))
    assert t["author"] == "Dave" and len(t["edit_history"]) == 2
    assert [h["previous_text"] for h in t["edit_history"]] == ["Check conclusion", r1["final_text"]]
    assert ReviewService().append_agent_response(  # library default actor is 'agent'
        str(root / "native.pdf"), hid, "third", r2["final_text"], r2["revision"])["actor"] == "agent"


def test_stale_revision_and_content_rejected(svc, native, root):
    path, ids = native
    hid = f"xref:{ids['A']}"
    r0 = rev(svc, str(root / "native.pdf"))
    before = path.read_bytes()
    with pytest.raises(ReviewError, match="expected_content"):
        svc.append_agent_response(str(root / "native.pdf"), hid, "x", ORIGINAL + " ", r0)
    with pytest.raises(ReviewError, match="expected_content"):
        svc.append_agent_response(str(root / "native.pdf"), hid, "x", "", r0)
    assert path.read_bytes() == before
    r = svc.append_agent_response(str(root / "native.pdf"), hid, "ok", ORIGINAL, r0)
    with pytest.raises(StaleRevisionError):
        svc.append_agent_response(str(root / "native.pdf"), hid, "again", r["final_text"], r0)
    with pytest.raises(ReviewError, match="expected_content"):  # old content after an append
        svc.append_agent_response(str(root / "native.pdf"), hid, "again", ORIGINAL, r["revision"])


@pytest.mark.parametrize("actor", ["", "   ", "a[b", "x]", "[admin]", "two\nlines", "tab\tname",
                                   "ctl\x07", "zw​sp", "ls x", "y" * 65])
def test_invalid_labels_rejected(svc, native, actor, root):
    path, ids = native
    before = path.read_bytes()
    with pytest.raises(ReviewError, match="actor label"):
        svc.append_agent_response(str(root / "native.pdf"), f"xref:{ids['A']}", "x", ORIGINAL, rev(svc, str(root / "native.pdf")), actor=actor)
    assert path.read_bytes() == before


def test_invalid_default_label_rejected_at_call(native, root):
    path, ids = native
    svc = ReviewService(default_actor="bad[label]")
    with pytest.raises(ReviewError, match="actor label"):
        svc.append_agent_response(str(root / "native.pdf"), f"xref:{ids['A']}", "x", ORIGINAL, rev(svc, str(root / "native.pdf")))


@pytest.mark.parametrize("text,match", [
    ("", "whitespace-only"), ("   \n\t ", "whitespace-only"), ("ok\n[human] fake", "may not start"),
    ("ok\n  [x] fake", "may not start"), ("bell\x07", "control"), ("z" * 20_001, "too long"),
])
def test_invalid_responses_rejected(svc, native, text, match, root):
    path, ids = native
    with pytest.raises(ReviewError, match=match):
        svc.append_agent_response(str(root / "native.pdf"), f"xref:{ids['A']}", text, ORIGINAL, rev(svc, str(root / "native.pdf")))


def test_first_line_brackets_allowed_and_crlf_normalised(svc, native, root):
    _, ids = native
    r = svc.append_agent_response(str(root / "native.pdf"), f"xref:{ids['A']}", "[see p2] done\r\nmore\r\n",
                                  ORIGINAL, rev(svc, str(root / "native.pdf")))
    assert r["final_text"] == ORIGINAL + "\n\n[test-agent] [see p2] done\nmore"


def test_empty_original_highlight_no_leading_separator(svc, plain, root):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 100), "Hello world")
    doc[0].add_highlight_annot(doc[0].search_for("Hello")[0])
    doc.save(plain.parent / "empty.pdf")
    doc.close()
    c = svc.list_comments(str(root / "empty.pdf"))["comments"][0]
    assert c["text"] == ""
    r = svc.append_agent_response(str(root / "empty.pdf"), c["id"], "what is meant?", "", rev(svc, str(root / "empty.pdf")))
    assert r["final_text"] == "[test-agent] what is meant?"
    t = thread(svc, c["id"], str(root / "empty.pdf"))
    assert t["edit_history"][-1]["previous_text"] == "" and t["type"] == "Highlight"


def test_rich_text_moved_to_history(svc, plain, root):
    doc = pymupdf.open(plain)
    a = doc[0].add_text_annot((100, 100), "plain body")
    doc.xref_set_key(a.xref, "RC", pymupdf.get_pdf_str("<body><p>rich body</p></body>"))
    doc.saveIncr()
    doc.close()
    c = svc.list_comments(str(root / "plain.pdf"))["comments"][0]
    r = svc.append_agent_response(str(root / "plain.pdf"), c["id"], "resp", "plain body", rev(svc, str(root / "plain.pdf")))
    t = thread(svc, c["id"], str(root / "plain.pdf"))
    assert t["text"] == "plain body\n\n[test-agent] resp" and "has_rich_text" not in t
    assert "rich body" in t["edit_history"][-1]["previous_rich_text"]
    assert r["history_entry"]["kind"] == "inline_agent_response"


def test_completed_root_stays_done_and_reply_target(svc, native, root):
    path, ids = native
    d = thread(svc, "human-nm-1", str(root / "native.pdf"))
    assert d["done"]
    r = svc.append_agent_response(str(root / "native.pdf"), "human-nm-1", "Reworded.", "Reword this", rev(svc, str(root / "native.pdf")))
    t = thread(svc, "human-nm-1", str(root / "native.pdf"))
    assert t["done"] and t["status"] == "Completed" and r["status"] == "Completed"
    assert t["effective_marker_id"] == d["effective_marker_id"]
    assert svc.review_summary(str(root / "native.pdf"))["done"] == 1
    # Also works on a nested human reply; root thread status unchanged.
    rr = svc.append_agent_response(str(root / "native.pdf"), f"xref:{ids['C']}", "ack", "The fox one", r["revision"])
    assert rr["root_id"] == f"xref:{ids['A']}"
    assert thread(svc, f"xref:{ids['A']}", str(root / "native.pdf"))["replies"][1]["text"] == "The fox one\n\n[test-agent] ack"


def test_unsupported_targets_refused(svc, native, plain, root):
    _, ids = native
    r0 = rev(svc, str(root / "native.pdf"))
    with pytest.raises(ReviewError, match="Status markers"):
        svc.append_agent_response(str(root / "native.pdf"), f"xref:{ids['S1']}", "x", "Accepted set by Human Alice", r0)
    with pytest.raises(ReviewError, match="Group member"):
        svc.append_agent_response(str(root / "native.pdf"), f"xref:{ids['G']}", "x", "", r0)
    doc = pymupdf.open(plain)
    doc[0].add_freetext_annot((100, 200, 300, 250), "free text")
    doc.saveIncr()
    doc.close()
    c = svc.list_comments(str(root / "plain.pdf"))["comments"][0]
    with pytest.raises(ReviewError, match="FreeText"):
        svc.append_agent_response(str(root / "plain.pdf"), c["id"], "x", "free text", rev(svc, str(root / "plain.pdf")))


def test_native_reply_creation_removed_external_replies_still_read(svc, native, root):
    """There is no native-reply tool; replies written by viewers are read unchanged."""
    _, ids = native
    hid = f"xref:{ids['A']}"
    assert not hasattr(svc, "reply_to_comment")
    t = thread(svc, hid, str(root / "native.pdf"))
    assert t["text"] == ORIGINAL and [r["text"] for r in t["replies"]] == ["Which one?", "The fox one"]


def test_verification_aborts_if_prefix_broken(svc, native, monkeypatch, root):
    path, ids = native
    monkeypatch.setattr("pdf_review_mcp.service.inline_text", lambda o, l, r: "REPLACED " + r)
    before = path.read_bytes()
    with pytest.raises(ReviewError, match="exact prefix"):
        svc.append_agent_response(str(root / "native.pdf"), f"xref:{ids['A']}", "x", ORIGINAL, rev(svc, str(root / "native.pdf")))
    assert path.read_bytes() == before


def test_history_limit_respected(svc, plain, monkeypatch, root):
    monkeypatch.setattr(pdfstore, "MAX_HISTORY_ENTRIES", 1)
    a = svc.add_comment(str(root / "plain.pdf"), 1, "t", rev(svc, str(root / "plain.pdf")))
    r = svc.append_agent_response(str(root / "plain.pdf"), a["id"], "one", "t", a["revision"])
    with pytest.raises(ReviewError, match="history is full"):
        svc.append_agent_response(str(root / "plain.pdf"), a["id"], "two", r["final_text"], r["revision"])


async def test_append_over_mcp_protocol(root, native):
    _, ids = native
    hid = f"xref:{ids['A']}"
    async with Client(build_server(default_actor="review-agent")) as c:
        s = (await c.call_tool("review_summary", {"path": str(root / "native.pdf")})).data
        r = (await c.call_tool("append_agent_response", {
            "path": str(root / "native.pdf"), "comment_id": hid, "text": "Done.",
            "expected_content": ORIGINAL, "expected_revision": s["revision"]})).data
        assert r["final_text"] == ORIGINAL + "\n\n[review-agent] Done."
        with pytest.raises(ToolError, match="actor label"):
            await c.call_tool("append_agent_response", {
                "path": str(root / "native.pdf"), "comment_id": hid, "text": "x", "actor": "[x]",
                "expected_content": r["final_text"], "expected_revision": r["revision"]})
        with pytest.raises(ToolError, match="required|missing|expected_content"):
            await c.call_tool("append_agent_response", {
                "path": str(root / "native.pdf"), "comment_id": hid, "text": "x", "expected_revision": r["revision"]})


def test_overflow_error_is_actionable_and_names_no_removed_tool(svc, root, monkeypatch):
    """Over-long inline answers are refused with concrete guidance (shorten to N chars, or report
    when no answer fits), the file is unchanged, and no error text recommends a tool that does
    not exist (e.g. the removed reply_to_comment)."""
    import pathlib

    import pdf_review_mcp
    from pdf_review_mcp import service as service_mod

    monkeypatch.setattr(service_mod, "MAX_INLINE_TOTAL", 60)
    d = pymupdf.open()
    x1 = d.new_page().add_text_annot((60, 90), "short request").xref
    x2 = d[0].add_text_annot((160, 90), "y" * 70).xref
    f = root / "o.pdf"
    d.save(f)
    d.close()
    f = str(f)
    data = open(f, "rb").read()
    # "short request" + "\n\n[test-agent] " = 28 chars of fixed text -> room for 32
    with pytest.raises(ReviewError) as e1:
        svc.append_agent_response(f, f"xref:{x1}", "z" * 40, "short request", rev(svc, f))
    assert "Shorten the response to at most 32 characters" in str(e1.value)
    with pytest.raises(ReviewError) as e2:
        svc.append_agent_response(f, f"xref:{x2}", "ok", "y" * 70, rev(svc, f))
    assert "no inline answer fits" in str(e2.value) and "never truncated" in str(e2.value)
    assert open(f, "rb").read() == data
    r = svc.append_agent_response(f, f"xref:{x1}", "z" * 32, "short request", rev(svc, f))
    assert len(r["final_text"]) == 60
    src = "\n".join(p.read_text() for p in pathlib.Path(pdf_review_mcp.__file__).parent.glob("*.py"))
    for gone in ("reply_to_comment", "consolidate_review_annotations", "Reply instead", "reply instead"):
        assert gone not in src, gone
    for msg in (str(e1.value), str(e2.value)):
        assert "reply" not in msg.lower()
