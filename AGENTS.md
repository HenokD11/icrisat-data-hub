# AGENTS.md — ICRISAT Data Hub

## What this is
Federation-first data hub for ICRISAT: teams drop CSV/Excel/Word files into a
watched inbox or a web page; a pipeline ingests and catalogues them (SQLite +
`data/catalog/assets.json`); an MCP server exposes the catalogue and the data
to LLMs. External datasets are registered as YAML pointers under `sources/`.
Modelled on the CGIAR CDH asset-mapping pipeline (submissions → ingest →
normalised assets.json → publish).

## Commands
- Setup: `python -m venv .venv && .venv\Scripts\pip install -r requirements.txt`
- Tests: `set PYTHONPATH=src && .venv\Scripts\python -m pytest tests -q`
- Ingest scan: `set PYTHONPATH=src && .venv\Scripts\python -m hub.ingest.pipeline --scan`
- Watcher: `scripts\start_watcher.cmd`
- Web app: `scripts\start_web.cmd` (http://localhost:8000)
- MCP stdio: `scripts\start_mcp_stdio.cmd`; MCP HTTP: `scripts\start_mcp_http.cmd` (port 8100)
- Validate YAML pointers: `set PYTHONPATH=src && .venv\Scripts\python -m hub.sources`
- Refresh GitHub Pages app data: `python -m hub.pages` then commit `docs/data/*.json`

## Conventions
- All `python -m hub...` invocations need `PYTHONPATH=src` (scripts set it).
- Paths resolve from the project root via `config/hub.yaml` — never hardcode
  absolute paths in code; use `HubConfig.path(...)`.
- One ingest path only: `hub.ingest.pipeline.ingest_file`. Web app, watcher,
  and CLI all go through it. Don't add parallel ingest logic.
- Catalogue writes go through `hub.catalog.Catalog`; it auto-exports
  `assets.json` on every change — keep that invariant.
- Metadata model: infer → sidecar `.meta.yaml` → explicit metadata (web form /
  API), later sources override earlier. Incomplete assets are status
  `needs_review` with a `*_metadata_review.yaml` generated next to the file.
- YAML pointer schema lives in `src/hub/sources.py` docstring and
  `sources/README.md` — update both if it changes.
- Deployed mode: `HUB_DATA_DIR` remaps data/ paths onto the host volume
  (`config.py`); the web app reads `PORT`; after uploads `hub/sync.py` pushes
  catalogue JSON (never raw files) to GitHub when `GITHUB_TOKEN`/`GITHUB_REPO`
  are set. Dockerfile + .dockerignore must keep local state out of the image.
- Phase-1 file types: .csv, .xlsx/.xls, .docx (`src/hub/ingest/readers.py`).
  New types: add a reader + register the extension in `config/hub.yaml` and
  `detect_file_type`.
- MCP tools must never mutate data; `query_asset` is read-only SELECT via
  DuckDB with a row cap.

## Testing
pytest suite in `tests/` builds fixtures (CSV/XLSX/DOCX) in tmp dirs — it
never touches `data/`. Run before changing ingest/catalog/MCP code.
