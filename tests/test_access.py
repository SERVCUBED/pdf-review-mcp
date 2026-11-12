"""Access policy: zero-arg startup, absolute paths, --allow-dir
allowlist, refusal of --root/PDF_REVIEW_ROOT, config conflicts, actor defaults. Includes real stdio launches."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from fastmcp import Client
from fastmcp.client.transports import StdioTransport
from fastmcp.exceptions import ToolError

from pdf_review_mcp.access import (
    ALLOWLIST,
    UNRESTRICTED,
    ConfigError,
    AccessError,
    resolve_config,
)
from pdf_review_mcp.pdfstore import ReviewError
from pdf_review_mcp.server import build_server
from pdf_review_mcp.service import ReviewService

from conftest import make_plain_pdf


def clean_env(**extra: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("PDF_REVIEW_")}
    env.update(extra)
    return env


@pytest.fixture
def dirs(tmp_path):
    a, b, c = tmp_path / "alpha docs", tmp_path / "beta", tmp_path / "outside"
    for d in (a, b, c):
        d.mkdir()
        make_plain_pdf(d / f"{d.name.split()[0]}.pdf")
    return a, b, c


def pdf(d: Path) -> Path:
    return d / f"{d.name.split()[0]}.pdf"


def rev(svc, path):
    return svc.review_summary(str(path))["revision"]


# ----------------------------------------------------------------- unrestricted default


def test_default_mode_is_unrestricted_and_ignores_env(monkeypatch, dirs):
    monkeypatch.setenv("PDF_REVIEW_ROOT", str(dirs[2]))  # library never reads env
    svc = ReviewService()
    assert svc.policy.mode == UNRESTRICTED
    assert "NOT a sandbox" in svc.policy.config.describe()


def test_two_distinct_dirs_absolute_paths_cwd_independent(monkeypatch, dirs, tmp_path):
    a, b, _ = dirs
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    make_plain_pdf(elsewhere / "alpha.pdf")  # same name in cwd must not be picked up
    monkeypatch.chdir(elsewhere)
    svc = ReviewService(default_actor="bot")
    for d in (a, b):
        r = svc.add_comment(str(pdf(d)), 1, f"note in {d.name}", rev(svc, pdf(d)))
        assert r["path"] == str(pdf(d).resolve())
        t = svc.list_comments(str(pdf(d)))
        assert t["path"] == str(pdf(d).resolve()) and t["comments"][0]["text"] == f"note in {d.name}"
    assert svc.review_summary(str(elsewhere / "alpha.pdf"))["root_comments"] == 0


def test_relative_paths_rejected_no_implicit_discovery(monkeypatch, dirs):
    a, _, _ = dirs
    monkeypatch.chdir(a)
    svc = ReviewService()
    for bad in ["alpha.pdf", "./alpha.pdf", "../alpha docs/alpha.pdf"]:
        with pytest.raises(AccessError, match="Relative path .* rejected"):
            svc.review_summary(bad)
        with pytest.raises(AccessError, match="Relative"):
            svc.add_comment(bad, 1, "x", "sha256:" + "0" * 64)
    with pytest.raises(AccessError, match="requires an explicit absolute 'directory'"):
        svc.list_pdfs()
    with pytest.raises(AccessError, match="Relative directory"):
        svc.list_pdfs(".")
    assert [f["path"] for f in svc.list_pdfs(str(a))["files"]] == [str(a / "alpha.pdf")]


def test_list_pdfs_bounded_depth_and_no_dir_symlink_follow(dirs, tmp_path):
    a, _, c = dirs
    deep = a / "l1" / "l2" / "l3" / "l4"
    deep.mkdir(parents=True)
    make_plain_pdf(deep / "deep.pdf")
    (a / "linkdir").symlink_to(c, target_is_directory=True)
    svc = ReviewService()
    names = [Path(f["path"]).name for f in svc.list_pdfs(str(a))["files"]]
    assert names == ["alpha.pdf"]  # depth 4 > default 3; linkdir not followed
    names = [Path(f["path"]).name for f in svc.list_pdfs(str(a), max_depth=4)["files"]]
    assert sorted(names) == ["alpha.pdf", "deep.pdf"]


# ----------------------------------------------------------------- allowlist


def test_allowlist_read_write_list_and_escapes(dirs):
    a, b, c = dirs
    (a / "escape.pdf").symlink_to(pdf(c))
    (a / "escdir").symlink_to(c, target_is_directory=True)
    svc = ReviewService(allow_dirs=[str(a), str(b)])
    assert svc.policy.mode == ALLOWLIST
    for d in (a, b):
        r = svc.add_comment(str(pdf(d)), 1, "ok", rev(svc, pdf(d)))
        assert r["id"].startswith("rv-")
        assert [Path(f["path"]).name for f in svc.list_pdfs(str(d))["files"]] == [pdf(d).name]
    before = pdf(c).read_bytes()
    for bad in [str(pdf(c)), str(a / "escape.pdf"), str(a / "escdir" / "outside.pdf"),
                str(a / ".." / "outside" / "outside.pdf")]:
        with pytest.raises(AccessError, match="escapes the allowed directories"):
            svc.review_summary(bad)
        with pytest.raises(AccessError, match="escapes"):
            svc.add_comment(bad, 1, "x", "sha256:" + "0" * 64)
    assert pdf(c).read_bytes() == before
    with pytest.raises(AccessError, match="escapes"):
        svc.list_pdfs(str(c))
    with pytest.raises(AccessError, match="explicit absolute"):
        svc.list_pdfs()
    with pytest.raises(AccessError, match="Relative"):
        svc.review_summary("alpha.pdf")


def test_allow_dir_given_as_symlink_is_resolved(dirs, tmp_path):
    a, _, _ = dirs
    alias = tmp_path / "alias"
    alias.symlink_to(a, target_is_directory=True)
    svc = ReviewService(allow_dirs=[str(alias)])
    assert svc.policy.config.allow_dirs == (a.resolve(),)
    assert svc.review_summary(str(alias / "alpha.pdf"))["path"] == str(pdf(a).resolve())


@pytest.mark.parametrize("kwargs,env,match", [
    ({"allow_dirs": ["relative/dir"]}, {}, "must be an absolute path"),
    ({"allow_dirs": ["/nonexistent/x/y"]}, {}, "does not exist"),
    ({"allow_dirs": [""]}, {}, "non-empty"),
    ({"allow_dirs": ["/tmp"]}, {"PDF_REVIEW_ALLOW_DIRS": '["/tmp"]'}, "Conflicting access configuration"),
    ({"allow_dirs": ["/tmp"]}, {"PDF_REVIEW_ROOT": "/tmp"}, "not supported"),
    ({}, {"PDF_REVIEW_ROOT": "/tmp", "PDF_REVIEW_ALLOW_DIRS": '["/tmp"]'}, "not supported"),
    ({}, {"PDF_REVIEW_ALLOW_DIRS": "[]"}, "empty list"),
    ({}, {"PDF_REVIEW_ALLOW_DIRS": ""}, "JSON array"),
    ({}, {"PDF_REVIEW_ALLOW_DIRS": "/tmp"}, "JSON array"),
    ({}, {"PDF_REVIEW_ALLOW_DIRS": '{"a": 1}'}, "JSON array of strings"),
    ({}, {"PDF_REVIEW_ALLOW_DIRS": '["/tmp", 3]'}, "JSON array of strings"),
    ({}, {"PDF_REVIEW_ALLOW_DIRS": '["rel"]'}, "absolute"),
    ({}, {"PDF_REVIEW_ROOT": ""}, "not supported"),
    ({}, {"PDF_REVIEW_ROOT": "/tmp"}, "not supported"),
])
def test_invalid_or_conflicting_config_fails_closed(kwargs, env, match):
    with pytest.raises(ConfigError, match=match):
        resolve_config(environ=env, **kwargs)


def test_env_config_modes(dirs):
    a, b, _ = dirs
    c = resolve_config(environ={"PDF_REVIEW_ALLOW_DIRS": f'["{a}", "{b}", "{a}"]'})
    assert c.mode == ALLOWLIST and c.allow_dirs == (a.resolve(), b.resolve())
    assert resolve_config(environ={"UNRELATED": "1"}).mode == UNRESTRICTED


@pytest.mark.parametrize("args,env,match", [
    (["--allow-dir", "relative"], {}, "absolute"),
    (["--allow-dir", "/nonexistent/q"], {}, "does not exist"),
    (["--root", "/tmp"], {}, "unrecognized arguments: --root"),
    (["--root", "/tmp", "--allow-dir", "/tmp"], {}, "unrecognized arguments: --root"),
    ([], {"PDF_REVIEW_ROOT": "/tmp"}, "not supported"),
    ([], {"PDF_REVIEW_ROOT": ""}, "not supported"),
    (["--allow-dir", "/tmp"], {"PDF_REVIEW_ROOT": "/tmp"}, "not supported"),
    (["--allow-dir", "/tmp"], {"PDF_REVIEW_ALLOW_DIRS": '["/tmp"]'}, "Conflicting"),
    ([], {"PDF_REVIEW_ALLOW_DIRS": "[]"}, "empty list"),
    ([], {"PDF_REVIEW_ALLOW_DIRS": "not json"}, "JSON array"),
])
def test_cli_invalid_config_exits_2(args, env, match, tmp_path):
    p = subprocess.run([sys.executable, "-m", "pdf_review_mcp.server", *args], env=clean_env(**env),
                       cwd=tmp_path, capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    assert p.returncode == 2, p.stderr
    assert match in p.stderr


# ----------------------------------------------------------------- no root mode (guard)


def test_no_root_mode_api(dirs):
    a, _, _ = dirs
    with pytest.raises(TypeError):
        resolve_config(root=str(a))  # there is no root parameter
    with pytest.raises(ReviewError, match="actor must be a string|default actor"):
        ReviewService(a)  # the first positional is the actor, not a root
    svc = ReviewService()
    with pytest.raises(AccessError, match="Relative path"):
        svc.review_summary("alpha.pdf")
    import pdf_review_mcp.access as ac
    assert not hasattr(ac, "LEGACY_ROOT") and not hasattr(ac, "Sandbox") and not hasattr(ac, "resolve_root")
    assert not hasattr(svc, "sandbox")


# ----------------------------------------------------------------- actor


def test_actor_default_startup_and_override(dirs):
    a, _, _ = dirs
    p = pdf(a)
    svc = ReviewService()
    r1 = svc.add_comment(str(p), 1, "default actor", rev(svc, p))
    svc2 = ReviewService(default_actor="startup-bot")
    r2 = svc2.add_comment(str(p), 1, "startup actor", r1["revision"])
    r3 = svc2.append_agent_response(str(p), r2["id"], "override", "startup actor", r2["revision"], actor="Per Call")
    r4 = svc2.mark_comment_done(str(p), r1["id"], "done", r3["revision"])
    authors = {c["id"]: c["author"] for c in svc.list_comments(str(p))["comments"]}
    assert authors == {r1["id"]: "agent", r2["id"]: "startup-bot"}  # /T never changes
    assert svc.get_comment(str(p), r2["id"], 0)["thread"]["text"] == "startup actor\n\n[Per Call] override"
    hist = svc.get_comment(str(p), r1["id"], 0)["thread"]["state_history"]
    assert hist[-1]["actor"] == "startup-bot" and r4["annotations_created"] == 0


# ----------------------------------------------------------------- real stdio


async def _stdio(args, cwd, env):
    return Client(StdioTransport(command=sys.executable, args=["-m", "pdf_review_mcp.server", *args],
                                 env=env, cwd=str(cwd)))


async def test_stdio_zero_args_two_dirs_cwd_independent(dirs, tmp_path):
    a, b, _ = dirs
    cwd = tmp_path / "unrelated cwd"
    cwd.mkdir()
    make_plain_pdf(cwd / "alpha.pdf")
    cwd_bytes = (cwd / "alpha.pdf").read_bytes()
    async with await _stdio([], cwd, clean_env()) as c:
        assert "unrestricted mode" in c.instructions and "NOT a sandbox" in c.instructions
        assert len(await c.list_tools()) == 13
        for d in (a, b):
            s = (await c.call_tool("review_summary", {"path": str(pdf(d))})).data
            w = (await c.call_tool("add_comment", {"path": str(pdf(d)), "page": 1, "text": "stdio",
                                                   "expected_revision": s["revision"]})).data
            g = (await c.call_tool("get_comment", {"path": str(pdf(d)), "comment_id": w["id"]})).data
            assert g["thread"]["author"] == "agent" and g["path"] == str(pdf(d).resolve())
        with pytest.raises(ToolError, match="Relative path"):
            await c.call_tool("review_summary", {"path": "alpha.pdf"})
        with pytest.raises(ToolError, match="directory"):
            await c.call_tool("list_pdfs", {})  # schema: directory is required
        lst = (await c.call_tool("list_pdfs", {"directory": str(b)})).data
        assert [f["path"] for f in lst["files"]] == [str(b / "beta.pdf")]
    assert (cwd / "alpha.pdf").read_bytes() == cwd_bytes  # cwd file never touched
    assert not [n for n in os.listdir(cwd) if n.startswith(".")]  # no lock files in cwd


async def test_stdio_allow_dir_and_actor_env(dirs, tmp_path):
    a, _, c_dir = dirs
    env = clean_env(PDF_REVIEW_ACTOR="env-actor")
    async with await _stdio(["--allow-dir", str(a)], tmp_path, env) as c:
        assert "allowlist mode" in c.instructions
        s = (await c.call_tool("review_summary", {"path": str(pdf(a))})).data
        w = (await c.call_tool("add_comment", {"path": str(pdf(a)), "page": 1, "text": "x",
                                               "expected_revision": s["revision"]})).data
        g = (await c.call_tool("get_comment", {"path": str(pdf(a)), "comment_id": w["id"]})).data
        assert g["thread"]["author"] == "env-actor"
        with pytest.raises(ToolError, match="escapes the allowed directories"):
            await c.call_tool("review_summary", {"path": str(pdf(c_dir))})
    async with await _stdio(["--allow-dir", str(a), "--actor", "cli-actor"], tmp_path, env) as c:
        s = (await c.call_tool("review_summary", {"path": str(pdf(a))})).data
        w = (await c.call_tool("add_comment", {"path": str(pdf(a)), "page": 1, "text": "y",
                                               "expected_revision": s["revision"]})).data
        g = (await c.call_tool("get_comment", {"path": str(pdf(a)), "comment_id": w["id"]})).data
        assert g["thread"]["author"] == "cli-actor"


async def test_stdio_refuses_pdf_review_root_env(dirs, tmp_path):
    a, _, _ = dirs
    p = subprocess.run([sys.executable, "-m", "pdf_review_mcp.server"], cwd=tmp_path, timeout=60,
                       env=clean_env(PDF_REVIEW_ROOT=str(a)), capture_output=True, text=True,
                       stdin=subprocess.DEVNULL)
    assert p.returncode == 2 and "not supported" in p.stderr and "widening" in p.stderr


async def test_in_memory_build_server_default_is_unrestricted(dirs):
    async with Client(build_server()) as c:
        assert "unrestricted mode" in c.instructions
        s = (await c.call_tool("review_summary", {"path": str(pdf(dirs[0]))})).data
        assert s["root_comments"] == 0
