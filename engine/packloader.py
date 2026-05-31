"""Domain-pack loader — domain-agnostic.

Reads a domain pack from domains/<domain>/pack.yaml and returns a fully-
populated DomainPack value object.  No domain literals may appear in this
file.  See engine/interfaces.py for the frozen contracts.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from engine.interfaces import DomainPack, RoleSpec


# --------------------------------------------------------------------------- #
# Public API                                                                   #
# --------------------------------------------------------------------------- #

def load_pack(
    domain: str,
    *,
    domains_dir: str = "domains",
    prompts_dir: str = "prompts",
) -> DomainPack:
    """Read domains/<domain>/pack.yaml, resolve and READ the convener_fragment
    file (its .md text becomes DomainPack.convener_fragment), load rubric.yaml
    and scenarios.yaml.

    Validation:
    - domain directory must exist.
    - pack.yaml must contain: domain, display_name, dispatcher_voice_id, and a
      non-empty roles list where each role has role_id + display_name.

    Raises ValueError with a clear message on any validation failure.
    Returns a fully-populated DomainPack (with .path set to the domain dir).
    scenarios.yaml top-level key is `scenarios:` (a list) — stored in
    DomainPack.scenarios.  rubric.yaml content is stored in DomainPack.rubric.
    """
    domain_dir = Path(domains_dir) / domain
    if not domain_dir.is_dir():
        raise ValueError(
            f"Domain directory not found: '{domain_dir}'. "
            f"Expected a directory at {domain_dir.resolve()}."
        )

    # --- pack.yaml --------------------------------------------------------- #
    pack_path = domain_dir / "pack.yaml"
    if not pack_path.is_file():
        raise ValueError(
            f"Missing pack.yaml in domain directory: '{domain_dir}'. "
            f"Expected file at {pack_path.resolve()}."
        )

    raw: dict[str, Any] = _load_yaml(pack_path)

    # Required top-level fields
    for required_field in ("domain", "display_name", "dispatcher_voice_id", "roles"):
        if required_field not in raw:
            raise ValueError(
                f"pack.yaml for domain '{domain}' is missing required field: "
                f"'{required_field}'."
            )

    if not isinstance(raw["roles"], list) or len(raw["roles"]) == 0:
        raise ValueError(
            f"pack.yaml for domain '{domain}': 'roles' must be a non-empty list."
        )

    roles: list[RoleSpec] = []
    for i, role_raw in enumerate(raw["roles"]):
        if not isinstance(role_raw, dict):
            raise ValueError(
                f"pack.yaml for domain '{domain}': roles[{i}] must be a mapping."
            )
        for req in ("role_id", "display_name"):
            if req not in role_raw:
                raise ValueError(
                    f"pack.yaml for domain '{domain}': roles[{i}] is missing "
                    f"required field '{req}'."
                )
        roles.append(
            RoleSpec(
                role_id=role_raw["role_id"],
                display_name=role_raw["display_name"],
                voice_id=role_raw.get("voice_id", ""),
                zone_id=role_raw.get("zone_id", "z0"),
            )
        )

    # --- convener fragment (.md) ------------------------------------------ #
    fragment_ref: str = raw.get("convener_fragment", "convener.md")
    fragment_path = domain_dir / fragment_ref
    if not fragment_path.is_file():
        raise ValueError(
            f"Domain '{domain}': convener fragment file not found: "
            f"'{fragment_path.resolve()}'. "
            f"(pack.yaml convener_fragment = '{fragment_ref}')"
        )
    convener_fragment: str = fragment_path.read_text(encoding="utf-8")

    # --- rubric.yaml (base + optional domain extension) -------------------- #
    # Every domain inherits the universal BASE RUBRIC (prompts/base_rubric.yaml).
    # The domain's own rubric.yaml is OPTIONAL and ADDITIVE: it may add domain-
    # specific metrics, and may override a base metric by reusing its `name`.
    base_rubric = _load_base_rubric(prompts_dir)
    rubric_ref: str = raw.get("rubric", "rubric.yaml")
    rubric_path = domain_dir / rubric_ref
    domain_rubric: dict = {}
    if rubric_path.is_file():
        domain_rubric = _load_yaml(rubric_path) or {}
    rubric: dict = _merge_rubrics(base_rubric, domain_rubric)

    # --- scenarios.yaml ----------------------------------------------------- #
    scenarios_ref: str = raw.get("scenarios", "scenarios.yaml")
    scenarios_path = domain_dir / scenarios_ref
    scenarios: list[dict] = []
    if scenarios_path.is_file():
        scenarios_raw = _load_yaml(scenarios_path) or {}
        scenarios = scenarios_raw.get("scenarios", [])
    else:
        raise ValueError(
            f"Domain '{domain}': scenarios file not found: "
            f"'{scenarios_path.resolve()}'. "
            f"(pack.yaml scenarios = '{scenarios_ref}')"
        )

    return DomainPack(
        domain=raw["domain"],
        display_name=raw["display_name"],
        dispatcher_voice_id=raw["dispatcher_voice_id"],
        roles=roles,
        convener_fragment=convener_fragment,
        rubric=rubric,
        scenarios=scenarios,
        path=str(domain_dir.resolve()),
    )


def load_scaffold(prompts_dir: str = "prompts") -> str:
    """Read and return prompts/convener_scaffold.md as text."""
    scaffold_path = Path(prompts_dir) / "convener_scaffold.md"
    if not scaffold_path.is_file():
        raise ValueError(
            f"Scaffold file not found: '{scaffold_path.resolve()}'. "
            f"Expected prompts/convener_scaffold.md relative to the working directory."
        )
    return scaffold_path.read_text(encoding="utf-8")


def assemble_system_prompt(scaffold: str, pack: DomainPack) -> str:
    """Return scaffold text + a separator + pack.convener_fragment.

    This is the convener's full system prompt. Generic only — no domain
    literals appear here; all domain knowledge is inside pack.convener_fragment.
    """
    separator = "\n\n---\n\n"
    return scaffold + separator + pack.convener_fragment


# --------------------------------------------------------------------------- #
# Internal helpers                                                              #
# --------------------------------------------------------------------------- #

def _load_base_rubric(prompts_dir: str) -> dict:
    """Load the universal base rubric from prompts/base_rubric.yaml.

    The base rubric defines shared evaluation metrics that every domain inherits.
    Returns an empty rubric ({"metrics": []}) if the file is absent — the merge
    is then a no-op over the domain's own rubric, so behaviour degrades gracefully.
    """
    base_path = Path(prompts_dir) / "base_rubric.yaml"
    if not base_path.is_file():
        return {"metrics": []}
    return _load_yaml(base_path) or {"metrics": []}


def _merge_rubrics(base: dict, extension: dict) -> dict:
    """Merge a base rubric with a domain extension rubric.

    Metric merge is by `name`: an extension metric with the same name OVERRIDES
    the base metric (in place, preserving order); a new name is APPENDED. All
    other top-level keys from base are kept and overlaid by the extension.
    Domain-agnostic — no metric names are referenced here, only the `name` key.
    """
    base = base or {}
    extension = extension or {}

    merged: dict = {k: v for k, v in base.items() if k != "metrics"}
    for k, v in extension.items():
        if k != "metrics":
            merged[k] = v

    base_metrics = [m for m in (base.get("metrics") or []) if isinstance(m, dict)]
    ext_metrics = [m for m in (extension.get("metrics") or []) if isinstance(m, dict)]

    result: list[dict] = [dict(m) for m in base_metrics]
    index = {m.get("name"): i for i, m in enumerate(result) if m.get("name")}
    for m in ext_metrics:
        name = m.get("name")
        if name and name in index:
            result[index[name]] = dict(m)  # override base metric in place
        else:
            if name:
                index[name] = len(result)
            result.append(dict(m))  # new domain-specific metric

    merged["metrics"] = result
    return merged


def _load_yaml(path: Path) -> Any:
    """Load a YAML file and return parsed content. Raises ValueError on parse error."""
    try:
        with path.open(encoding="utf-8") as fh:
            return yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise ValueError(f"Failed to parse YAML file '{path}': {exc}") from exc
