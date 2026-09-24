"""Who may see, review, and download what.

Roles come from ``access_control`` in config/hub.yaml (verified email only):

* admin      — curates every category, sees everything.
* curator    — reviews/publishes datasets in the categories that list them.
* owner      — the uploader (``owner_email``); edits drafts, shares data.
* anyone     — sees metadata of *published* datasets (CGIAR: metadata is
               always open); files follow the dataset's access level:
               open = everyone, internal = org domains, restricted = grants.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any

from .config import HubConfig

EMAIL_RE = re.compile(r"[a-z0-9._%+'-]+@[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}")
DOMAIN_RE = re.compile(r"@[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}")


def with_db_roles(cfg: HubConfig, catalog) -> HubConfig:
    """hub.yaml access_control + roles/categories added in the admin UI.

    Config admins are the bootstrap (and can't be removed from the UI, so an
    admin can never lock everyone out); everything else is additive.
    """
    ac = dict(cfg.get("access_control", default={}) or {})
    admins = list(ac.get("admins") or [])
    cats = {name: list(people or []) for name, people in (ac.get("categories") or {}).items()}
    for name in catalog.extra_categories():
        cats.setdefault(name, [])
    for r in catalog.roles():
        if r["role"] == "admin":
            admins.append(r["email"])
        elif r["role"] == "curator" and r["category"] in cats:
            cats[r["category"]].append(r["email"])
    ac.update(admins=admins, categories=cats, config_admins=list(ac.get("admins") or []))
    return HubConfig({**cfg._raw, "access_control": ac}, cfg.root)


def is_config_admin(cfg: HubConfig, email: str | None) -> bool:
    return bool(email) and email.lower() in _lower(cfg.get("access_control", "config_admins")
                                                  or cfg.get("access_control", "admins"))


def _lower(values) -> set[str]:
    return {str(v).strip().lower() for v in (values or [])}


def is_admin(cfg: HubConfig, email: str | None) -> bool:
    return bool(email) and email.lower() in _lower(cfg.get("access_control", "admins"))


def categories(cfg: HubConfig) -> dict[str, list[str]]:
    return cfg.get("access_control", "categories", default={}) or {}


def curated_categories(cfg: HubConfig, email: str | None) -> set[str] | None:
    """Categories this user curates; None means all (admin)."""
    if is_admin(cfg, email):
        return None
    if not email:
        return set()
    return {c for c, people in categories(cfg).items() if email.lower() in _lower(people)}


def curators_for(cfg: HubConfig, category: str | None) -> list[str]:
    """Who gets the 'please review' email; falls back to admins."""
    people = categories(cfg).get(category or "") or []
    return list(people) or list(cfg.get("access_control", "admins", default=[]) or [])


def is_curator(cfg: HubConfig, email: str | None) -> bool:
    cats = curated_categories(cfg, email)
    return cats is None or bool(cats)


def can_curate(cfg: HubConfig, email: str | None, asset: dict[str, Any]) -> bool:
    cats = curated_categories(cfg, email)
    return cats is None or (asset.get("category") in cats)


def is_owner(email: str | None, asset: dict[str, Any]) -> bool:
    return bool(email) and email.lower() == (asset.get("owner_email") or "").lower()


def can_view(cfg: HubConfig, email: str | None, asset: dict[str, Any]) -> bool:
    return (asset.get("status") == "published" or is_owner(email, asset)
            or (bool(email) and can_curate(cfg, email, asset)))


def can_manage(cfg: HubConfig, email: str | None, asset: dict[str, Any]) -> bool:
    """Change access level, grants, and decide access requests."""
    return is_owner(email, asset) or (bool(email) and can_curate(cfg, email, asset))


def effective_access(asset: dict[str, Any], today: date | None = None) -> str:
    level = asset.get("access") or "restricted"  # unknown -> safest
    until = asset.get("embargo_until")
    if level == "restricted" and until and not asset.get("contains_pii"):
        if until <= (today or date.today()).isoformat():
            return "open"  # embargo lifted automatically
    return level


def grant_matches(email: str, grants: list[dict[str, Any]], today: date | None = None) -> bool:
    email = email.lower()
    now = (today or date.today()).isoformat()
    for g in grants:
        if g.get("expires_at") and g["expires_at"] < now:
            continue
        p = g["principal"]
        if p == email or (p.startswith("@") and email.endswith(p)):
            return True
    return False


def can_download(cfg: HubConfig, email: str | None, asset: dict[str, Any],
                 grants: list[dict[str, Any]]) -> bool:
    if can_manage(cfg, email, asset):
        return True
    if asset.get("status") != "published":
        return False
    level = effective_access(asset)
    if level == "open":
        return True
    if not email:
        return False
    if level == "internal":
        return email.lower().rsplit("@", 1)[-1] in _lower(cfg.get("access_control", "org_domains"))
    return grant_matches(email, grants)


def parse_principals(text: str) -> tuple[list[str], list[str]]:
    """Pull emails and '@domain' entries out of pasted text or a CSV.

    Tokens without an '@' (names, CSV headers) are ignored; tokens with an
    '@' that are not valid are reported back as invalid.
    """
    valid: list[str] = []
    invalid: list[str] = []
    for raw in re.split(r"[\s,;]+", text or ""):
        token = raw.strip().strip("\"'<>()[]").lower()
        if "@" not in token:
            continue
        if EMAIL_RE.fullmatch(token) or DOMAIN_RE.fullmatch(token):
            if token not in valid:
                valid.append(token)
        else:
            invalid.append(raw.strip())
    return valid, invalid
