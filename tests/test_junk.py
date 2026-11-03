"""Junk brands: rules, learning from team tags, and skipping. Fictional data."""

import os
import tempfile

import httpx
import pytest
import pytest_asyncio

from harvey import junk
from harvey import supabase_sync as ss
from harvey.models.company import Company
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


@pytest.mark.parametrize("name,domain,expect", [
    ("Startup Goa", "startup.goa.gov.in", "Government / education"),
    ("Built In Chennai", "builtinchennai.in", "Directory / listing site"),
    ("Goa Startup Mission", "goamission.example", "Government / incubator / institution"),
    ("GlowCo", "glowco.example", ""),
    ("Netflix", "netflix.com", ""),          # not "x.com"
])
def test_rules(name, domain, expect):
    assert junk.rule_reason(name, domain) == expect


def test_learn_needs_two_tags_and_no_real_brand():
    tags = [{"name": "Acme Ventures", "domain": "acmeinfo.example", "junk": 1},
            {"name": "Zed Ventures", "domain": "zedinfo.example", "junk": 1},
            {"name": "Acme Shoes", "domain": "acmeshoes.example", "junk": 0}]
    learned = junk.learn(tags)
    assert learned == {"word:ventures": 2}           # "acme" is on a real brand
    reason, source = junk.judge({"id": "x", "name": "New Ventures", "domain": "new.example"}, {}, learned)
    assert source == "learned" and "ventures" in reason
    assert junk.judge({"id": "x", "name": "Acme Bags", "domain": "acmebags.example"}, {}, learned) == ("", "")


def test_team_tag_beats_rules():
    company = {"id": "c1", "name": "Built In Pune", "domain": "builtinpune.in"}
    assert junk.judge(company, {"c1": {"junk": 0}}, {}) == ("", "team")
    assert junk.judge({"id": "c2", "name": "GlowCo", "domain": "glowco.example"},
                      {"c2": {"junk": 1, "reason": "Agency"}}, {}) == ("Agency", "team")


@pytest.mark.asyncio
async def test_apply_skips_junk_and_restores_untagged(state):
    real = await state.add_company(Company(name="GlowCo", domain="glowco.example"))
    gov = await state.add_company(Company(name="Startup Goa", domain="startup.goa.gov.in"))
    counts = await junk.apply(state)
    assert counts["rule"] == 1
    assert [c["id"] for c in await state.companies_for_people_search()] == [real]
    assert [c["id"] for c in await state.companies_needing_profile()] == [real]

    await state.mark_people_searched(gov)
    await state.save_brand_tags([{"company_id": gov, "junk": 0}])      # team: it's real
    await junk.apply(state)
    assert {c["id"] for c in await state.companies_for_people_search()} == {real, gov}


@pytest.mark.asyncio
async def test_pull_tags_replaces_the_set(state):
    cid = await state.add_company(Company(name="Acme Ventures", domain="acme.example"))

    def handler(request):
        if "liked=is.true" in str(request.url):
            return httpx.Response(200, json=[])
        assert "junk=not.is.null" in str(request.url)
        return httpx.Response(200, json=[
            {"brand_id": cid, "junk": True, "junk_reason": "IT agency", "junk_by": "asha", "junk_at": None},
            {"brand_id": "unknown", "junk": True}])
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await ss.pull_tags(state, "https://x.supabase.co", "k", client=client) == 1
    tags = await state.brand_tags()
    assert tags[0]["reason"] == "IT agency" and tags[0]["junk"] == 1
    await junk.apply(state)
    from tests.test_supabase_sync import CONFIG
    rows, _ = await ss.build_rows(state, CONFIG)
    assert rows[0]["is_junk"] and rows[0]["junk_source"] == "team"
    assert rows[0]["next_step"] == "Skip: IT agency"
