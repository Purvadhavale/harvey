"""More brands like the ones the team liked in the CRM.

For each liked brand, once: two Google searches ("brands like X",
"X competitors"). A result that is a brand's own home page, or a post on
its own blog, becomes a new company. List and competitor pages ("Top
competitors of X include A, B and C") give brand names; up to MAX_RESOLVE
of those are looked up ("A official website") to find their sites.

New companies take the liked brand's sector and city, and say where they
came from ("Similar to X"), so the CRM can show it and brand fit can score
them. Junk rules (harvey.junk) apply as for any other discovery.
"""

import logging
import re

from urllib.parse import urlparse

from harvey.collectors.discover import (business_name, is_junk, name_from_domain,
                                        name_matches_domain, normalize_domain)
from harvey.junk import icp_rules, rule_reason
from harvey.models.company import Company

logger = logging.getLogger("harvey.lookalikes")

SERPER_URL = "https://google.serper.dev/search"
MAX_LIKES_PER_RUN = 5
MAX_RESOLVE = 8            # brand names looked up per liked brand
SOURCE = "lookalike"

# Where a list of brand names starts in a snippet.
_LIST_START = re.compile(r"\b(?:include|includes|including|such as|like|are)\b\s*:?|:", re.I)
_SPLIT = re.compile(r"\s*(?:,|;|:|•|\||\band\b|\bor\b|\betc\b|\.(?:\s|$))\s*")
# A "name" starting with one of these is a phrase, not a brand.
_NOT_A_NAME = {"top", "best", "more", "other", "others", "similar", "list", "read", "view", "see", "find",
               "compare", "the", "here", "which", "these", "those", "many", "some", "all", "popular",
               "leading", "brands", "competitors", "alternatives", "india", "indian", "shop", "buy"}


def names_from_text(text: str, exclude: str = "") -> list[str]:
    """Brand names listed in a search snippet, in order, without ``exclude``."""
    out = []
    for cue in _LIST_START.finditer(text or ""):
        for part in _SPLIT.split(text[cue.end():]):
            part = re.sub(r"^\s*(\d+[.)]\s*)", "", part).strip(" -–—\"'()[]")
            words = part.split()
            if not (1 <= len(words) <= 4) or not (2 <= len(part) <= 40):
                if out:         # a sentence after the list: the list is over
                    break
                continue
            if not all(w[0].isupper() or w[0].isdigit() or w == "&" for w in words) \
                    or words[0].lower() in _NOT_A_NAME:
                continue
            if part.lower() != exclude.lower() and part not in out:
                out.append(part)
    return out


def _queries(like: dict, country: str) -> list[str]:
    name = like.get("name") or like.get("domain") or ""
    where = " India" if country == "in" else ""
    return [f"brands like {name}{where}", f"{name} competitors"]


async def _search(client, api_key: str, q: str, country: str) -> list[dict]:
    r = await client.post(SERPER_URL, headers={"X-API-KEY": api_key, "Content-Type": "application/json"},
                          json={"q": q, "num": 10, **({"gl": country} if country else {})})
    r.raise_for_status()
    return r.json().get("organic") or []


_BLOG_PATH = re.compile(r"/(blogs?|journal|stories|magazine)(/|$)", re.I)


def _brand_site(item: dict, liked: dict, extra=()) -> tuple[str, str] | None:
    """(name, domain) when a search result is a brand's own site: its home
    page, or a blog post on it. A brand writing "top footwear brands in
    India" on its own blog is usually a D2C brand in the same market; an
    article page anywhere else is a list to read names from, not a brand."""
    link = item.get("link") or ""
    domain = normalize_domain(link)
    liked_name = liked.get("name") or ""
    if not domain or domain == (liked.get("domain") or "").lower() \
            or (liked_name and name_matches_domain(liked_name, domain)):
        return None
    path = urlparse(link).path.strip("/")
    blog = bool(_BLOG_PATH.search("/" + path))
    if path and not blog:
        return None
    title = "" if blog else (item.get("title") or "")
    name = business_name(title, domain) if title else name_from_domain(domain)
    if is_junk(domain, name) or rule_reason(name, domain, extra) or (title and rule_reason(title, domain, extra)):
        return None
    return name, domain


async def find_lookalikes(state, config, api_key: str, client=None,
                          max_likes: int = MAX_LIKES_PER_RUN) -> dict:
    """Search for brands like each liked brand not searched yet. Returns counts."""
    import httpx

    country = (getattr(getattr(config, "icp", None), "search_country", "") or "").lower()
    likes = await state.brand_likes(unsearched_only=True, limit=max_likes)
    extra = icp_rules(config)
    counts = {"likes": len(likes), "searches": 0, "added": 0, "known": 0, "added_ids": []}
    if not likes or not api_key:
        return counts
    own = client is None
    client = client or httpx.AsyncClient(timeout=20)
    try:
        for like in likes:
            liked_domain = (like.get("domain") or "").lower()
            found: dict[str, tuple[str, str]] = {}      # domain -> (name, link)
            names: list[str] = []
            for q in _queries(like, country):
                try:
                    items = await _search(client, api_key, q, country)
                except Exception as e:
                    logger.warning(f"look-alike search failed for {q!r}: {e}")
                    continue
                counts["searches"] += 1
                for item in items:
                    site = _brand_site(item, like, extra)
                    if site:
                        found.setdefault(site[1], (site[0], item.get("link") or ""))
                    text = f"{item.get('title') or ''}. {item.get('snippet') or ''}"
                    names += [n for n in names_from_text(text, like.get("name") or "") if n not in names]
            for name in names[:MAX_RESOLVE]:
                if any(name_matches_domain(name, d) for d in found):
                    continue
                try:
                    items = await _search(client, api_key, f"{name} official website", country)
                except Exception as e:
                    logger.warning(f"look-alike lookup failed for {name!r}: {e}")
                    continue
                counts["searches"] += 1
                for item in items[:3]:
                    site = _brand_site(item, like, extra)
                    if site and name_matches_domain(name, site[1]):
                        found.setdefault(site[1], (name, item.get("link") or ""))
                        break
            for domain, (name, link) in found.items():
                if await state.get_company_by_domain(domain):
                    counts["known"] += 1
                    continue
                cid = await state.add_company(Company(
                    name=name, domain=domain, website=f"https://{domain}",
                    industry=like.get("industry") or "", location=like.get("location") or "",
                    source=SOURCE, source_url=link, notes=f"Similar to {like.get('name') or liked_domain}"))
                counts["added"] += 1
                counts["added_ids"].append(cid)
            await state.mark_like_searched(like["company_id"])
            logger.info(f"Look-alikes of {like.get('name')}: {len(found)} brand site(s) found")
    finally:
        if own:
            await client.aclose()
    return counts


def summary(counts: dict) -> str:
    if not counts["likes"]:
        return "No new liked brands to search from"
    return (f"Searched for brands like {counts['likes']} liked brand(s): "
            f"{counts['added']} new, {counts['known']} already known ({counts['searches']} searches)")
