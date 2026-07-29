"""Export catalogue + sources as static JSON for the GitHub Pages app.

Run:  python -m hub.pages

Writes:
    docs/data/assets.json   — full catalogue (same payload as data/catalog/assets.json)
    docs/data/sources.json  — federated source pointers from sources/*.yaml
    docs/data/summary.json  — portfolio statistics

Commit the regenerated docs/data/*.json to update the published dashboard.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .catalog import Catalog
from .config import load_config
from .sources import load_sources


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def export_pages() -> dict[str, Path]:
    cfg = load_config()
    docs_data = cfg.root / "docs" / "data"
    docs_data.mkdir(parents=True, exist_ok=True)

    cat = Catalog(cfg)
    assets = [
        cat.get_asset(a["asset_id"], with_tables=True)
        for a in cat.list_assets(limit=100000)
    ]

    assets_payload = {
        "hub": cfg.hub_name,
        "generated_at": _now(),
        "n_assets": len(assets),
        "assets": assets,
    }
    assets_path = docs_data / "assets.json"
    assets_path.write_text(
        json.dumps(assets_payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    pointers, problems = load_sources(cfg)
    sources_payload = {
        "generated_at": _now(),
        "n_sources": len(pointers),
        "sources": [p.to_dict() for p in pointers],
        "validation_problems": problems,
    }
    sources_path = docs_data / "sources.json"
    sources_path.write_text(
        json.dumps(sources_payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    summary_path = docs_data / "summary.json"
    summary_path.write_text(
        json.dumps(cat.summary(), indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return {"assets": assets_path, "sources": sources_path, "summary": summary_path}


def main() -> int:
    paths = export_pages()
    for kind, path in paths.items():
        print(f"{kind:8s} -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
