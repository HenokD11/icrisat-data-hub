"""Web app — upload, review, share. Run:  python -m hub.web.app  (port 8010)

* Anyone: browse published datasets (metadata is always visible), download
  those whose access level allows it, request access to restricted ones.
* Signed-in users: upload files with a category, complete metadata, submit
  for review, share their datasets (emails, pasted lists, CSV, @domains).
* Curators (per category, config/hub.yaml -> access_control): review queue,
  publish / return-with-reason / edit, decide access requests.

Uploads are streamed to data/staging (never the watched inbox) and go
through the one ingest path, ``hub.ingest.pipeline.ingest_file``.
"""

from __future__ import annotations

import csv
import html
import io
import os
import shutil
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .. import access as acl
from .. import notify
from ..catalog import Catalog
from ..config import PROJECT_ROOT, HubConfig, load_config
from ..ingest.pipeline import ingest_file
from ..sync import sync_in_background
from .auth import STATE_COOKIE, Auth, safe_next

MB = 1 << 20
STATUS = {
    "needs_review": ("Needs details", "b-rev"),
    "submitted": ("In review", "b-sidecar"),
    "returned": ("Returned", "b-rev"),
    "published": ("Published", "b-pub"),
    "withdrawn": ("Withdrawn", "b-unspecified"),
}
ACCESS = {"open": "Open", "internal": "ICRISAT only", "restricted": "Restricted"}
TEXT_FIELDS = ("title", "description", "category", "team", "owner", "license", "access",
               "access_reason", "embargo_until", "spatial_coverage", "temporal_coverage",
               "owner_email")

CSS = """
.wrap{max-width:1150px;margin:0 auto;padding:1.1rem 1.3rem}
a.btn{text-decoration:none;display:inline-block}
.stack>*+*{margin-top:.9rem}
.form-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(250px,1fr));gap:.8rem}
.full{grid-column:1/-1}
label{display:block;font-size:.8rem;font-weight:700;color:var(--green-dark)}
label small{font-weight:400;color:var(--grey)}
input[type=text],input[type=email],input[type=date],select,textarea{width:100%;padding:.45rem .55rem;border:1px solid #CBD2D9;
  border-radius:7px;font:inherit;font-size:.88rem;margin-top:.2rem;background:#fff}
input:focus,select:focus,textarea:focus{outline:2px solid var(--green);outline-offset:-1px}
.check{display:flex;gap:.5rem;align-items:flex-start;font-weight:600}
.check input{margin-top:.2rem;accent-color:var(--green)}
.drop{display:block;border:2px dashed var(--green);border-radius:10px;padding:1.6rem;background:var(--green-tint);text-align:center}
.drop.over{background:#D5EBDD}
.drop input{margin-top:.6rem}
.flash{background:var(--green-tint);border:1px solid var(--green);border-radius:8px;padding:.6rem .9rem}
.warnbox{background:var(--orange-tint);border:1px solid var(--orange);border-radius:8px;padding:.6rem .9rem}
.errbox{background:var(--red-tint);border:1px solid var(--red);border-radius:8px;padding:.6rem .9rem}
.row{display:flex;gap:.6rem;flex-wrap:wrap;align-items:flex-end}
.row>form{display:flex;gap:.5rem;flex-wrap:wrap;align-items:flex-end;margin:0}
.btn-danger{background:#fff;color:var(--red);border-color:var(--red)}
.btn-link{background:none;border:none;color:var(--green);font-weight:600;cursor:pointer;padding:0;font:inherit;font-size:.82rem}
h2.page{margin:.2rem 0 .1rem;color:var(--green-dark);font-size:1.35rem}
.chips{display:flex;gap:.35rem;flex-wrap:wrap;align-items:center;margin:.3rem 0}
.navlinks{display:flex;gap:.3rem;flex-wrap:wrap}
.navlinks a{text-decoration:none}
.count{background:var(--orange);color:#fff;border-radius:999px;padding:0 .45rem;font-size:.7rem;margin-left:.2rem}
.who{font-size:.8rem;color:var(--grey)}
.two{display:grid;grid-template-columns:2fr 1fr;gap:.9rem}
@media(max-width:900px){.two{grid-template-columns:1fr}}
.list{list-style:none;padding:0;margin:0}.list li{border-bottom:1px solid var(--line);padding:.45rem 0;font-size:.85rem}
tbody tr{cursor:default}
"""


def e(value: Any) -> str:
    return html.escape("" if value is None else str(value))


def badge(status: str) -> str:
    label, cls = STATUS.get(status, (status, "b-unspecified"))
    return f'<span class="badge {cls}">{e(label)}</span>'


def access_badge(level: str | None) -> str:
    level = level or "unspecified"
    return f'<span class="badge b-{e(level)}">{e(ACCESS.get(level, "Access not set"))}</span>'


def create_app(cfg: HubConfig | None = None) -> FastAPI:
    cfg = cfg or load_config()
    auth = Auth()
    app = FastAPI(title=cfg.hub_name, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "docs"), name="static")
    max_upload = int(cfg.get("ingest", "max_upload_mb", default=200)) * MB
    vocab = lambda key: cfg.get("vocabulary", key, default=[]) or []  # noqa: E731

    def catalog() -> Catalog:
        return Catalog(cfg)

    policy: dict[str, HubConfig] = {}

    def P() -> HubConfig:
        """hub.yaml + admin-UI roles. ponytail: cached per process and dropped on
        every admin change; run one web process (or add a short TTL) if scaled out."""
        if "cfg" not in policy:
            policy["cfg"] = acl.with_db_roles(cfg, catalog())
        return policy["cfg"]

    def current_user(request: Request) -> dict | None:
        """Session cookie, or 'Authorization: Bearer hub_…' personal API token."""
        header = request.headers.get("authorization", "")
        if header.lower().startswith("bearer "):
            email = catalog().token_email(header[7:].strip())
            return {"email": email, "name": "", "api": True} if email else None
        return auth.user(request)

    @app.middleware("http")
    async def cap_body(request: Request, call_next):
        # ponytail: whole-request cap stops disk-filling uploads before parsing;
        # per-file limit is enforced while streaming. Put a proxy limit in front too.
        length = request.headers.get("content-length")
        if length and length.isdigit() and int(length) > max_upload * 10:
            return JSONResponse({"error": "upload too large"}, status_code=413)
        return await call_next(request)

    # ------------------------------------------------------------ layout
    def page(request: Request, title: str, body: str, status: int = 200) -> HTMLResponse:
        user = current_user(request)
        email = user["email"] if user else None
        links = [("/", "Catalogue"), ("/upload", "Upload")]
        if user:
            links.append(("/mine", "My datasets"))
        if acl.is_curator(P(), email):
            n = sum(1 for a in catalog().list_assets(status="submitted", limit=5000)
                    if acl.can_curate(P(), email, a))
            links.append(("/review", "Review" + (f'<span class="count">{n}</span>' if n else "")))
        if acl.is_admin(P(), email):
            links.append(("/admin", "Admin"))
        nav ="".join(f'<a class="viewtab" href="{h}"><span class="viewtab-title">{t}</span></a>'
                      for h, t in links)
        if user:
            who = (f'<span class="who">{e(email)}</span><form method="post" action="/logout" style="margin:0">'
                   f'<button class="btn btn-quiet">Sign out</button></form>')
        else:
            who = f'<a class="btn" href="/login?next={e(request.url.path)}">Sign in</a>'
        msg = request.query_params.get("msg")
        flash = f'<div class="flash">{e(msg)}</div>' if msg else ""
        dash = cfg.get("web", "dashboard_url", default="")
        dash_link = f'<a class="ghost-link" href="{e(dash)}" target="_blank" rel="noopener">Dashboard ↗</a>' if dash else ""
        return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(title)} — {e(cfg.hub_name)}</title>
<link rel="icon" href="/static/assets/favicon.ico">
<link rel="stylesheet" href="/static/styles.css"><style>{CSS}</style></head><body>
<header class="topbar"><a class="brand" href="/" style="text-decoration:none">
<img src="/static/assets/icrisat-logo.png" alt="ICRISAT" class="brand-logo">
<div><p class="brand-eyebrow">{e(cfg.hub_name)}</p><h1>Data Repository</h1></div></a>
<nav class="navlinks" aria-label="Main">{nav}</nav>
<div class="topbar-actions">{dash_link}{who}</div></header>
<main class="wrap stack">{flash}{body}</main></body></html>""", status_code=status)

    def deny(request: Request, message: str = "You don't have permission to do that.", status: int = 403):
        return page(request, "Not allowed", f'<div class="errbox">{e(message)}</div>', status)

    def not_found(request: Request):
        return page(request, "Not found", '<div class="errbox">Dataset not found.</div>', 404)

    def to_login(request: Request):
        if request.headers.get("authorization"):  # API client: no redirects
            return JSONResponse({"error": "invalid or revoked API token"}, status_code=401)
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    def back(asset_id: str, msg: str = "") -> RedirectResponse:
        q = f"?msg={quote(msg)}" if msg else ""
        return RedirectResponse(f"/datasets/{asset_id}{q}", status_code=303)

    def who(request: Request) -> str | None:
        user = current_user(request)
        return user["email"] if user else None

    # ------------------------------------------------------------ forms
    def options(values, selected, blank="— choose —") -> str:
        opts = [f'<option value="">{e(blank)}</option>'] if blank else []
        for v in values:
            opts.append(f'<option value="{e(v)}"{" selected" if v == selected else ""}>{e(v)}</option>')
        if selected and selected not in values:  # legacy value stays visible
            opts.append(f'<option value="{e(selected)}" selected>{e(selected)}</option>')
        return "".join(opts)

    def meta_form(a: dict[str, Any], teams: list[str], curator: bool, upload: bool) -> str:
        access_opts = "".join(
            f'<option value="{k}"{" selected" if (a.get("access") or "open") == k else ""}>{e(v)}</option>'
            for k, v in ACCESS.items())
        req = "" if not upload else " required"
        owner_email = (f'<label>Owner email <small>(curators can reassign)</small>'
                       f'<input type="email" name="owner_email" value="{e(a.get("owner_email"))}"></label>'
                       if curator and not upload else "")
        title_hint = "<small>(one file: optional, taken from the file name; several files: leave blank)</small>" if upload else ""
        return f"""
<div class="form-grid">
 <label class="full">Title {title_hint}<input type="text" name="title" maxlength="300" value="{e(a.get("title"))}"></label>
 <label class="full">Description <span class="req">*</span> <small>What is it, how was it collected, what does each file contain?</small>
  <textarea name="description" rows="3" maxlength="5000"{req}>{e(a.get("description"))}</textarea></label>
 <label>Category <span class="req">*</span><select name="category"{req}>{options(list(acl.categories(P())), a.get("category"))}</select></label>
 <label>Team / programme <span class="req">*</span><input type="text" name="team" list="teams" value="{e(a.get("team"))}"{req}>
  <datalist id="teams">{"".join(f'<option value="{e(t)}">' for t in teams)}</datalist></label>
 <label>Licence <span class="req">*</span><select name="license"{req}>{options(vocab("licenses"), a.get("license") or "CC-BY-4.0", blank="")}</select></label>
 <label>Who can download the files? <span class="req">*</span><select name="access">{access_opts}</select>
  <small>Metadata is public once published. Open is the CGIAR default.</small></label>
 <label>Reason for restriction <small>(required if Restricted)</small>
  <select name="access_reason">{options(vocab("access_reasons"), a.get("access_reason"), blank="— none —")}</select></label>
 <label>Restricted until <small>(embargo, max 12 months)</small><input type="date" name="embargo_until" value="{e(a.get("embargo_until"))}"></label>
 <label>Geography <small>(countries, regions)</small><input type="text" name="spatial_coverage" value="{e(a.get("spatial_coverage"))}"></label>
 <label>Period covered <small>(e.g. 2021–2025)</small><input type="text" name="temporal_coverage" value="{e(a.get("temporal_coverage"))}"></label>
 <label>Keywords <small>(comma-separated)</small><input type="text" name="tags" value="{e(", ".join(a.get("tags") or []))}"></label>
 {owner_email}
 <label class="check full"><input type="checkbox" name="contains_pii"{" checked" if a.get("contains_pii") else ""}>
  <span>Contains personal data (names, phone numbers, GPS of homes…) — the dataset will be Restricted.</span></label>
</div>"""

    def read_form(form, curator: bool) -> tuple[dict[str, Any], list[str]]:
        """Validate metadata fields at the trust boundary. Only fields present are returned."""
        m: dict[str, Any] = {k: str(form.get(k) or "").strip()[:5000] for k in TEXT_FIELDS if k in form}
        if not curator:
            m.pop("owner_email", None)
        errors = []
        checks = [("category", list(acl.categories(P()))), ("license", vocab("licenses")),
                  ("access", list(ACCESS)), ("access_reason", vocab("access_reasons"))]
        for key, allowed in checks:
            if m.get(key) and m[key] not in allowed:
                errors.append(f"Unknown {key.replace('_', ' ')}: {m[key]}")
        if m.get("embargo_until"):
            try:
                until = date.fromisoformat(m["embargo_until"])
                if until > date.today() + timedelta(days=366):
                    errors.append("An embargo can last at most 12 months (CGIAR Open & FAIR policy).")
            except ValueError:
                errors.append("Embargo date must be a date.")
        if "tags" in form:
            m["tags"] = [t.strip() for t in str(form.get("tags") or "").split(",") if t.strip()]
        if "access" in form:
            m["contains_pii"] = form.get("contains_pii") in ("on", "1", "true")
        return m, errors

    def teams() -> list[str]:
        return sorted({a["team"] for a in catalog().list_assets(limit=100000) if a.get("team")})

    # ------------------------------------------------------------ auth
    @app.get("/healthz")
    def healthz():
        catalog()  # opens the DB
        return {"ok": True}

    @app.get("/login")
    def login_page(request: Request, next: str = "/"):
        nxt = safe_next(next)
        if current_user(request):
            return RedirectResponse(nxt, status_code=303)
        if auth.workos:
            url, state = auth.authorize_url(request, nxt)
            resp = RedirectResponse(url, status_code=303)
            resp.set_cookie(STATE_COOKIE, state, max_age=600, httponly=True, samesite="lax",
                            secure=auth.secure_cookies)
            return resp
        if auth.dev:
            return page(request, "Sign in", f"""<article class="panel panel-hero" style="max-width:460px">
<div class="panel-head"><h3>Sign in (local development)</h3>
<p class="panel-sub">Dev login is on (HUB_DEV_LOGIN=1). Production uses WorkOS email codes.</p></div>
<form method="post" action="/login?next={e(nxt)}" class="stack">
<label>Email<input type="email" name="email" required></label>
<label>Name<input type="text" name="name"></label>
<button class="btn">Sign in</button></form></article>""")
        return page(request, "Sign in", '<div class="errbox">Sign-in is not configured on this server '
                    '(set WORKOS_API_KEY and WORKOS_CLIENT_ID).</div>', 503)

    @app.post("/login")
    async def dev_login(request: Request, next: str = "/"):
        if not auth.dev:
            return deny(request, "Not available.", 404)
        form = await request.form()
        email = str(form.get("email") or "").strip().lower()
        if not acl.EMAIL_RE.fullmatch(email):
            return deny(request, "Enter a valid email.", 400)
        resp = RedirectResponse(safe_next(next), status_code=303)
        auth.login(resp, email, str(form.get("name") or ""))
        return resp

    @app.get("/auth/callback")
    async def auth_callback(request: Request, code: str = "", state: str = ""):
        saved = auth.unsign(request.cookies.get(STATE_COOKIE), max_age=600)
        if not (auth.workos and code and saved and saved.get("nonce") == state):
            return deny(request, "Sign-in expired or was tampered with — please try again.", 400)
        try:
            user = await run_in_threadpool(auth.exchange, code)
        except Exception:
            return deny(request, "Sign-in failed — please try again.", 502)
        if not user.get("email_verified"):
            return deny(request, "Your email address is not verified.")
        resp = RedirectResponse(safe_next(saved.get("next")), status_code=303)
        name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")]))
        auth.login(resp, user["email"], name)
        resp.delete_cookie(STATE_COOKIE)
        return resp

    @app.post("/logout")
    def logout():
        resp = RedirectResponse("/", status_code=303)
        auth.logout(resp)
        return resp

    # ------------------------------------------------------------ browse
    def asset_table(assets: list[dict[str, Any]], show_status: bool = False) -> str:
        if not assets:
            return '<p class="muted">Nothing here yet.</p>'
        rows = "".join(
            f'<tr><td><a class="t-title" href="/datasets/{e(a["asset_id"])}">{e(a["title"])}</a>'
            f'<div class="t-desc">{e((a.get("description") or "")[:160])}</div></td>'
            f'<td>{e(a.get("category") or "—")}</td><td>{e(a.get("team") or "—")}</td>'
            f'<td>{access_badge(a.get("access"))}</td>'
            + (f'<td>{badge(a["status"])}</td>' if show_status else "")
            + f'<td>{e((a.get("updated_at") or "")[:10])}</td></tr>'
            for a in assets)
        head = "<th>Dataset</th><th>Category</th><th>Team</th><th>Access</th>" + \
               ("<th>Status</th>" if show_status else "") + "<th>Updated</th>"
        return f'<div class="table-wrap"><table><thead><tr>{head}</tr></thead><tbody>{rows}</tbody></table></div>'

    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, q: str = "", category: str = ""):
        cat = catalog()
        assets = cat.search(q, limit=500) if q else cat.list_assets(status="published", limit=500)
        assets = [a for a in assets if a["status"] == "published"
                  and (not category or a.get("category") == category)]
        cats = options(list(acl.categories(P())), category, blank="All categories")
        return page(request, "Catalogue", f"""
<article class="panel panel-hero"><div class="panel-head"><h3>Datasets <span class="count-chip">{len(assets)}</span></h3>
<p class="panel-sub">Reviewed datasets from ICRISAT teams. Anyone can read the descriptions; downloads follow each dataset's access level.</p></div>
<form class="row" method="get" action="/"><label style="flex:2">Search<input type="text" name="q" value="{e(q)}" placeholder="title, description, column names…"></label>
<label style="flex:1">Category<select name="category">{cats}</select></label><button class="btn">Search</button></form></article>
{asset_table(assets)}""")

    @app.get("/api/assets")
    def assets_json(request: Request):
        email, cat = who(request), catalog()
        out = []
        for a in cat.list_assets(status="published", limit=100000):
            full = cat.get_asset(a["asset_id"])
            if not acl.can_download(P(), email, full, cat.list_grants(a["asset_id"])):
                full["tables"] = []  # schema + samples only for people allowed the data
            for private in ("file_path", "sha256", "ingest_log", "owner_email"):
                full.pop(private, None)
            out.append(full)
        return out

    # ------------------------------------------------------------ upload
    @app.get("/upload", response_class=HTMLResponse)
    def upload_page(request: Request):
        user = current_user(request)
        if not user:
            return to_login(request)
        exts = ", ".join(sorted(cfg.supported_extensions))
        return page(request, "Upload", f"""
<article class="panel panel-hero"><div class="panel-head"><h3>Upload data</h3>
<p class="panel-sub">1 · choose files → 2 · describe them → 3 · a curator for the category reviews and publishes.
Several files uploaded together share the description below.</p></div>
<form method="post" action="/upload" enctype="multipart/form-data" class="stack">
<label class="drop" id="drop">Drag files here or click to choose<br><small>{e(exts)} · up to {max_upload // MB} MB each</small>
<input type="file" name="files" multiple required accept="{e(",".join(sorted(cfg.supported_extensions)))}"></label>
{meta_form({"owner": user.get("name")}, teams(), False, upload=True)}
<input type="hidden" name="owner" value="{e(user.get("name") or user["email"])}">
<div class="row"><button class="btn">Submit for review</button>
<button class="btn btn-quiet" formnovalidate>Upload now, add details later</button></div>
</form></article>
<article class="panel"><div class="panel-head"><h3>Other ways in</h3></div>
<ul class="list"><li><b>Shared drop folder</b> (ICRISAT network): copy files into the hub inbox; add a <code>name.meta.yaml</code> next to a file to describe it, including <code>category</code> and <code>owner_email</code>.</li>
<li><b>Many files, one description</b>: select them all above — each becomes its own dataset with the same details.</li></ul></article>
<script>const d=document.getElementById('drop');['dragenter','dragover'].forEach(t=>d.addEventListener(t,()=>d.classList.add('over')));
['dragleave','drop'].forEach(t=>d.addEventListener(t,()=>d.classList.remove('over')));</script>""")

    async def stage(upload, dest_dir: Path) -> Path | None:
        """Stream one upload to staging; None if it exceeds the size cap."""
        name = Path(str(upload.filename or "upload.bin").replace("\\", "/")).name or "upload.bin"
        dest_dir.mkdir(parents=True)
        dest = dest_dir / name
        size = 0
        with open(dest, "wb") as out:
            while chunk := await upload.read(MB):
                size += len(chunk)
                if size > max_upload:
                    break
                out.write(chunk)
        if size > max_upload:
            shutil.rmtree(dest_dir, ignore_errors=True)
            return None
        return dest

    @app.post("/upload")
    async def upload(request: Request):
        user = current_user(request)
        if not user:
            return to_login(request)
        form = await request.form()
        files = [f for f in form.getlist("files") if getattr(f, "filename", None)]
        meta, errors = read_form(form, curator=False)
        if not files:
            errors.append("Choose at least one file.")
        if errors:
            if user.get("api"):
                return JSONResponse({"errors": errors}, status_code=400)
            return page(request, "Upload", "".join(f'<div class="errbox">{e(x)}</div>' for x in errors)
                        + '<p><a href="/upload">Back to upload</a></p>', 400)
        meta.update(owner=meta.get("owner") or user.get("name") or user["email"],
                    contact=user["email"], owner_email=user["email"])
        if len(files) > 1:
            meta.pop("title", None)  # each file keeps its own name-derived title
        cat, results, too_big = catalog(), [], False
        for f in files:
            tmp = cfg.path("staging") / uuid.uuid4().hex
            staged = await stage(f, tmp)
            if staged is None:
                too_big = True
                results.append((f.filename, None, f"Too large (limit {max_upload // MB} MB)"))
                continue
            try:
                res = await run_in_threadpool(ingest_file, staged, cfg, cat, meta)
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            if res.get("ok"):
                a = cat.get_asset(res["asset_id"], with_tables=False)
                results.append((f.filename, a, ""))
                if a["status"] == "submitted":
                    notify.send(acl.curators_for(P(), a.get("category")), f"Review requested: {a['title']}",
                                f"{user['email']} submitted \"{a['title']}\" [{a.get('category')}].\n\n"
                                f"Review it: {notify.link('/datasets/' + a['asset_id'])}")
            else:
                reason = res.get("reason", "")
                friendly = ("Already in the hub (identical file)" if "duplicate" in reason
                            else "File type not supported" if "unsupported" in reason
                            else "Could not read this file — is it a valid CSV/Excel/Word file?")
                results.append((f.filename, None, friendly))
        if user.get("api"):
            return JSONResponse([
                {"file": name, "ok": a is not None, "error": err or None,
                 **({"asset_id": a["asset_id"], "status": a["status"], "missing_fields": a["missing_fields"],
                     "url": notify.link(f"/datasets/{a['asset_id']}")} if a else {})}
                for name, a, err in results], status_code=413 if too_big else 200)
        items = []
        for name, a, err in results:
            if a is None:
                items.append(f'<li><b>{e(name)}</b> — <span class="badge b-restricted">Not uploaded</span> {e(err)}</li>')
            elif a["status"] == "needs_review":
                items.append(f'<li><b>{e(name)}</b> — {badge(a["status"])} '
                             f'<a href="/datasets/{e(a["asset_id"])}/edit">Add the missing details</a> '
                             f'({e(", ".join(a["missing_fields"]))}) then submit.</li>')
            else:
                items.append(f'<li><b>{e(name)}</b> — {badge(a["status"])} '
                             f'<a href="/datasets/{e(a["asset_id"])}">View</a> · a curator has been notified.</li>')
        return page(request, "Upload results", f"""<article class="panel panel-hero"><div class="panel-head"><h3>Upload results</h3></div>
<ul class="list">{"".join(items)}</ul><p><a href="/upload">Upload more</a> · <a href="/mine">My datasets</a></p></article>""",
                    413 if too_big else 200)

    # ------------------------------------------------------------ my work
    @app.get("/mine", response_class=HTMLResponse)
    def mine(request: Request):
        email = who(request)
        if not email:
            return to_login(request)
        cat = catalog()
        assets = cat.list_assets(owner_email=email, limit=5000)
        requests_ = cat.list_requests(email=email)
        req_rows = "".join(
            f'<li><a href="/datasets/{e(r["asset_id"])}">{e(r["asset_id"])}</a> — {e(r["status"])}'
            f'{(" · " + e(r["reason"])) if r.get("reason") else ""}</li>' for r in requests_)
        return page(request, "My datasets", f"""<h2 class="page">My datasets</h2>
{asset_table(assets, show_status=True)}
<article class="panel"><div class="panel-head"><h3>My access requests</h3></div>
<ul class="list">{req_rows or '<li class="muted">None.</li>'}</ul></article>
<p><a href="/tokens">API tokens</a> — upload from scripts or R/Python without a browser.</p>""")

    # ------------------------------------------------------------ API tokens
    def tokens_page(request: Request, email: str, new_token: str = "") -> HTMLResponse:
        rows = "".join(
            f'<tr><td>{e(t["name"])}</td><td>{e(t["created_at"][:10])}</td><td>{e((t["last_used_at"] or "never")[:10])}</td>'
            + (f'<td class="muted">revoked {e(t["revoked_at"][:10])}</td>' if t["revoked_at"] else
               f'<td><form method="post" action="/tokens/{e(t["token_id"])}/revoke"><button class="btn-link">Revoke</button></form></td>')
            + "</tr>" for t in catalog().list_tokens(email))
        shown = (f'<div class="warnbox"><b>Copy this token now — it will not be shown again.</b><br>'
                 f'<code style="font-size:.95rem;user-select:all">{e(new_token)}</code></div>') if new_token else ""
        host = e(notify.link(""))
        return page(request, "API tokens", f"""<h2 class="page">API tokens</h2>{shown}
<article class="panel"><div class="panel-head"><h3>Create a token</h3>
<p class="panel-sub">A token acts as you. Keep it secret; revoke it if it leaks.</p></div>
<form method="post" action="/tokens" class="row"><label style="flex:1">Name<input type="text" name="name" required maxlength="100" placeholder="e.g. field-station sync script"></label>
<button class="btn">Create token</button></form></article>
<div class="table-wrap"><table><thead><tr><th>Name</th><th>Created</th><th>Last used</th><th></th></tr></thead>
<tbody>{rows or '<tr><td colspan="4" class="muted">No tokens yet.</td></tr>'}</tbody></table></div>
<article class="panel"><div class="panel-head"><h3>Upload from a script</h3></div>
<pre class="d-doc">curl -H "Authorization: Bearer $HUB_TOKEN" \\
  -F files=@yield_trials.csv -F files=@soil.xlsx \\
  -F category="Soil &amp; Agronomy" -F team="Soil Intelligence" \\
  -F description="On-farm trials 2025" -F license=CC-BY-4.0 -F access=open \\
  {host}/upload</pre>
<p class="muted">Returns JSON: one entry per file with its dataset id, status and any missing fields.
The same header works for <code>GET /api/assets</code> and <code>/datasets/&lt;id&gt;/download</code>.</p></article>""")

    @app.get("/tokens", response_class=HTMLResponse)
    def tokens(request: Request):
        user = auth.user(request)  # tokens are managed from a browser session only
        if not user:
            return to_login(request)
        return tokens_page(request, user["email"])

    @app.post("/tokens")
    async def create_token(request: Request):
        user = auth.user(request)
        if not user:
            return to_login(request)
        name = str((await request.form()).get("name") or "").strip() or "token"
        _, token = catalog().create_token(user["email"], name)
        return tokens_page(request, user["email"], token)

    @app.post("/tokens/{token_id}/revoke")
    def revoke_token(request: Request, token_id: str):
        user = auth.user(request)
        if not user:
            return to_login(request)
        catalog().revoke_token(token_id, user["email"])
        return RedirectResponse("/tokens?msg=Token%20revoked.", status_code=303)

    # ------------------------------------------------------------ admin
    @app.get("/admin", response_class=HTMLResponse)
    def admin_page(request: Request):
        email = who(request)
        if not email:
            return to_login(request)
        pol = P()
        if not acl.is_admin(pol, email):
            return deny(request, "Admins only.")
        cat = catalog()
        yaml_cats = set((cfg.get("access_control", "categories", default={}) or {}))
        db_roles = {(r["email"], r["role"], r["category"]) for r in cat.roles()}
        counts = {}
        for a in cat.list_assets(limit=100000):
            counts[a.get("category")] = counts.get(a.get("category"), 0) + 1

        def remove_btn(action: str, **fields) -> str:
            hidden = "".join(f'<input type="hidden" name="{k}" value="{e(v)}">' for k, v in fields.items())
            return f'<form method="post" action="{action}" style="display:inline">{hidden}<button class="btn-link">Remove</button></form>'

        admins = "".join(
            f'<li class="row" style="justify-content:space-between"><span>{e(a)}</span>'
            + ('<span class="muted">from config/hub.yaml</span>' if acl.is_config_admin(pol, a)
               else remove_btn("/admin/admins/remove", email=a)) + "</li>"
            for a in dict.fromkeys(x.lower() for x in pol.get("access_control", "admins") or []))
        cat_rows = []
        for name, people in acl.categories(pol).items():
            curators = "".join(
                f'<li>{e(p)} ' + (remove_btn("/admin/curators/remove", category=name, email=p)
                                  if (p.lower(), "curator", name) in db_roles else '<span class="muted">(config)</span>') + "</li>"
                for p in dict.fromkeys(people))
            removable = name not in yaml_cats and not counts.get(name)
            cat_rows.append(f"""<tr><td><b>{e(name)}</b><div class="muted">{counts.get(name, 0)} datasets</div>
{remove_btn("/admin/categories/remove", name=name) if removable else ""}</td>
<td><ul class="list">{curators or '<li class="muted">No curators — admins review these.</li>'}</ul></td>
<td><form method="post" action="/admin/curators" class="row"><input type="hidden" name="category" value="{e(name)}">
<input type="text" name="emails" placeholder="emails, comma or newline separated" style="min-width:220px"><button class="btn btn-quiet">Add</button></form></td></tr>""")
        log = "".join(f'<li>{e(ev["at"][:16].replace("T", " "))} · <b>{e(ev["actor"])}</b> {e(ev["action"])} — {e(ev["comment"])}</li>'
                      for ev in reversed(cat.events(Catalog.ADMIN_LOG)[-30:]))
        return page(request, "Admin", f"""<h2 class="page">Admin</h2>
<article class="panel"><div class="panel-head"><h3>Categories &amp; curators</h3>
<p class="panel-sub">Curators review and publish datasets in their category. Changes apply immediately.</p></div>
<div class="table-wrap"><table><thead><tr><th>Category</th><th>Curators</th><th>Add curators</th></tr></thead><tbody>{"".join(cat_rows)}</tbody></table></div>
<form method="post" action="/admin/categories" class="row" style="margin-top:.8rem"><label>New category<input type="text" name="name" required maxlength="80"></label>
<button class="btn">Add category</button></form></article>
<article class="panel"><div class="panel-head"><h3>Admins</h3><p class="panel-sub">Admins curate every category and manage this page.
Admins listed in config/hub.yaml can't be removed here, so nobody gets locked out.</p></div>
<ul class="list">{admins}</ul>
<form method="post" action="/admin/admins" class="row"><label style="flex:1">Add admin<input type="email" name="email" required></label><button class="btn btn-quiet">Add</button></form></article>
<article class="panel"><div class="panel-head"><h3>Recent changes</h3></div><ul class="list">{log or '<li class="muted">None yet.</li>'}</ul></article>""")

    async def admin_form(request: Request):
        email = who(request)
        if not email or not acl.is_admin(P(), email):
            return None, None
        return email, await request.form()

    def admin_done(msg: str) -> RedirectResponse:
        policy.clear()  # roles changed: rebuild on next request
        return RedirectResponse(f"/admin?msg={quote(msg)}", status_code=303)

    @app.post("/admin/admins")
    async def add_admin(request: Request):
        email, form = await admin_form(request)
        if not email:
            return deny(request, "Admins only.")
        valid, invalid = acl.parse_principals(str(form.get("email") or ""))
        valid = [v for v in valid if not v.startswith("@")]
        catalog().add_roles(valid, "admin", "", email)
        return admin_done(f"{len(valid)} added, {len(invalid)} invalid")

    @app.post("/admin/admins/remove")
    async def remove_admin(request: Request):
        email, form = await admin_form(request)
        if not email:
            return deny(request, "Admins only.")
        target = str(form.get("email") or "")
        if acl.is_config_admin(P(), target):
            return deny(request, "This admin is set in config/hub.yaml; remove them there.", 400)
        catalog().remove_role(target, "admin", "", email)
        return admin_done("Admin removed.")

    @app.post("/admin/categories")
    async def add_category(request: Request):
        email, form = await admin_form(request)
        if not email:
            return deny(request, "Admins only.")
        name = " ".join(str(form.get("name") or "").split())[:80]
        if not name:
            return deny(request, "Give the category a name.", 400)
        catalog().add_category(name, email)
        return admin_done(f"Category '{name}' added.")

    @app.post("/admin/categories/remove")
    async def remove_category(request: Request):
        email, form = await admin_form(request)
        if not email:
            return deny(request, "Admins only.")
        name = str(form.get("name") or "")
        if name in (cfg.get("access_control", "categories", default={}) or {}):
            return deny(request, "This category is set in config/hub.yaml; remove it there.", 400)
        try:
            catalog().remove_category(name, email)
        except ValueError as exc:
            return deny(request, str(exc), 400)
        return admin_done(f"Category '{name}' removed.")

    @app.post("/admin/curators")
    async def add_curators(request: Request):
        email, form = await admin_form(request)
        if not email:
            return deny(request, "Admins only.")
        category = str(form.get("category") or "")
        if category not in acl.categories(P()):
            return deny(request, "Unknown category.", 400)
        valid, invalid = acl.parse_principals(str(form.get("emails") or ""))
        valid = [v for v in valid if not v.startswith("@")]
        added = catalog().add_roles(valid, "curator", category, email)
        return admin_done(f"{added} added, {len(invalid)} invalid" + (f": {', '.join(invalid[:10])}" if invalid else ""))

    @app.post("/admin/curators/remove")
    async def remove_curator(request: Request):
        email, form = await admin_form(request)
        if not email:
            return deny(request, "Admins only.")
        catalog().remove_role(str(form.get("email") or ""), "curator", str(form.get("category") or ""), email)
        return admin_done("Curator removed.")

    @app.get("/review", response_class=HTMLResponse)
    def review(request: Request):
        email = who(request)
        if not email:
            return to_login(request)
        if not acl.is_curator(P(), email):
            return deny(request, "The review queue is for curators.")
        cat = catalog()
        queue = sorted((a for a in cat.list_assets(status="submitted", limit=5000)
                        if acl.can_curate(P(), email, a)), key=lambda a: a.get("submitted_at") or "")
        pending = [(r, a) for a in cat.list_assets(status="published", limit=5000)
                   if acl.can_curate(P(), email, a)
                   for r in cat.list_requests(asset_id=a["asset_id"]) if r["status"] == "pending"]
        cats = "all categories" if acl.curated_categories(P(), email) is None \
            else ", ".join(sorted(acl.curated_categories(P(), email)))
        rows = "".join(
            f'<tr><td><a class="t-title" href="/datasets/{e(a["asset_id"])}">{e(a["title"])}</a></td>'
            f'<td>{e(a.get("category"))}</td><td>{e(a.get("team"))}</td><td>{e(a.get("owner_email") or "drop folder")}</td>'
            f'<td>{e(a["file_type"])}</td><td>{e((a.get("submitted_at") or "")[:10])}</td></tr>' for a in queue)
        reqs = "".join(f'<li><a href="/datasets/{e(a["asset_id"])}">{e(a["title"])}</a> — {e(r["email"])}: {e(r["purpose"])}</li>'
                       for r, a in pending)
        return page(request, "Review queue", f"""<h2 class="page">Review queue</h2><p class="muted">You curate {e(cats)}. Oldest first.</p>
<div class="table-wrap"><table><thead><tr><th>Dataset</th><th>Category</th><th>Team</th><th>Submitted by</th><th>Type</th><th>Submitted</th></tr></thead>
<tbody>{rows or '<tr><td colspan="6" class="muted">Nothing waiting for review.</td></tr>'}</tbody></table></div>
<article class="panel"><div class="panel-head"><h3>Pending access requests</h3></div><ul class="list">{reqs or '<li class="muted">None.</li>'}</ul></article>""")

    # ------------------------------------------------------------ dataset
    def load(asset_id: str, request: Request):
        cat = catalog()
        a = cat.get_asset(asset_id)
        email = who(request)
        if a is None or not acl.can_view(P(), email, a):
            return cat, None, email
        return cat, a, email

    @app.get("/datasets/{asset_id}", response_class=HTMLResponse)
    def dataset(request: Request, asset_id: str):
        cat, a, email = load(asset_id, request)
        if a is None:
            return not_found(request)
        grants = cat.list_grants(asset_id)
        owner, curator = acl.is_owner(email, a), bool(email) and acl.can_curate(P(), email, a)
        manage = owner or curator
        allowed = acl.can_download(P(), email, a, grants)
        events = cat.events(asset_id)
        parts = []

        last_return = next((ev for ev in reversed(events) if ev["action"] == "return"), None)
        if a["status"] == "returned" and last_return and manage:
            parts.append(f'<div class="warnbox"><b>Returned by {e(last_return["actor"])}:</b> {e(last_return["comment"])}</div>')
        if a["missing_fields"] and manage:
            parts.append(f'<div class="warnbox">Missing before it can be submitted: <b>{e(", ".join(a["missing_fields"]))}</b></div>')

        # workflow buttons
        buttons = []
        def act(action: str, label: str, cls: str = "btn") -> str:
            return (f'<form method="post" action="/datasets/{e(asset_id)}/action"><input type="hidden" name="action" value="{action}">'
                    f'<button class="{cls}">{label}</button></form>')
        editable = curator or (owner and a["status"] in ("needs_review", "returned"))
        if editable:
            buttons.append(f'<a class="btn btn-quiet" href="/datasets/{e(asset_id)}/edit">Edit details</a>')
        if manage and a["status"] in ("needs_review", "returned") and not a["missing_fields"]:
            buttons.append(act("submit", "Submit for review"))
        if curator and a["status"] == "submitted":
            buttons.append(act("publish", "Approve &amp; publish"))
            buttons.append(f'<form method="post" action="/datasets/{e(asset_id)}/action"><input type="hidden" name="action" value="return">'
                           f'<input type="text" name="comment" placeholder="What needs fixing? (sent to uploader)" required style="min-width:280px">'
                           f'<button class="btn btn-quiet">Return to uploader</button></form>')
        if manage and a["status"] == "published":
            buttons.append(act("withdraw", "Withdraw", "btn btn-danger"))
        if curator and a["status"] == "withdrawn":
            buttons.append(act("publish", "Publish again"))
        if buttons:
            parts.append(f'<div class="row">{"".join(buttons)}</div>')

        meta_rows = [("Description", a.get("description")), ("Category", a.get("category")), ("Team", a.get("team")),
                     ("Owner", a.get("owner")), ("Contact", a.get("contact")), ("Licence", a.get("license")),
                     ("Access", ACCESS.get(a.get("access") or "", "not set")
                      + (f" — {a['access_reason']}" if a.get("access_reason") else "")
                      + (f" (until {a['embargo_until']})" if a.get("embargo_until") else "")),
                     ("Geography", a.get("spatial_coverage")), ("Period", a.get("temporal_coverage")),
                     ("Keywords", ", ".join(a.get("tags") or [])), ("File", f"{a['file_name']} · {a['file_type']} · "
                      f"{round((a.get('size_bytes') or 0) / 1024)} KB"), ("Added", (a.get("created_at") or "")[:10])]
        dl = "".join(f"<dt>{e(k)}</dt><dd>{e(v) or '—'}</dd>" for k, v in meta_rows)

        # data section
        schema = []
        for t in a.get("tables", []):
            if t["kind"] == "document":
                preview = f'<div class="d-doc">{e((t.get("text_preview") or "")[:3000])}</div>' if allowed else ""
                schema.append(f'<div class="d-schema"><b>Document text</b>{preview}</div>')
                continue
            cols = "".join(
                f'<div class="d-col"><b>{e(c["name"])}</b> <span class="samples">{e(c.get("dtype"))}'
                + (f' — e.g. {e(", ".join(map(str, c.get("samples", [])[:4])))}' if allowed and c.get("samples") else "")
                + "</span></div>" for c in t.get("columns", []))
            schema.append(f'<div class="d-schema"><b>{e(t["name"])}</b> · {e(t.get("n_rows"))} rows × {e(t.get("n_cols"))} columns{cols}</div>')
        if allowed:
            data_head = f'<a class="btn" href="/datasets/{e(asset_id)}/download">Download {e(a["file_name"])}</a>'
        elif not email:
            data_head = f'<div class="warnbox">Sign in to download or request access. <a href="/login?next=/datasets/{e(asset_id)}">Sign in</a></div>'
        elif acl.effective_access(a) == "internal":
            data_head = '<div class="warnbox">Files are available to ICRISAT staff only. Ask the owner to share it with you.</div>'
        else:
            mine = cat.list_requests(asset_id=asset_id, email=email)
            latest = mine[0] if mine else None
            form = (f'<form method="post" action="/datasets/{e(asset_id)}/request" class="row">'
                    f'<label style="flex:1">Why do you need it?<input type="text" name="purpose" required maxlength="500"></label>'
                    f'<button class="btn">Request access</button></form>')
            if latest and latest["status"] == "pending":
                data_head = '<div class="warnbox">Access requested — the owner has been notified.</div>'
            elif latest and latest["status"] == "rejected":
                data_head = (f'<div class="errbox">Your request was declined: {e(latest.get("reason") or "no reason given")}</div>' + form)
            else:
                data_head = f'<div class="warnbox">This dataset is restricted.</div>{form}'

        body = f"""<div><h2 class="page">{e(a["title"])}</h2><div class="chips">{badge(a["status"])}{access_badge(a.get("access"))}
<span class="tag">{e(a.get("category") or "no category")}</span></div></div>
{"".join(parts)}
<div class="two"><article class="panel"><div class="panel-head"><h3>About this dataset</h3></div><dl class="d-meta">{dl}</dl></article>
<article class="panel"><div class="panel-head"><h3>Data</h3></div><div class="stack">{data_head}{"".join(schema)}</div></article></div>"""

        if manage:
            body += share_panel(a, grants, cat.list_requests(asset_id=asset_id))
        history = "".join(
            f'<li>{e(ev["at"][:16].replace("T", " "))} · <b>{e(ev["actor"] or "system")}</b> {e(ev["action"])}'
            + (f' → {badge(ev["to_status"])}' if ev.get("to_status") and ev["to_status"] != ev.get("from_status") else "")
            + (f' — {e(ev["comment"])}' if ev.get("comment") else "") + "</li>"
            for ev in events if ev["action"] != "downloaded")
        downloads = [ev for ev in events if ev["action"] == "downloaded"]
        people = {ev["actor"] for ev in downloads if ev["actor"]}
        if manage:
            body += (f'<article class="panel"><div class="panel-head"><h3>History</h3>'
                     f'<p class="panel-sub">{len(downloads)} downloads by {len(people)} signed-in people'
                     f'{" + anonymous" if any(not ev["actor"] for ev in downloads) else ""} · '
                     f'<a href="/datasets/{e(asset_id)}/access-report.csv">Access report (CSV)</a></p></div>'
                     f'<ul class="list">{history}</ul></article>')
        return page(request, a["title"], body)

    def share_panel(a: dict[str, Any], grants: list[dict[str, Any]], reqs: list[dict[str, Any]]) -> str:
        aid = e(a["asset_id"])
        access_opts = "".join(f'<option value="{k}"{" selected" if a.get("access") == k else ""}>{e(v)}</option>'
                              for k, v in ACCESS.items())
        glist = "".join(
            f'<li class="row" style="justify-content:space-between"><span>{e(g["principal"])}'
            f'{" · until " + e(g["expires_at"]) if g.get("expires_at") else ""} <span class="muted">by {e(g["granted_by"])}</span></span>'
            f'<form method="post" action="/datasets/{aid}/grants/revoke"><input type="hidden" name="principal" value="{e(g["principal"])}">'
            f'<button class="btn-link">Remove</button></form></li>' for g in grants)
        pending = "".join(
            f'<li><b>{e(r["email"])}</b> — {e(r["purpose"])} <span class="muted">{e(r["created_at"][:10])}</span>'
            f'<div class="row"><form method="post" action="/requests/{e(r["request_id"])}"><input type="hidden" name="decision" value="accept">'
            f'<button class="btn">Accept</button></form>'
            f'<form method="post" action="/requests/{e(r["request_id"])}"><input type="hidden" name="decision" value="reject">'
            f'<input type="text" name="reason" maxlength="200" placeholder="Reason (shown to requester)"><button class="btn btn-quiet">Decline</button></form></div></li>'
            for r in reqs if r["status"] == "pending")
        return f"""<article class="panel panel-hero"><div class="panel-head"><h3>Share</h3>
<p class="panel-sub">General access applies once published. People and domains below can always download.</p></div>
<div class="stack">
<form method="post" action="/datasets/{aid}/access" class="row">
<label>General access<select name="access">{access_opts}</select></label>
<label>Reason (if restricted)<select name="access_reason">{options(vocab("access_reasons"), a.get("access_reason"), blank="— none —")}</select></label>
<label>Restricted until<input type="date" name="embargo_until" value="{e(a.get("embargo_until"))}"></label>
<button class="btn btn-quiet">Save access</button></form>
<form method="post" action="/datasets/{aid}/grants" enctype="multipart/form-data" class="stack">
<label>Add people <small>— emails or whole domains like @cgiar.org; paste a list separated by commas or new lines</small>
<textarea name="principals" rows="2" placeholder="name@icrisat.org, partner@university.edu, @cgiar.org"></textarea></label>
<div class="row"><label>…or a CSV/text file of emails<input type="file" name="csv" accept=".csv,.txt"></label>
<label>Expires <small>(optional)</small><input type="date" name="expires_at"></label>
<label class="check"><input type="checkbox" name="notify" checked><span>Email them</span></label>
<button class="btn">Add</button></div></form>
<div><b>Has access ({len(grants)})</b><ul class="list">{glist or '<li class="muted">Nobody added yet.</li>'}</ul></div>
<div><b>Access requests</b><ul class="list">{pending or '<li class="muted">No pending requests.</li>'}</ul></div>
</div></article>"""

    @app.get("/datasets/{asset_id}/edit", response_class=HTMLResponse)
    def edit_page(request: Request, asset_id: str):
        cat, a, email = load(asset_id, request)
        if not email:
            return to_login(request)
        if a is None:
            return not_found(request)
        curator = acl.can_curate(P(), email, a)
        if not (curator or (acl.is_owner(email, a) and a["status"] in ("needs_review", "returned"))):
            return deny(request, "This dataset can't be edited right now (it is in review or published).")
        return page(request, "Edit " + a["title"], f"""<article class="panel panel-hero"><div class="panel-head"><h3>Edit details — {e(a["title"])}</h3></div>
<form method="post" action="/datasets/{e(asset_id)}/edit" class="stack">
<input type="hidden" name="owner" value="{e(a.get("owner"))}">{meta_form(a, teams(), curator, upload=False)}
<div class="row"><button class="btn">Save</button><a href="/datasets/{e(asset_id)}">Cancel</a></div></form></article>""")

    @app.post("/datasets/{asset_id}/edit")
    async def edit(request: Request, asset_id: str):
        cat, a, email = load(asset_id, request)
        if not email:
            return to_login(request)
        if a is None:
            return not_found(request)
        curator = acl.can_curate(P(), email, a)
        if not (curator or (acl.is_owner(email, a) and a["status"] in ("needs_review", "returned"))):
            return deny(request, "This dataset can't be edited right now (it is in review or published).")
        meta, errors = read_form(await request.form(), curator)
        if errors:
            return page(request, "Edit", "".join(f'<div class="errbox">{e(x)}</div>' for x in errors), 400)
        cat.update_metadata(asset_id, meta, actor=email)
        after = cat.get_asset(asset_id, with_tables=False)
        if after["status"] == "submitted" and a["status"] == "needs_review":
            notify.send(acl.curators_for(P(), after.get("category")), f"Review requested: {after['title']}",
                        f"Review it: {notify.link('/datasets/' + asset_id)}")
        if after["status"] == "published":
            sync_in_background()
        return back(asset_id, "Saved.")

    @app.post("/datasets/{asset_id}/action")
    async def action(request: Request, asset_id: str):
        cat, a, email = load(asset_id, request)
        if not email:
            return to_login(request)
        if a is None:
            return not_found(request)
        form = await request.form()
        act, comment = str(form.get("action") or ""), str(form.get("comment") or "").strip()[:2000]
        curator, owner = acl.can_curate(P(), email, a), acl.is_owner(email, a)
        allowed = {"submit": owner or curator, "withdraw": owner or curator,
                   "publish": curator, "return": curator}.get(act, False)
        if not allowed:
            return deny(request)
        try:
            new = cat.transition(asset_id, act, email, comment or None)
        except ValueError as exc:
            return deny(request, str(exc), 400)
        link = notify.link(f"/datasets/{asset_id}")
        if new == "submitted":
            notify.send(acl.curators_for(P(), a.get("category")), f"Review requested: {a['title']}", f"Review it: {link}")
        elif act == "publish":
            notify.send([a.get("owner_email")], f"Published: {a['title']}", f"Your dataset is now published: {link}")
        elif act == "return":
            notify.send([a.get("owner_email")], f"Changes requested: {a['title']}",
                        f"{email} returned your dataset:\n\n{comment}\n\nEdit it: {link}/edit")
        if act in ("publish", "withdraw"):
            sync_in_background()  # refresh the public dashboard
        return back(asset_id, f"Status: {STATUS[new][0]}.")

    # ------------------------------------------------------------ sharing
    @app.post("/datasets/{asset_id}/access")
    async def set_access(request: Request, asset_id: str):
        cat, a, email = load(asset_id, request)
        if a is None or not acl.can_manage(P(), email, a):
            return deny(request)
        form = await request.form()
        meta, errors = read_form({k: form.get(k) for k in ("access", "access_reason", "embargo_until")}, False)
        meta.pop("contains_pii", None)
        if errors:
            return deny(request, " ".join(errors), 400)
        if meta.get("access") == "restricted" and not meta.get("access_reason"):
            return deny(request, "Choose a reason for restricting access.", 400)
        cat.update_metadata(asset_id, meta, actor=email)
        if a["status"] == "published":
            sync_in_background()
        return back(asset_id, "Access updated.")

    @app.post("/datasets/{asset_id}/grants")
    async def add_grants(request: Request, asset_id: str):
        cat, a, email = load(asset_id, request)
        if a is None or not acl.can_manage(P(), email, a):
            return deny(request)
        form = await request.form()
        text = str(form.get("principals") or "")
        upload = form.get("csv")
        if getattr(upload, "filename", None):
            text += "\n" + (await upload.read(MB)).decode("utf-8", "replace")
        valid, invalid = acl.parse_principals(text)
        expires = str(form.get("expires_at") or "") or None
        if expires:
            try:
                date.fromisoformat(expires)
            except ValueError:
                return deny(request, "Expiry must be a date.", 400)
        added = cat.add_grants(asset_id, valid, email, expires) if valid else 0
        if form.get("notify") and valid:
            notify.send([p for p in valid if not p.startswith("@")], f"Shared with you: {a['title']}",
                        f"{email} gave you access to \"{a['title']}\": {notify.link('/datasets/' + asset_id)}")
        msg = f"{added} added, {len(invalid)} invalid" + (f": {', '.join(invalid[:10])}" if invalid else "")
        return back(asset_id, msg)

    @app.post("/datasets/{asset_id}/grants/revoke")
    async def revoke(request: Request, asset_id: str):
        cat, a, email = load(asset_id, request)
        if a is None or not acl.can_manage(P(), email, a):
            return deny(request)
        form = await request.form()
        cat.revoke_grant(asset_id, str(form.get("principal") or ""), email)
        return back(asset_id, "Access removed.")

    @app.post("/datasets/{asset_id}/request")
    async def request_access(request: Request, asset_id: str):
        cat, a, email = load(asset_id, request)
        if not email:
            return to_login(request)
        if a is None or a["status"] != "published":
            return not_found(request)
        purpose = str((await request.form()).get("purpose") or "").strip()[:500]
        if not purpose:
            return deny(request, "Tell the owner why you need the data.", 400)
        cat.request_access(asset_id, email, purpose)
        notify.send([a.get("owner_email"), *acl.curators_for(P(), a.get("category"))],
                    f"Access request: {a['title']}",
                    f"{email} asked for access:\n\n{purpose}\n\nDecide: {notify.link('/datasets/' + asset_id)}")
        return back(asset_id, "Request sent.")

    @app.post("/requests/{request_id}")
    async def decide(request: Request, request_id: str):
        cat, email = catalog(), who(request)
        req = cat.get_request(request_id)
        a = cat.get_asset(req["asset_id"], with_tables=False) if req else None
        if a is None or not acl.can_manage(P(), email, a):
            return deny(request)
        form = await request.form()
        accept = form.get("decision") == "accept"
        try:
            cat.decide_request(request_id, accept, email, str(form.get("reason") or "").strip()[:200] or None)
        except ValueError as exc:
            return deny(request, str(exc), 400)
        notify.send([req["email"]], f"Access {'granted' if accept else 'declined'}: {a['title']}",
                    notify.link(f"/datasets/{a['asset_id']}"))
        return back(a["asset_id"], "Request accepted." if accept else "Request declined.")

    @app.get("/datasets/{asset_id}/download")
    def download(request: Request, asset_id: str):
        cat, a, email = load(asset_id, request)
        if a is None:
            return not_found(request)
        if not acl.can_download(P(), email, a, cat.list_grants(asset_id)):
            return deny(request, "You don't have access to these files.")
        path = Path(a["file_path"])
        path = path if path.is_absolute() else cfg.root / path
        if not path.exists():
            return not_found(request)
        cat.record_download(asset_id, email)
        return FileResponse(path, filename=a["file_name"])

    @app.get("/datasets/{asset_id}/access-report.csv")
    def access_report(request: Request, asset_id: str):
        """Who can get the data and who did (Hugging Face-style access report)."""
        cat, a, email = load(asset_id, request)
        if a is None or not acl.can_manage(P(), email, a):
            return deny(request)
        out = io.StringIO()
        w = csv.writer(out)
        w.writerow(["kind", "who", "detail", "by", "at"])
        for g in cat.list_grants(asset_id):
            w.writerow(["grant", g["principal"], f"expires {g['expires_at']}" if g["expires_at"] else "",
                        g["granted_by"], g["granted_at"]])
        for r in cat.list_requests(asset_id=asset_id):
            w.writerow([f"request {r['status']}", r["email"], r["purpose"] or "", r["decided_by"] or "",
                        r["created_at"]])
        for ev in cat.events(asset_id):
            if ev["action"] == "downloaded":
                w.writerow(["download", ev["actor"] or "anonymous", "", "", ev["at"]])
        return Response(out.getvalue(), media_type="text/csv", headers={
            "Content-Disposition": f'attachment; filename="{asset_id}_access_report.csv"'})

    return app


def main() -> None:
    import uvicorn

    cfg = load_config()
    # Cloud hosts (Railway/Render/Fly) inject PORT; fall back to hub.yaml.
    port = int(os.environ.get("PORT") or cfg.get("web", "port", default=8010))
    uvicorn.run(create_app(cfg), host=cfg.get("web", "host", default="0.0.0.0"), port=port,
                proxy_headers=True)


if __name__ == "__main__":
    main()
