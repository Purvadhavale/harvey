"""Phone numbers from a business's own pages, validated before they count.

Three places a site puts a number: ``tel:`` links, WhatsApp links (``wa.me``
and ``api.whatsapp.com/send?phone=``), and plain text ("+91 98xxx xxxxx",
"022-2xxx xxxx"). Each candidate is parsed with ``phonenumbers`` against the
region prospects are in, and only numbers it calls valid survive: order ids,
pincodes and GST numbers look like digits too.

A number on the website is usually the brand's support line, not a
founder's, so every number keeps where it came from.
"""

import logging
import re
from dataclasses import dataclass
from html import unescape as html_unescape
from urllib.parse import parse_qs, unquote, urlparse

logger = logging.getLogger("harvey.collectors.phones")

MAX_TEXT_MATCHES = 20      # a page listing hundreds of numbers isn't a contact page

_TEL_RE = re.compile(r"""(?i)href\s*=\s*["']\s*tel:([^"']+)["']""")
_WA_RE = re.compile(
    r"""(?i)href\s*=\s*["']\s*(https?://(?:wa\.me|api\.whatsapp\.com|web\.whatsapp\.com)/[^"']*)["']""")
_SCRIPT_RE = re.compile(r"(?is)<(script|style|noscript)\b.*?</\1>")
_TAG_RE = re.compile(r"(?s)<[^>]+>")

# Better first: a WhatsApp number reaches a person's phone, a tel: link is one
# the business chose to make tappable, text is whatever else is on the page.
_METHOD_RANK = {"whatsapp": 0, "tel": 1, "text": 2}
# A mobile can take a WhatsApp message; a toll-free line is a call centre.
_TYPE_RANK = {"mobile": 0, "fixed_or_mobile": 1, "landline": 2, "voip": 3, "other": 4, "toll_free": 5}


@dataclass(frozen=True)
class Phone:
    e164: str            # "+91XXXXXXXXXX"
    type: str            # mobile | landline | fixed_or_mobile | toll_free | voip | other
    method: str          # whatsapp | tel | text
    url: str = ""        # page it was found on

    def label(self) -> str:
        return f"{self.e164} ({self.type}, {self.method})"


def _phonenumbers():
    try:
        import phonenumbers
        return phonenumbers
    except ImportError:
        logger.warning("phonenumbers is not installed: run `pip install phonenumbers` "
                       "to collect phone numbers")
        return None


def _type_name(pn, number) -> str:
    t = pn.number_type(number)
    T = pn.PhoneNumberType
    return {
        T.MOBILE: "mobile",
        T.FIXED_LINE: "landline",
        T.FIXED_LINE_OR_MOBILE: "fixed_or_mobile",
        T.TOLL_FREE: "toll_free",
        T.VOIP: "voip",
    }.get(t, "other")


def validate(raw: str, region: str = "IN") -> tuple[str, str] | None:
    """(E.164, type) for a number ``phonenumbers`` calls valid, else None.

    ``region`` reads numbers written without a country code ("98765 43210",
    "022 2345 6789"); a number with one ("+44 ...") is read as written.
    """
    pn = _phonenumbers()
    if pn is None or not raw:
        return None
    try:
        number = pn.parse(raw.strip(), (region or "IN").upper())
    except pn.NumberParseException:
        return None
    if not pn.is_valid_number(number):
        return None
    if len(set(str(number.national_number))) <= 2:
        return None     # 9999999999, 9898989898: placeholders, not lines
    return pn.format_number(number, pn.PhoneNumberFormat.E164), _type_name(pn, number)


def _whatsapp_number(url: str) -> str:
    """The number in a WhatsApp link, as "+<digits>", or ""."""
    parsed = urlparse(html_unescape(url))
    if parsed.netloc.lower().endswith("wa.me"):
        digits = re.sub(r"\D", "", parsed.path.strip("/").split("/")[0])
    else:
        digits = re.sub(r"\D", "", (parse_qs(parsed.query).get("phone") or [""])[0])
    # wa.me numbers are always full international numbers without the "+".
    return f"+{digits}" if len(digits) >= 8 else ""


def _visible_text(html: str) -> str:
    return re.sub(r"\s+", " ", html_unescape(_TAG_RE.sub(" ", _SCRIPT_RE.sub(" ", html))))


def extract_phones(html: str, url: str = "", region: str = "IN") -> list[Phone]:
    """Every valid number on one page, deduplicated, best first."""
    pn = _phonenumbers()
    if pn is None or not html:
        return []
    found: dict[str, Phone] = {}

    def keep(raw: str, method: str):
        checked = validate(raw, region)
        if not checked:
            return
        e164, kind = checked
        new = Phone(e164, kind, method, url)
        old = found.get(e164)
        if old is None or _METHOD_RANK[method] < _METHOD_RANK[old.method]:
            found[e164] = new

    for m in _WA_RE.finditer(html):
        keep(_whatsapp_number(m.group(1)), "whatsapp")
    for m in _TEL_RE.finditer(html):
        keep(unquote(html_unescape(m.group(1))).split(";")[0].split(",")[0], "tel")
    try:
        matches = pn.PhoneNumberMatcher(_visible_text(html), (region or "IN").upper(),
                                        leniency=pn.Leniency.VALID, max_tries=200)
        for i, match in enumerate(matches):
            if i >= MAX_TEXT_MATCHES:
                break
            keep(match.raw_string, "text")
    except Exception as e:      # the matcher is third-party; a bad page costs only its text numbers
        logger.debug(f"phone text scan failed on {url}: {e}")
    return rank(list(found.values()))


def rank(phones: list[Phone]) -> list[Phone]:
    """Best first: mobiles before landlines, WhatsApp before tel: before text."""
    return sorted(phones, key=lambda p: (_TYPE_RANK.get(p.type, 9), _METHOD_RANK.get(p.method, 9)))
