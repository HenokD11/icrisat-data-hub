"""Turn raw reader output into catalogue table profiles."""

from __future__ import annotations

from typing import Any

import pandas as pd

from ..config import HubConfig, load_config


def _profile_frame(df: pd.DataFrame, sample_values: int) -> dict[str, Any]:
    columns = []
    for col in df.columns:
        series = df[col]
        non_null = series.dropna()
        samples = [str(v) for v in non_null.head(sample_values).tolist()]
        columns.append(
            {
                "name": str(col),
                "dtype": str(series.dtype),
                "non_null": int(non_null.shape[0]),
                "n_unique": int(series.nunique(dropna=True)),
                "samples": samples,
            }
        )
    return {
        "n_rows": int(df.shape[0]),
        "n_cols": int(df.shape[1]),
        "columns": columns,
    }


def profile_tables(
    tables: list[dict[str, Any]], config: HubConfig | None = None
) -> list[dict[str, Any]]:
    """Produce catalogue-ready table entries from reader output."""
    cfg = config or load_config()
    sample_values = cfg.get("ingest", "column_sample_values", default=5)
    doc_chars = cfg.get("ingest", "doc_text_chars", default=20000)

    profiled = []
    for t in tables:
        if t["kind"] == "document":
            text = t.get("text", "")
            headings = t.get("headings") or []
            preview = text[:doc_chars]
            profiled.append(
                {
                    "name": t["name"],
                    "kind": "document",
                    "n_rows": t.get("n_paragraphs"),
                    "n_cols": None,
                    "columns": [{"name": h, "dtype": "heading"} for h in headings],
                    "text_preview": preview,
                }
            )
        else:
            prof = _profile_frame(t["frame"], sample_values)
            profiled.append(
                {
                    "name": t["name"],
                    "kind": "table",
                    "n_rows": prof["n_rows"],
                    "n_cols": prof["n_cols"],
                    "columns": prof["columns"],
                    "text_preview": None,
                }
            )
    return profiled
