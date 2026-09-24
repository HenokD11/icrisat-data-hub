# Tracker — remaining work

Status after PR #2 (review workflow, sharing, admin UI, API tokens).
Tick items as they land; add the PR/issue number next to each.

## P0 — before go-live

- [ ] **WorkOS project**: create it, enable Email + Magic Auth, add redirect
      `https://<domain>/auth/callback`, set `WORKOS_API_KEY`, `WORKOS_CLIENT_ID`,
      `HUB_BASE_URL`, `HUB_SECRET_KEY`. Test sign-in on staging (internal and
      external email).
- [ ] **SMTP**: set `SMTP_*`; check the submit, publish, return, access-request
      and grant emails arrive. Until then they are only logged.
- [ ] **Real curators and categories**: the 7 categories in `config/hub.yaml`
      are placeholders. Confirm the list and add curators, in the Admin page or
      in config.
- [ ] **Existing catalogue**: none of the 14 assets have a category. 8 are
      `needs_review`; 6 were published as `internal` before review existed.
      Complete their metadata, re-review them, and choose which to make Open.
      The public site shows 0 until then.
- [ ] **Deploy** (Railway + `/data` volume). Set `web.app_url` in `hub.yaml`
      so the dashboard links to the hub.
- [ ] **Backups**: nightly copy of `/data/catalog/catalog.db` and `/data/processed`.
      Not automated yet.
- [ ] **Proxy request-size limit** in front of the app. The in-app check is
      per file plus a coarse 10× whole-request cap.

## P1 — security / data hygiene

- [ ] **Git history**: old commits of `data/catalog/assets.json` and
      `docs/data/assets.json` contain internal metadata and sample values.
      Decide whether to purge (`git filter-repo` + force-push) or accept.
- [ ] Rate-limit `/login`, `/datasets/*/request`, `/tokens`, `/upload`.
- [ ] CSRF currently relies on `SameSite=Lax` cookies. Add form tokens if the
      app is ever embedded or served cross-site.
- [ ] Optional token expiry, and show API-token use in the access report.
- [ ] E2E coverage for the WorkOS callback (mock `api.workos.com`). Today only
      the dev login path is exercised.
- [ ] Multi-process deploys: the role/policy cache is per process
      (`P()` in `web/app.py`). Add a short TTL before running >1 worker.
- [ ] MCP over stdio still serves internal/restricted published assets (by
      design, same machine). Per-user auth is needed before exposing more over HTTP.
- [ ] `data/inbox/.gitkeep` contains the text "unsupported type". Find out
      what wrote it.

## P2 — product

- [ ] Dataset **versions** (upload a new version of the same dataset instead
      of a new asset).
- [ ] **Drop-folder ownership**: files dropped without `owner_email` have no
      owner. Add a "claim" or assign flow for curators.
- [ ] Terms of access / attach a data-sharing agreement for restricted datasets.
- [ ] Curator bulk actions (approve / return several) and a daily digest email
      option.
- [ ] Admin-editable vocabularies (org domains, licences, access reasons).
      They are still in `hub.yaml`.
- [ ] Full-text search (SQLite FTS5) instead of `LIKE`.
- [ ] Dashboard: the `domain` (CGIAR climate vocab) field is no longer
      collected on upload. Drop it or add it to the form.
- [ ] CG Core-aligned export so datasets can be harvested into CGSpace / GARDIAN.

## Done (PR #2)

- Upload with category → curator review → publish / return / withdraw, with audit log
- Access levels open / ICRISAT-only / restricted; grants by email, list, CSV
  or @domain; access requests; PII forces restricted; embargo ≤ 12 months
- WorkOS sign-in, dev login for local use only
- Admin page (categories, curators, admins); API tokens; download log +
  access report CSV
- Public outputs limited to published + open; staging uploads; no overwrite
  of same-name files; e2e suite (49 checks)
