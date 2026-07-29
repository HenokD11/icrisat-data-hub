"""Web upload + browse app — the no-special-skills front door.

Run:  python -m hub.web.app      then open http://localhost:8000

* Teams drag & drop CSV / Excel / Word files, optionally fill in a few
  metadata fields, and the pipeline ingests immediately.
* Files uploaded here are written into the inbox *with a generated sidecar*
  and ingested at once — identical to dropping files in the shared folder,
  so there is exactly one ingest path to maintain.
* /assets shows the catalogue; /api/assets gives JSON for other tools.
"""

from __future__ import annotations

import html
import os
from pathlib import Path

from fastapi import FastAPI, File, Form, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from ..catalog import Catalog
from ..config import load_config
from ..ingest import metadata as meta
from ..ingest.pipeline import ingest_file
from ..sync import sync_in_background

# Optional shared-secret gate for when the page is exposed via a public tunnel:
# set HUB_UPLOAD_TOKEN and share the link as https://<host>/?token=<secret>.
# When unset (default local use) no token is required.
UPLOAD_TOKEN = os.environ.get("HUB_UPLOAD_TOKEN", "")

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{hub_name} — Upload</title>
<style>
  body {{ font-family: system-ui, sans-serif; max-width: 760px; margin: 2rem auto; padding: 0 1rem; color: #1a1a1a; }}
  h1 {{ font-size: 1.4rem; }}
  .drop {{ border: 2px dashed #4a7ebb; border-radius: 10px; padding: 2.5rem; text-align: center;
          color: #4a7ebb; background: #f6f9fd; cursor: pointer; }}
  .drop.over {{ background: #e3eefb; }}
  form > label {{ display: block; margin-top: .8rem; font-size: .85rem; font-weight: 600; }}
  input[type=text], textarea {{ width: 100%; padding: .45rem; border: 1px solid #bbb; border-radius: 6px; box-sizing: border-box; }}
  button {{ margin-top: 1.2rem; padding: .6rem 1.6rem; background: #2c5f9e; color: white; border: 0;
           border-radius: 6px; font-size: 1rem; cursor: pointer; }}
  .hint {{ font-size: .8rem; color: #666; }}
  #results {{ margin-top: 1.5rem; }}
  .ok {{ background: #e9f7ef; border: 1px solid #7bc8a4; padding: .6rem; border-radius: 6px; margin-top: .5rem; }}
  .bad {{ background: #fdecea; border: 1px solid #e08a80; padding: .6rem; border-radius: 6px; margin-top: .5rem; }}
  nav {{ margin-bottom: 1rem; font-size: .9rem; }}
</style>
</head>
<body>
<nav><b>{hub_name}</b> &nbsp;·&nbsp; <a href="/">Upload</a> &nbsp;·&nbsp; <a href="/assets">Catalogue</a> &nbsp;·&nbsp; <a href="/api/assets">JSON</a></nav>
<h1>Upload data</h1>
<p class="hint">Drop <b>.csv</b>, <b>.xlsx/.xls</b>, or <b>.docx</b> files. All fields below are optional —
the pipeline auto-infers what's missing and flags the asset for review.
You can also just copy files into the shared inbox folder: <code>{inbox}</code></p>

<div class="drop" id="drop">Drag &amp; drop files here<br>or click to choose</div>
<input type="file" id="picker" multiple accept=".csv,.xlsx,.xls,.docx" hidden>

<form id="meta">
  <label>Team <input type="text" name="team" placeholder="e.g. Soil Intelligence"></label>
  <label>Your name <input type="text" name="owner"></label>
  <label>Contact email <input type="text" name="contact"></label>
  <label>Description <textarea name="description" rows="2" placeholder="What is this data? Where did it come from?"></textarea></label>
  <label>Tags (comma-separated) <input type="text" name="tags" placeholder="e.g. sorghum, yield-trial, ethiopia"></label>
  <button type="submit">Upload</button>
</form>
<div id="results"></div>

<script>
const drop = document.getElementById('drop');
const picker = document.getElementById('picker');
let files = [];
drop.onclick = () => picker.click();
picker.onchange = () => {{ files = [...picker.files]; drop.textContent = files.map(f => f.name).join(', '); }};
drop.ondragover = e => {{ e.preventDefault(); drop.classList.add('over'); }};
drop.ondragleave = () => drop.classList.remove('over');
drop.ondrop = e => {{ e.preventDefault(); drop.classList.remove('over');
  files = [...e.dataTransfer.files]; drop.textContent = files.map(f => f.name).join(', '); }};

const urlToken = new URLSearchParams(location.search).get('token') || '';

document.getElementById('meta').onsubmit = async e => {{
  e.preventDefault();
  if (!files.length) {{ alert('Choose at least one file'); return; }}
  const out = document.getElementById('results');
  out.innerHTML = '';
  const form = new FormData(e.target);
  for (const f of files) {{
    const fd = new FormData();
    fd.append('file', f);
    fd.append('token', urlToken);
    for (const [k, v] of form.entries()) fd.append(k, v);
    const res = await fetch('/upload', {{ method: 'POST', body: fd }});
    const j = await res.json();
    const div = document.createElement('div');
    div.className = j.ok ? 'ok' : 'bad';
    div.textContent = j.ok
      ? `${{f.name}} — ingested (${{j.status}})${{j.status === 'needs_review' ? ', review template: ' + (j.review_template || '') : ''}}`
      : `${{f.name}} — failed: ${{j.reason || res.status}}`;
    out.appendChild(div);
  }}
}};
</script>
</body>
</html>"""


def create_app() -> FastAPI:
    cfg = load_config()
    app = FastAPI(title=cfg.hub_name)

    def catalog() -> Catalog:
        return Catalog(cfg)

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return PAGE.format(hub_name=html.escape(cfg.hub_name), inbox=cfg.path("inbox"))

    @app.post("/upload")
    async def upload(
        file: UploadFile = File(...),
        team: str = Form(""),
        owner: str = Form(""),
        contact: str = Form(""),
        description: str = Form(""),
        tags: str = Form(""),
        token: str = Form(""),
    ):
        if UPLOAD_TOKEN and token != UPLOAD_TOKEN:
            return JSONResponse(
                {"ok": False, "reason": "invalid or missing upload token"},
                status_code=403,
            )
        inbox = cfg.path("inbox")
        safe_name = Path(file.filename or "upload.bin").name
        dest = inbox / safe_name
        stem, ext = dest.stem, dest.suffix
        n = 1
        while dest.exists():
            dest = inbox / f"{stem}_{n}{ext}"
            n += 1
        dest.write_bytes(await file.read())

        sidecar_meta = {
            k: v for k, v in {
                "team": team or None,
                "owner": owner or None,
                "contact": contact or None,
                "description": description or None,
                "tags": [t.strip() for t in tags.split(",") if t.strip()] or None,
            }.items() if v
        }
        result = ingest_file(dest, cfg, catalog(), extra_metadata=sidecar_meta)
        if result.get("ok"):
            sync_in_background()  # deployed mode: push catalogue JSON to GitHub
        status = 200 if result.get("ok") else 422
        return JSONResponse(result, status_code=status)

    @app.get("/assets", response_class=HTMLResponse)
    def assets_page(status: str | None = None) -> str:
        assets = catalog().list_assets(status=status, limit=500)
        rows = "".join(
            "<tr>"
            f"<td><code>{a['asset_id']}</code></td>"
            f"<td>{html.escape(a['title'])}</td>"
            f"<td>{html.escape(a.get('team') or '—')}</td>"
            f"<td>{a['file_type']}</td>"
            f"<td>{a['status']}</td>"
            f"<td>{html.escape(a['file_name'])}</td>"
            "</tr>"
            for a in assets
        )
        return f"""<!doctype html><html><head><meta charset="utf-8">
<title>{html.escape(cfg.hub_name)} — Catalogue</title>
<style>body{{font-family:system-ui,sans-serif;max-width:1100px;margin:2rem auto;padding:0 1rem}}
table{{border-collapse:collapse;width:100%;font-size:.85rem}}
td,th{{border:1px solid #ccc;padding:.35rem .5rem;text-align:left}}
tr:nth-child(even){{background:#f6f8fa}}</style></head><body>
<nav><b>{html.escape(cfg.hub_name)}</b> &nbsp;·&nbsp; <a href="/">Upload</a> &nbsp;·&nbsp; <a href="/assets">Catalogue</a></nav>
<h1>Catalogue ({len(assets)} assets)</h1>
<p><a href="/assets">all</a> · <a href="/assets?status=published">published</a> · <a href="/assets?status=needs_review">needs review</a></p>
<table><tr><th>id</th><th>title</th><th>team</th><th>type</th><th>status</th><th>file</th></tr>{rows}</table>
</body></html>"""

    @app.get("/api/assets")
    def assets_json(status: str | None = None):
        return catalog().list_assets(status=status, limit=1000)

    @app.get("/api/summary")
    def summary_json():
        return catalog().summary()

    return app


def main() -> None:
    import uvicorn

    cfg = load_config()
    # Cloud hosts (Railway/Render/Fly) inject PORT; fall back to hub.yaml.
    port = int(os.environ.get("PORT") or cfg.get("web", "port", default=8010))
    uvicorn.run(
        create_app(),
        host=cfg.get("web", "host", default="0.0.0.0"),
        port=port,
    )


if __name__ == "__main__":
    main()
