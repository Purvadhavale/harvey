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


def found_via(company: dict) -> str:
    """Where Harvey found a brand, in a few words."""
    source = company.get("source") or ""
    if source == "lookalike":
        return (company.get("notes") or "Similar to a liked brand") + " (liked)"
    if source.startswith("discover:"):
        what = " ".join(x for x in (company.get("industry"), company.get("location")) if x)
        return f"Google search: {what}" if what else "Google search"
    return source


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
        junk, junk_source = c.get("junk") or "", c.get("junk_source") or ""
        if not junk and do_not_contact_domain_match(c["domain"], dnc):
            junk, junk_source = "On the do-not-contact list", "rule"
        brands.append({
            "id": c["id"],
            "name": c.get("name") or c["domain"],
            "domain": c["domain"],
            # The home page, not the search-result page discovery landed on.
            "website": f"https://{c['domain']}",
            "sector": _sector(c.get("industry")),
            "city": c.get("location") or "",
            "is_junk": bool(junk),
            "junk_reason": junk,
            "junk_source": junk_source,
            "fit": int(reg.get("fit") or 0),
            "fit_reasons": " · ".join(reg.get("fit_reasons") or []),
            "ready": int(reg.get("ready") or 0),
            "ready_reasons": " · ".join(reg.get("ready_reasons") or []),
            "next_step": "Skip: " + junk if junk else next_step(people, sig, phone_type, titles),
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
            "found_via": found_via(c),
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


async def _get(client, url: str, key: str, path: str) -> list[dict]:
    r = await client.get(f"{url.rstrip('/')}/rest/v1/{path}",
                         headers={"apikey": key, "Authorization": f"Bearer {key}"})
    if r.status_code >= 300:
        raise RuntimeError(f"Supabase read failed: HTTP {r.status_code} {r.text[:300]}")
    return r.json()


async def pull_tags(state, url: str, key: str, client=None) -> int:
    """Copy the team's junk / real-brand tags and likes from the CRM into Harvey."""
    import aiosqlite
    import httpx

    own = client is None
    client = client or httpx.AsyncClient(timeout=30)
    try:
        rows = await _get(client, url, key,
                          "crm_brands?select=brand_id,junk,junk_reason,junk_by,junk_at&junk=not.is.null")
        async with aiosqlite.connect(state.db_path) as db:
            async with db.execute("SELECT id, name, domain FROM companies") as cur:
                known = {i: (n or "", d or "") for i, n, d in await cur.fetchall()}
        likes = await _get(client, url, key,
                           "crm_brands?select=brand_id,liked_by,liked_at&liked=is.true")
    finally:
        if own:
            await client.aclose()
    await state.save_brand_likes([{"company_id": t["brand_id"], "liked_by": t.get("liked_by") or "",
                                   "liked_at": t.get("liked_at")} for t in likes if t.get("brand_id") in known])
    tags = [{"company_id": t["brand_id"], "name": known[t["brand_id"]][0],
             "domain": known[t["brand_id"]][1], "junk": 1 if t["junk"] else 0,
             "reason": t.get("junk_reason") or "", "tagged_by": t.get("junk_by") or "",
             "tagged_at": t.get("junk_at")}
            for t in rows if t.get("brand_id") in known]
    return await state.save_brand_tags(tags)


def configured() -> bool:
    """Are SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY set?"""
    try:
        credentials()
        return True
    except RuntimeError:
        return False


async def auto_sync(state, config=None) -> str:
    """Sync after a lead step, when Supabase is set up. Never raises: a
    failed sync must not fail the step that found the leads. Returns a
    one-line outcome for the log or the page."""
    if not configured():
        try:        # junk rules still apply without the CRM
            from harvey import junk
            from harvey.config import load_config
            await junk.apply(state, config or load_config())
        except Exception as e:
            logger.warning(f"Junk check failed: {e}")
        return "CRM sync skipped: add SUPABASE_SERVICE_ROLE_KEY to .env to sync automatically"
    try:
        if config is None:
            from harvey.config import load_config
            config = load_config()
        notes = []

        async def likes():
            notes.append(await search_likes(state, config))
        brands, contacts = await sync(state, config, after_pull=likes)
        extra = next((n for n in notes if n), "")
        return f"Synced to CRM: {brands} brands, {contacts} contacts" + (f". {extra}" if extra else "")
    except Exception as e:
        logger.warning(f"CRM sync failed: {e}")
        return f"CRM sync failed: {e}"


async def search_likes(state, config) -> str:
    """Brands liked since the last run: search for look-alikes and read the
    new sites, so they arrive in the CRM scored. "" when there was nothing."""
    from harvey import lookalikes
    from harvey.config import load_env

    if not await state.brand_likes(unsearched_only=True, limit=1):
        return ""
    api_key = getattr(load_env(), "serper_api_key", "") or ""
    if not api_key:
        return "Liked brands waiting: add SERPER_API_KEY to search for look-alikes"
    counts = await lookalikes.find_lookalikes(state, config, api_key)
    if counts["added_ids"]:
        from harvey.pipeline import run_profile_stage
        await run_profile_stage(state, limit=len(counts["added_ids"]) + 50, config=config)
    return lookalikes.summary(counts)


async def sync(state, config, after_pull=None) -> tuple[int, int]:
    """Read the team's junk tags and likes, re-judge every company, then
    build the rows and push them. Returns (brands, contacts) sent.
    ``after_pull``: an async callable run once the tags and likes are in
    (auto_sync searches for look-alikes there)."""
    from harvey import junk

    url, key = credentials()
    await pull_tags(state, url, key)
    if after_pull:
        await after_pull()
    logger.info(junk.summary(await junk.apply(state, config)))
    brands, contacts = await build_rows(state, config)
    counts = await push(brands, contacts, url, key)
    logger.info(f"Synced {counts[0]} brands and {counts[1]} contacts to Supabase")
    return counts
