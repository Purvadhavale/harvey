"""Phone extraction, the India search setting, reverify and the export join.

Numbers come from phonenumbers' own example numbers, never a real business.
"""

import os
import tempfile

import phonenumbers as pn
import pytest
import pytest_asyncio

from harvey.collectors import phones
from harvey.collectors.discover import DiscoveryQuery, build_queries
from harvey.collectors.profile import ProfileCollector
from harvey.export import CSV_COLUMNS, collect_prospects
from harvey.integrations import email_finder
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.signals import seed_signal_catalog
from harvey.state import StateManager


def _example(kind) -> tuple[str, str]:
    """(national digits, E.164) of phonenumbers' example IN number of this kind."""
    number = pn.example_number_for_type("IN", kind)
    return str(number.national_number), pn.format_number(number, pn.PhoneNumberFormat.E164)


MOBILE, MOBILE_E164 = _example(pn.PhoneNumberType.MOBILE)
# IN's example fixed-line number is one phonenumbers can't tell from a
# mobile, so a made-up sequential Mumbai (022) landline instead.
LANDLINE, LANDLINE_E164 = "2223456789", "+912223456789"


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


# ── extraction ──


def test_whatsapp_tel_and_text_numbers_are_found_and_typed():
    html = f"""<html><body>
      <a href="https://wa.me/91{MOBILE}?text=hi">Chat</a>
      <a href="tel:+91-{LANDLINE}">Call</a>
      <footer>Office: +91 {LANDLINE[:2]} {LANDLINE[2:]}</footer>
    </body></html>"""
    found = phones.extract_phones(html, "https://example.com", "IN")
    by_number = {p.e164: p for p in found}
    assert by_number[MOBILE_E164].type == "mobile"
    assert by_number[MOBILE_E164].method == "whatsapp"
    # The tel: link wins over the same number in text.
    assert by_number[LANDLINE_E164].method == "tel"
    assert by_number[LANDLINE_E164].type == "landline"
    assert found[0].e164 == MOBILE_E164         # mobiles rank first


def test_text_number_without_country_code_reads_in_region():
    found = phones.extract_phones(f"<p>Call {MOBILE[:5]} {MOBILE[5:]} today</p>", region="IN")
    assert [p.e164 for p in found] == [MOBILE_E164]
    assert found[0].method == "text"


def test_digits_that_are_not_phones_are_dropped():
    html = "<p>Order 1234567890123456 · PIN 400001 · GSTIN 27AAAAA0000A1Z5</p><script>var t='+91 " \
           f"{MOBILE}';</script>"
    assert phones.extract_phones(html, region="IN") == []


def test_validate_rejects_short_and_bad_numbers():
    assert phones.validate("12345", "IN") is None
    assert phones.validate("", "IN") is None
    assert phones.validate(f"+91{MOBILE}", "IN") == (MOBILE_E164, "mobile")


# ── profile writes companies.phone ──


@pytest.mark.asyncio
async def test_profile_saves_website_phone_and_keeps_valid_listing_phone(state):
    await seed_signal_catalog(state)
    site_only = await state.add_company(Company(name="Acme", domain="acme.example"))
    listed = await state.add_company(Company(name="Beta", domain="beta.example",
                                             phone=f"0{LANDLINE}"))
    collector = ProfileCollector(state, {"PHONE_FOUND"}, phone_region="IN")

    async def no_contact_page(site, paths):
        return "", ""
    collector._first_present = no_contact_page
    home = f'<a href="https://wa.me/91{MOBILE}">WhatsApp</a>'
    obs = []

    def add(code, *, num=None, text="", conf=1.0, url=""):
        obs.append((code, num, text))

    await collector._phones(site_only, "https://acme.example", home, add)
    company = await state.get_company(site_only)
    assert (company.phone, company.phone_type, company.phone_source) == (
        MOBILE_E164, "mobile", "website")
    assert obs and obs[0][0] == "PHONE_FOUND" and MOBILE_E164 in obs[0][2]

    await collector._phones(listed, "https://beta.example", home, add)
    company = await state.get_company(listed)
    # The listing's own valid number stays, now normalised and typed.
    assert (company.phone, company.phone_type, company.phone_source) == (
        LANDLINE_E164, "landline", "listing")


# ── search country ──


class _Cfg:
    class icp:
        industries = ["skincare brand"]
        geography = ["Mumbai"]
        keyword_variants = {}
        geo_coordinates = {}
        search_country = "in"

    class product:
        name = "x"


def test_queries_carry_search_country():
    queries = build_queries(_Cfg())
    assert queries and all(q.country == "in" for q in queries)
    assert DiscoveryQuery(term="x").country == ""


# ── reverify ──


@pytest.mark.asyncio
async def test_reverify_upgrades_decided_guesses_only(state, monkeypatch):
    cid = await state.add_company(Company(name="Acme", domain="acme.example"))
    for first, email in (("Ana", "ana@acme.example"), ("Ben", "ben@acme.example")):
        await state.add_prospect(Prospect(company_id=cid, first_name=first, last_name="Test",
                                          title="Founder", email=email, email_status="guess"))

    async def mx(domain):
        return "mx.acme.example"

    async def verdict(email, mx_type, env):
        return ("verified" if email.startswith("ana") else None), False

    monkeypatch.setattr(email_finder, "get_mx_host", mx)
    monkeypatch.setattr(email_finder, "_verify_candidate", verdict)
    counts = await email_finder.reverify_guesses(state, env=None)
    assert counts == {"verified": 1, "guess": 1}
    left = await state.prospects_with_email_status("guess")
    assert [p.email for p in left] == ["ben@acme.example"]


# ── export ──


@pytest.mark.asyncio
async def test_export_joins_company_phone_and_key_signals(state):
    await seed_signal_catalog(state)
    cid = await state.add_company(Company(name="Acme", domain="acme.example"))
    await state.set_company_phone(cid, MOBILE_E164, "mobile", "website")
    await state.add_observations([
        {"signal_code": "RUNNING_META_ADS", "company_id": cid, "collector": "profile",
         "value_num": 1, "value_text": "", "confidence": 1.0, "evidence_url": ""},
        {"signal_code": "TECH_STACK", "company_id": cid, "collector": "profile",
         "value_num": None, "value_text": "Shopify", "confidence": 1.0, "evidence_url": ""},
    ], run_id="r1")
    await state.add_prospect(Prospect(company_id=cid, first_name="Ana", last_name="Test",
                                      title="Founder", email="ana@acme.example",
                                      email_status="verified"))
    rows = await collect_prospects(state)
    assert len(rows) == 1
    row = rows[0]
    assert {"phone", "phone_type", "phone_source", "meta_ads", "tech_stack"} <= set(CSV_COLUMNS)
    assert (row["phone"], row["phone_type"], row["phone_source"]) == (
        MOBILE_E164, "mobile", "website")
    assert row["meta_ads"] == "yes"
    assert row["tech_stack"] == "Shopify"
    assert row["company_domain"] == "acme.example"


@pytest.mark.asyncio
async def test_export_leaves_out_an_unvalidated_listing_phone(state):
    cid = await state.add_company(Company(name="Acme", domain="acme.example", phone="12345"))
    await state.add_prospect(Prospect(company_id=cid, first_name="Ana", last_name="Test",
                                      title="Founder", email="ana@acme.example",
                                      email_status="verified"))
    row = (await collect_prospects(state))[0]
    assert row["phone"] == "" and row["phone_source"] == ""


def test_placeholder_numbers_are_not_phones():
    assert phones.validate("+91 99999 99999", "IN") is None
    assert phones.validate("+91 98989 89898", "IN") is None
