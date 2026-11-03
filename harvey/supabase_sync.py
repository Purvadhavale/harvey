"""Copy the lead list to Supabase: one row per brand, one per contact.

The local SQLite database stays the source of truth (it's what every stage
writes to). This pushes a snapshot to two Supabase tables, ``brands`` and
``contacts`` (created by docs/supabase_leads.sql), keyed on Harvey's own ids,
so running it again updates rows instead of duplicating them. A shared copy
there is what a hosted dashboard, Metabase or a teammate reads.

Uses the project's secret (service-role) key from .env, which bypasses row
level security: keep it on this machine, never in a browser.
"""

import logging
import os
import re
from datetime import datetime, timezone

from harvey.collectors.linkedin_search import title_rank

logger = logging.getLogger("harvey.supabase_sync")

BATCH = 500
_SIGNAL_COLUMNS = ("linkedin_page", "instagram", "marketing_email", "meta_ads", "google_ads",
                   "tech_stack", "hiring_roles", "funding_round")


def _sector(industry: str) -> str:
    return re.sub(r"\s+(brand|chain|platform|startup)$", "", (industry or "").strip(), flags=re.I)


def _ts(value) -> str | None:
    """SQLite timestamp text -> ISO 8601 UTC, or None."""
    if not value:
        return None
    text = str(value).strip().replace(" ", "T")
    if not re.search(r"(Z|[+-]\d\d:?\d\d)$", text):
        text += "+00:00"
    return text


def _best_contact(contacts: list[dict], titles: list[str]) -> dict | None:
    """The named person to reach first: a working email beats none, then a
    LinkedIn profile, then the better title."""
    named = [c for c in contacts if c.get("first_name")]

    def key(c):
        rank = title_rank(c.get("title") or "", titles)
        return ({"verified": 0, "risky": 1}.get(c.get("email_status") or "", 2),
                0 if c.get("linkedin_url") else 1, 99 if rank is None else rank)
    return min(named, key=key) if named else None


def next_step(contacts: list[dict], signals: dict, phone_type: str, titles: list[str]) -> str:
    """One plain instruction: who to reach and how."""
    best = _best_contact(contacts, titles)
    status = (best or {}).get("email_status") or ""
    if best and status == "verified":
        return f"Email {best['first_name']}"
    if best and status == "risky":
        return f"Email {best['first_name']} (check on LinkedIn first)"
    if best and best.get("linkedin_url"):
        return f"Connect with {best['first_name']} on LinkedIn"
    if signals.get("marketing_email"):
        return f"Email {signals['marketing_email']}"
    inbox = next((c for c in contacts if not c.get("first_name")
                  and c.get("email_status") in ("verified", "risky")), None)
    if inbox:
        return f"Email {inbox['email']}"
    if phone_type in ("mobile", "fixed_or_mobile"):
        return "WhatsApp / call brand number"
    if signals.get("instagram"):
        return f"DM {signals['instagram']} on Instagram"
    if phone_type:
        return "Call brand number"
    return "Find a contact manually"


async def build_rows(state, config) -> tuple[list[dict], list[dict]]:
    """(brands, contacts) rows for the two Supabase tables."""
    import aiosqlite

    from harvey.config import live_do_not_contact
    from harvey.export import _key_signals
    from harvey.gate import do_not_contact_domain_match
    from harvey.register import build_register, sqlite_query

    try:
        dnc = live_do_not_contact(config)
    except Exception:
        dnc = []
    titles = list(getattr(getattr(config, "icp", None), "titles", None) or [])
    fit = {r["id"]: r for r in await build_register(sqlite_query(state.db_path), config)}
    now = datetime.now(timezone.utc).isoformat()

    async with aiosqlite.connect(state.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT * FROM companies WHERE COALESCE(domain, '') != ''") as cur:
            companies = [dict(r) for r in await cur.fetchall()]
        async with db.execute(
                "SELECT * FROM prospects WHERE company_id != '' AND status IS NOT 'opted_out'") as cur:
            prospects = [dict(r) for r in await cur.fetchall()]
        signals = await _key_signals(db, {c["id"] for c in companies})

    by_company: dict[str, list[dict]] = {}
    for p in prospects:
        by_company.setdefault(p["company_id"], []).append(p)

    brands, contacts = [], []
    known = {c["id"] for c in companies}
    for c in companies:
        sig = {k: signals.get(c["id"], {}).get(k, "") for k in _SIGNAL_COLUMNS}
        phone_type = c.get("phone_type") or ""
        reg = fit.get(c["id"], {})
        people = by_company.get(c["id"], [])
        brands.append({
            "id": c["id"],
            "name": c.get("name") or c["domain"],
            "domain": c["domain"],
            # The home page, not the search-result page discovery landed on.
            "website": f"https://{c['domain']}",
            "sector": _sector(c.get("industry")),
            "city": c.get("location") or "",
            "is_junk": bool(do_not_contact_domain_match(c["domain"], dnc)),
            "fit": int(reg.get("fit") or 0),
            "fit_reasons": " · ".join(reg.get("fit_reasons") or []),
            "next_step": next_step(people, sig, phone_type, titles),
            "phone": (c.get("phone") or "") if phone_type else "",
            "phone_type": phone_type,
            "phone_source": (c.get("phone_source") or "") if phone_type else "",
            "linkedin_page": sig["linkedin_page"],
            "instagram": sig["instagram"],
            "marketing_email": sig["marketing_email"],
            "meta_ads": {"yes": True, "no": False}.get(sig["meta_ads"]),
            "google_ads": {"yes": True, "no": False}.get(sig["google_ads"]),
            "tech_stack": sig["tech_stack"],
            "hiring_roles": sig["hiring_roles"][:500],
            "funding_round": sig["funding_round"],
            "first_found_at": _ts(c.get("created_at")),
            "synced_at": now,
        })
    for p in prospects:
        if p["company_id"] not in known:
            continue
        contacts.append({
            "id": p["id"],
            "brand_id": p["company_id"],
            "first_name": p.get("first_name") or "",
            "last_name": p.get("last_name") or "",
            "title": p.get("title") or "",
            "email": p.get("email") or "",
            "email_status": p.get("email_status") or "",
            "linkedin_url": p.get("linkedin_url") or "",
            "source": p.get("source") or "",
            "is_inbox": not (p.get("first_name") or "").strip(),
            "created_at": _ts(p.get("created_at")),
            "synced_at": now,
        })
    return brands, contacts


def credentials() -> tuple[str, str]:
    """(url, secret key) from .env, or raise with what's missing."""
    from harvey.config import load_env
    load_env()      # reads .env into the environment
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if not (url and key):
        raise RuntimeError("Set SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in .env to sync "
                           "to Supabase (Project Settings → API keys → secret key).")
    return url, key


async def push(brands: list[dict], contacts: list[dict], url: str, key: str,
               client=None) -> tuple[int, int]:
    """Upsert brands, then contacts (they reference brands). Returns counts."""
    import httpx

    headers = {"apikey": key, "Authorization": f"Bearer {key}",
               "Content-Type": "application/json",
               "Prefer": "resolution=merge-duplicates,return=minimal"}
    own = client is None
    client = client or httpx.AsyncClient(timeout=30)
    try:
        for table, rows in (("brands", brands), ("contacts", contacts)):
            endpoint = f"{url.rstrip('/')}/rest/v1/{table}?on_conflict=id"
            for start in range(0, len(rows), BATCH):
                r = await client.post(endpoint, headers=headers, json=rows[start:start + BATCH])
                if r.status_code >= 300:
                    raise RuntimeError(f"Supabase {table} upsert failed: "
                                       f"HTTP {r.status_code} {r.text[:300]}")
    finally:
        if own:
            await client.aclose()
    return len(brands), len(contacts)


async def sync(state, config) -> tuple[int, int]:
    """Build the rows and push them. Returns (brands, contacts) sent."""
    url, key = credentials()
    brands, contacts = await build_rows(state, config)
    counts = await push(brands, contacts, url, key)
    logger.info(f"Synced {counts[0]} brands and {counts[1]} contacts to Supabase")
    return counts
