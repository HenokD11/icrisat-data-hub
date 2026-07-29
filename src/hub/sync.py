"""Push the published catalogue JSON to GitHub after uploads (deployed mode).

Used when the hub runs on a cloud host: uploads land on the host's private
volume, and only the *metadata* JSON (assets/sources/summary — never raw
files) is pushed back to the repo so the GitHub Pages dashboard stays fresh.

Env vars (set on the host):
    GITHUB_TOKEN   fine-grained PAT, contents read/write on this repo only
    GITHUB_REPO    e.g. HenokD11/icrisat-data-hub
    GITHUB_BRANCH  branch Pages builds from (default: main)

No GITHUB_TOKEN -> sync is a silent no-op (local development mode).
Debounced so a batch of uploads produces at most one push per minute.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import threading
import time
from pathlib import Path
from urllib import request as urlrequest

log = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 60
PUSHED_FILES = (
    "data/catalog/assets.json",
    "docs/data/assets.json",
    "docs/data/sources.json",
    "docs/data/summary.json",
)

_lock = threading.Lock()
_last_push = 0.0


def sync_catalogue_to_github(force: bool = False) -> bool:
    """Export pages data and push the JSON files to GitHub. Best-effort."""
    global _last_push
    token = os.environ.get("GITHUB_TOKEN", "")
    repo = os.environ.get("GITHUB_REPO", "")
    if not token or not repo:
        return False
    with _lock:
        if not force and (time.time() - _last_push) < DEBOUNCE_SECONDS:
            return False
        _last_push = time.time()
    try:
        from .config import load_config
        from .pages import export_pages

        cfg = load_config()
        export_pages()  # regenerate docs/data/*.json from the host catalogue
        branch = os.environ.get("GITHUB_BRANCH", "main")
        for rel in PUSHED_FILES:
            _put_file(cfg.root / rel, rel, repo, branch, token)
        log.info("catalogue JSON pushed to %s@%s", repo, branch)
        return True
    except Exception:
        log.exception("catalogue sync failed (catalogue itself is fine)")
        return False


def sync_in_background(force: bool = False) -> None:
    threading.Thread(target=sync_catalogue_to_github, kwargs={"force": force}, daemon=True).start()


def _put_file(path: Path, rel: str, repo: str, branch: str, token: str) -> None:
    api = f"https://api.github.com/repos/{repo}/contents/{rel}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    sha = None
    try:
        with urlrequest.urlopen(
            urlrequest.Request(f"{api}?ref={branch}", headers=headers), timeout=30
        ) as r:
            sha = json.load(r).get("sha")
    except Exception:
        pass  # file does not exist yet on that branch
    body = {
        "message": "chore: sync catalogue from deployed hub [skip ci]",
        "content": base64.b64encode(path.read_bytes()).decode(),
        "branch": branch,
    }
    if sha:
        body["sha"] = sha
    req = urlrequest.Request(
        api, data=json.dumps(body).encode(), headers=headers, method="PUT"
    )
    with urlrequest.urlopen(req, timeout=30) as r:
        r.read()
