"""Metadata handling: sidecar files, inference, and review templates.

Teams can drop a file *with no extra work at all* — the pipeline infers what
it can and flags the asset ``needs_review``. If they want full metadata in
one step, they drop a tiny sidecar file next to the data file:

    yield_trials_2025.csv
    yield_trials_2025.meta.yaml      <- same stem + .meta.yaml

or one batch file covering several uploads:

    metadata.yaml                    <- {file_name: {field: value, ...}, ...}

Incomplete assets get a pre-filled ``metadata_review.yaml`` written next to
the processed file so completing metadata is editing one small file — no
database, no special tooling.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

SIDECAR_SUFFIX = ".meta.yaml"
BATCH_SIDECAR = "metadata.yaml"

# Fields a sidecar / review template may carry.
METADATA_FIELDS = (
    "title",
    "description",
    "team",
    "owner",
    "contact",
    "license",
    "access",
    "domain",
    "hub_role",
    "spatial_coverage",
    "temporal_coverage",
    "tags",
)


def _clean_meta(raw: dict[str, Any]) -> dict[str, Any]:
    meta = {}
    for field in METADATA_FIELDS:
        value = raw.get(field)
        if value in (None, "", "TODO", "..."):
            continue
        if field == "tags" and isinstance(value, str):
            value = [t.strip() for t in value.split(",") if t.strip()]
        meta[field] = value
    return meta


def load_sidecar_metadata(file_path: Path) -> dict[str, Any]:
    """Merge per-file sidecar and batch ``metadata.yaml`` if present."""
    meta: dict[str, Any] = {}

    batch = file_path.parent / BATCH_SIDECAR
    if batch.exists() and batch.name != f"{file_path.stem}{SIDECAR_SUFFIX}":
        try:
            raw = yaml.safe_load(batch.read_text(encoding="utf-8")) or {}
            entry = raw.get(file_path.name) or {}
            if isinstance(entry, dict):
                meta.update(_clean_meta(entry))
        except yaml.YAMLError:
            pass

    sidecar = file_path.with_name(f"{file_path.stem}{SIDECAR_SUFFIX}")
    if sidecar.exists():
        try:
            raw = yaml.safe_load(sidecar.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                meta.update(_clean_meta(raw))
        except yaml.YAMLError:
            pass

    return meta


def infer_metadata(file_path: Path, tables: list[dict[str, Any]]) -> dict[str, Any]:
    """Best-effort inference from the file itself — no team input needed."""
    title = re.sub(r"[_\-]+", " ", file_path.stem).strip().title()

    inferred: dict[str, Any] = {"title": title}

    # Use Word headings / first lines as a draft description.
    for t in tables:
        if t["kind"] == "document" and t.get("text"):
            first_lines = [ln for ln in t["text"].splitlines() if ln.strip()][:3]
            if first_lines:
                inferred["description"] = " ".join(first_lines)[:500]
            break
    else:
        # Tabular: summarise shape and columns.
        if tables:
            t0 = tables[0]
            frame = t0.get("frame")
            if frame is not None:
                cols = [str(c) for c in frame.columns[:12]]
                inferred["description"] = (
                    f"Tabular dataset '{file_path.name}' with {frame.shape[0]} rows "
                    f"and {frame.shape[1]} columns: {', '.join(cols)}"
                    + ("..." if frame.shape[1] > 12 else "")
                )
    return inferred


def write_review_template(
    asset: dict[str, Any], directory: Path
) -> Path:
    """Write a pre-filled metadata_review.yaml for a needs_review asset."""
    template: dict[str, Any] = {
        "# asset_id": asset["asset_id"],
        "# instructions": (
            "Fill in the fields below, then either (a) drop this file back into "
            "the inbox folder, or (b) run: python -m hub.ingest.pipeline --review <this file>"
        ),
        "asset_id": asset["asset_id"],
    }
    for field in METADATA_FIELDS:
        template[field] = asset.get(field) or "TODO"
    if template["tags"] in (None, "TODO", []):
        template["tags"] = ["TODO"]

    out = directory / f"{asset['asset_id']}_metadata_review.yaml"
    out.write_text(
        yaml.safe_dump(template, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    return out


def load_review_template(path: Path) -> dict[str, Any]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {k: v for k, v in raw.items() if not k.startswith("#")}
