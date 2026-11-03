"""A brand's own social profiles and marketing inbox, from its website.

Brands link their LinkedIn page, Instagram, YouTube and Facebook from the
header or footer, and some publish a collab or marketing address. Share
buttons ("share on Facebook") and post links point at the same networks, so
only links to a profile itself count.
"""

import re
from collections import Counter
from html import unescape as html_unescape
from urllib.parse import urlparse

_HREF_RE = re.compile(r"""(?i)href\s*=\s*["']([^"']+)["']""")
_EMAIL_RE = re.compile(r"(?i)(?:mailto:)?([a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})")

# First path segments that are never a profile: posts, share and login pages.
_NOT_PROFILE = {
    "instagram": {"p", "reel", "reels", "explore", "accounts", "stories", "tv", "direct", "about", "developer", "legal"},
    "facebook": {"sharer", "sharer.php", "share", "dialog", "plugins", "tr", "login", "watch", "events", "groups", "hashtag", "photo.php", "help", "policies"},
    "youtube": {"watch", "embed", "results", "shorts", "playlist", "redirect", "iframe_api"},
    "x": {"intent", "share", "home", "search", "hashtag", "i"},
}

# Local parts of an address meant for marketing, collabs or press, best first.
MARKETING_LOCALS = (
    "collab", "collabs", "collaborations", "collaboration", "influencer", "influencers",
    "marketing", "brand", "brands", "partnerships", "partner", "partners", "pr", "media",
    "press", "social", "growth", "hello", "connect", "business",
)


def _network(host: str) -> str:
    host = host.lower().removeprefix("www.").removeprefix("m.").removeprefix("in.")
    if host.endswith("linkedin.com"):
        return "linkedin"
    if host in ("instagram.com", "instagr.am"):
        return "instagram"
    if host in ("facebook.com", "fb.com", "fb.me"):
        return "facebook"
    if host in ("youtube.com", "youtu.be"):
        return "youtube"
    if host in ("twitter.com", "x.com"):
        return "x"
    return ""


def _profile_url(net: str, parsed) -> str:
    """The canonical profile URL for a link, or "" when it isn't a profile."""
    parts = [p for p in parsed.path.split("/") if p]
    if net == "linkedin":
        # Company pages only: /company/<slug> or /showcase/<slug>.
        if len(parts) >= 2 and parts[0] in ("company", "showcase"):
            return f"https://www.linkedin.com/{parts[0]}/{parts[1]}"
        return ""
    if not parts or parts[0].lower() in _NOT_PROFILE.get(net, set()):
        return ""
    if net == "youtube":
        if parts[0].startswith("@"):
            return f"https://www.youtube.com/{parts[0]}"
        if parts[0] in ("channel", "c", "user") and len(parts) > 1:
            return f"https://www.youtube.com/{parts[0]}/{parts[1]}"
        return ""
    if not re.fullmatch(r"[A-Za-z0-9._-]{2,60}", parts[0]):
        return ""
    host = {"instagram": "www.instagram.com", "facebook": "www.facebook.com", "x": "x.com"}[net]
    return f"https://{host}/{parts[0]}"


def social_links(html: str) -> dict[str, str]:
    """{network: profile URL} for linkedin, instagram, facebook, youtube, x.
    When a page links several profiles on one network, the most linked wins
    (the footer link repeats; a partner's link usually doesn't)."""
    seen: dict[str, Counter] = {}
    for m in _HREF_RE.finditer(html or ""):
        href = html_unescape(m.group(1)).strip()
        if href.startswith("//"):
            href = "https:" + href
        parsed = urlparse(href)
        net = _network(parsed.netloc or "")
        if not net:
            continue
        url = _profile_url(net, parsed)
        if url:
            seen.setdefault(net, Counter())[url] += 1
    return {net: counts.most_common(1)[0][0] for net, counts in seen.items()}


def instagram_handle(url: str) -> str:
    parts = [p for p in urlparse(url).path.split("/") if p]
    return f"@{parts[0]}" if parts else ""


def marketing_emails(html: str, domain: str) -> list[str]:
    """Addresses on the brand's own domain whose local part reads as a
    marketing, collab or press inbox, best first."""
    domain = domain.lower().removeprefix("www.")
    found = set()
    for m in _EMAIL_RE.finditer(html_unescape(html or "")):
        email = m.group(1).lower().strip(".")
        local, _, host = email.partition("@")
        if not (host == domain or host.endswith("." + domain)):
            continue
        if re.sub(r"[^a-z]", "", local) in MARKETING_LOCALS or local in MARKETING_LOCALS:
            found.add(email)
    rank = {name: i for i, name in enumerate(MARKETING_LOCALS)}
    return sorted(found, key=lambda e: rank.get(re.sub(r"[^a-z]", "", e.split("@")[0]), 99))
