"""MCP server — lets other LLMs discover and use ICRISAT hub data.

stdio (Claude Desktop / opencode / local agents):

    python -m hub.mcp_server.server

HTTP (other machines / LLMs on the network):

    python -m hub.mcp_server.server --http --port 8100

Tools exposed:
    list_assets, search_assets, get_asset, preview_asset, query_asset,
    read_document, list_sources, get_catalog_summary
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

from ..access import effective_access
from ..catalog import Catalog
from ..config import HubConfig, load_config
from ..ingest.readers import read_excel
from ..sources import load_sources

MAX_QUERY_ROWS = 1000

# Which access levels MCP may expose. stdio runs on a machine that already
# has the files; --http is reachable by others, so it only serves open data.
VISIBLE_ACCESS = {"open", "internal", "restricted"}


def _visible(asset: dict[str, Any] | None) -> bool:
    return bool(asset) and asset.get("status") == "published" and effective_access(asset) in VISIBLE_ACCESS


def _cfg() -> HubConfig:
    return load_config()


def _catalog() -> Catalog:
    return Catalog(_cfg())


def _resolve_asset_file(asset: dict[str, Any]) -> Path:
    cfg = _cfg()
    path = Path(asset["file_path"])
    if not path.is_absolute():
        path = cfg.root / path
    if not path.exists():
        raise FileNotFoundError(f"Asset file not found: {path}")
    return path


def _slim(asset: dict[str, Any]) -> dict[str, Any]:
    """Compact asset record for list/search results."""
    keys = (
        "asset_id", "title", "description", "team", "owner", "file_name",
        "file_type", "status", "tags", "access", "domain",
        "spatial_coverage", "temporal_coverage", "upload_channel", "created_at",
    )
    return {k: asset.get(k) for k in keys}


mcp = MCPServer(
    name="icrisat-data-hub",
    instructions=(
        "ICRISAT Data Hub. Use list_assets/search_assets to discover datasets, "
        "get_asset for full metadata and schema, preview_asset/query_asset to "
        "read tabular data (CSV/Excel), read_document for Word documents, and "
        "list_sources for federated external datasets (YAML pointers)."
    ),
)


@mcp.tool()
def list_assets(
    status: str | None = None,
    team: str | None = None,
    file_type: str | None = None,
    tag: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """List published assets. Filter by team, file_type ('csv'/'excel'/'word'),
    or tag. (status is accepted for compatibility; only published is served.)"""
    assets = _catalog().list_assets(
        status="published", team=team, file_type=file_type, tag=tag, limit=100000
    )
    return [_slim(a) for a in assets if _visible(a)][:limit]


@mcp.tool()
def search_assets(query: str, limit: int = 25) -> list[dict[str, Any]]:
    """Search assets by keyword — matches title, description, team, tags,
    column names, and Word document text."""
    return [_slim(a) for a in _catalog().search(query, limit=500) if _visible(a)][:limit]


@mcp.tool()
def get_asset(asset_id: str) -> dict[str, Any]:
    """Get full metadata for one asset, including per-table/sheet schema
    (column names, dtypes, sample values) and any missing-metadata fields."""
    asset = _catalog().get_asset(asset_id)
    if not _visible(asset):
        return {"error": f"asset '{asset_id}' not found"}
    return {k: v for k, v in asset.items() if k not in ("file_path", "owner_email")}


@mcp.tool()
def preview_asset(asset_id: str, sheet: str | None = None, n: int = 10) -> dict[str, Any]:
    """Preview the first n rows of a CSV/Excel asset (max 50 rows).
    For Excel, pass a sheet name from get_asset's table list; defaults to first."""
    n = min(max(n, 1), 50)
    asset = _catalog().get_asset(asset_id, with_tables=False)
    if not _visible(asset):
        return {"error": f"asset '{asset_id}' not found"}
    if asset["file_type"] == "word":
        return {"error": "Word asset — use read_document instead"}
    try:
        import pandas as pd

        path = _resolve_asset_file(asset)
        if asset["file_type"] == "csv":
            df = pd.read_csv(path, nrows=n, encoding_errors="replace")
            used_sheet = "data"
        else:
            sheets = pd.read_excel(path, sheet_name=None, nrows=n)
            wanted = sheet or next(iter(sheets))
            if wanted not in sheets:
                return {"error": f"sheet '{wanted}' not found", "available": list(sheets)}
            df = sheets[wanted]
            used_sheet = wanted
        return {
            "asset_id": asset_id,
            "sheet": used_sheet,
            "n_rows": len(df),
            "columns": [str(c) for c in df.columns],
            "rows": df.astype(str).to_dict(orient="records"),
        }
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
def query_asset(asset_id: str, sql: str, sheet: str | None = None) -> dict[str, Any]:
    """Run a read-only SQL query (DuckDB dialect) against a CSV/Excel asset.
    The data is exposed as a table named after the sheet (default 'data' for
    CSV, or the first Excel sheet). Results are capped at 1000 rows.
    Example: SELECT region, AVG(yield) FROM data GROUP BY region"""
    asset = _catalog().get_asset(asset_id, with_tables=False)
    if not _visible(asset):
        return {"error": f"asset '{asset_id}' not found"}
    if asset["file_type"] == "word":
        return {"error": "Word asset — use read_document instead"}

    lowered = sql.strip().lower().rstrip(";")
    if not (lowered.startswith("select") or lowered.startswith("with")):
        return {"error": "only SELECT queries are allowed"}

    try:
        import duckdb

        path = _resolve_asset_file(asset)
        con = duckdb.connect(database=":memory:")
        if asset["file_type"] == "csv":
            csv_path = str(path).replace("'", "''")
            con.execute(
                f"CREATE VIEW data AS SELECT * FROM read_csv_auto('{csv_path}', header=true)"
            )
            table_names = ["data"]
        else:
            frames = {t["name"]: t["frame"] for t in read_excel(path)}
            table_names = []
            for name, df in frames.items():
                safe = re.sub(r"\W+", "_", name).strip("_") or "sheet"
                con.register(safe, df)
                table_names.append(safe)
        limit_sql = f"{lowered} LIMIT {MAX_QUERY_ROWS}"
        df = con.execute(limit_sql).df()
        return {
            "asset_id": asset_id,
            "tables": table_names,
            "n_rows": len(df),
            "columns": [str(c) for c in df.columns],
            "rows": df.astype(str).to_dict(orient="records"),
            "note": f"results capped at {MAX_QUERY_ROWS} rows",
        }
    except Exception as exc:
        return {"error": str(exc)}


@mcp.tool()
def read_document(asset_id: str, max_chars: int = 10000) -> dict[str, Any]:
    """Read the extracted text of a Word (.docx) asset."""
    asset = _catalog().get_asset(asset_id)
    if not _visible(asset):
        return {"error": f"asset '{asset_id}' not found"}
    if asset["file_type"] != "word":
        return {"error": "not a Word asset — use preview_asset/query_asset instead"}
    doc_table = next((t for t in asset.get("tables", []) if t["kind"] == "document"), None)
    text = (doc_table or {}).get("text_preview") or ""
    return {
        "asset_id": asset_id,
        "title": asset["title"],
        "truncated": len(text) >= max_chars,
        "text": text[:max_chars],
    }


@mcp.tool()
def list_sources() -> list[dict[str, Any]]:
    """List federated external data sources registered as YAML pointers —
    APIs, remote datasets, databases, and shared drives the hub points to
    rather than hosts."""
    pointers, problems = load_sources()
    result = [p.to_dict() for p in pointers]
    if problems:
        result.append({"validation_problems": problems})
    return result


@mcp.tool()
def get_catalog_summary() -> dict[str, Any]:
    """Portfolio-level statistics over the published assets this server
    exposes: counts by file type, team, category, and access level."""
    from ..pages import _count_by

    assets = [a for a in _catalog().list_assets(status="published", limit=100000) if _visible(a)]
    return {"total_assets": len(assets), **{f"by_{k}": _count_by(assets, k)
            for k in ("file_type", "team", "category", "access")}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ICRISAT Data Hub MCP server")
    parser.add_argument("--http", action="store_true", help="serve over streamable HTTP instead of stdio")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args(argv)

    cfg = load_config()
    if args.http:
        VISIBLE_ACCESS.intersection_update({"open"})
        host = args.host or cfg.get("mcp", "http_host", default="0.0.0.0")
        port = args.port or int(cfg.get("mcp", "http_port", default=8100))
        print(f"MCP (streamable HTTP) on http://{host}:{port}/mcp", flush=True)
        mcp.run(transport="streamable-http", host=host, port=port)
    else:
        mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
