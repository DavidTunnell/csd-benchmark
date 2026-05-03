"""Shared helpers for the csd-benchmark seed scripts.

Keep this module dependency-light. boto3 + PyYAML + stdlib only.
Anything domain-specific belongs in the calling script.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

LOG_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def get_logger(name: str) -> logging.Logger:
    """Return a logger configured once per process."""
    root = logging.getLogger()
    if not root.handlers:
        logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, stream=sys.stdout)
    return logging.getLogger(name)


@dataclass(frozen=True)
class Config:
    """Typed view over config.yaml. Keep parsing in one place."""

    raw: dict[str, Any]

    @classmethod
    def load(cls, path: str | os.PathLike[str] | None = None) -> "Config":
        """Load config.yaml from the seed/ directory unless an override is given."""
        if path is None:
            path = Path(__file__).resolve().parent / "config.yaml"
        path = Path(path)
        if not path.is_file():
            raise FileNotFoundError(f"config.yaml not found at {path}")
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if not isinstance(data, dict):
            raise ValueError(f"config.yaml must be a mapping, got {type(data).__name__}")
        return cls(raw=data)

    @property
    def region(self) -> str:
        return self.raw["aws"]["region"]

    @property
    def account_id(self) -> str:
        return str(self.raw["aws"]["account_id"])

    def bucket(self, key: str) -> dict[str, Any]:
        return self.raw["buckets"][key]

    def oss_sources(self) -> list[dict[str, Any]]:
        return list(self.raw["oss_sources"])


def run_cmd(cmd: list[str], cwd: str | os.PathLike[str] | None = None, check: bool = True) -> str:
    """Run a subprocess, capture stdout, raise on non-zero exit by default.

    Returns stdout as a stripped string. Stderr is forwarded to the parent process
    so users can see git progress in real time.
    """
    log = get_logger("run_cmd")
    log.debug("running: %s (cwd=%s)", " ".join(cmd), cwd)
    proc = subprocess.run(
        cmd,
        cwd=str(cwd) if cwd else None,
        check=False,
        capture_output=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=None,
    )
    if check and proc.returncode != 0:
        raise RuntimeError(
            f"command failed (exit {proc.returncode}): {' '.join(cmd)}"
        )
    return (proc.stdout or "").strip()


def workdir(*parts: str) -> Path:
    """Return a path under .work/ at the repo root, creating dirs as needed.

    .work/ is git-ignored. Used for OSS clones and scratch files during seeding.
    """
    root = Path(__file__).resolve().parents[1] / ".work"
    p = root.joinpath(*parts)
    p.mkdir(parents=True, exist_ok=True)
    return p


class SeedingError(RuntimeError):
    """Raised when a seeding precondition or postcondition fails."""
