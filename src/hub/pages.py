"""Export catalogue + sources as static JSON for the GitHub Pages app.

Run:  python -m hub.pages

Writes:
    docs/data/assets.json   — PUBLISHED + OPEN datasets only (the site is public)
    docs/data/sources.json  — federated source pointers from sources/*.yaml
    docs/data/summary.json  — portfolio statistics

Locally, assets come from the SQLite catalogue. In CI (no catalog.db — it is
gitignored local state) the last exported JSON is re-filtered instead. GitHub Actions runs this on every catalogue/sources change on main
(see .github/workflows/refresh-pages-data.yml); to refresh by hand, run this
module and commit docs/data/*.json.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from .access import effective_access
from .catalog import Catalog
from .config import load_config
from .sources import load_sources


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _count_by(assets: list[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for a in assets:
        k = a.get(key) or "unspecified"
        counts[k] = counts.get(k, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


def _summary_from_assets(assets: list[dict]) -> dict:
    """Summary statistics computed from a plain asset list (CI fallback)."""
    return {
        "total_assets": len(assets),
        "by_status": _count_by(assets, "status"),
        "by_file_type": _count_by(assets, "file_type"),
        "by_team": _count_by(assets, "team"),
        "by_access": _count_by(assets, "access"),
        "needs_review": sum(1 for a in assets if a.get("status") == "needs_review"),
    }


PRIVATE_KEYS = ("file_path", "sha256", "ingest_log", "owner_email")


def public_assets(assets: list[dict]) -> list[dict]:
    """The public site only carries reviewed, open datasets — nothing else."""
    out = []
    for a in assets:
        if a.get("status") != "published" or effective_access(a) != "open":
            continue
        out.append({k: v for k, v in a.items() if k not in PRIVATE_KEYS})
    return out


def _load_assets(cfg) -> list[dict]:
    """All assets from the SQLite catalogue, else the last exported JSON.

    In CI (GitHub Actions) the DB does not exist — it is local working state —
    so fall back to data/catalog/assets.json if present, else the already
    published docs/data/assets.json (re-filtering is idempotent).
    """
    if cfg.path("catalog_db").exists():
        cat = Catalog(cfg)
        return [
            cat.get_asset(a["asset_id"], with_tables=True)
            for a in cat.list_assets(limit=100000)
        ]
    for path in (cfg.path("catalog_json"), cfg.root / "docs" / "data" / "assets.json"):
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8")).get("assets", [])
    return []


def export_pages() -> dict[str, Path]:
    cfg = load_config()
    docs_data = cfg.root / "docs" / "data"
    docs_data.mkdir(parents=True, exist_ok=True)

    assets = public_assets(_load_assets(cfg))
    summary = _summary_from_assets(assets)

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

    summary["hub"] = cfg.hub_name
    summary["generated_at"] = assets_payload["generated_at"]
    summary["domain_definitions"] = cfg.get("vocabulary", "domain_definitions", default={})
    # Where "Upload" / "Open in the data hub" links point on the public site.
    summary["app_url"] = os.environ.get("HUB_BASE_URL") or cfg.get("web", "app_url", default="")

    summary_path = docs_data / "summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    return {"assets": assets_path, "sources": sources_path, "summary": summary_path}


def main() -> int:
    paths = export_pages()
    for kind, path in paths.items():
        print(f"{kind:8s} -> {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
