# Installation

The package is not published on PyPI. All installs are from a local source tree, usually a clone
of https://github.com/SERVCUBED/pdf-review-mcp.

## Prerequisites

* Linux (supported) or macOS (expected to work, untested). Windows is not supported for writing.
* Python ≥ 3.11 (`python3 --version`). Release 1.0.0 was validated on CPython 3.12; the CI
  configuration covers 3.11 to 3.14.
* Either [uv](https://docs.astral.sh/uv/) (recommended) or `pip` with `venv`.
* Network access to the package index, to download `fastmcp`, `pymupdf` and `portalocker`, unless
  they are already in your local cache or wheelhouse (see "Offline" below).
* **portalocker must be exactly 4.4.2** (pinned in `pyproject.toml`). If another version ends up
  installed, for example through a manual upgrade or a shared environment, the server still
  reads but **refuses every write** with "reviewed only for portalocker==4.4.2". Do not upgrade
  it on your own: a new version needs a code review and the lock tests rerun.
* Writes are supported only for PDFs in a stable directory you own, on a local POSIX
  filesystem. Shared, network and other-user-writable directories are not supported; see
  `SECURITY.md`.

## Option A: uv tool (stable, non-editable, user level)

```bash
cd pdf-review-mcp
uv build --wheel --out-dir dist
uv tool install ./dist/pdf_review_mcp-1.0.0-py3-none-any.whl
pdf-review-mcp --version                      # 1.0.0
command -v pdf-review-mcp                     # absolute path for your MCP client config
```

uv places the environment under its tool directory (`uv tool dir`) and the executable in its bin
directory (`uv tool dir --bin`, usually `~/.local/bin`). If that directory is not on `PATH`, use
the absolute path in your client config; you don't need to edit shell profiles.

## Option B: pip in a dedicated venv

```bash
python3 -m venv ~/.local/share/pdf-review-mcp-venv
~/.local/share/pdf-review-mcp-venv/bin/pip install /path/to/pdf-review-mcp
~/.local/share/pdf-review-mcp-venv/bin/pdf-review-mcp --version
```

Use `~/.local/share/pdf-review-mcp-venv/bin/pdf-review-mcp` (as an absolute path) in your MCP
client config.

## Option C: from a git URL

The official repository is `https://github.com/SERVCUBED/pdf-review-mcp`. Use a different URL
only if the repository owner gave you one.

```bash
git clone https://github.com/SERVCUBED/pdf-review-mcp pdf-review-mcp && cd pdf-review-mcp   # then Option A or B
```

## Offline

* **With a populated uv cache:** `uv build --wheel --offline` and
  `uv tool install --offline ./dist/<wheel>` work if `fastmcp`, `pymupdf`, `portalocker` and
  their dependencies are already cached.
* **With a wheelhouse prepared on a connected machine** (`pip download . -d wheelhouse`):
  `pip install --no-index --find-links wheelhouse /path/to/pdf-review-mcp`.

## Validate

```bash
python3 scripts/smoke_test.py --command "$(command -v pdf-review-mcp)"   # needs fastmcp+pymupdf+portalocker in that python
# or, from a development venv:
.venv/bin/python scripts/smoke_test.py --command "$(command -v pdf-review-mcp)"
```

It must print `SMOKE OK`. It uses only a temporary directory, a temporary PDF and a temporary
`XDG_CONFIG_HOME`; your PDFs and your style key are not touched.

## Configure your MCP client

See `examples/mcp-client-config.json` (unrestricted) and
`examples/mcp-client-config.restricted.json` (allowlist). Replace `<ABSOLUTE_PATH_TO>` with the
output of `command -v pdf-review-mcp`. **Restart or reconnect the client afterwards.**

Environment variables (all optional):

| Variable | Meaning |
|---|---|
| `PDF_REVIEW_ALLOW_DIRS` | JSON array of absolute directories (allowlist); conflicts with `--allow-dir` |
| `PDF_REVIEW_ACTOR` | default label for answers and history (`--actor` overrides it) |
| `PDF_REVIEW_LOCK_TIMEOUT` | seconds to wait for a busy file (default 10) |
| `PDF_REVIEW_MAX_BYTES` | refuse larger PDFs (default 200 MiB) |
| `PDF_REVIEW_ROOT` | **not supported**: if set, the server refuses to start |

## Update and uninstall

* **uv:** `uv tool install --force ./dist/<new wheel>` to update; `uv tool uninstall
  pdf-review-mcp` to remove.
* **pip:** `<venv>/bin/pip install --upgrade /path/to/new/source` to update; delete the venv
  directory to remove.
* After either one, update or remove the MCP client entry and reconnect the client.
* **Optional:** remove `~/.config/pdf-review-mcp/` (the completion-style key). Grey comments can
  then no longer be un-greyed by `reopen_comment`; their text and review state are unaffected.
