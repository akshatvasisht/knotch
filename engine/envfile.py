"""Tiny .env loader (no dependency). Domain-agnostic.

Loads KEY=VALUE lines from a .env file into os.environ (without overriding
already-set vars, so real shell env wins). Endpoints/keys live in .env, never
in code.
"""
from __future__ import annotations

import os
import re
from pathlib import Path


def load_env(path: str = ".env", *, override: bool = False) -> None:
    p = Path(path)
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        # Strip an inline comment ( whitespace + # ) on UNQUOTED values; a '#'
        # without leading space (e.g. a URL fragment) is kept.
        val = re.split(r"\s+#", val, maxsplit=1)[0]
        val = val.strip().strip('"').strip("'")
        if key and (override or key not in os.environ):
            os.environ[key] = val
