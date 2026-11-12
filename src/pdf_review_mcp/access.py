"""Path access policy.

Two mutually exclusive modes:

* ``unrestricted`` (default, zero configuration): any ABSOLUTE path the server process can
  access under normal OS permissions. Not a sandbox. Relative paths are always rejected: there
  is no root, working-directory or project-directory resolution.
* ``allowlist``: absolute paths only; the symlink-resolved real path must lie inside one of
  the configured allowed directories (``--allow-dir``, repeatable, or the
  ``PDF_REVIEW_ALLOW_DIRS`` JSON array).

Containment is evaluated on the fully resolved real path for every read, write and listing.

Guard: ``PDF_REVIEW_ROOT`` is not a supported setting. If it is present in the environment the
server refuses to start (exit 2) instead of ignoring it, so a configuration that expected a
restricted root can never silently run with unrestricted access.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

UNRESTRICTED = "unrestricted"
ALLOWLIST = "allowlist"

ENV_ALLOW_DIRS = "PDF_REVIEW_ALLOW_DIRS"
ENV_REFUSED_ROOT = "PDF_REVIEW_ROOT"


class AccessError(ValueError):
    """A requested path is not allowed or not usable under the active access policy."""


class ConfigError(AccessError):
    """The access configuration itself is invalid or conflicting (startup fails closed)."""


def _real_dir(value: str | os.PathLike[str], what: str) -> Path:
    s = os.fspath(value)
    if not isinstance(s, str) or not s.strip():
        raise ConfigError(f"{what} must be a non-empty path")
    if "\x00" in s:
        raise ConfigError(f"{what} contains a NUL byte")
    p = Path(s).expanduser()
    if not p.is_absolute():
        raise ConfigError(f"{what} must be an absolute path: {s!r}")
    if not p.exists():
        raise ConfigError(f"{what} does not exist: {s}")
    real = p.resolve(strict=True)
    if not real.is_dir():
        raise ConfigError(f"{what} is not a directory: {real}")
    return real


def parse_allow_dirs_env(raw: str) -> list[str]:
    try:
        val = json.loads(raw)
    except ValueError as exc:
        raise ConfigError(f"{ENV_ALLOW_DIRS} must be a JSON array of absolute paths: {exc}") from None
    if not isinstance(val, list) or not all(isinstance(v, str) for v in val):
        raise ConfigError(f"{ENV_ALLOW_DIRS} must be a JSON array of strings")
    if not val:
        raise ConfigError(f"{ENV_ALLOW_DIRS} is an empty list; refusing to start (would be ambiguous)")
    return val


@dataclass(frozen=True)
class AccessConfig:
    mode: str
    allow_dirs: tuple[Path, ...] = ()
    source: str = "default"

    def describe(self) -> str:
        if self.mode == ALLOWLIST:
            dirs = ", ".join(str(d) for d in self.allow_dirs)
            return (f"allowlist mode ({self.source}): ABSOLUTE paths only, and the resolved real "
                    f"path must be inside one of: {dirs}.")
        return ("unrestricted mode (default): ABSOLUTE paths to any PDF the server process can "
                "access under normal OS permissions. This is NOT a sandbox. Relative paths are "
                "rejected; list_pdfs requires an explicit absolute directory.")


def resolve_config(
    allow_dirs: Sequence[str | os.PathLike[str]] | None = None,
    environ: Mapping[str, str] | None = None,
) -> AccessConfig:
    """Build the access configuration from explicit arguments and (optionally) environment.

    Rules (fail closed, never silently widen):
      * PDF_REVIEW_ROOT set (any value) -> error (unsupported; never silently ignored).
      * CLI --allow-dir together with PDF_REVIEW_ALLOW_DIRS -> error.
      * Set-but-empty / invalid values -> error (never fall back to unrestricted).
      * Nothing configured -> unrestricted.
    Pass ``environ=None`` to ignore the environment entirely (library use).
    """
    cli_allow = list(allow_dirs or [])
    env = environ or {}
    if env.get(ENV_REFUSED_ROOT) is not None:
        raise ConfigError(
            f"{ENV_REFUSED_ROOT} is not supported. Remove it from the environment; use absolute "
            f"paths, optionally restricted with --allow-dir or {ENV_ALLOW_DIRS}. Refusing to start "
            "rather than silently widening access."
        )
    env_allow = env.get(ENV_ALLOW_DIRS)
    if cli_allow and env_allow is not None:
        raise ConfigError(
            f"Conflicting access configuration: --allow-dir given while {ENV_ALLOW_DIRS} is set in "
            "the environment. Unset the variable or drop the option(s)."
        )
    if cli_allow:
        dirs = tuple(dict.fromkeys(_real_dir(d, "--allow-dir") for d in cli_allow))
        return AccessConfig(ALLOWLIST, allow_dirs=dirs, source="--allow-dir")
    if env_allow is not None:
        dirs = tuple(dict.fromkeys(_real_dir(d, ENV_ALLOW_DIRS + " entry")
                                   for d in parse_allow_dirs_env(env_allow)))
        return AccessConfig(ALLOWLIST, allow_dirs=dirs, source=ENV_ALLOW_DIRS)
    return AccessConfig(UNRESTRICTED)


def _inside(p: Path, base: Path) -> bool:
    try:
        p.relative_to(base)
        return True
    except ValueError:
        return False


class AccessPolicy:
    def __init__(self, config: AccessConfig):
        self.config = config

    @property
    def mode(self) -> str:
        return self.config.mode

    def allowed(self, real: Path) -> bool:
        if self.config.mode == ALLOWLIST:
            return any(_inside(real, d) for d in self.config.allow_dirs)
        return True

    def _resolve_any(self, value: str, what: str = "path") -> Path:
        if not isinstance(value, str) or value == "":
            raise AccessError(f"{what} must be a non-empty string.")
        if "\x00" in value:
            raise AccessError(f"{what} contains a NUL byte.")
        candidate = Path(value)
        if not candidate.is_absolute():
            raise AccessError(
                f"Relative {what} {value!r} rejected: pass an absolute path "
                "(e.g. '/home/user/docs/report.pdf'). This server has no working or project root."
            )
        try:
            real = candidate.resolve(strict=False)
        except (OSError, RuntimeError) as exc:  # symlink loops etc.
            raise AccessError(f"Cannot resolve {what} {value!r}: {exc}") from None
        # Containment first, so nothing is revealed about disallowed locations.
        if not self.allowed(real):
            raise AccessError(f"Path escapes the allowed directories: {value!r}")
        if not real.exists():
            raise AccessError(f"Path not found: {value!r}")
        return real

    def resolve_dir(self, value: str) -> Path:
        real = self._resolve_any(value, "directory")
        if not real.is_dir():
            raise AccessError(f"Not a directory: {value!r}")
        return real

    def resolve_pdf(self, value: str) -> Path:
        """Resolve to an existing regular .pdf file whose real path is allowed."""
        real = self._resolve_any(value, "path")
        if not real.is_file():
            raise AccessError(f"Not a regular file: {value!r}")
        if real.suffix.lower() != ".pdf":
            raise AccessError(f"Only .pdf files are allowed: {value!r}")
        return real

    @staticmethod
    def display(real: Path) -> str:
        """Absolute real path (output paths are always absolute)."""
        return str(real)
