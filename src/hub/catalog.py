"""Catalogue store.

SQLite-backed asset catalogue. Every ingested file becomes one *asset* with
one or more *tables* (sheets for Excel; a single table for CSV; an extracted
document body for Word). The catalogue is also exported to
``data/catalog/assets.json`` — the hub equivalent of the CDH
``data/normalized/assets.json`` — so it can be published, diffed in git, and
consumed by anything that does not speak SQL.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import HubConfig, load_config

SCHEMA = """
CREATE TABLE IF NOT EXISTS assets (
    asset_id        TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    description     TEXT,
    team            TEXT,
    owner           TEXT,
    contact         TEXT,
    file_name       TEXT NOT NULL,
    file_path       TEXT NOT NULL,          -- current location (processed/...)
    file_type       TEXT NOT NULL,          -- csv | excel | word
    file_ext        TEXT NOT NULL,
    size_bytes      INTEGER,
    sha256          TEXT UNIQUE,            -- dedup key
    status          TEXT NOT NULL,          -- see STATUSES
    tags            TEXT,                   -- JSON array
    license         TEXT,
    access          TEXT,                   -- open | internal | restricted
    domain          TEXT,
    hub_role        TEXT,
    spatial_coverage TEXT,
    temporal_coverage TEXT,
    upload_channel  TEXT,                   -- inbox | web | sidecar
    missing_fields  TEXT,                   -- JSON array of metadata gaps
    ingest_log      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tables (
    table_id    TEXT PRIMARY KEY,
    asset_id    TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
    name        TEXT NOT NULL,              -- sheet name / 'data' / 'document'
    kind        TEXT NOT NULL,              -- table | document
    n_rows      INTEGER,
    n_cols      INTEGER,
    columns     TEXT,                       -- JSON: [{name, dtype, samples: []}]
    text_preview TEXT                       -- for word documents
);

-- Who may download a restricted dataset: an email or a whole '@domain'.
CREATE TABLE IF NOT EXISTS grants (
    asset_id    TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
    principal   TEXT NOT NULL,
    granted_by  TEXT,
    granted_at  TEXT NOT NULL,
    expires_at  TEXT,                       -- ISO date, NULL = no expiry
    PRIMARY KEY (asset_id, principal)
);

CREATE TABLE IF NOT EXISTS access_requests (
    request_id  TEXT PRIMARY KEY,
    asset_id    TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
    email       TEXT NOT NULL,
    purpose     TEXT,
    status      TEXT NOT NULL,              -- pending | accepted | rejected
    decided_by  TEXT,
    reason      TEXT,
    created_at  TEXT NOT NULL,
    decided_at  TEXT
);

-- Audit trail; doubles as the review comment thread.
CREATE TABLE IF NOT EXISTS events (
    event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_id    TEXT NOT NULL,
    actor       TEXT,
    action      TEXT NOT NULL,
    from_status TEXT,
    to_status   TEXT,
    comment     TEXT,
    at          TEXT NOT NULL
);

-- Roles granted in the admin UI, on top of access_control in hub.yaml.
-- role = 'admin' (category '') or 'curator' (category = its name).
CREATE TABLE IF NOT EXISTS roles (
    email       TEXT NOT NULL,
    role        TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT '',
    granted_by  TEXT,
    granted_at  TEXT NOT NULL,
    PRIMARY KEY (email, role, category)
);

CREATE TABLE IF NOT EXISTS categories (
    name        TEXT PRIMARY KEY,
    created_by  TEXT,
    created_at  TEXT NOT NULL
);

-- Personal API tokens for scripted uploads; only a SHA-256 of the token is kept.
CREATE TABLE IF NOT EXISTS api_tokens (
    token_id     TEXT PRIMARY KEY,
    token_hash   TEXT NOT NULL UNIQUE,
    email        TEXT NOT NULL,
    name         TEXT,
    created_at   TEXT NOT NULL,
    last_used_at TEXT,
    revoked_at   TEXT
);

CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(status);
CREATE INDEX IF NOT EXISTS idx_assets_team ON assets(team);
CREATE INDEX IF NOT EXISTS idx_tables_asset ON tables(asset_id);
CREATE INDEX IF NOT EXISTS idx_events_asset ON events(asset_id);
CREATE INDEX IF NOT EXISTS idx_requests_asset ON access_requests(asset_id);
"""

REVIEW_FIELDS = ("team", "owner", "description", "license", "access", "category")

# Review workflow (Dataverse / DSpace pattern):
#   needs_review --(fields complete)--> submitted --publish--> published
#   submitted --return (reason)--> returned --submit--> submitted
#   published --withdraw--> withdrawn --publish--> published
STATUSES = ("needs_review", "submitted", "returned", "published", "withdrawn")
TRANSITIONS = {
    ("needs_review", "submit"): "submitted",
    ("returned", "submit"): "submitted",
    ("submitted", "publish"): "published",
    ("submitted", "return"): "returned",
    ("published", "withdraw"): "withdrawn",
    ("withdrawn", "publish"): "published",
}

# Columns copied verbatim from an asset record on insert.
ASSET_COLUMNS = (
    "description", "team", "owner", "contact", "file_name", "file_path",
    "file_type", "file_ext", "size_bytes", "sha256", "license", "access",
    "domain", "hub_role", "spatial_coverage", "temporal_coverage",
    "upload_channel", "ingest_log", "category", "owner_email",
    "access_reason", "embargo_until", "contains_pii",
)
EDITABLE_FIELDS = {
    "title", "description", "team", "owner", "contact", "license", "access",
    "domain", "hub_role", "spatial_coverage", "temporal_coverage", "tags",
    "category", "owner_email", "access_reason", "embargo_until", "contains_pii",
}
ADDED_COLUMNS = {
    "upload_channel": "TEXT", "category": "TEXT", "owner_email": "TEXT",
    "access_reason": "TEXT", "embargo_until": "TEXT",
    "contains_pii": "INTEGER DEFAULT 0", "submitted_at": "TEXT",
}


def _missing(asset: dict[str, Any]) -> list[str]:
    missing = [f for f in REVIEW_FIELDS if not asset.get(f)]
    if asset.get("access") == "restricted" and not asset.get("access_reason"):
        missing.append("access_reason")
    return missing


def _apply_rules(fields: dict[str, Any]) -> dict[str, Any]:
    """Personal data is never open (CGIAR Research Ethics Code)."""
    if "contains_pii" in fields:
        fields["contains_pii"] = 1 if fields["contains_pii"] in (True, 1, "1", "true", "on", "yes") else 0
    if fields.get("contains_pii"):
        fields["access"] = "restricted"
        fields["access_reason"] = fields.get("access_reason") or "Contains personal data (PII)"
    return fields


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class Catalog:
    def __init__(self, config: HubConfig | None = None):
        self.config = config or load_config()
        self.db_path = self.config.path("catalog_db")
        self._init_db()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns introduced after the initial schema (idempotent)."""
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(assets)")}
        for name, sqltype in ADDED_COLUMNS.items():
            if name not in cols:
                conn.execute(f"ALTER TABLE assets ADD COLUMN {name} {sqltype}")

    # ------------------------------------------------------------------ write
    def has_hash(self, sha256: str) -> bool:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM assets WHERE sha256 = ?", (sha256,)
            ).fetchone()
        return row is not None

    def add_asset(self, asset: dict[str, Any], tables: list[dict[str, Any]]) -> str:
        asset_id = asset.get("asset_id") or new_id("asset")
        now = _now()
        asset = _apply_rules(dict(asset))
        missing = _missing(asset)
        # Complete metadata goes straight to a curator; nothing self-publishes.
        status = "submitted" if not missing else "needs_review"
        row = {c: asset.get(c) for c in ASSET_COLUMNS}
        row.update(
            asset_id=asset_id,
            title=asset.get("title") or "Untitled asset",
            status=status,
            tags=json.dumps(asset.get("tags") or []),
            missing_fields=json.dumps(missing),
            submitted_at=now if status == "submitted" else None,
            created_at=now,
            updated_at=now,
        )
        with self.connect() as conn:
            conn.execute(
                f"INSERT INTO assets ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})",
                tuple(row.values()),
            )
            for t in tables:
                conn.execute(
                    """
                    INSERT INTO tables (table_id, asset_id, name, kind, n_rows,
                                        n_cols, columns, text_preview)
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        new_id("tbl"),
                        asset_id,
                        t.get("name", "data"),
                        t.get("kind", "table"),
                        t.get("n_rows"),
                        t.get("n_cols"),
                        json.dumps(t.get("columns") or []),
                        t.get("text_preview"),
                    ),
                )
            self._event(conn, asset_id, asset.get("owner_email") or asset.get("upload_channel"),
                        "created", None, status)
        self.export_json()
        return asset_id

    def update_metadata(self, asset_id: str, updates: dict[str, Any], actor: str | None = None) -> bool:
        """Edit metadata (web form or completed review template).

        Status only moves needs_review -> submitted once the record is
        complete; publishing is always a curator's explicit transition.
        """
        fields = {k: v for k, v in updates.items() if k in EDITABLE_FIELDS and v is not None}
        if not fields:
            return False
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM assets WHERE asset_id = ?", (asset_id,)
            ).fetchone()
            if row is None:
                return False
            merged = {**dict(row), **fields}
            fields.update(_apply_rules({"contains_pii": merged.get("contains_pii"),
                                        "access": merged.get("access"),
                                        "access_reason": merged.get("access_reason")}))
            merged.update(fields)
            missing = _missing(merged)
            fields["missing_fields"] = json.dumps(missing)
            if row["status"] == "needs_review" and not missing:
                fields["status"] = "submitted"
                fields["submitted_at"] = _now()
            if "tags" in fields:
                fields["tags"] = json.dumps(fields["tags"])
            fields["updated_at"] = _now()
            assignments = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(
                f"UPDATE assets SET {assignments} WHERE asset_id = ?",
                (*fields.values(), asset_id),
            )
            self._event(conn, asset_id, actor, "edited", row["status"],
                        fields.get("status", row["status"]))
        self.export_json()
        return True

    def transition(self, asset_id: str, action: str, actor: str | None,
                   comment: str | None = None) -> str:
        """Move an asset through the review workflow. Raises ValueError."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM assets WHERE asset_id = ?", (asset_id,)
            ).fetchone()
            if row is None:
                raise ValueError("asset not found")
            target = TRANSITIONS.get((row["status"], action))
            if target is None:
                raise ValueError(f"cannot {action} a dataset that is {row['status']}")
            if action == "submit" and json.loads(row["missing_fields"] or "[]"):
                raise ValueError("complete the required fields before submitting")
            if action == "return" and not (comment or "").strip():
                raise ValueError("a reason is required when returning a dataset")
            now = _now()
            conn.execute(
                "UPDATE assets SET status = ?, updated_at = ?, "
                "submitted_at = CASE WHEN ? = 'submitted' THEN ? ELSE submitted_at END "
                "WHERE asset_id = ?",
                (target, now, target, now, asset_id),
            )
            self._event(conn, asset_id, actor, action, row["status"], target, comment)
        self.export_json()
        return target

    # ------------------------------------------------------------ access
    def add_grants(self, asset_id: str, principals: list[str], by: str,
                   expires_at: str | None = None) -> int:
        with self.connect() as conn:
            existing = {r["principal"] for r in conn.execute(
                "SELECT principal FROM grants WHERE asset_id = ?", (asset_id,))}
            for p in principals:
                conn.execute(
                    "INSERT OR REPLACE INTO grants VALUES (?,?,?,?,?)",
                    (asset_id, p.lower(), by, _now(), expires_at or None),
                )
            self._event(conn, asset_id, by, "granted", None, None, ", ".join(principals))
        return len({p.lower() for p in principals} - existing)

    def revoke_grant(self, asset_id: str, principal: str, by: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM grants WHERE asset_id = ? AND principal = ?",
                         (asset_id, principal.lower()))
            self._event(conn, asset_id, by, "revoked", None, None, principal)

    def list_grants(self, asset_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM grants WHERE asset_id = ? ORDER BY principal", (asset_id,))]

    def request_access(self, asset_id: str, email: str, purpose: str) -> str:
        with self.connect() as conn:
            pending = conn.execute(
                "SELECT request_id FROM access_requests WHERE asset_id = ? AND email = ? "
                "AND status = 'pending'", (asset_id, email.lower())).fetchone()
            if pending:
                return pending["request_id"]
            rid = new_id("req")
            conn.execute(
                "INSERT INTO access_requests (request_id, asset_id, email, purpose, status, created_at) "
                "VALUES (?,?,?,?,?,?)", (rid, asset_id, email.lower(), purpose, "pending", _now()))
        return rid

    def list_requests(self, asset_id: str | None = None, email: str | None = None) -> list[dict[str, Any]]:
        query, params = "SELECT * FROM access_requests WHERE 1=1", []
        if asset_id:
            query += " AND asset_id = ?"
            params.append(asset_id)
        if email:
            query += " AND email = ?"
            params.append(email.lower())
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(query + " ORDER BY created_at DESC, rowid DESC", params)]

    def get_request(self, request_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            r = conn.execute("SELECT * FROM access_requests WHERE request_id = ?", (request_id,)).fetchone()
        return dict(r) if r else None

    def decide_request(self, request_id: str, accept: bool, by: str, reason: str | None = None) -> dict[str, Any]:
        req = self.get_request(request_id)
        if req is None or req["status"] != "pending":
            raise ValueError("request not found or already decided")
        status = "accepted" if accept else "rejected"
        with self.connect() as conn:
            conn.execute(
                "UPDATE access_requests SET status = ?, decided_by = ?, reason = ?, decided_at = ? "
                "WHERE request_id = ?", (status, by, (reason or "")[:500], _now(), request_id))
        if accept:
            self.add_grants(req["asset_id"], [req["email"]], by)
        return {**req, "status": status, "reason": reason}

    def record_download(self, asset_id: str, email: str | None) -> None:
        with self.connect() as conn:
            self._event(conn, asset_id, email, "downloaded", None, None)

    # ------------------------------------------------------------- admin
    ADMIN_LOG = "_admin"  # events.asset_id for role/category changes

    def roles(self) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM roles ORDER BY role, category, email")]

    def add_roles(self, emails: list[str], role: str, category: str, by: str) -> int:
        with self.connect() as conn:
            before = conn.total_changes
            for email in emails:
                conn.execute("INSERT OR IGNORE INTO roles VALUES (?,?,?,?,?)",
                             (email.lower(), role, category, by, _now()))
            added = conn.total_changes - before
            self._event(conn, self.ADMIN_LOG, by, f"add {role}", None, None,
                        f"{category}: {', '.join(emails)}" if category else ", ".join(emails))
        return added

    def remove_role(self, email: str, role: str, category: str, by: str) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM roles WHERE email = ? AND role = ? AND category = ?",
                         (email.lower(), role, category))
            self._event(conn, self.ADMIN_LOG, by, f"remove {role}", None, None,
                        f"{category}: {email}" if category else email)

    def extra_categories(self) -> list[str]:
        with self.connect() as conn:
            return [r["name"] for r in conn.execute("SELECT name FROM categories ORDER BY name")]

    def add_category(self, name: str, by: str) -> None:
        with self.connect() as conn:
            conn.execute("INSERT OR IGNORE INTO categories VALUES (?,?,?)", (name, by, _now()))
            self._event(conn, self.ADMIN_LOG, by, "add category", None, None, name)

    def remove_category(self, name: str, by: str) -> None:
        with self.connect() as conn:
            if conn.execute("SELECT 1 FROM assets WHERE category = ?", (name,)).fetchone():
                raise ValueError(f"'{name}' still has datasets; move them to another category first")
            conn.execute("DELETE FROM categories WHERE name = ?", (name,))
            conn.execute("DELETE FROM roles WHERE role = 'curator' AND category = ?", (name,))
            self._event(conn, self.ADMIN_LOG, by, "remove category", None, None, name)

    # ------------------------------------------------------------ tokens
    def create_token(self, email: str, name: str) -> tuple[str, str]:
        """Returns (token_id, token). The token itself is never stored."""
        token = "hub_" + secrets.token_urlsafe(32)
        token_id = new_id("tok")
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO api_tokens (token_id, token_hash, email, name, created_at) VALUES (?,?,?,?,?)",
                (token_id, hashlib.sha256(token.encode()).hexdigest(), email.lower(), name[:100], _now()))
        return token_id, token

    def token_email(self, token: str) -> str | None:
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.connect() as conn:
            row = conn.execute("SELECT token_id, email FROM api_tokens WHERE token_hash = ? "
                               "AND revoked_at IS NULL", (digest,)).fetchone()
            if row is None:
                return None
            conn.execute("UPDATE api_tokens SET last_used_at = ? WHERE token_id = ?", (_now(), row["token_id"]))
        return row["email"]

    def list_tokens(self, email: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT token_id, name, created_at, last_used_at, revoked_at FROM api_tokens "
                "WHERE email = ? ORDER BY created_at DESC", (email.lower(),))]

    def revoke_token(self, token_id: str, email: str) -> None:
        with self.connect() as conn:
            conn.execute("UPDATE api_tokens SET revoked_at = ? WHERE token_id = ? AND email = ?",
                         (_now(), token_id, email.lower()))

    def events(self, asset_id: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM events WHERE asset_id = ? ORDER BY event_id", (asset_id,))]

    @staticmethod
    def _event(conn, asset_id, actor, action, from_status, to_status, comment=None) -> None:
        conn.execute(
            "INSERT INTO events (asset_id, actor, action, from_status, to_status, comment, at) "
            "VALUES (?,?,?,?,?,?,?)",
            (asset_id, actor, action, from_status, to_status, comment, _now()))

    # ------------------------------------------------------------------- read
    def get_asset(self, asset_id: str, with_tables: bool = True) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM assets WHERE asset_id = ?", (asset_id,)
            ).fetchone()
            if row is None:
                return None
            asset = self._row_to_asset(row)
            if with_tables:
                asset["tables"] = [
                    self._row_to_table(r)
                    for r in conn.execute(
                        "SELECT * FROM tables WHERE asset_id = ? ORDER BY name",
                        (asset_id,),
                    )
                ]
        return asset

    def list_assets(
        self,
        status: str | None = None,
        team: str | None = None,
        file_type: str | None = None,
        tag: str | None = None,
        limit: int = 100,
        owner_email: str | None = None,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM assets WHERE 1=1"
        params: list[Any] = []
        if owner_email:
            query += " AND LOWER(owner_email) = LOWER(?)"
            params.append(owner_email)
        if status:
            query += " AND status = ?"
            params.append(status)
        if team:
            query += " AND LOWER(team) = LOWER(?)"
            params.append(team)
        if file_type:
            query += " AND file_type = ?"
            params.append(file_type)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with self.connect() as conn:
            assets = [self._row_to_asset(r) for r in conn.execute(query, params)]
        if tag:
            assets = [a for a in assets if tag.lower() in [t.lower() for t in a["tags"]]]
        return assets

    def search(self, query: str, limit: int = 25) -> list[dict[str, Any]]:
        """Full-text-ish search over title, description, team, and column names."""
        like = f"%{query.lower()}%"
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT DISTINCT a.* FROM assets a
                LEFT JOIN tables t ON t.asset_id = a.asset_id
                WHERE LOWER(a.title) LIKE ?
                   OR LOWER(COALESCE(a.description, '')) LIKE ?
                   OR LOWER(COALESCE(a.team, '')) LIKE ?
                   OR LOWER(COALESCE(a.tags, '')) LIKE ?
                   OR LOWER(COALESCE(t.columns, '')) LIKE ?
                   OR LOWER(COALESCE(t.text_preview, '')) LIKE ?
                ORDER BY a.created_at DESC LIMIT ?
                """,
                (like, like, like, like, like, like, limit),
            ).fetchall()
        return [self._row_to_asset(r) for r in rows]

    def summary(self) -> dict[str, Any]:
        with self.connect() as conn:
            def count_by(col: str) -> dict[str, int]:
                rows = conn.execute(
                    f"SELECT COALESCE({col}, 'unspecified') AS k, COUNT(*) AS n "
                    f"FROM assets GROUP BY k ORDER BY n DESC"
                ).fetchall()
                return {r["k"]: r["n"] for r in rows}

            total = conn.execute("SELECT COUNT(*) AS n FROM assets").fetchone()["n"]
            return {
                "total_assets": total,
                "by_status": count_by("status"),
                "by_file_type": count_by("file_type"),
                "by_team": count_by("team"),
                "by_access": count_by("access"),
                "by_upload_channel": count_by("upload_channel"),
                "needs_review": conn.execute(
                    "SELECT COUNT(*) AS n FROM assets WHERE status = 'needs_review'"
                ).fetchone()["n"],
            }

    # ------------------------------------------------------------------ export
    def export_json(self) -> Path:
        out = self.config.path("catalog_json")
        assets = []
        for a in self.list_assets(limit=100000):
            full = self.get_asset(a["asset_id"], with_tables=True)
            assets.append(full)
        payload = {
            "hub": self.config.hub_name,
            "generated_at": _now(),
            "n_assets": len(assets),
            "assets": assets,
        }
        out.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        return out

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _row_to_asset(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["tags"] = json.loads(d.get("tags") or "[]")
        d["missing_fields"] = json.loads(d.get("missing_fields") or "[]")
        d["contains_pii"] = bool(d.get("contains_pii"))
        return d

    @staticmethod
    def _row_to_table(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["columns"] = json.loads(d.get("columns") or "[]")
        return d
