"""Prospect-list export — the list itself is the product.

Produces a CSV that drops straight into Instantly, Smartlead, or any
sequencer (email / first_name / last_name / company_name / personalization
columns), plus Harvey's own quality columns (score, email_status, signals)
so a human can eyeball what they're getting.
"""

import csv
import io
import logging

from harvey.gate import do_not_contact_match
from harvey.state import StateManager

logger = logging.getLogger("harvey.export")

# Column order chosen so the file imports cleanly into cold-email tools
# without remapping; extra columns are simply available as custom variables.
# The company's phone and key signals come last, joined from its company.
CSV_COLUMNS = [
    "email", "first_name", "last_name", "company_name", "title",
    "website", "linkedin_url", "industry", "personalization",
    "email_status", "score", "seniority", "source", "status", "created_at",
    "company_domain", "linkedin_page", "instagram", "marketing_email",
    "phone", "phone_type", "phone_source",
    "meta_ads", "google_ads", "funding_round", "hiring_roles", "tech_stack",
]

# Export column -> signal code. Each holds the company's latest reading:
# "yes"/"no" for the ad pixels, the text (joined with "; ") for the rest.
KEY_SIGNALS = {
    "meta_ads": "RUNNING_META_ADS",
    "google_ads": "RUNNING_GOOGLE_ADS",
    "funding_round": "FUNDING_ROUND",
    "hiring_roles": "HIRING_ROLE",
    "tech_stack": "TECH_STACK",
    "linkedin_page": "LINKEDIN_PAGE",
    "instagram": "SOCIAL_PROFILE",       # only the "instagram: @handle" row
    "marketing_email": "MARKETING_EMAIL",
}

DEFAULT_EMAIL_STATUSES = ["verified", "risky"]


async def collect_prospects(
    state: StateManager,
    email_statuses: list[str] | None = None,
    min_score: int = 0,
    statuses: list[str] | None = None,
    include_all: bool = False,
    config=None,
) -> list[dict]:
    """Fetch prospects matching the filters as export-ready dicts.

    Default view is the *deliverable* list (verified + risky emails).
    Outreach exports exclude opted-out prospects even with explicit filters.
    ``include_all`` disables every filter for a full dump.
    """
    import aiosqlite

    where = []
    params: list = []
    if not include_all:
        # Opt-outs must not be reintroduced by an outreach filter. IS NOT also
        # preserves legacy NULL statuses; only known opt-outs are suppressed.
        where.append("p.status IS NOT ?")
        params.append("opted_out")
        wanted = email_statuses if email_statuses is not None else DEFAULT_EMAIL_STATUSES
        if wanted:
            placeholders = ",".join("?" for _ in wanted)
            where.append(f"p.email != '' AND p.email_status IN ({placeholders})")
            params.extend(wanted)
        if min_score > 0:
            where.append("p.score >= ?")
            params.append(min_score)
        if statuses:
            placeholders = ",".join("?" for _ in statuses)
            where.append(f"p.status IN ({placeholders})")
            params.extend(statuses)

    sql = ("SELECT p.*, c.domain AS company_domain, c.phone AS company_phone, "
           "c.phone_type AS company_phone_type, c.phone_source AS company_phone_source "
           "FROM prospects p LEFT JOIN companies c ON c.id = p.company_id")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY p.score DESC, p.created_at DESC"

    async with aiosqlite.connect(state.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(sql, params) as cursor:
            rows = [dict(r) for r in await cursor.fetchall()]
        signals = await _key_signals(db, {r["company_id"] for r in rows if r.get("company_id")})

    if not include_all:
        # A list for another sequencer is still contact: leave out anyone on
        # compliance.do_not_contact, as the send gate would.
        rows = [r for r in rows
                if not do_not_contact_match(r.get("email") or "", _do_not_contact(config))]

    out = []
    for r in rows:
        out.append({
            "harvey_id": r.get("id", ""),
            "email": r.get("email", ""),
            "first_name": r.get("first_name", ""),
            "last_name": r.get("last_name", ""),
            "company_name": r.get("company", ""),
            "title": r.get("title", ""),
            "website": r.get("source_url", ""),
            "linkedin_url": r.get("linkedin_url", ""),
            "industry": r.get("industry", ""),
            "personalization": r.get("personalization_notes", ""),
            "email_status": r.get("email_status", ""),
            "score": r.get("score", 0),
            "seniority": r.get("seniority", ""),
            "source": r.get("source", ""),
            "status": r.get("status", ""),
            "created_at": r.get("created_at", ""),
            "company_domain": r.get("company_domain") or "",
            # Only a number that passed validation (phone_type set) is exported.
            "phone": (r.get("company_phone") or "") if r.get("company_phone_type") else "",
            "phone_type": r.get("company_phone_type") or "",
            "phone_source": (r.get("company_phone_source") or "") if r.get("company_phone_type") else "",
            **signals.get(r.get("company_id") or "", {}),
        })
    return out


async def _key_signals(db, company_ids: set[str]) -> dict[str, dict[str, str]]:
    """{company_id: {column: value}} from each company's latest reading of
    every KEY_SIGNALS code. A reading is every row its newest run wrote, so
    three open roles found together all show."""
    if not company_ids:
        return {}
    ids = sorted(company_ids)
    codes = list(KEY_SIGNALS.values())
    column = {code: col for col, code in KEY_SIGNALS.items()}
    latest: dict[tuple[str, str], str] = {}     # (company, code) -> run_id of newest row
    values: dict[tuple[str, str], list[str]] = {}
    for start in range(0, len(ids), 500):      # SQLite caps bound parameters
        chunk = ids[start:start + 500]
        sql = (f"SELECT company_id, signal_code, value_num, value_text, run_id FROM observations "
               f"WHERE company_id IN ({','.join('?' for _ in chunk)}) "
               f"AND signal_code IN ({','.join('?' for _ in codes)}) "
               "ORDER BY observed_at DESC")
        async with db.execute(sql, (*chunk, *codes)) as cursor:
            for company_id, code, num, text, run_id in await cursor.fetchall():
                key = (company_id, code)
                if latest.setdefault(key, run_id or "") != (run_id or ""):
                    continue
                if code in ("RUNNING_META_ADS", "RUNNING_GOOGLE_ADS"):
                    value = "yes" if num else "no"
                elif code == "SOCIAL_PROFILE":
                    text = (text or "").strip()
                    value = text.removeprefix("instagram: ") if text.startswith("instagram: ") else ""
                else:
                    value = (text or "").strip()
                if value and value not in values.setdefault(key, []):
                    values[key].append(value)
    out: dict[str, dict[str, str]] = {}
    for (company_id, code), vals in values.items():
        out.setdefault(company_id, {})[column[code]] = "; ".join(vals)
    return out


def _do_not_contact(config) -> list[str]:
    from harvey.config import (
        ConfigError, ConfigFileNotFoundError, do_not_contact_from_file, live_do_not_contact,
    )
    if config is not None:
        return live_do_not_contact(config)
    try:
        return do_not_contact_from_file()
    except (ConfigFileNotFoundError, FileNotFoundError):
        return []
    except Exception as e:
        # Never export the people the list was meant to keep out.
        raise ConfigError(f"compliance.do_not_contact couldn't be read ({e}). "
                          "Fix it before exporting.") from e


def to_csv(rows: list[dict]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return buf.getvalue()


async def export_prospects_csv(
    state: StateManager,
    out_path: str | None = None,
    email_statuses: list[str] | None = None,
    min_score: int = 0,
    statuses: list[str] | None = None,
    include_all: bool = False,
    config=None,
) -> tuple[int, str]:
    """Export prospects to CSV. Returns (row_count, csv_text).

    Writes to ``out_path`` when given; caller handles printing/serving.
    """
    rows = await collect_prospects(
        state,
        email_statuses=email_statuses,
        min_score=min_score,
        statuses=statuses,
        include_all=include_all,
        config=config,
    )
    text = to_csv(rows)
    if out_path:
        with open(out_path, "w", newline="", encoding="utf-8") as f:
            f.write(text)
        logger.info(f"Exported {len(rows)} prospects to {out_path}")
    return len(rows), text
