"""YAML source pointers — federation-first registration of external data.

Each YAML file under ``sources/`` describes a dataset that lives *somewhere
else* (an API, a remote file, a database, a shared drive). The hub does not
copy these; it registers a pointer so the catalogue and the MCP server can
direct LLMs/users to the source — the same federation model as the CDH.

Schema (see sources/examples/ for complete samples):

    id: chirps-rainfall-africa          # required, unique
    title: CHIRPS daily rainfall        # required
    description: ...
    type: remote_dataset | api | database | file_share   # required
    format: geotiff | csv | json | parquet | ...
    url: https:// ...                   # or connection string / UNC path
    access: open | internal | restricted
    update_frequency: daily | weekly | monthly | annual | static
    owner: {team: ..., contact: ...}
    license: ...
    hub_role: federation | ingest | derived | reference
    tags: [...]
    spatial_coverage: ...
    temporal_coverage: ...
    auth_env_var: NAME_OF_ENV_VAR       # optional — never put secrets in YAML
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config import HubConfig, load_config

REQUIRED = ("id", "title", "type", "url")
VALID_TYPES = {"remote_dataset", "api", "database", "file_share"}


@dataclass
class SourcePointer:
    id: str
    title: str
    type: str
    url: str
    description: str | None = None
    format: str | None = None
    access: str = "open"
    update_frequency: str | None = None
    owner: dict[str, Any] = field(default_factory=dict)
    license: str | None = None
    hub_role: str = "federation"
    tags: list[str] = field(default_factory=list)
    spatial_coverage: str | None = None
    temporal_coverage: str | None = None
    auth_env_var: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)
    source_file: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v not in (None, [], {})}


def validate_pointer(raw: dict[str, Any], file: Path) -> list[str]:
    problems = []
    for key in REQUIRED:
        if not raw.get(key):
            problems.append(f"{file.name}: missing required field '{key}'")
    if raw.get("type") and raw["type"] not in VALID_TYPES:
        problems.append(
            f"{file.name}: type must be one of {sorted(VALID_TYPES)}, got '{raw['type']}'"
        )
    return problems


def load_sources(
    config: HubConfig | None = None,
) -> tuple[list[SourcePointer], list[str]]:
    """Load every YAML pointer in sources/ (recursively).

    Returns (pointers, validation_problems). Invalid files are skipped, not fatal.
    """
    cfg = config or load_config()
    root = cfg.path("sources_dir")
    pointers: list[SourcePointer] = []
    problems: list[str] = []
    seen_ids: set[str] = set()

    for file in sorted(root.rglob("*.yaml")) + sorted(root.rglob("*.yml")):
        try:
            raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            problems.append(f"{file.name}: YAML parse error: {exc}")
            continue
        if not isinstance(raw, dict):
            problems.append(f"{file.name}: top level must be a mapping")
            continue

        issues = validate_pointer(raw, file)
        if issues:
            problems.extend(issues)
            continue
        if raw["id"] in seen_ids:
            problems.append(f"{file.name}: duplicate id '{raw['id']}'")
            continue
        seen_ids.add(raw["id"])

        known = {f for f in SourcePointer.__dataclass_fields__} - {"extra", "source_file"}
        pointers.append(
            SourcePointer(
                **{k: v for k, v in raw.items() if k in known},
                extra={k: v for k, v in raw.items() if k not in known},
                source_file=str(file.relative_to(cfg.root)),
            )
        )
    return pointers, problems


if __name__ == "__main__":
    ptrs, probs = load_sources()
    for p in ptrs:
        print(f"OK   {p.id}  ({p.type}, {p.access})  <- {p.source_file}")
    for prob in probs:
        print(f"BAD  {prob}")
    print(f"\n{len(ptrs)} pointer(s), {len(probs)} problem(s)")
    raise SystemExit(1 if probs else 0)
