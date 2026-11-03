"""Brand fit: how good a customer the company itself would be (0-100).

``harvey.fit`` scores how ready a lead is to contact: a named decision
maker and a verified email. That says nothing about whether the brand would
buy. This scores the brand, from what reading its website and search
results showed (all free signals), weighed against the ICP:

    sector            up to 25   points per industry in icp.brand_fit.sectors
    Instagram spend   30         Meta pixel on the site (paid social today);
                      or 12      an Instagram account but no Meta pixel
    Google ads        5
    D2C store         10         Shopify / WooCommerce / Magento storefront
    marketing push    15         hiring a role in icp.hiring_signals; or
                      10 / 5     2+ / 1 marketing people found
    funding           5          a funding round in the news
    LinkedIn page     5          an established company page
    priority city     5          located in icp.geography
    like a liked one  10         same sector and 2+ of the same signals as a
                                 brand the team liked, or found by searching
                                 for brands like it (harvey.lookalikes)

Junk companies (harvey.junk) and the do-not-contact list score 0. Every
weight can be overridden under ``icp.brand_fit`` in the config.
"""

from __future__ import annotations

import re

from harvey.fit import _hiring_terms, _role_matches, current_roles
from harvey.gate import do_not_contact_match

DEFAULTS = {
    # Industry name (as in icp.industries) -> points. An ICP industry not
    # listed gets other_icp_sector; a sector outside the ICP gets 0.
    "sectors": {},
    "other_icp_sector": 15,
    "meta_ads": 30,
    "instagram_only": 12,
    "google_ads": 5,
    "d2c_store": 10,
    "hiring_marketing": 15,
    "marketing_team": 10,       # 2+ marketing people found
    "marketing_person": 5,      # 1 marketing person found
    "funding": 5,
    "linkedin_page": 5,
    "priority_city": 5,
    "similar_to_liked": 10,
}
STOREFRONTS = re.compile(r"shopify|woocommerce|magento|bigcommerce|dukaan|shopflo|gokwik", re.I)
MARKETING_TITLE = re.compile(
    r"market|brand|social|influenc|growth|digital|communicat|\bpr\b|partnership|content|\bcmo\b", re.I)
# Observations brand_fit reads, for callers that load them selectively.
SIGNALS = ("RUNNING_META_ADS", "RUNNING_GOOGLE_ADS", "SOCIAL_PROFILE", "TECH_STACK",
           "FUNDING_ROUND", "LINKEDIN_PAGE", "HIRING_ROLE")


def weights(config) -> dict:
    custom = getattr(getattr(config, "icp", None), "brand_fit", None) or {}
    return {**DEFAULTS, **custom}


def traits(observations: list[dict]) -> set[str]:
    """The ad / storefront / social signals two brands can share."""
    out = set()
    if _latest_bool(observations, "RUNNING_META_ADS"):
        out.add("meta")
    if _latest_bool(observations, "RUNNING_GOOGLE_ADS"):
        out.add("google")
    if any((o.get("value_text") or "").lower().startswith("instagram:")
           for o in observations if o.get("signal_code") == "SOCIAL_PROFILE"):
        out.add("instagram")
    if STOREFRONTS.search(" ".join(o.get("value_text") or "" for o in observations
                                   if o.get("signal_code") == "TECH_STACK")):
        out.add("d2c")
    return out


def _latest_bool(observations: list[dict], code: str) -> bool | None:
    rows = sorted((o for o in observations if o.get("signal_code") == code),
                  key=lambda o: o.get("observed_at") or "")
    if not rows:
        return None
    return bool(rows[-1].get("value_num"))


def brand_fit(company: dict, prospects: list[dict], observations: list[dict],
              config, dnc: list[str] | None = None,
              liked: list[dict] | None = None) -> tuple[int, list[str]]:
    """(score, reasons) for one company. Reasons read like "+30 running Meta ads".

    ``liked``: the team's liked brands, each {"id", "name", "industry", "traits"}."""
    icp = config.icp
    w = weights(config)
    if dnc is None:
        dnc = getattr(getattr(config, "compliance", None), "do_not_contact", None) or []
    domain = (company.get("domain") or "").lower()
    if domain and dnc and do_not_contact_match(f"x@{domain}", dnc):
        return 0, ["on the do-not-contact list"]
    if company.get("junk"):
        return 0, [f"junk: {company['junk']}"]

    score, reasons = 0, []

    def add(points: int, why: str):
        nonlocal score
        if points:
            score += points
            reasons.append(f"+{points} {why}")

    industry = (company.get("industry") or "").strip()
    sectors = {k.strip().lower(): v for k, v in (w["sectors"] or {}).items()}
    in_icp = industry.lower() in {i.strip().lower() for i in (icp.industries or [])}
    points = sectors.get(industry.lower(), w["other_icp_sector"] if in_icp else 0)
    if points:
        add(int(points), f"sector: {industry}")
    elif industry:
        reasons.append(f"+0 sector outside the ICP: {industry}")

    meta = _latest_bool(observations, "RUNNING_META_ADS")
    instagram = any((o.get("value_text") or "").lower().startswith("instagram:")
                    for o in observations if o.get("signal_code") == "SOCIAL_PROFILE")
    if meta:
        add(w["meta_ads"], "running Meta ads (spends on Instagram)")
    elif instagram:
        add(w["instagram_only"], "on Instagram, no Meta ads seen")
    if _latest_bool(observations, "RUNNING_GOOGLE_ADS"):
        add(w["google_ads"], "running Google ads")

    stack = " ".join(o.get("value_text") or "" for o in observations if o.get("signal_code") == "TECH_STACK")
    if store := STOREFRONTS.search(stack):
        add(w["d2c_store"], f"D2C store ({store.group(0).title()})")

    terms = _hiring_terms(icp)
    hit = next((t for o in current_roles(observations) if (t := _role_matches(o.get("value_text"), terms))), "")
    marketers = [p for p in prospects if (p.get("first_name") or "").strip()
                 and p.get("status") not in ("lost", "opted_out")
                 and MARKETING_TITLE.search(p.get("title") or "")]
    if hit:
        add(w["hiring_marketing"], f"hiring: {hit}")
    elif len(marketers) >= 2:
        add(w["marketing_team"], f"marketing team ({len(marketers)} people found)")
    elif marketers:
        add(w["marketing_person"], "a marketing person found")

    if any(o.get("signal_code") == "FUNDING_ROUND" and (o.get("value_num") or (o.get("value_text") or "").strip())
           for o in observations):
        add(w["funding"], "recent funding")
    if any(o.get("signal_code") == "LINKEDIN_PAGE" for o in observations):
        add(w["linkedin_page"], "LinkedIn company page")
    city = (company.get("location") or "").strip().lower()
    if city and city in {g.strip().lower() for g in (icp.geography or [])}:
        add(w["priority_city"], f"priority city: {company['location']}")

    if company.get("source") == "lookalike":
        note = company.get("notes") or "Similar to a liked brand"
        add(w["similar_to_liked"], note[:1].lower() + note[1:] + " (liked)")
    else:
        mine = traits(observations)
        twin = next((b for b in liked or [] if b["id"] != company.get("id")
                     and (b.get("industry") or "").lower() == industry.lower()
                     and len(mine & b["traits"]) >= 2), None)
        if twin:
            add(w["similar_to_liked"], f"like {twin['name']} (liked)")
    if any(b["id"] == company.get("id") for b in liked or []):
        reasons.append("liked by the team")

    if meta is None and not company.get("profile_checked_at"):
        reasons.append("website not read yet: score will change")
    return min(score, 100), reasons
