"""Hub configuration loading.

Everything resolves from the project root (the folder that contains
``config/hub.yaml``) so the pipeline, watcher, web app, and MCP server all
agree on where data lives regardless of the current working directory.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

# src/hub/config.py -> project root is three levels up
PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = PROJECT_ROOT / "config" / "hub.yaml"


class HubConfig:
    def __init__(self, raw: dict[str, Any], root: Path):
        self._raw = raw
        self.root = root

    # -- generic access ----------------------------------------------------
    def get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self._raw
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node

    # -- typed helpers -----------------------------------------------------
    def path(self, name: str) -> Path:
        """Resolve a path defined under ``paths:`` against the project root.

        When deployed (container), setting HUB_DATA_DIR remaps the local
        working-state paths (data/...) onto the mounted volume so uploads and
        the SQLite catalogue survive redeploys; repo content (config, sources)
        stays in the image.
        """
        value = self.get("paths", name)
        if value is None:
            raise KeyError(f"paths.{name} not defined in {CONFIG_PATH}")
        data_dir = os.environ.get("HUB_DATA_DIR")
        if data_dir and value.startswith("data/"):
            return Path(data_dir) / Path(value).relative_to("data")
        p = Path(value)
        if not p.is_absolute():
            p = self.root / p
        return p

    @property
    def hub_name(self) -> str:
        return self.get("hub", "name", default="ICRISAT Data Hub")

    @property
    def supported_extensions(self) -> set[str]:
        return {e.lower() for e in self.get("ingest", "supported_extensions", default=[])}

    def ensure_dirs(self) -> None:
        for name in ("inbox", "processed", "failed", "staging"):
            if self.get("paths", name):
                self.path(name).mkdir(parents=True, exist_ok=True)
        self.path("catalog_db").parent.mkdir(parents=True, exist_ok=True)
        self.path("catalog_json").parent.mkdir(parents=True, exist_ok=True)
        self.path("sources_dir").mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def load_config(path: str | os.PathLike[str] | None = None) -> HubConfig:
    # HUB_CONFIG points a deployment (or the e2e test) at its own hub.yaml;
    # the project root is then the folder containing that config/ dir.
    cfg_path = Path(path or os.environ.get("HUB_CONFIG") or CONFIG_PATH)
    with open(cfg_path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}
    cfg = HubConfig(raw, cfg_path.resolve().parents[1])
    cfg.ensure_dirs()
    return cfg
