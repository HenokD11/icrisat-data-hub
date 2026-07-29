import json
import shutil

import yaml

from hub.catalog import Catalog
from hub.ingest.pipeline import apply_review, ingest_file, scan_inbox


def _drop(cfg, src, name=None):
    dest = cfg.path("inbox") / (name or src.name)
    shutil.copy(src, dest)
    return dest


def test_ingest_csv_needs_review(cfg, sample_csv):
    cat = Catalog(cfg)
    res = ingest_file(_drop(cfg, sample_csv), cfg, cat)
    assert res["ok"], res
    assert res["status"] == "needs_review"  # no team/owner/license supplied

    asset = cat.get_asset(res["asset_id"])
    assert asset["file_type"] == "csv"
    assert asset["title"] == "Yield Trials 2025"  # inferred from filename
    assert asset["tables"][0]["n_rows"] == 3
    col_names = [c["name"] for c in asset["tables"][0]["columns"]]
    assert col_names == ["region", "crop", "yield_kg_ha"]
    assert "team" in asset["missing_fields"]

    # file moved to processed, review template written next to it
    assert not (cfg.path("inbox") / sample_csv.name).exists()
    review = cfg.path("processed").rglob("*_metadata_review.yaml")
    assert len(list(review)) == 1


def test_ingest_excel_multi_sheet(cfg, sample_xlsx):
    cat = Catalog(cfg)
    res = ingest_file(_drop(cfg, sample_xlsx), cfg, cat)
    assert res["ok"], res
    asset = cat.get_asset(res["asset_id"])
    assert asset["file_type"] == "excel"
    sheet_names = {t["name"] for t in asset["tables"]}
    assert sheet_names == {"Soil pH", "Organic Carbon"}


def test_ingest_word_document(cfg, sample_docx):
    cat = Catalog(cfg)
    res = ingest_file(_drop(cfg, sample_docx), cfg, cat)
    assert res["ok"], res
    asset = cat.get_asset(res["asset_id"])
    assert asset["file_type"] == "word"
    doc = next(t for t in asset["tables"] if t["kind"] == "document")
    assert "Fertilizer Advisory Protocol" in doc["text_preview"]


def test_sidecar_metadata_publishes(cfg, sample_csv):
    sidecar = cfg.path("inbox") / "yield_trials_2025.meta.yaml"
    _drop(cfg, sample_csv)
    sidecar.write_text(
        yaml.safe_dump(
            {
                "team": "Soil Intelligence",
                "owner": "H. Desalegn",
                "description": "Sorghum/teff/maize yield trials",
                "license": "internal",
                "access": "internal",
                "tags": ["yield", "trials"],
            }
        ),
        encoding="utf-8",
    )
    cat = Catalog(cfg)
    results = scan_inbox(cfg, cat)
    res = next(r for r in results if r["file"] == sample_csv.name)
    assert res["ok"] and res["status"] == "published", res
    asset = cat.get_asset(res["asset_id"])
    assert asset["team"] == "Soil Intelligence"
    assert asset["tags"] == ["yield", "trials"]


def test_duplicate_file_skipped(cfg, sample_csv):
    cat = Catalog(cfg)
    first = ingest_file(_drop(cfg, sample_csv), cfg, cat)
    second = ingest_file(_drop(cfg, sample_csv, name="copy.csv"), cfg, cat)
    assert first["ok"]
    assert not second["ok"]
    assert "duplicate" in second["reason"]


def test_unsupported_file_quarantined(cfg, tmp_path):
    bad = tmp_path / "notes.txt"
    bad.write_text("hello", encoding="utf-8")
    res = ingest_file(_drop(cfg, bad), cfg, Catalog(cfg))
    assert not res["ok"]
    errors = list(cfg.path("failed").rglob("*.error.txt"))
    assert errors and "unsupported type" in errors[0].read_text()


def test_review_template_completes_asset(cfg, sample_csv):
    cat = Catalog(cfg)
    res = ingest_file(_drop(cfg, sample_csv), cfg, cat)
    template = next(cfg.path("processed").rglob("*_metadata_review.yaml"))
    values = yaml.safe_load(template.read_text(encoding="utf-8"))
    values.update(
        {"team": "Ag Data", "owner": "Someone", "license": "internal", "access": "open"}
    )
    template.write_text(yaml.safe_dump(values), encoding="utf-8")

    out = apply_review(template, cat)
    assert out["ok"]
    asset = cat.get_asset(res["asset_id"])
    assert asset["status"] == "published"
    assert asset["team"] == "Ag Data"


def test_assets_json_exported(cfg, sample_csv):
    cat = Catalog(cfg)
    res = ingest_file(_drop(cfg, sample_csv), cfg, cat)
    payload = json.loads(cfg.path("catalog_json").read_text(encoding="utf-8"))
    assert payload["n_assets"] == 1
    assert payload["assets"][0]["asset_id"] == res["asset_id"]


def test_search_finds_by_column_name(cfg, sample_csv):
    cat = Catalog(cfg)
    ingest_file(_drop(cfg, sample_csv), cfg, cat)
    hits = cat.search("yield_kg_ha")
    assert len(hits) == 1
