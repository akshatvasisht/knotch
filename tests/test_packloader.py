"""Tests for engine.domain_loader — load_pack, load_scaffold, assemble_system_prompt, _merge_rubrics."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from engine.domain_loader import (
    _merge_rubrics,
    assemble_system_prompt,
    load_pack,
    load_scaffold,
)
from engine.interfaces import DomainPack, RoleSpec


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #

def _write_yaml(path: Path, data: object) -> None:
    path.write_text(yaml.dump(data), encoding="utf-8")


def _make_domain(
    tmp_path: Path,
    *,
    domain_name: str = "testdomain",
    pack_extra: dict | None = None,
    missing_pack: bool = False,
    missing_fragment: bool = False,
    missing_scenarios: bool = False,
    fragment_text: str = "# Fragment\nHello from fragment.",
    scenarios: list[dict] | None = None,
    roles: list[dict] | None = None,
) -> tuple[Path, Path]:
    """Create a minimal on-disk domain directory + prompts directory."""
    domains_dir = tmp_path / "domains"
    prompts_dir = tmp_path / "prompts"
    domain_dir = domains_dir / domain_name
    domain_dir.mkdir(parents=True)
    prompts_dir.mkdir(parents=True)

    # routing_scaffold.md (prompts)
    (prompts_dir / "routing_scaffold.md").write_text("# Scaffold\nBe helpful.", encoding="utf-8")

    # base_rubric.yaml (prompts) — present but minimal
    _write_yaml(prompts_dir / "base_rubric.yaml", {"metrics": [{"name": "base_metric", "description": "base"}]})

    if roles is None:
        roles = [
            {"role_id": "role_a", "display_name": "Alice"},
            {"role_id": "role_b", "display_name": "Bob"},
        ]

    pack_data: dict = {
        "domain": domain_name,
        "display_name": "Test Domain",
        "dispatcher_voice_id": "voice-x",
        "roles": roles,
        "routing_policy": "coordinator.md",
        "rubric": "rubric.yaml",
        "scenarios": "scenarios.yaml",
    }
    if pack_extra:
        pack_data.update(pack_extra)

    if not missing_pack:
        _write_yaml(domain_dir / "pack.yaml", pack_data)

    if not missing_fragment:
        (domain_dir / "coordinator.md").write_text(fragment_text, encoding="utf-8")

    if not missing_scenarios:
        sc = scenarios if scenarios is not None else [{"name": "s1", "utterances": []}]
        _write_yaml(domain_dir / "scenarios.yaml", {"scenarios": sc})

    # domain rubric (optional — always write empty one so it doesn't block)
    _write_yaml(domain_dir / "rubric.yaml", {})

    return domains_dir, prompts_dir


# --------------------------------------------------------------------------- #
# load_pack — success                                                          #
# --------------------------------------------------------------------------- #

def test_load_pack_minimal_valid(tmp_path):
    """load_pack succeeds on a minimal valid domain with 2 roles."""
    domains_dir, prompts_dir = _make_domain(tmp_path)
    pack = load_pack("testdomain", domains_dir=str(domains_dir), prompts_dir=str(prompts_dir))
    assert isinstance(pack, DomainPack)


def test_load_pack_correct_field_values(tmp_path):
    """Returned DomainPack fields match the pack.yaml and fragment content."""
    fragment = "Coordinator fragment text."
    domains_dir, prompts_dir = _make_domain(tmp_path, fragment_text=fragment)
    pack = load_pack("testdomain", domains_dir=str(domains_dir), prompts_dir=str(prompts_dir))

    assert pack.domain == "testdomain"
    assert pack.display_name == "Test Domain"
    assert pack.dispatcher_voice_id == "voice-x"
    assert len(pack.roles) == 2
    assert pack.roles[0].role_id == "role_a"
    assert pack.roles[0].display_name == "Alice"
    assert pack.roles[1].role_id == "role_b"
    assert pack.roles[1].display_name == "Bob"
    assert pack.routing_policy == fragment


# --------------------------------------------------------------------------- #
# load_pack — missing directory / files                                        #
# --------------------------------------------------------------------------- #

def test_load_pack_missing_domain_dir(tmp_path):
    """Raises ValueError when the domain directory doesn't exist."""
    domains_dir = tmp_path / "domains"
    domains_dir.mkdir()
    with pytest.raises(ValueError, match="Domain directory not found"):
        load_pack("nonexistent", domains_dir=str(domains_dir))


def test_load_pack_missing_pack_yaml(tmp_path):
    """Raises ValueError when pack.yaml is absent."""
    domains_dir, prompts_dir = _make_domain(tmp_path, missing_pack=True)
    with pytest.raises(ValueError, match="Missing pack.yaml"):
        load_pack("testdomain", domains_dir=str(domains_dir), prompts_dir=str(prompts_dir))


def test_load_pack_missing_routing_policy(tmp_path):
    """Raises ValueError when the coordinator fragment .md file is missing."""
    domains_dir, prompts_dir = _make_domain(tmp_path, missing_fragment=True)
    with pytest.raises(ValueError, match="coordinator fragment file not found"):
        load_pack("testdomain", domains_dir=str(domains_dir), prompts_dir=str(prompts_dir))


def test_load_pack_missing_scenarios_yaml(tmp_path):
    """Raises ValueError when scenarios.yaml is absent."""
    domains_dir, prompts_dir = _make_domain(tmp_path, missing_scenarios=True)
    with pytest.raises(ValueError, match="scenarios file not found"):
        load_pack("testdomain", domains_dir=str(domains_dir), prompts_dir=str(prompts_dir))


# --------------------------------------------------------------------------- #
# load_pack — required field validation                                        #
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("missing_field", ["display_name", "dispatcher_voice_id", "roles", "domain"])
def test_load_pack_missing_required_field(tmp_path, missing_field):
    """Raises ValueError for each missing required top-level pack.yaml field."""
    domains_dir, prompts_dir = _make_domain(tmp_path)
    # Overwrite pack.yaml with the field removed
    domain_dir = domains_dir / "testdomain"
    pack_data = yaml.safe_load((domain_dir / "pack.yaml").read_text())
    del pack_data[missing_field]
    _write_yaml(domain_dir / "pack.yaml", pack_data)

    with pytest.raises(ValueError, match=missing_field):
        load_pack("testdomain", domains_dir=str(domains_dir), prompts_dir=str(prompts_dir))


def test_load_pack_empty_roles_list(tmp_path):
    """Raises ValueError when roles is present but empty."""
    domains_dir, prompts_dir = _make_domain(tmp_path, roles=[])
    with pytest.raises(ValueError, match="non-empty list"):
        load_pack("testdomain", domains_dir=str(domains_dir), prompts_dir=str(prompts_dir))


# --------------------------------------------------------------------------- #
# load_pack — role defaults                                                    #
# --------------------------------------------------------------------------- #

def test_load_pack_role_defaults(tmp_path):
    """voice_id defaults to '' and zone_id defaults to 'z0' when omitted."""
    roles = [
        {"role_id": "role_a", "display_name": "Alice"},  # no voice_id, no zone_id
    ]
    domains_dir, prompts_dir = _make_domain(tmp_path, roles=roles)
    pack = load_pack("testdomain", domains_dir=str(domains_dir), prompts_dir=str(prompts_dir))
    assert pack.roles[0].voice_id == ""
    assert pack.roles[0].zone_id == "z0"


# --------------------------------------------------------------------------- #
# _merge_rubrics                                                               #
# --------------------------------------------------------------------------- #

def test_merge_rubrics_extension_overrides_base_metric():
    """Extension metric with the same name replaces the base metric in place."""
    base = {"metrics": [{"name": "accuracy", "description": "orig"}, {"name": "other", "description": "x"}]}
    ext = {"metrics": [{"name": "accuracy", "description": "improved"}]}
    result = _merge_rubrics(base, ext)
    names = [m["name"] for m in result["metrics"]]
    assert names == ["accuracy", "other"]
    acc = next(m for m in result["metrics"] if m["name"] == "accuracy")
    assert acc["description"] == "improved"


def test_merge_rubrics_new_metric_appended():
    """A metric name present only in the extension is appended after base metrics."""
    base = {"metrics": [{"name": "base_m", "description": "b"}]}
    ext = {"metrics": [{"name": "new_m", "description": "n"}]}
    result = _merge_rubrics(base, ext)
    names = [m["name"] for m in result["metrics"]]
    assert names == ["base_m", "new_m"]


def test_merge_rubrics_top_level_keys_overlaid():
    """Non-metrics top-level keys from extension override base."""
    base = {"version": 1, "author": "base", "metrics": []}
    ext = {"version": 2, "metrics": []}
    result = _merge_rubrics(base, ext)
    assert result["version"] == 2
    assert result["author"] == "base"  # base key kept when extension doesn't supply it


def test_merge_rubrics_empty_extension_is_noop():
    """Empty extension leaves base metrics and keys unchanged."""
    base = {"metrics": [{"name": "m1", "description": "d1"}], "foo": "bar"}
    result = _merge_rubrics(base, {})
    assert result["metrics"] == [{"name": "m1", "description": "d1"}]
    assert result["foo"] == "bar"


# --------------------------------------------------------------------------- #
# assemble_system_prompt                                                       #
# --------------------------------------------------------------------------- #

def test_assemble_system_prompt_joins_with_separator(tmp_path):
    """assemble_system_prompt = scaffold + '\\n\\n---\\n\\n' + fragment."""
    scaffold = "SCAFFOLD TEXT"
    fragment = "FRAGMENT TEXT"
    # Build a minimal DomainPack inline (no disk I/O needed)
    pack = DomainPack(
        domain="d",
        display_name="D",
        dispatcher_voice_id="v",
        roles=[RoleSpec(role_id="r", display_name="R")],
        routing_policy=fragment,
    )
    result = assemble_system_prompt(scaffold, pack)
    assert result == "SCAFFOLD TEXT\n\n---\n\nFRAGMENT TEXT"


# --------------------------------------------------------------------------- #
# load_scaffold                                                                 #
# --------------------------------------------------------------------------- #

def test_load_scaffold_raises_when_file_missing(tmp_path):
    """Raises ValueError when routing_scaffold.md doesn't exist."""
    empty_prompts = tmp_path / "empty_prompts"
    empty_prompts.mkdir()
    with pytest.raises(ValueError, match="Scaffold file not found"):
        load_scaffold(prompts_dir=str(empty_prompts))


def test_load_scaffold_returns_text(tmp_path):
    """load_scaffold returns the exact text content of the scaffold file."""
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    content = "# My scaffold\nLine two."
    (prompts_dir / "routing_scaffold.md").write_text(content, encoding="utf-8")
    result = load_scaffold(prompts_dir=str(prompts_dir))
    assert result == content
