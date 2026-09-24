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

## For teams: how to share data

Every dataset goes **upload → describe → curator review → published**.
Nothing is public until the curator for its category approves it.

**1. Web page (recommended, anyone with an email).** Sign in, open
**Upload**, drop one or more files, pick a **category**, fill the short form
(description, team, licence, who can download) and press *Submit for review*.
The curator for that category is emailed. Several files uploaded together
share one description and become separate datasets. Missing details? Use
*Upload now, add details later* and finish them on the dataset's **Edit** page.

**2. Shared drop folder (ICRISAT network).** Copy files into `data/inbox/`.
Add `yield_trials_2025.meta.yaml` next to a file (or one `metadata.yaml` for a
batch) with `title, description, team, owner, owner_email, category, license,
access` and it goes straight to the review queue; otherwise it waits as
*Needs details* with a pre-filled `*_metadata_review.yaml` to complete.

**Statuses:** Needs details → In review → Published (or Returned with the
curator's comment → fix → resubmit). Published datasets can be Withdrawn.

**Who can download** (set on upload, changeable on the dataset's Share panel):

| Level | Files available to |
|---|---|
| Open *(CGIAR default)* | everyone, no sign-in |
| ICRISAT only | signed-in users with an `access_control.org_domains` email |
| Restricted | people/domains on the Share list — add one email, paste a list, upload a CSV, or a whole `@cgiar.org` domain; optional expiry. Needs a reason; personal data forces this level. Others can **Request access**; owners/curators accept or decline with a reason. |

Descriptions of published datasets are always visible (CGIAR Open & FAIR
policy); only the files are gated. The public GitHub Pages dashboard and the
HTTP MCP server show **published + open** datasets only.

**Roles:** admins manage the **Admin** page — add categories, add/remove
curators per category (paste several emails at once) and other admins; changes
apply immediately and are logged. `config/hub.yaml` → `access_control` holds
the bootstrap admins (not removable from the UI, so nobody is locked out) and
any curators you prefer to keep in config. A category with no curators goes
to the admins.

**Scripts / API:** *My datasets → API tokens* creates a personal token (shown
once, stored hashed, revocable). Then:

```
curl -H "Authorization: Bearer $HUB_TOKEN" -F files=@trials.csv      -F category="Soil & Agronomy" -F team="Soil Intelligence"      -F description="On-farm trials 2025" -F license=CC-BY-4.0 -F access=open      https://<hub>/upload
```

returns JSON per file (dataset id, status, missing fields). The same header
works for `GET /api/assets` and `GET /datasets/<id>/download`.

**Access report:** every download is logged; owners and curators see counts
in the dataset's History and can download an access report CSV (grants,
requests, downloads).

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
| Web app (upload, review, share) | `scripts\start_web.cmd` | http://localhost:8010 (dev login) |
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

## Deploy online

The repo is deploy-from-GitHub ready (Dockerfile included). Recommended host:
**Railway** (persistent volume, permanent domain).

1. **railway.app** -> New Project -> Deploy from GitHub repo -> `icrisat-data-hub`.
2. **Volume** mounted at `/data` — raw uploads and the SQLite catalogue live
   there; back it up (e.g. nightly copy of `/data/catalog/catalog.db`).
3. **Sign-in (WorkOS AuthKit, free tier):** create a WorkOS project ->
   *Authentication* -> enable **Email + Magic Auth** (and Google/Microsoft if
   wanted) -> *Redirects* -> add `https://<your-domain>/auth/callback`.
4. **Variables:**

   | Variable | Value |
   |---|---|
   | `WORKOS_API_KEY`, `WORKOS_CLIENT_ID` | from the WorkOS dashboard |
   | `HUB_BASE_URL` | `https://<your-domain>` |
   | `HUB_SECRET_KEY` | long random string (signs session cookies) |
   | `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM` | any SMTP relay, for review/access emails (optional — logged if unset) |
   | `GITHUB_TOKEN`, `GITHUB_REPO`, `GITHUB_BRANCH` | optional: push the public dashboard JSON after each publish |

5. Settings -> Networking -> Generate Domain, then share `https://<your-domain>`.

`HUB_DEV_LOGIN=1` (used by `scripts\start_web.cmd`) gives a password-less
email login for local development only; it is ignored when WorkOS is set.

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
