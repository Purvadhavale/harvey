"""Which discovered companies are junk: not a brand that would buy.

Search results bring in government portals, startup directories, news sites
and IT agencies alongside real brands. Three verdicts, strongest first:

1. The team's tag in the CRM (junk or "real brand"): always wins.
2. Built-in rules: domains and names that are never a buyer (.gov.in,
   directories, job boards, incubators).
3. Learned: words in the name or domain that only ever showed up on brands
   the team tagged junk. Each tag teaches it; a "real brand" tag on a
   look-alike unteaches it.

Junk companies are skipped by every step that costs a search or a site
visit, and hidden in the CRM unless someone looks for them.
"""

import re
from collections import Counter

# (pattern on the domain, reason)
DOMAIN_RULES = [
    (r"(^|\.)(gov|nic|gov\.in|nic\.in|ac\.in|edu|edu\.in|res\.in|mil)$", "Government / education"),
    (r"(^|\.)(startupindia|startup|startups|investindia|meity|msme|dpiit)[.-]", "Government / startup portal"),
    (r"(builtin|tracxn|crunchbase|yourstory|inc42|startus-insights|f6s|angel\.co|wellfound|"
     r"zaubacorp|tofler|justdial|indiamart|sulekha|clutch\.co|goodfirms|g2\.com|"
     r"ambitionbox|glassdoor|naukri|indeed|internshala|shine\.com|foundit|rocketreach|zoominfo|"
     r"owler|leadiq|apollo\.io|signalhire|contactout|lusha|cbinsights|lbb\.in)", "Directory / listing site"),
    (r"(wikipedia|linkedin|facebook|instagram|youtube|twitter|(^|\.)x\.com$|quora|reddit|medium\.com|"
     r"pinterest|economictimes|livemint|moneycontrol|business-standard|thehindu|"
     r"timesofindia|ndtv|hindustantimes|forbesindia|entrackr|vccircle)", "News / social site"),
    (r"(^|\.)(zomato|district|swiggy|zepto|blinkit|bigbasket|instamart|flipkart|amazon|myntra|ajio|"
     r"tatacliq|meesho|snapdeal|jiomart|bookmyshow|makemytrip|goibibo|cleartrip|yatra|oyorooms|"
     r"urbancompany|dunzo|magicpin|eazydiner|dineout|paytm|phonepe|justdial|firstcry|purplle|"
     r"insider|skyscanner|tripadvisor|zomaland)\.", "Aggregator / marketplace"),
    (r"(news|magazine|blog|traveller|scribd|wanderlog|whatshot|startupsmap|guide|t-hub)",
     "News / media / guide"),
]
# (word in the name, reason)
NAME_RULES = [
    (r"\b(startup mission|government|ministry|initiative|incubat\w*|accelerator|"
     r"chamber of|association|council|foundation|university|college|institute)\b",
     "Government / incubator / institution"),
    (r"\b(built in|top \d+|list of|directory)\b", "Directory / listing site"),
    (r"\b(news|magazine|travel guide)\b", "News / media / guide"),
    (r"(^(how|what|why|when|where|which|top|best|list|guide|local brands)\b|\?)",
     "Article or web page, not a brand"),
    (r"\b(digital solutions|web ?tech|infotech|it solutions|software|web development|"
     r"digital marketing agency|technology park|science and technology)\b", "Agency / IT services"),
]
STOP = {"india", "indian", "the", "and", "of", "pvt", "ltd", "private", "limited", "co",
        "company", "group", "official", "online", "store", "shop", "brand", "brands", "com",
        "www", "in", "llp", "inc", "by", "for", "a"}
MIN_JUNK_TAGS = 2      # a word must be on this many junk tags...
                       # ...and on no brand tagged real, to count


def features(name: str, domain: str) -> set[str]:
    """Words that describe a company: name words, domain words, its suffix."""
    domain = (domain or "").lower().strip()
    label = domain.split(".")[0]
    words = set(re.findall(r"[a-z]{3,}", (name or "").lower()))
    words |= set(re.findall(r"[a-z]{3,}", label.replace("-", " ")))
    words -= STOP
    out = {"word:" + w for w in words}
    parts = domain.split(".")
    if len(parts) >= 3 and len(parts[-2]) <= 3:
        out.add("tld:" + ".".join(parts[-2:]))
    elif len(parts) >= 2:
        out.add("tld:" + parts[-1])
    return out


def icp_rules(config) -> list[tuple[str, re.Pattern | None, re.Pattern | None]]:
    """icp.not_icp as (reason, name pattern, domain pattern)."""
    out = []
    for reason, spec in (getattr(getattr(config, "icp", None), "not_icp", None) or {}).items():
        spec = spec or {}
        words = [re.escape(str(w).strip().lower()) for w in spec.get("words") or [] if str(w).strip()]
        domains = [re.escape(str(d).strip().lower()) for d in spec.get("domains") or [] if str(d).strip()]
        out.append((f"Not our ICP: {reason}",
                    re.compile(r"\b(" + "|".join(words) + r")\b") if words else None,
                    re.compile("|".join(domains)) if domains else None))
    return out


def rule_reason(name: str, domain: str, extra=()) -> str:
    """Why a company is junk by rule, or "". ``extra``: icp_rules(config)."""
    domain, name = (domain or "").lower(), (name or "").lower()
    for reason, by_name, by_domain in extra:
        if (by_name and by_name.search(name)) or (by_domain and by_domain.search(domain)):
            return reason
    for pattern, reason in DOMAIN_RULES:
        if re.search(pattern, domain):
            return reason
    for pattern, reason in NAME_RULES:
        if re.search(pattern, name):
            return reason
    return ""


def learn(tags: list[dict]) -> dict[str, int]:
    """Word -> how many junk tags it's on, for words never seen on a brand
    the team called real. Domain suffixes don't count: one .in junk tag
    mustn't hide every .in brand (the rules cover .gov.in and the like)."""
    junk, real = Counter(), Counter()
    for t in tags:
        (junk if t.get("junk") else real).update(features(t.get("name", ""), t.get("domain", "")))
    return {f: n for f, n in junk.items()
            if n >= MIN_JUNK_TAGS and not real[f] and f.startswith("word:")}


def judge(company: dict, tags_by_id: dict[str, dict], learned: dict[str, int],
          extra=()) -> tuple[str, str]:
    """(reason, source) — reason '' means a real brand. ``extra``: icp_rules(config)."""
    tag = tags_by_id.get(company["id"])
    if tag is not None:
        return ((tag.get("reason") or "Tagged junk by the team"), "team") if tag.get("junk") else ("", "team")
    name, domain = company.get("name", ""), company.get("domain", "")
    if reason := rule_reason(name, domain, extra):
        return reason, "rule"
    hits = sorted((f for f in features(name, domain) if f in learned), key=lambda f: -learned[f])
    if hits:
        f = hits[0]
        what = f.split(":", 1)[1]
        return (f"Like {learned[f]} brands the team marked junk ('{what}')", "learned")
    return "", ""


async def apply(state, config=None) -> dict:
    """Re-judge every company from the stored tags (and the config's
    icp.not_icp, when given). Returns counts by source."""
    import aiosqlite

    extra = icp_rules(config)
    tags = await state.brand_tags()
    by_id = {t["company_id"]: t for t in tags}
    learned = learn(tags)
    async with aiosqlite.connect(state.db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
                "SELECT id, name, domain, COALESCE(junk, '') AS junk, "
                "COALESCE(junk_source, '') AS junk_source FROM companies") as cur:
            companies = [dict(r) for r in await cur.fetchall()]
    changes, counts = {}, Counter()
    for c in companies:
        reason, source = judge(c, by_id, learned, extra)
        if reason:
            counts[source] += 1
        if (reason, source if reason else "") != (c["junk"], c["junk_source"]):
            changes[c["id"]] = (reason, source)
    if changes:
        await state.set_company_junk(changes)
    return {"team": counts["team"], "rule": counts["rule"], "learned": counts["learned"],
            "changed": len(changes), "patterns": len(learned)}


def summary(counts: dict) -> str:
    total = counts["team"] + counts["rule"] + counts["learned"]
    return (f"Junk: {total} hidden ({counts['team']} tagged by the team, "
            f"{counts['rule']} by rule, {counts['learned']} learned from "
            f"{counts['patterns']} patterns)")
