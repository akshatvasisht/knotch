"""THE GENERALITY GATE.

Walks every *.py file under engine/ (recursively) and asserts that none
contain domain literals.  This is the CI gate that proves the engine is
domain-agnostic.

Only engine/ is scanned — adapters/, domains/, and prompts/ are intentionally
excluded because those directories are allowed to hold domain knowledge.
"""
from __future__ import annotations

import pathlib

# Resolve the engine/ directory relative to this test file's own location.
# tests/ sits at the same level as engine/.
_TESTS_DIR = pathlib.Path(__file__).parent
_ENGINE_DIR = _TESTS_DIR.parent / "engine"

# Case-insensitive substring matches to prohibit in engine/ source files.
DOMAIN_LITERALS = [
    "kitchen",
    "cook",
    "grill",
    "fryer",
    "expo",
    "restaurant",
    "race_ops",
    "warehouse",
    "brigade",
    "salads",
]


def _collect_engine_py_files() -> list[pathlib.Path]:
    """Return all *.py files found recursively under engine/."""
    return sorted(_ENGINE_DIR.rglob("*.py"))


def test_engine_dir_exists_and_has_interfaces():
    """Sanity: engine/ directory exists and contains at least interfaces.py."""
    assert _ENGINE_DIR.exists(), (
        f"engine/ directory not found at {_ENGINE_DIR}. "
        "Check repository layout."
    )
    assert _ENGINE_DIR.is_dir(), f"{_ENGINE_DIR} is not a directory."
    interfaces_py = _ENGINE_DIR / "interfaces.py"
    assert interfaces_py.exists(), (
        f"engine/interfaces.py not found at {interfaces_py}. "
        "The frozen contracts file is missing."
    )


def test_no_domain_literals_in_engine():
    """Assert that no domain-specific word appears anywhere under engine/."""
    py_files = _collect_engine_py_files()
    assert py_files, (
        f"No *.py files found under {_ENGINE_DIR}. "
        "Either the directory is empty or the path is wrong."
    )

    violations: list[str] = []

    for py_file in py_files:
        text = py_file.read_text(encoding="utf-8")
        lines = text.splitlines()
        for lineno, line in enumerate(lines, start=1):
            line_lower = line.lower()
            for word in DOMAIN_LITERALS:
                if word in line_lower:
                    rel = py_file.relative_to(_ENGINE_DIR.parent)
                    violations.append(
                        f"{rel}:{lineno}: found '{word}' in: {line.rstrip()}"
                    )

    assert not violations, (
        "Domain literals found in engine/ source files "
        "(engine must remain domain-agnostic):\n"
        + "\n".join(violations)
    )
