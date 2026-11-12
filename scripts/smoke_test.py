#!/usr/bin/env python
"""Stdio smoke test: launches the server with ZERO arguments as a real MCP subprocess (from an
unrelated temp working directory, PDF_REVIEW_* env cleared) and drives the single-comment
lifecycle on ABSOLUTE paths: inline answer, done ([DONE] + grey, no annotation), no-op sync,
answer while done, reopen (prefix removed, answers kept); no lock or temp file left.

Usage: python scripts/smoke_test.py [--command /path/to/pdf-review-mcp]
(default command: <this python> -m pdf_review_mcp.server)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
from pathlib import Path

import pymupdf
from fastmcp import Client
from fastmcp.client.transports import StdioTransport


def make_sample(path: Path) -> None:
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((72, 100), "Quarterly revenue grew 12 percent year over year.")
    a = page.add_text_annot((60, 90), "Please cite the source for 12%")
    a.set_info(title="Human Reviewer")
    a.update()
    doc.save(path)
    doc.close()


def annots(path: str) -> list:
    d = pymupdf.open(path)
    try:
        return [(p.number, d.xref_get_key(p.xref, "Annots")) for p in d]
    finally:
        d.close()


async def run(root: Path, command: list[str]) -> None:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PDF_REVIEW_")}
    # Disposable key dir: the smoke test must never create the real user style key.
    env["XDG_CONFIG_HOME"] = str(root / "xdg-config")
    key = root / "xdg-config" / "pdf-review-mcp" / "completion-style.key"
    cwd = root / "cwd"
    cwd.mkdir()
    transport = StdioTransport(command=command[0], args=command[1:], env=env, cwd=str(cwd))
    sample = str(root / "sample.pdf")
    async with Client(transport) as c:
        info = c.server_info
        print("server:", getattr(info, "name", "?"), getattr(info, "version", "?"),
              "| command:", command[0], "| args:", command[1:], "| cwd:", cwd)
        assert "unrestricted mode" in (c.instructions or ""), c.instructions
        tools = sorted(t.name for t in await c.list_tools())
        print("tools:", len(tools), ", ".join(tools))
        assert len(tools) == 13, tools
        assert not {"reply_to_comment", "consolidate_review_annotations"} & set(tools), tools
        s = (await c.call_tool("review_summary", {"path": sample})).data
        assert s["open"] == 1, s
        oc = (await c.call_tool("list_open_comments", {"path": sample})).data
        cid, orig = oc["comments"][0]["id"], oc["comments"][0]["text"]
        ctx = (await c.call_tool("get_comment_context", {"path": sample, "comment_id": cid})).data
        assert "revenue" in ctx["context"]["surrounding_text"]
        assert not key.exists(), "read-only calls must not create the style key"
        a0 = annots(sample)
        ap = (await c.call_tool("append_agent_response", {
            "path": sample, "comment_id": cid, "text": "Added citation [3] after the figure.",
            "expected_content": orig, "expected_revision": oc["revision"], "actor": "review-agent"})).data
        d = (await c.call_tool("mark_comment_done", {
            "path": sample, "comment_id": cid, "resolution_note": "Citation [3] added in source.",
            "expected_revision": ap["revision"]})).data
        assert d["appearance"]["action"] == "apply" and d["annotations_created"] == 0, d
        assert d["text"] == "[DONE] " + ap["final_text"], d
        assert key.exists() and (key.stat().st_mode & 0o777) == 0o600
        assert (key.parent.stat().st_mode & 0o777) == 0o700 and key.read_text().strip() not in str(d)
        print("style key: created lazily on first styling write, 0600 in 0700 dir (disposable)")
        gd = (await c.call_tool("get_comment", {"path": sample, "comment_id": cid})).data
        assert gd["thread"]["appearance"]["color"] == [0.55, 0.55, 0.55] and gd["thread"]["done"]
        sy = (await c.call_tool("sync_comment_appearance", {
            "path": sample, "comment_id": cid, "expected_revision": d["revision"]})).data
        assert sy["changed"] is False and sy["revision"] == d["revision"], sy
        ap2 = (await c.call_tool("append_agent_response", {
            "path": sample, "comment_id": cid, "text": "URL added too.", "expected_content": d["text"],
            "expected_revision": d["revision"], "actor": "review-agent"})).data
        o = (await c.call_tool("reopen_comment", {
            "path": sample, "comment_id": cid, "note": "Reviewer wants more",
            "expected_revision": ap2["revision"], "actor": "smoke-agent"})).data
        assert o["appearance"]["action"] == "restore", o
        assert o["text"] == ap["final_text"] + "\n\n[review-agent] URL added too.", o  # prefix only removed
        assert annots(sample) == a0, "annotation list changed"
        g = (await c.call_tool("get_comment", {"path": sample, "comment_id": cid})).data
        assert g["thread"]["author"] == "Human Reviewer" and g["thread"]["reply_count"] == 0
        assert [h["state"] for h in g["thread"]["state_history"]] == ["Completed", "None"]
        print("single comment:", repr(g["thread"]["text"]))
        locks = [p.name for p in root.iterdir() if p.name.endswith(".review.lock")]
        assert not locks, f"lock file left beside the PDF: {locks}"
        assert not [p.name for p in root.iterdir() if p.name.endswith(".tmp")]
        print("no lock/tmp files left; no annotation created by done/reopen/sync")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--command", default=None, help="server executable (launched with no args)")
    a = ap.parse_args()
    command = [a.command] if a.command else [sys.executable, "-m", "pdf_review_mcp.server"]
    with tempfile.TemporaryDirectory() as d:
        root = Path(d)
        make_sample(root / "sample.pdf")
        asyncio.run(run(root, command))
    print("SMOKE OK")


if __name__ == "__main__":
    main()
