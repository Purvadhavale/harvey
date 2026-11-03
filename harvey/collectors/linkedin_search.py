"""People at a company, from Google's index of public LinkedIn profiles.

Harvey never opens LinkedIn itself. A Google search restricted to
linkedin.com/in returns each public profile as a result whose title reads
"Name - Title - Company | LinkedIn" and whose snippet usually says
"Experience: Company · Location: City". That is enough for a name, a title,
the profile URL and whether the person works at the brand.

Google's copy can be months old, so someone who has moved on can still show
the old employer. The email check that follows is what catches that.
"""

import re
from urllib.parse import urlparse

# Words that don't identify a brand and so don't count toward a name match.
_GENERIC = frozenset("""
pvt private ltd limited llp inc co company india indian the and of by official store
shop online brand brands group foods food beverages fashion lifestyle shoes
""".split())

# Titles worth a row when none of the configured titles match, best first.
FALLBACK_KEYWORDS = (
    "chief marketing", "cmo", "head of marketing", "marketing head", "brand head",
    "head of brand", "influencer", "social media", "brand manager", "brand marketing",
    "digital marketing", "growth", "marketing", "brand", "partnerships",
    "founder", "co-founder", "ceo", "director",
)

_FORMER = re.compile(r"(?i)\b(former|formerly|ex[-\s]|previously|past:)")
_CREDENTIALS = re.compile(r",?\s*\b(MBA|PhD|CFA|CA|CPA|PMP)\b\.?", re.I)


def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def brand_tokens(name: str, domain: str) -> list[str]:
    """Strings whose presence in a result ties it to this brand: the brand
    name without generic words, and the domain's name ("labeldc")."""
    words = [w for w in re.findall(r"[a-z0-9]+", (name or "").lower()) if w not in _GENERIC]
    tokens = []
    if words:
        tokens.append("".join(words))
    label = (domain or "").lower().removeprefix("www.").split(".")[0]
    if len(label) >= 4:
        tokens.append(re.sub(r"[^a-z0-9]", "", label))
    return [t for t in dict.fromkeys(tokens) if len(t) >= 3]


def parse_result(title: str, snippet: str, link: str) -> dict | None:
    """{first_name, last_name, title, company, linkedin_url} from one search
    result, or None when it isn't a person's profile."""
    path = urlparse(link or "").path
    if not re.match(r"^/in/[^/]+/?$", path):
        return None
    head = re.split(r"\s*\|\s*LinkedIn", title or "", maxsplit=1)[0].strip()
    parts = [p.strip() for p in re.split(r"\s+[-–—]\s+", head) if p.strip()]
    if not parts:
        return None
    name = _CREDENTIALS.sub("", parts[0]).strip(" ,")
    names = [n for n in name.split() if re.fullmatch(r"[A-Za-z][A-Za-z.'-]*", n)]
    if len(names) < 2 or len(names) > 4:
        return None
    role, company = "", ""
    if len(parts) >= 3:
        role, company = parts[1], parts[2]
    elif len(parts) == 2:
        m = re.match(r"(?i)^(.*?)\s+(?:at|@)\s+(.+)$", parts[1])
        if m:
            role, company = m.group(1), m.group(2)
        else:
            company = parts[1]
    if not role and snippet:
        m = re.match(r"(?i)^\s*([^·.]{3,80}?)\s+(?:at|@)\s+([^·.]{2,60})", snippet)
        if m:
            role, company = m.group(1).strip(), company or m.group(2).strip()
    if not company and snippet:
        m = re.search(r"(?i)Experience:\s*([^·]{2,60})", snippet)
        if m:
            company = m.group(1).strip()
    return {
        "first_name": names[0].title() if names[0].islower() else names[0],
        "last_name": " ".join(n.title() if n.islower() else n for n in names[1:]),
        "title": role[:80],
        "company": company[:80],
        "linkedin_url": f"https://www.linkedin.com{path.rstrip('/')}",
    }


def works_at(person: dict, snippet: str, tokens: list[str]) -> bool:
    """Does the result tie this person to the brand now? The company field
    or the snippet's "Experience" must name it, and not as a former job."""
    if not tokens:
        return False
    current = _alnum(person.get("company", "")) + _alnum(person.get("title", ""))
    experience = ""
    m = re.search(r"(?i)Experience:\s*([^·]{2,60})", snippet or "")
    if m:
        experience = _alnum(m.group(1))
    if not any(t in current or t in experience for t in tokens):
        return False
    return not _FORMER.search(person.get("title", ""))


_INDIA = re.compile(
    r"(?i)\b(india|mumbai|bombay|delhi|ncr|new delhi|gurugram|gurgaon|noida|bengaluru|bangalore|"
    r"pune|hyderabad|chennai|kolkata|ahmedabad|jaipur|goa|surat|lucknow|chandigarh|indore|kochi|"
    r"thane|navi mumbai|maharashtra|karnataka|telangana|tamil nadu|gujarat|rajasthan|kerala|"
    r"west bengal|haryana|uttar pradesh|punjab)\b")


def outside_country(snippet: str, country: str, link: str = "") -> bool:
    """True when the result places the person outside the search country:
    a country-coded profile host ("uk.linkedin.com" when searching India),
    or a snippet "Location:" elsewhere. Only India is known; nothing shown
    is not outside."""
    if (country or "").lower() != "in":
        return False
    host = urlparse(link or "").netloc.lower()
    prefix = host.split(".")[0] if host.endswith(".linkedin.com") else ""
    if prefix and prefix not in ("www", "in", "m"):
        return True
    m = re.search(r"(?i)Location:\s*([^·]{2,80})", snippet or "")
    return bool(m) and not _INDIA.search(m.group(1))


def title_rank(title: str, priorities: list[str]) -> int | None:
    """Position of a title in the buyer list (lower is better), or None when
    it isn't a buyer. Configured titles come first, in their own order; then
    the fallback keywords."""
    t = (title or "").lower()
    if not t:
        return None
    for i, wanted in enumerate(priorities):
        words = [w for w in re.findall(r"[a-z0-9]+", wanted.lower())]
        if words and all(re.search(rf"\b{re.escape(w)}\b", t) for w in words):
            return i
    for j, kw in enumerate(FALLBACK_KEYWORDS):
        if re.search(rf"\b{re.escape(kw)}\b", t):
            return len(priorities) + j
    return None


def search_query(brand: str) -> str:
    return (f'site:linkedin.com/in "{brand}" '
            '(marketing OR brand OR influencer OR "social media" OR growth OR founder OR CMO)')
