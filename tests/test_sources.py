from hub.sources import load_sources


def _write(cfg, name, text):
    p = cfg.path("sources_dir") / name
    p.write_text(text, encoding="utf-8")
    return p


def test_valid_pointer_loads(cfg):
    _write(
        cfg,
        "chirps.yaml",
        """
id: chirps-africa
title: CHIRPS rainfall
type: remote_dataset
url: https://data.chc.ucsb.edu/products/CHIRPS-2.0/
tags: [rainfall]
""",
    )
    pointers, problems = load_sources(cfg)
    assert problems == []
    assert len(pointers) == 1
    assert pointers[0].id == "chirps-africa"
    assert pointers[0].tags == ["rainfall"]
    d = pointers[0].to_dict()
    assert d["hub_role"] == "federation"  # default applied


def test_missing_required_fields_reported(cfg):
    _write(cfg, "bad.yaml", "title: No id or url\n")
    pointers, problems = load_sources(cfg)
    assert pointers == []
    assert any("missing required field 'id'" in p for p in problems)


def test_invalid_type_reported(cfg):
    _write(
        cfg,
        "badtype.yaml",
        "id: x\ntitle: X\ntype: ftp_server\nurl: ftp://x\n",
    )
    pointers, problems = load_sources(cfg)
    assert any("type must be one of" in p for p in problems)


def test_duplicate_ids_reported(cfg):
    text = "id: dup\ntitle: D\ntype: api\nurl: https://x\n"
    _write(cfg, "a.yaml", text)
    _write(cfg, "b.yaml", text)
    pointers, problems = load_sources(cfg)
    assert len(pointers) == 1
    assert any("duplicate id" in p for p in problems)
