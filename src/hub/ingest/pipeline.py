"""Ingest pipeline: inbox -> read -> profile -> catalogue -> processed/.

Can be driven three ways:

* the watcher (``python -m hub.watcher``) picks files up as they are dropped;
* a manual/scheduled scan: ``python -m hub.ingest.pipeline --scan``;
* applying a completed metadata review template:
  ``python -m hub.ingest.pipeline --review path/to/asset_xxx_metadata_review.yaml``
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ..catalog import Catalog
from ..config import HubConfig, load_config
from . import metadata as meta
from .profiler import profile_tables
from .readers import detect_file_type, read_file

IGNORED_NAMES = {meta.BATCH_SIDECAR, "thumbs.db", "desktop.ini"}


def sha256_of(path: Path, chunk_size: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _wait_for_settle(path: Path, settle_seconds: float) -> bool:
    """Wait until the file size stops changing (copy finished)."""
    try:
        last = path.stat().st_size
    except FileNotFoundError:
        return False
    deadline = time.time() + max(settle_seconds, 0) + 30
    while time.time() < deadline:
        time.sleep(min(settle_seconds, 1.0))
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return False
        if size == last:
            return True
        last = size
    return True


def ingest_file(
    file_path: Path,
    config: HubConfig | None = None,
    catalog: Catalog | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Ingest one file from the inbox. Returns a result dict."""
    cfg = config or load_config()
    cat = catalog or Catalog(cfg)
    file_path = Path(file_path)

    result: dict[str, Any] = {"file": file_path.name, "ok": False}

    if file_path.name.lower() in IGNORED_NAMES:
        result["reason"] = "ignored"
        return result

    ftype = detect_file_type(file_path)
    if ftype is None:
        result["reason"] = f"unsupported type {file_path.suffix}"
        _quarantine(file_path, cfg, result["reason"])
        return result

    _wait_for_settle(file_path, cfg.get("ingest", "settle_seconds", default=2.0))

    digest = sha256_of(file_path)
    if cat.has_hash(digest):
        result["reason"] = "duplicate (already catalogued)"
        file_path.unlink(missing_ok=True)
        return result

    try:
        tables = read_file(file_path)
    except Exception as exc:  # unreadable file -> quarantine with reason
        result["reason"] = f"read error: {exc}"
        _quarantine(file_path, cfg, result["reason"])
        return result

    # Metadata: sidecar < web-form metadata < inference (first non-empty wins).
    metadata: dict[str, Any] = meta.infer_metadata(file_path, tables)
    metadata.update(meta.load_sidecar_metadata(file_path))
    if extra_metadata:
        metadata.update({k: v for k, v in extra_metadata.items() if v})

    profiled = profile_tables(tables, cfg)

    team = metadata.get("team") or "unknown"
    dest_dir = (
        cfg.path("processed")
        / _safe_dirname(str(team))
        / datetime.now().strftime("%Y-%m")
    )
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / file_path.name
    if cfg.get("ingest", "after_ingest", default="move") == "move":
        shutil.move(str(file_path), dest)
    else:
        shutil.copy2(file_path, dest)

    asset_record = {
        **metadata,
        "file_name": file_path.name,
        "file_path": str(dest.relative_to(cfg.root)),
        "file_type": ftype,
        "file_ext": file_path.suffix.lower(),
        "size_bytes": dest.stat().st_size,
        "sha256": digest,
        "ingest_log": f"Ingested {datetime.now().isoformat(timespec='seconds')}",
    }
    asset_id = cat.add_asset(asset_record, profiled)

    asset = cat.get_asset(asset_id, with_tables=False)
    if asset and asset["status"] == "needs_review":
        review_path = meta.write_review_template(asset, dest_dir)
        result["review_template"] = str(review_path)

    # Sidecar consumed — tidy up if it exists.
    sidecar = file_path.with_name(f"{file_path.stem}{meta.SIDECAR_SUFFIX}")
    if sidecar.exists():
        shutil.move(str(sidecar), dest_dir / sidecar.name)

    result.update(
        {
            "ok": True,
            "asset_id": asset_id,
            "status": asset["status"] if asset else "unknown",
            "processed_path": str(dest),
        }
    )
    return result


def apply_review(review_file: Path, catalog: Catalog | None = None) -> dict[str, Any]:
    cat = catalog or Catalog()
    values = meta.load_review_template(review_file)
    asset_id = values.pop("asset_id", None)
    if not asset_id:
        return {"ok": False, "reason": "template has no asset_id"}
    cleaned = meta._clean_meta(values)
    ok = cat.update_metadata(asset_id, cleaned)
    return {"ok": ok, "asset_id": asset_id, "applied": sorted(cleaned)}


def scan_inbox(config: HubConfig | None = None, catalog: Catalog | None = None) -> list[dict[str, Any]]:
    cfg = config or load_config()
    cat = catalog or Catalog(cfg)
    inbox = cfg.path("inbox")

    results = []

    # Completed review templates dropped back into the inbox.
    for review in sorted(inbox.glob("*_metadata_review.yaml")):
        res = apply_review(review, cat)
        res["file"] = review.name
        results.append(res)
        review.unlink(missing_ok=True)

    for path in sorted(inbox.rglob("*")):
        if not path.is_file() or path.suffix.lower() in (".yaml", ".yml"):
            continue
        results.append(ingest_file(path, cfg, cat))
    return results


def _quarantine(file_path: Path, cfg: HubConfig, reason: str) -> None:
    failed_dir = cfg.path("failed") / datetime.now().strftime("%Y-%m")
    failed_dir.mkdir(parents=True, exist_ok=True)
    dest = failed_dir / file_path.name
    try:
        shutil.move(str(file_path), dest)
    except shutil.Error:
        dest = failed_dir / f"{file_path.stem}_{int(time.time())}{file_path.suffix}"
        shutil.move(str(file_path), dest)
    (dest.parent / f"{dest.name}.error.txt").write_text(reason, encoding="utf-8")


def _safe_dirname(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_ " else "_" for c in name).strip() or "unknown"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ICRISAT Data Hub ingest pipeline")
    parser.add_argument("--scan", action="store_true", help="scan the inbox now")
    parser.add_argument("--file", type=Path, help="ingest a single file")
    parser.add_argument("--review", type=Path, help="apply a completed metadata review template")
    args = parser.parse_args(argv)

    if args.review:
        res = apply_review(args.review)
        print(res)
        return 0 if res["ok"] else 1

    if args.file:
        res = ingest_file(args.file)
        print(res)
        return 0 if res["ok"] else 1

    # default: scan
    results = scan_inbox()
    ok = sum(1 for r in results if r.get("ok"))
    for r in results:
        print(r)
    print(f"\n{ok}/{len(results)} file(s) ingested")
    return 0


if __name__ == "__main__":
    sys.exit(main())
