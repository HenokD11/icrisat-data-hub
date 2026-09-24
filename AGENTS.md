# AGENTS.md — ICRISAT Data Hub

## Testing

- NEVER write unit tests after you write code.
- Highly prefer E2E tests as the sole testing mechanism. Use them to verify complex features work. At the end of E2E tests, produce a verifiable and repeatable artifact.
- If you must test a system in isolation, FIRST write all the ways it could fail, THEN write the code.

When implementing with test-driven development:

- Tautological tests considered harmful.
- Change-detector tests considered harmful.
- Do not create regression tests for bug fixes without a genuine gap in behavior testing.

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
  DuckDB with a row cap. MCP serves published assets only; `--http` serves
  open ones only.
- Review workflow: statuses/transitions live in `catalog.TRANSITIONS`; only
  `Catalog.transition` changes status (plus the automatic needs_review ->
  submitted once metadata is complete). Nothing self-publishes.
- Permissions live in `src/hub/access.py`. Roles = `access_control` in
  hub.yaml + admin-UI roles/categories in SQLite, merged by
  `access.with_db_roles` (the web app's `P()`, cached and cleared on admin
  changes). Web routes ask it; don't inline role checks.
- API tokens: only SHA-256 hashes are stored; bearer requests get JSON/401,
  never redirects.
- Public outputs (`hub.pages` -> docs/data, sync) carry published + open
  assets only; `data/catalog/assets.json` is the full internal export and is
  never committed or pushed.
- Web uploads stream to `data/staging`, never the watched inbox.
- E2E: `tests/test_e2e_web.py` runs the real server (dev login) against a
  temp `HUB_CONFIG`; its report is `tests/artifacts/e2e_web_report.json`.

## Testing
pytest suite in `tests/` builds fixtures (CSV/XLSX/DOCX) in tmp dirs — it
never touches `data/`. Run before changing ingest/catalog/MCP code.
