# ICRISAT Data Hub

A lightweight, federation-first data hub for ICRISAT teams, modelled on the
CGIAR Climate Data Hub asset-mapping approach:

```
teams drop files  ->  inbox (watched)  ->  ingest pipeline  ->  catalogue  ->  MCP server  ->  any LLM
   (no skills needed)                    (auto: read, profile,              (SQLite + assets.json)
                                         register, flag gaps)
```

Phase 1 supports **CSV, Excel (.xlsx/.xls), and Word (.docx)** files.
External data is registered as **YAML pointers** (federation: the hub points
to data where it lives rather than copying it).

## For teams: how to share data (30 seconds, no skills needed)

**Option A - drop folder.** Copy your file into `data/inbox/` (this can be a
shared network drive or a OneDrive-synced folder). That's it. The pipeline
picks it up automatically, catalogues it, and flags anything it couldn't
figure out for review.

**Option B - web page.** Run `scripts\start_web.cmd` once, open
http://localhost:8010, drag & drop, optionally fill 5 small fields.

**Option C - with metadata.** Drop a tiny sidecar next to your file:

```
yield_trials_2025.csv
yield_trials_2025.meta.yaml     # title, team, owner, description, tags, ...
```

or one `metadata.yaml` for a batch of files. Incomplete uploads get a
pre-filled `*_metadata_review.yaml` written next to the processed file -
complete it and drop it back in the inbox (or edit via the review CLI).

## Setup

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Running the pieces

| Component | Command | Purpose |
|---|---|---|
| Inbox watcher | `scripts\start_watcher.cmd` or `python -m hub.watcher` | auto-ingest dropped files |
| One-off scan | `python -m hub.ingest.pipeline --scan` | scheduled/manual ingest |
| Web upload + catalogue | `scripts\start_web.cmd` or `python -m hub.web.app` | http://localhost:8010 |
| MCP server (stdio) | `python -m hub.mcp_server.server` | local LLM agents |
| MCP server (HTTP) | `python -m hub.mcp_server.server --http` | http://localhost:8100/mcp for other machines |
| Validate YAML pointers | `python -m hub.sources` | check `sources/*.yaml` |
| Export Pages app data | `python -m hub.pages` | regenerate `docs/data/*.json` |

All `python -m hub...` commands run from the project root with
`PYTHONPATH=src` (the scripts set this for you).

## Connecting an LLM (MCP)

**Claude Desktop / opencode (stdio)** - add to your MCP config:

```json
{
  "mcpServers": {
    "icrisat-data-hub": {
      "command": "C:\\Users\\HDesalegn\\ICRISAT\\icrisat-data-hub\\.venv\\Scripts\\python.exe",
      "args": ["-m", "hub.mcp_server.server"],
      "env": { "PYTHONPATH": "C:\\Users\\HDesalegn\\ICRISAT\\icrisat-data-hub\\src" }
    }
  }
}
```

**Other machines/LLMs (HTTP)** - start with `--http` and point clients at
`http://<host>:8100/mcp`.

### MCP tools

| Tool | What it does |
|---|---|
| `list_assets` | List catalogue, filter by status/team/type/tag |
| `search_assets` | Keyword search over titles, descriptions, column names, document text |
| `get_asset` | Full metadata + per-sheet schema (columns, dtypes, samples) |
| `preview_asset` | First N rows of a CSV/Excel asset |
| `query_asset` | Read-only SQL (DuckDB) over a CSV/Excel asset |
| `read_document` | Extracted text of a Word asset |
| `list_sources` | Federated external sources (YAML pointers) |
| `get_catalog_summary` | Portfolio stats (counts by status/type/team/access) |

## YAML source pointers (federation)

Each external dataset/API/database gets one small YAML in `sources/` - see
`sources/README.md` and `sources/examples/`. The hub registers and exposes
these through the MCP server without copying the data.

## Deploy online (shareable upload link)

The repo is deploy-from-GitHub ready (Dockerfile included) — no local tooling
needed. Recommended host: **Railway** (persistent volumes, permanent domain,
~$5/mo hobby plan covers app + 1 GB volume).

1. **railway.app** -> log in with GitHub -> **New Project** -> **Deploy from
   GitHub repo** -> select `icrisat-data-hub` (Dockerfile is auto-detected).
2. **Add a volume**: service -> Settings -> Volumes -> mount at `/data` (1 GB).
   Raw uploads and the SQLite catalogue live there - internal data never
   touches GitHub.
3. **Set variables** (service -> Variables):
   - `HUB_UPLOAD_TOKEN` = a shared secret - the upload link carries it as
     `?token=...`
   - `GITHUB_TOKEN` = fine-grained PAT, *contents: read/write* on this repo
     only (github.com -> Settings -> Developer settings -> Fine-grained tokens)
   - `GITHUB_REPO` = `HenokD11/icrisat-data-hub`
   - `GITHUB_BRANCH` = `main` (or the branch Pages builds from)
4. **Generate the public domain**: Settings -> Networking -> Generate Domain.
5. Share `https://<your-domain>/?token=<HUB_UPLOAD_TOKEN>` with teams.

After every upload the host pushes only the catalogue JSON (never raw files)
to the repo, so the Pages dashboard refreshes automatically. Without
`GITHUB_TOKEN` the sync is a no-op (local dev mode).

## GitHub Pages catalogue app

A static dashboard lives in `docs/` and is served at
https://henokd11.github.io/icrisat-data-hub/. It reads `docs/data/*.json`.
A GitHub Action regenerates them automatically whenever the catalogue or the
YAML pointers change on `main`; to refresh by hand:

```
set PYTHONPATH=src
.venv\Scripts\python -m hub.pages
git add docs/data && git commit -m "Refresh catalogue app data" && git push
```

## Layout

```
config/hub.yaml          hub-wide settings (paths, vocab, ports)
data/inbox/              drop zone (watched)
data/processed/          ingested files, organised by team/month
data/failed/             rejected files + .error.txt
data/catalog/            catalog.db (SQLite) + assets.json (exported catalogue)
docs/                    GitHub Pages app (dashboard + data/*.json)
sources/                 YAML pointers to federated external data
src/hub/                 pipeline, watcher, web app, MCP server, pages exporter
tests/                   pytest suite
```
