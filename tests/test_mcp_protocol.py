"""End-to-end tests through the real MCP protocol (FastMCP in-memory and stdio transports)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from pdf_review_mcp.server import build_server

EXPECTED_TOOLS = {
    "list_pdfs", "review_summary", "list_comments", "list_open_comments", "get_comment",
    "get_comment_context", "read_page_text", "add_comment", "edit_comment", "append_agent_response",
    "mark_comment_done", "reopen_comment", "sync_comment_appearance",
}
DESTRUCTIVE_TOOLS = {"edit_comment", "append_agent_response",
                     "mark_comment_done", "reopen_comment", "sync_comment_appearance"}
READ_ONLY = EXPECTED_TOOLS - {"add_comment", "edit_comment", "append_agent_response",
                              "mark_comment_done", "reopen_comment", "sync_comment_appearance"}


async def test_tools_schema_and_instructions(root, native):
    async with Client(build_server()) as c:
        tools = {t.name: t for t in await c.list_tools()}
        assert set(tools) == EXPECTED_TOOLS
        for name in READ_ONLY:
            assert tools[name].annotations.read_only_hint is True, name
        for name in EXPECTED_TOOLS - READ_ONLY:
            assert tools[name].annotations.read_only_hint is False
            assert "expected_revision" in tools[name].input_schema["required"]
        for name in EXPECTED_TOOLS:
            assert tools[name].annotations.destructive_hint is (name in DESTRUCTIVE_TOOLS), name
        assert len(tools) == 13
        assert not {"reply_to_comment", "consolidate_review_annotations"} & set(tools)
        for name in ("edit_comment", "append_agent_response"):
            assert {"expected_content", "expected_revision"} <= set(tools[name].input_schema["required"])
            assert tools[name].annotations.read_only_hint is False
        assert "actor" not in tools["append_agent_response"].input_schema["required"]
        assert "DESTRUCTIVE" in tools["append_agent_response"].description
        assert "There is no native-reply tool" in c.instructions
        assert "only add_comment adds" in c.instructions and "never creates reply" not in c.instructions and "[DONE]" in c.instructions
        assert "expected_content" in tools["edit_comment"].input_schema["required"]
        assert "DESTRUCTIVE" in tools["edit_comment"].description
        assert "untrusted" in tools["list_comments"].description.lower()
        assert "untrusted" in tools["get_comment"].description.lower()
        assert "not done" in tools["mark_comment_done"].description.lower()
        instructions = c.instructions
        assert "UNTRUSTED DATA" in instructions
        assert "Reading a comment is not" in instructions


async def test_lifecycle_over_protocol(root, native):
    _, ids = native
    async with Client(build_server(default_actor="mcp-agent")) as c:
        s = (await c.call_tool("review_summary", {"path": str(root / "native.pdf")})).data
        assert s["open"] == 3
        opened = (await c.call_tool("list_open_comments", {"path": str(root / "native.pdf")})).data
        target = next(x for x in opened["comments"] if x["author"] == "Human Alice")
        r = (await c.call_tool("append_agent_response", {
            "path": str(root / "native.pdf"), "comment_id": target["id"], "text": "Done: fixed typo",
            "expected_content": target["text"], "expected_revision": opened["revision"]})).data
        d = (await c.call_tool("mark_comment_done", {
            "path": str(root / "native.pdf"), "comment_id": target["id"],
            "resolution_note": "Typo fixed in source; rebuilt PDF p1.",
            "expected_revision": r["revision"]})).data
        assert d["state"] == "Completed"
        # stale write surfaces as a tool error
        with pytest.raises(ToolError, match="Stale revision"):
            await c.call_tool("append_agent_response", {
                "path": str(root / "native.pdf"), "comment_id": target["id"], "text": "late",
                "expected_content": target["text"], "expected_revision": opened["revision"]})
        with pytest.raises(ToolError, match="Relative path"):
            await c.call_tool("review_summary", {"path": "../x.pdf"})
        g = (await c.call_tool("get_comment", {"path": str(root / "native.pdf"), "comment_id": target["id"]})).data
        assert g["thread"]["done"] and g["thread"]["text"] == "[DONE] " + target["text"] + "\n\n[mcp-agent] Done: fixed typo"
        assert g["thread"]["author"] == "Human Alice" and g["thread"]["reply_count"] == 2  # viewer replies kept
        with pytest.raises(ToolError, match="Unknown tool"):
            await c.call_tool("reply_to_comment", {"path": "x", "comment_id": "y", "text": "z",
                                                   "expected_revision": "sha256:0"})
        s2 = (await c.call_tool("review_summary", {"path": str(root / "native.pdf")})).data
        assert s2["open"] == 2 and s2["revision"] == d["revision"]


def test_stdio_smoke_script(root, native):
    script = Path(__file__).resolve().parents[1] / "scripts" / "smoke_test.py"
    env = dict(os.environ)
    p = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                       timeout=180, env=env, stdin=subprocess.DEVNULL)
    assert p.returncode == 0, p.stdout + p.stderr
    assert "SMOKE OK" in p.stdout
