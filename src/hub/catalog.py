"""Catalogue store.

SQLite-backed asset catalogue. Every ingested file becomes one *asset* with
one or more *tables* (sheets for Excel; a single table for CSV; an extracted
document body for Word). The catalogue is also exported to
``data/catalog/assets.json`` — the hub equivalent of the CDH
``data/normalized/assets.json`` — so it can be published, diffed in git, and
consumed by anything that does not speak SQL.
"""

from __future__ import annotations

import json
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
    status          TEXT NOT NULL,          -- published | needs_review
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

CREATE INDEX IF NOT EXISTS idx_assets_status ON assets(status);
CREATE INDEX IF NOT EXISTS idx_assets_team ON assets(team);
CREATE INDEX IF NOT EXISTS idx_tables_asset ON tables(asset_id);
"""

REVIEW_FIELDS = ("team", "owner", "description", "license", "access")


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
        if "upload_channel" not in cols:
            conn.execute("ALTER TABLE assets ADD COLUMN upload_channel TEXT")

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
        missing = [f for f in REVIEW_FIELDS if not asset.get(f)]
        status = "published" if not missing else "needs_review"

        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO assets (
                    asset_id, title, description, team, owner, contact,
                    file_name, file_path, file_type, file_ext, size_bytes,
                    sha256, status, tags, license, access, domain, hub_role,
                    spatial_coverage, temporal_coverage, upload_channel,
                    missing_fields, ingest_log, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    asset_id,
                    asset.get("title", "Untitled asset"),
                    asset.get("description"),
                    asset.get("team"),
                    asset.get("owner"),
                    asset.get("contact"),
                    asset["file_name"],
                    asset["file_path"],
                    asset["file_type"],
                    asset["file_ext"],
                    asset.get("size_bytes"),
                    asset["sha256"],
                    status,
                    json.dumps(asset.get("tags") or []),
                    asset.get("license"),
                    asset.get("access"),
                    asset.get("domain"),
                    asset.get("hub_role"),
                    asset.get("spatial_coverage"),
                    asset.get("temporal_coverage"),
                    asset.get("upload_channel"),
                    json.dumps(missing),
                    asset.get("ingest_log"),
                    now,
                    now,
                ),
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
        self.export_json()
        return asset_id

    def update_metadata(self, asset_id: str, updates: dict[str, Any]) -> bool:
        """Apply a completed metadata review template to an asset."""
        allowed = {
            "title", "description", "team", "owner", "contact", "license",
            "access", "domain", "hub_role", "spatial_coverage",
            "temporal_coverage", "tags",
        }
        fields = {k: v for k, v in updates.items() if k in allowed and v is not None}
        if not fields:
            return False
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM assets WHERE asset_id = ?", (asset_id,)
            ).fetchone()
            if row is None:
                return False
            merged = dict(row)
            merged["tags"] = json.loads(row["tags"] or "[]")
            merged.update(fields)
            missing = [f for f in REVIEW_FIELDS if not merged.get(f)]
            fields["missing_fields"] = json.dumps(missing)
            fields["status"] = "published" if not missing else "needs_review"
            if "tags" in fields:
                fields["tags"] = json.dumps(fields["tags"])
            fields["updated_at"] = _now()
            assignments = ", ".join(f"{k} = ?" for k in fields)
            conn.execute(
                f"UPDATE assets SET {assignments} WHERE asset_id = ?",
                (*fields.values(), asset_id),
            )
        self.export_json()
        return True

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
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM assets WHERE 1=1"
        params: list[Any] = []
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
        return d

    @staticmethod
    def _row_to_table(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["columns"] = json.loads(d.get("columns") or "[]")
        return d
