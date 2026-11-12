# Contributing

Thank you for your interest. By contributing you agree that your contributions are licensed
under this project's licenses: code under **AGPL-3.0-or-later** and documentation under
**CC-BY-SA-4.0**, as described in [LICENSING.md](LICENSING.md) and [`REUSE.toml`](REUSE.toml).
Keep existing copyright and license notices. If you add a file outside the paths covered by
`REUSE.toml`, add it there (or give it SPDX headers), and keep `reuse lint` passing.

## Setup

```bash
uv venv .venv && uv pip install --python .venv/bin/python -e '.[dev]'
# or: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
```

## Checks before a pull request

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/smoke_test.py
uv build --out-dir dist        # or: .venv/bin/pip wheel . -w dist --no-deps
```

All of them must pass. The CI workflow (`.github/workflows/ci.yml`) runs the same steps on Linux.

## Rules

* **Tests use generated fixtures only.** Never commit real PDFs, keys, logs or personal paths.
  Each test gets its own `XDG_CONFIG_HOME` (see `tests/conftest.py`), and that isolation must not
  be bypassed.
* **Keep the safety properties:** absolute paths and the allowlist, revision checks, atomic
  verified writes, the transient lock, fail-closed key handling, the zero-annotation-delta state
  changes, and no native reply or status creation. Changes to these need tests demonstrating the
  new behaviour and its failure modes.
* **Don't add compatibility shims or migration paths** for unreleased formats. Unknown record
  versions must keep failing closed.
* **Treat PDF content as untrusted** in code and in tool descriptions.
* Keep tool schemas, `docs/REFERENCE.md`, `AGENTS.md` and `docs/AGENT_GUIDE.md` consistent.
  Tests assert the exact tool set.
