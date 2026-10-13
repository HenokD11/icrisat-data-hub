"""End-to-end: real web server, real HTTP, several signed-in users.

Ways the upload → review → access flow can fail (each is checked below):

 F1  anonymous visitor can upload                        -> must be sent to /login
 F2  anonymous visitor sees an unpublished dataset       -> 404
 F3  complete upload auto-publishes, skipping review     -> must be 'submitted'
 F4  incomplete upload can be submitted                  -> submit refused until complete
 F5  uploader can publish their own dataset              -> 403
 F6  curator of another category can publish it          -> 403
 F7  "return to uploader" accepted without a reason      -> 400
 F8  uploader can edit while the dataset is in review    -> 403
 F9  oversize upload accepted, or leaves a partial file  -> 413, staging empty
 F10 web upload written into the watched inbox (race)    -> inbox untouched
 F11 two same-named files overwrite each other on disk   -> both files kept
 F12 restricted data downloadable without a grant        -> 403; grant -> 200; revoke -> 403
 F13 domain grant (@cgiar.org) ignored                   -> matching user gets 200
 F14 internal data downloadable by a non-org account     -> 403; @icrisat.org -> 200
 F15 bulk grant silently drops/duplicates bad emails     -> added/invalid counts reported
 F16 access request can't be accepted / rejection hidden -> accept grants; reason shown
 F17 public API or Pages export leaks non-open/unpublished assets
 F18 personal-data checkbox doesn't force 'restricted'
 F19 HTML in metadata rendered unescaped (stored XSS)
 F20 tampered session cookie accepted
 F21 open redirect via ?next=
 F22 dev login available when HUB_DEV_LOGIN is not set
 F23 server filesystem paths leak into pages
 F24 non-admin can open the admin page                   -> 403
 F25 curator added in the admin UI gets no rights        -> sees queue at once
 F26 removed curator keeps rights                        -> publish refused
 F27 config admins removable from the UI (lockout)       -> 400
 F28 category added in the UI unusable                   -> offered on upload
 F29 downloads not recorded / access report leaks        -> report lists them; others 403
 F30 API tokens: stored in plain text, bearer upload not JSON,
     bad or revoked token accepted                       -> hashed; JSON; 401

Artifact: tests/artifacts/e2e_web_report.json — every check with its observed
value, rewritten on each run (repeatable; diff it between runs).
"""

from __future__ import annotations

import copy
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from http.cookiejar import CookieJar
from pathlib import Path
from urllib import error, parse, request

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "tests" / "artifacts" / "e2e_web_report.json"

ADMIN = "admin@icrisat.org"
SOIL_CURATOR = "soil.curator@icrisat.org"
CLIMATE_CURATOR = "climate.curator@icrisat.org"
ALICE = "alice@icrisat.org"      # contributor
BOB = "bob@icrisat.org"          # colleague (org domain)
PARTNER = "partner@cgiar.org"    # external, domain-granted
EVE = "eve@example.com"          # external, no grants


# ----------------------------------------------------------------- harness
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _write_config(root: Path) -> Path:
    raw = yaml.safe_load((ROOT / "config" / "hub.yaml").read_text(encoding="utf-8"))
    raw["ingest"]["settle_seconds"] = 0
    raw["ingest"]["max_upload_mb"] = 1
    raw["access_control"] = {
        "org_domains": ["icrisat.org"],
        "admins": [ADMIN],
        "categories": {
            "Soil & Agronomy": [SOIL_CURATOR],
            "Climate & Weather": [CLIMATE_CURATOR],
            "Other": [],
        },
    }
    cfg = root / "config" / "hub.yaml"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return cfg


def _start(cfg_path: Path, dev_login: bool) -> tuple[subprocess.Popen, str]:
    port = _free_port()
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "HUB_CONFIG": str(cfg_path),
           "PORT": str(port), "HUB_SECRET_KEY": "e2e-secret"}
    env.pop("HUB_DEV_LOGIN", None)
    env.pop("HUB_DATA_DIR", None)
    env.pop("WORKOS_API_KEY", None)
    env.pop("GITHUB_TOKEN", None)
    if dev_login:
        env["HUB_DEV_LOGIN"] = "1"
    log = open(cfg_path.parents[1] / "server.log", "ab")  # a pipe nobody reads would fill and block
    proc = subprocess.Popen([sys.executable, "-m", "hub.web.app"], env=env, cwd=ROOT,
                            stdout=log, stderr=subprocess.STDOUT)
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            request.urlopen(base + "/healthz", timeout=1)
            return proc, base
        except Exception:
            if proc.poll() is not None:
                raise RuntimeError((cfg_path.parents[1] / "server.log").read_text(errors="replace"))
            time.sleep(0.2)
    proc.kill()
    raise RuntimeError("server did not start")


class NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Client:
    def __init__(self, base: str, email: str | None = None, follow: bool = True):
        self.base = base
        self.jar = CookieJar()
        handlers = [request.HTTPCookieProcessor(self.jar)]
        if not follow:
            handlers.append(NoRedirect())
        self.opener = request.build_opener(*handlers)
        if email:
            status, _, url = self.post("/login", {"email": email, "name": email.split("@")[0]})
            assert status == 200 and "/login" not in url, (status, url)

    def _open(self, req):
        try:
            with self.opener.open(req, timeout=30) as r:
                return r.status, r.read().decode("utf-8", "replace"), r.geturl()
        except error.HTTPError as e:
            return e.code, e.read().decode("utf-8", "replace"), e.geturl()

    def post_location(self, path: str, fields: dict) -> str:
        """POST without following the redirect; return its Location header."""
        req = request.Request(self.base + path, data=parse.urlencode(fields).encode())
        try:
            self.opener.open(req, timeout=30)
            return ""
        except error.HTTPError as e:
            return e.headers.get("location", "")

    def get(self, path: str):
        return self._open(request.Request(self.base + path))

    def post(self, path: str, fields: dict, files: list[tuple[str, str, bytes]] | None = None,
             headers: dict | None = None):
        if files is None:
            data = parse.urlencode(fields, doseq=True).encode()
            req = request.Request(self.base + path, data=data, headers=headers or {})
        else:
            boundary = uuid.uuid4().hex
            parts = []
            for k, v in fields.items():
                for item in (v if isinstance(v, list) else [v]):
                    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{item}\r\n'.encode())
            for field, name, content in files:
                parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{name}"\r\n'
                             f"Content-Type: application/octet-stream\r\n\r\n".encode() + content + b"\r\n")
            parts.append(f"--{boundary}--\r\n".encode())
            req = request.Request(self.base + path, data=b"".join(parts),
                                  headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                                           **(headers or {})})
        return self._open(req)


CSV = b"woreda,crop,yield_kg_ha\nAdama,teff,1830\nHawassa,maize,3100\n"

COMPLETE = {
    "title": "Teff yield trials 2025",
    "description": "On-farm teff trials",
    "category": "Soil & Agronomy",
    "team": "Soil Intelligence",
    "license": "CC-BY-4.0",
    "access": "open",
}


REPORT: dict[str, object] = {}


def check(key: str, ok: bool, observed):
    REPORT[key] = {"ok": bool(ok), "observed": observed}
    assert ok, (key, observed)


def _ids(html: str) -> list[str]:
    return list(dict.fromkeys(re.findall(r"/datasets/(asset_[0-9a-f]{12})", html)))


# ------------------------------------------------------------------- tests
@pytest.fixture(scope="module")
def hub(tmp_path_factory):
    root = tmp_path_factory.mktemp("hub")
    cfg = _write_config(root)
    proc, base = _start(cfg, dev_login=True)
    yield {"base": base, "root": root, "cfg": cfg}
    proc.kill()
    ARTIFACT.parent.mkdir(exist_ok=True)
    ARTIFACT.write_text(json.dumps(REPORT, indent=2), encoding="utf-8")


def test_upload_review_access_flow(hub):
    base, root = hub["base"], hub["root"]
    anon = Client(base)
    alice, bob = Client(base, ALICE), Client(base, BOB)
    soil, climate, admin = Client(base, SOIL_CURATOR), Client(base, CLIMATE_CURATOR), Client(base, ADMIN)
    partner, eve = Client(base, PARTNER), Client(base, EVE)

    # F1
    _, _, url = anon.get("/upload")
    check("F1_anon_upload_redirects_to_login", "/login" in url, url)

    # F3 + F10 + F11: two complete uploads with the same file name, different content
    st, html, _ = alice.post("/upload", COMPLETE, files=[("files", "trial.csv", CSV)])
    first = _ids(html)
    st2, html2, _ = alice.post("/upload", {**COMPLETE, "title": "Teff trials B"},
                               files=[("files", "trial.csv", CSV + b"Bahir Dar,teff,1700\n")])
    second = _ids(html2)
    check("F3_upload_ok", st == 200 and len(first) == 1 and len(second) == 1, [st, first, second])
    ds = first[0]
    _, page, _ = alice.get(f"/datasets/{ds}")
    check("F3_complete_upload_is_submitted_not_published", "In review" in page and "Published" not in page,
          re.findall(r'class="badge [^"]*">([^<]+)<', page))
    inbox_files = [p.name for p in (root / "data" / "inbox").iterdir() if p.name != ".gitkeep"]
    check("F10_inbox_untouched", inbox_files == [], inbox_files)
    stored = sorted(p.name for p in (root / "data" / "processed").rglob("trial*.csv"))
    check("F11_same_name_files_both_kept", len(stored) == 2, stored)

    # F2
    st, _, _ = anon.get(f"/datasets/{ds}")
    check("F2_anon_cannot_see_unpublished", st == 404, st)

    # F8
    st, _, _ = alice.post(f"/datasets/{ds}/edit", {**COMPLETE, "title": "changed"})
    check("F8_owner_cannot_edit_in_review", st == 403, st)

    # F5, F6, F7
    st, _, _ = alice.post(f"/datasets/{ds}/action", {"action": "publish"})
    check("F5_owner_cannot_publish", st == 403, st)
    st, _, _ = climate.post(f"/datasets/{ds}/action", {"action": "publish"})
    check("F6_other_category_curator_cannot_publish", st in (403, 404), st)  # 404: cannot even see it
    st, _, _ = soil.post(f"/datasets/{ds}/action", {"action": "return", "comment": ""})
    check("F7_return_needs_reason", st == 400, st)

    # review queue shows it to the right curator only
    _, q_soil, _ = soil.get("/review")
    _, q_climate, _ = climate.get("/review")
    check("queue_routed_by_category", ds in q_soil and ds not in q_climate,
          {"soil": ds in q_soil, "climate": ds in q_climate})

    # return -> owner sees reason -> edits -> resubmits -> curator publishes
    st, _, _ = soil.post(f"/datasets/{ds}/action", {"action": "return", "comment": "Add the collection period please"})
    _, page, _ = alice.get(f"/datasets/{ds}")
    check("return_reason_shown_to_owner", st == 200 and "Add the collection period please" in page, st)
    st, _, _ = alice.post(f"/datasets/{ds}/edit", {**COMPLETE, "temporal_coverage": "2025"})
    check("owner_can_edit_when_returned", st == 200, st)
    alice.post(f"/datasets/{ds}/action", {"action": "submit"})
    st, _, _ = soil.post(f"/datasets/{ds}/action", {"action": "publish"})
    _, page, _ = anon.get(f"/datasets/{ds}")
    check("curator_publishes_then_public", st == 200 and "Published" in page, st)
    st, _, _ = anon.get(f"/datasets/{ds}/download")
    check("open_published_downloadable_by_anyone", st == 200, st)

    # F4: incomplete upload (no category / licence) cannot be submitted
    st, html, _ = alice.post("/upload", {"title": "Draft only"}, files=[("files", "draft.csv", CSV + b"x,y,1\n")])
    draft = _ids(html)[0]
    st, _, _ = alice.post(f"/datasets/{draft}/action", {"action": "submit"})
    check("F4_incomplete_cannot_submit", st == 400, st)

    # F9: oversize upload (cap is 1 MB in the test config)
    big = b"a,b\n" + b"1,2\n" * 300_000
    st, _, _ = alice.post("/upload", COMPLETE, files=[("files", "big.csv", big)])
    staging = [p.name for p in (root / "data" / "staging").rglob("*") if p.is_file()]
    check("F9_oversize_rejected_no_leftovers", st == 413 and staging == [], [st, staging])

    # F18 + F12..F16: restricted dataset with personal data
    st, html, _ = alice.post("/upload", {**COMPLETE, "title": "Household survey", "access": "open",
                                         "contains_pii": "on", "access_reason": "Contains personal data (PII)"},
                             files=[("files", "survey.csv", CSV + b"PII,row,9\n")])
    rs = _ids(html)[0]
    _, page, _ = alice.get(f"/datasets/{rs}")
    check("F18_pii_forces_restricted", "b-restricted" in page, "b-restricted" in page)
    soil.post(f"/datasets/{rs}/action", {"action": "publish"})

    st, _, _ = eve.get(f"/datasets/{rs}")
    check("restricted_metadata_still_visible", st == 200, st)
    st, _, _ = eve.get(f"/datasets/{rs}/download")
    check("F12_restricted_blocked_without_grant", st == 403, st)

    st, html, _ = alice.post(f"/datasets/{rs}/grants",
                             {"principals": f"{BOB}, not-an-email@, {BOB}\n@cgiar.org; bad@@x"})
    added = re.search(r"(\d+) added", html)
    invalid = re.search(r"(\d+) invalid", html)
    check("F15_bulk_grant_counts", added and added.group(1) == "2" and invalid and invalid.group(1) == "2",
          [added and added.group(0), invalid and invalid.group(0)])
    st, _, _ = bob.get(f"/datasets/{rs}/download")
    check("F12_grant_allows_download", st == 200, st)
    st, _, _ = partner.get(f"/datasets/{rs}/download")
    check("F13_domain_grant_works", st == 200, st)
    alice.post(f"/datasets/{rs}/grants/revoke", {"principal": BOB})
    st, _, _ = bob.get(f"/datasets/{rs}/download")
    check("F12_revoke_blocks_again", st == 403, st)

    # CSV bulk upload of grants
    st, html, _ = alice.post(f"/datasets/{rs}/grants", {"principals": ""},
                             files=[("csv", "people.csv", b"name,email\nBob,bob@icrisat.org\nEve,eve@example.com\n")])
    check("bulk_grant_from_csv", re.search(r"2 added", html) is not None, re.findall(r"\d+ added", html))
    alice.post(f"/datasets/{rs}/grants/revoke", {"principal": EVE})

    # F16: access request -> reject with reason -> request again -> accept
    st, _, _ = eve.post(f"/datasets/{rs}/request", {"purpose": "Regional meta-analysis"})
    _, page, _ = alice.get(f"/datasets/{rs}")
    rid = re.search(r"/requests/(req_[0-9a-f]{12})", page)
    check("F16_request_visible_to_owner", st == 200 and rid is not None, st)
    alice.post(f"/requests/{rid.group(1)}", {"decision": "reject", "reason": "Needs a signed DSA first"})
    _, page, _ = eve.get(f"/datasets/{rs}")
    check("F16_rejection_reason_shown", "Needs a signed DSA first" in page, "reason shown" if "Needs a signed DSA first" in page else page[:200])
    eve.post(f"/datasets/{rs}/request", {"purpose": "DSA signed"})
    _, page, _ = soil.get(f"/datasets/{rs}")
    rid = re.search(r"/requests/(req_[0-9a-f]{12})", page).group(1)
    soil.post(f"/requests/{rid}", {"decision": "accept"})
    st, _, _ = eve.get(f"/datasets/{rs}/download")
    check("F16_accept_grants_access", st == 200, st)

    # F14: internal dataset
    st, html, _ = alice.post("/upload", {**COMPLETE, "title": "Internal KPIs", "access": "internal"},
                             files=[("files", "kpi.csv", CSV + b"k,p,1\n")])
    internal = _ids(html)[0]
    admin.post(f"/datasets/{internal}/action", {"action": "publish"})
    st_out, _, _ = partner.get(f"/datasets/{internal}/download")
    st_in, _, _ = bob.get(f"/datasets/{internal}/download")
    check("F14_internal_org_only", st_out == 403 and st_in == 200, [st_out, st_in])

    # F19: stored XSS
    st, html, _ = alice.post("/upload", {**COMPLETE, "title": "<script>alert(1)</script>"},
                             files=[("files", "xss.csv", CSV + b"s,c,1\n")])
    xss = _ids(html)[0]
    _, page, _ = alice.get(f"/datasets/{xss}")
    check("F19_metadata_escaped", "<script>alert(1)</script>" not in page and "&lt;script&gt;" in page, "escaped")

    # F17: anonymous API + Pages export only carry published open assets
    _, api, _ = anon.get("/api/assets")
    listed = {a["asset_id"]: a for a in json.loads(api)}
    no_data = all(not a.get("tables") for i, a in listed.items() if i != ds)
    check("F17_api_only_published", set(listed) == {ds, rs, internal} and no_data, sorted(listed))
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "HUB_CONFIG": str(hub["cfg"])}
    subprocess.run([sys.executable, "-m", "hub.pages"], env=env, cwd=ROOT, check=True, capture_output=True)
    exported = json.loads((root / "docs" / "data" / "assets.json").read_text(encoding="utf-8"))
    check("F17_pages_only_published_open", [a["asset_id"] for a in exported["assets"]] == [ds],
          [a["asset_id"] for a in exported["assets"]])

    # F20 tampered cookie, F21 open redirect
    forged = Client(base)
    for c in alice.jar:
        c = copy.copy(c)
        c.value = c.value[:-2] + ("11" if c.value.endswith("00") else "00")
        forged.jar.set_cookie(c)
    _, _, url = forged.get("/upload")
    check("F20_tampered_cookie_rejected", "/login" in url, url)
    target = Client(base, follow=False).post_location("/login?next=//evil.example/x", {"email": BOB})
    check("F21_no_open_redirect", target.startswith("/") and not target.startswith("//"), target)

    # F23 no server paths
    pages = [alice.get(p)[1] for p in ("/", "/upload", "/mine", f"/datasets/{ds}")]
    leaked = [str(root) in p or "processed" in p for p in pages]
    check("F23_no_server_paths", not any(leaked), leaked)



def test_admin_roles_reports_and_tokens(hub):
    base, root = hub["base"], hub["root"]
    NEWCUR = "new.curator@icrisat.org"
    admin, alice, bob = Client(base, ADMIN), Client(base, ALICE), Client(base, BOB)
    newcur, eve, anon = Client(base, NEWCUR), Client(base, EVE), Client(base)

    # F24
    st, _, _ = alice.get("/admin")
    check("F24_non_admin_blocked", st == 403, st)

    # F28 category added in the UI
    admin.post("/admin/categories", {"name": "Livestock"})
    _, up, _ = alice.get("/upload")
    check("F28_new_category_on_upload", 'value="Livestock"' in up, 'value="Livestock"' in up)

    # F25 curator added in the UI (bulk box, one junk entry)
    _, html, _ = admin.post("/admin/curators", {"category": "Livestock", "emails": f"{NEWCUR}, junk@"})
    counts = re.findall(r"\d+ (?:added|invalid)", html)
    check("F25_curator_bulk_counts", counts[:2] == ["1 added", "1 invalid"], counts)
    _, html, _ = alice.post("/upload", {**COMPLETE, "category": "Livestock", "title": "Goat census"},
                            files=[("files", "goats.csv", CSV + b"goat,1,2\n")])
    goat = _ids(html)[0]
    _, q, _ = newcur.get("/review")
    check("F25_new_curator_sees_queue", goat in q, goat in q)

    # F26
    admin.post("/admin/curators/remove", {"category": "Livestock", "email": NEWCUR})
    st, _, _ = newcur.post(f"/datasets/{goat}/action", {"action": "publish"})
    check("F26_removed_curator_blocked", st in (403, 404), st)

    # F27 + UI-added admin works and can be removed
    st, _, _ = admin.post("/admin/admins/remove", {"email": ADMIN})
    check("F27_config_admin_not_removable", st == 400, st)
    admin.post("/admin/admins", {"email": BOB})
    st_in, _, _ = bob.get("/admin")
    admin.post("/admin/admins/remove", {"email": BOB})
    st_out, _, _ = bob.get("/admin")
    check("ui_admin_added_then_removed", [st_in, st_out] == [200, 403], [st_in, st_out])

    # F29 download log + access report
    admin.post(f"/datasets/{goat}/action", {"action": "publish"})
    eve.get(f"/datasets/{goat}/download")
    anon.get(f"/datasets/{goat}/download")
    st, csv_text, _ = alice.get(f"/datasets/{goat}/access-report.csv")
    check("F29_report_lists_downloads", st == 200 and EVE in csv_text and "anonymous" in csv_text,
          csv_text.splitlines()[:4])
    st, _, _ = eve.get(f"/datasets/{goat}/access-report.csv")
    check("F29_report_owner_only", st in (403, 404), st)

    # F30 API tokens
    _, html, _ = alice.post("/tokens", {"name": "laptop script"})
    token = re.search(r"(hub_[A-Za-z0-9_-]{30,})", html).group(1)
    tid = re.search(r"/tokens/(tok_[0-9a-f]{12})/revoke", html).group(1)
    import sqlite3
    con = sqlite3.connect(root / "data" / "catalog" / "catalog.db")
    dump = "\n".join(con.iterdump())
    con.close()
    check("F30_token_stored_hashed", token not in dump, "plaintext absent" if token not in dump else "LEAK")
    api = Client(base)
    bearer = {"Authorization": f"Bearer {token}"}
    st, body, _ = api.post("/upload", {**COMPLETE, "title": "From a script"},
                           files=[("files", "script.csv", CSV + b"api,1,1\n")], headers=bearer)
    res = json.loads(body)
    check("F30_bearer_upload_json", st == 200 and res[0]["status"] == "submitted", [st, res])
    _, page_, _ = admin.get(f"/datasets/{res[0]['asset_id']}")
    check("F30_token_upload_owned_by_user", ALICE in page_, ALICE in page_)
    st, _, _ = api.post("/upload", COMPLETE, files=[("files", "x.csv", CSV)],
                        headers={"Authorization": "Bearer hub_not-a-real-token"})
    check("F30_bad_token_401", st == 401, st)
    alice.post(f"/tokens/{tid}/revoke", {})
    st, _, _ = api.post("/upload", COMPLETE, files=[("files", "y.csv", CSV + b"y,1,1\n")], headers=bearer)
    check("F30_revoked_token_401", st == 401, st)


def test_dev_login_off_by_default(tmp_path):
    cfg = _write_config(tmp_path)
    proc, base = _start(cfg, dev_login=False)
    try:
        st, _, url = Client(base).post("/login", {"email": ADMIN})
        _, _, up = Client(base).get("/upload")
    finally:
        proc.kill()
    assert st in (403, 404), ("F22_dev_login_disabled", st)
    assert "/login" in up
