"""Social links, marketing inboxes and the LinkedIn people search.

Every business and person here is invented (example.* domains).
"""

import os
import tempfile
from types import SimpleNamespace

import pytest
import pytest_asyncio

from harvey.collectors import linkedin_search as li
from harvey.collectors.socials import instagram_handle, marketing_emails, social_links
from harvey.export import collect_prospects
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.signals import seed_signal_catalog
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


# ── socials ──


FOOTER = """
<a href="https://www.facebook.com/sharer/sharer.php?u=x">Share</a>
<a href="https://www.instagram.com/p/AbC123/">A post</a>
<footer>
  <a href="https://www.instagram.com/glowco.example/">Instagram</a>
  <a href="https://in.linkedin.com/company/glowco-example?trk=x">LinkedIn</a>
  <a href="https://www.youtube.com/@glowco">YouTube</a>
  <a href="//facebook.com/glowcoexample">Facebook</a>
  <a href="https://twitter.com/intent/tweet?text=hi">Tweet</a>
</footer>
<p>Collabs: <a href="mailto:collab@glowco.example">collab@glowco.example</a>
   Orders: support@glowco.example · Agency: hi@other.example</p>
"""


def test_profile_links_only_not_share_or_post_links():
    links = social_links(FOOTER)
    assert links == {
        "instagram": "https://www.instagram.com/glowco.example",
        "linkedin": "https://www.linkedin.com/company/glowco-example",
        "youtube": "https://www.youtube.com/@glowco",
        "facebook": "https://www.facebook.com/glowcoexample",
    }
    assert instagram_handle(links["instagram"]) == "@glowco.example"


def test_most_linked_profile_wins():
    html = ('<a href="https://instagram.com/partner.example">x</a>'
            + '<a href="https://instagram.com/glowco">y</a>' * 2)
    assert social_links(html)["instagram"] == "https://www.instagram.com/glowco"


def test_marketing_inbox_on_own_domain_only():
    assert marketing_emails(FOOTER, "glowco.example") == ["collab@glowco.example"]
    assert marketing_emails("<p>marketing@other.example</p>", "glowco.example") == []


# ── parsing LinkedIn results ──


def test_parse_three_part_title():
    p = li.parse_result("Asha Rao - Brand Manager - GlowCo | LinkedIn",
                        "Experience: GlowCo · Location: Mumbai",
                        "https://in.linkedin.com/in/asha-rao-123/")
    assert p == {"first_name": "Asha", "last_name": "Rao", "title": "Brand Manager",
                 "company": "GlowCo", "linkedin_url": "https://www.linkedin.com/in/asha-rao-123"}


def test_parse_at_form_and_snippet_fallback():
    p = li.parse_result("Vikram Shah, MBA – Head of Marketing at GlowCo | LinkedIn", "",
                        "https://www.linkedin.com/in/vshah")
    assert (p["first_name"], p["last_name"], p["title"], p["company"]) == (
        "Vikram", "Shah", "Head of Marketing", "GlowCo")
    p = li.parse_result("Neha Iyer - GlowCo | LinkedIn",
                        "Influencer Marketing Manager at GlowCo · Experience: GlowCo",
                        "https://www.linkedin.com/in/neha-iyer")
    assert p["title"] == "Influencer Marketing Manager"


def test_non_profile_links_are_ignored():
    assert li.parse_result("GlowCo | LinkedIn", "", "https://www.linkedin.com/company/glowco") is None
    assert li.parse_result("Posts | LinkedIn", "", "https://www.linkedin.com/in/x/recent-activity/") is None


def test_works_at_needs_the_brand_and_not_a_former_job():
    tokens = li.brand_tokens("GlowCo Pvt Ltd", "glowco.example")
    assert tokens == ["glowco"]
    now = {"title": "Brand Manager", "company": "GlowCo"}
    assert li.works_at(now, "", tokens)
    assert not li.works_at({"title": "Brand Manager", "company": "OtherCo"}, "Experience: OtherCo", tokens)
    assert not li.works_at({"title": "Former Brand Manager", "company": "GlowCo"}, "", tokens)


def test_title_rank_follows_configured_order_then_fallbacks():
    priorities = ["CMO", "Head of Marketing", "Brand Manager", "Founder"]
    assert li.title_rank("Head of Marketing", priorities) == 1
    assert li.title_rank("Senior Brand Manager", priorities) == 2
    assert li.title_rank("Social Media Lead", priorities) > 3
    assert li.title_rank("Software Engineer", priorities) is None


# ── the Scout strategy ──


@pytest.mark.asyncio
async def test_people_search_adds_best_ranked_current_staff(state, monkeypatch):
    from harvey.agents import scout as scout_mod
    from harvey.agents.scout import Scout

    cid = await state.add_company(Company(name="GlowCo", domain="glowco.example"))
    other = await state.add_company(Company(name="Junk Directory", domain="junk.example"))
    config = SimpleNamespace(
        icp=SimpleNamespace(titles=["Head of Marketing", "Brand Manager", "Founder"],
                            search_country="in"),
        compliance=SimpleNamespace(do_not_contact=["junk.example"]),
    )
    scout = Scout.__new__(Scout)
    scout.state, scout.config = state, config
    scout.env = SimpleNamespace(serper_api_key="fixture")
    scout._seen_prospect_keys, scout._site_evidence = set(), {}
    queries = []

    async def items(query, key):
        queries.append(query)
        return [
            {"title": "Ravi Kumar - Software Engineer - GlowCo | LinkedIn", "snippet": "",
             "link": "https://www.linkedin.com/in/ravi"},
            {"title": "Asha Rao - Brand Manager - GlowCo | LinkedIn", "snippet": "",
             "link": "https://www.linkedin.com/in/asha"},
            {"title": "Vikram Shah - Head of Marketing - GlowCo | LinkedIn", "snippet": "",
             "link": "https://www.linkedin.com/in/vikram"},
            {"title": "Neha Iyer - Founder - OtherCo | LinkedIn", "snippet": "Experience: OtherCo",
             "link": "https://www.linkedin.com/in/neha"},
            {"title": "Meera Das - Brand Manager - GlowCo | LinkedIn", "snippet": "",
             "link": "https://www.linkedin.com/in/meera"},
        ]

    async def resolve(first, last, domain):
        return f"{first.lower()}@{domain}", "verified"

    monkeypatch.setattr(scout, "_search_serper_items", items, raising=False)
    monkeypatch.setattr(scout, "_resolve_email", resolve, raising=False)
    monkeypatch.setattr("harvey.config.live_do_not_contact", lambda c: ["junk.example"])

    added = await Scout._prospect_via_people_search(scout)
    assert scout_mod.MAX_PEOPLE_PER_COMPANY == 2
    assert added == 3          # 2 with a checked email + the rest of the team (LinkedIn only)
    assert len(queries) == 1 and '"GlowCo"' in queries[0]      # junk company never searched
    rows = await collect_prospects(state)       # deliverable: the two with emails
    assert sorted((r["first_name"], r["title"], r["email"], r["linkedin_url"]) for r in rows) == [
        ("Asha", "Brand Manager", "asha@glowco.example", "https://www.linkedin.com/in/asha"),
        ("Vikram", "Head of Marketing", "vikram@glowco.example", "https://www.linkedin.com/in/vikram"),
    ]
    team = [r for r in await collect_prospects(state, include_all=True) if r["source"] == "linkedin_team"]
    assert [(r["first_name"], r["email"], r["linkedin_url"]) for r in team] == [
        ("Meera", "", "https://www.linkedin.com/in/meera")]
    # Searched once each: a second cycle spends nothing.
    assert await state.companies_for_people_search() == []
    assert other


@pytest.mark.asyncio
async def test_export_carries_linkedin_page_instagram_and_marketing_email(state):
    await seed_signal_catalog(state)
    cid = await state.add_company(Company(name="GlowCo", domain="glowco.example"))
    obs = [("LINKEDIN_PAGE", "https://www.linkedin.com/company/glowco"),
           ("SOCIAL_PROFILE", "instagram: @glowco"),
           ("SOCIAL_PROFILE", "youtube: https://www.youtube.com/@glowco"),
           ("MARKETING_EMAIL", "collab@glowco.example")]
    await state.add_observations([
        {"signal_code": c, "company_id": cid, "collector": "profile", "value_num": None,
         "value_text": t, "confidence": 1.0, "evidence_url": ""} for c, t in obs], run_id="r1")
    await state.add_prospect(Prospect(company_id=cid, first_name="Asha", last_name="Rao",
                                      title="Brand Manager", email="asha@glowco.example",
                                      email_status="verified"))
    row = (await collect_prospects(state))[0]
    assert row["linkedin_page"] == "https://www.linkedin.com/company/glowco"
    assert row["instagram"] == "@glowco"
    assert row["marketing_email"] == "collab@glowco.example"


def test_profiles_located_abroad_are_dropped_for_india():
    assert li.outside_country("Experience: GlowCo · Location: London, England", "in")
    assert not li.outside_country("Experience: GlowCo · Location: Mumbai, Maharashtra, India", "in")
    assert not li.outside_country("Experience: GlowCo", "in")          # no location shown
    assert not li.outside_country("Location: London", "")               # no country set
    assert li.outside_country("", "in", "https://uk.linkedin.com/in/someone")
    assert not li.outside_country("", "in", "https://in.linkedin.com/in/someone")
    assert not li.outside_country("", "in", "https://www.linkedin.com/in/someone")


@pytest.mark.asyncio
async def test_rejected_default_guess_tries_other_formats(monkeypatch):
    from harvey.integrations import email_finder as ef

    async def mx(domain):
        return "aspmx.l.google.com"

    async def pattern(domain, **kw):
        return "{f}.{l}", "default", 0.2

    async def verify(email, mx_type, env):
        return ("verified" if email == "asha@glowco.example" else "invalid"), False

    monkeypatch.setattr(ef, "get_mx_host", mx)
    monkeypatch.setattr(ef, "derive_pattern", pattern)
    monkeypatch.setattr(ef, "_verify_candidate", verify)
    result = await ef.find_email("Asha", "Rao", "glowco.example", env=object())
    assert (result.email, result.status, result.pattern) == ("asha@glowco.example", "verified", "{f}")
    none = await ef.find_email("Asha", "Rao", "glowco.example", env=object(), alternates=0)
    assert none.status == "invalid"
