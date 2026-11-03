"""Brands like the liked ones: names from snippets, search, likes, fit bonus. Fictional data."""

import json
import os
import tempfile
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from harvey import lookalikes
from harvey.brand_fit import brand_fit
from harvey.models.company import Company
from harvey.state import StateManager


@pytest_asyncio.fixture
async def state():
    with tempfile.TemporaryDirectory() as tmpdir:
        sm = StateManager(os.path.join(tmpdir, "test.db"))
        await sm.init_db()
        yield sm


CONFIG = SimpleNamespace(icp=SimpleNamespace(
    search_country="in", industries=["Footwear brand"], titles=[], geography=["Mumbai"],
    hiring_signals=[], fit_model="brand", brand_fit={}), compliance=SimpleNamespace(do_not_contact=[]))


def test_names_from_snippets():
    text = ("Top 10 Glowshoe Alternatives & Competitors. Top competitors of Glowshoe include "
            "Stridely, Walk Co, Metro Footwear and Peakstep. Glowshoe is a footwear company.")
    assert lookalikes.names_from_text(text, "Glowshoe") == ["Stridely", "Walk Co", "Metro Footwear", "Peakstep"]
    assert lookalikes.names_from_text("Brands like Glowshoe: Stridely, and more", "Glowshoe") == ["Stridely"]
    assert lookalikes.names_from_text("Shop the best shoes online at great prices") == []


@pytest.mark.asyncio
async def test_likes_keep_searched_at_and_drop_unliked(state):
    a = await state.add_company(Company(name="A", domain="a.example"))
    b = await state.add_company(Company(name="B", domain="b.example"))
    await state.save_brand_likes([{"company_id": a}, {"company_id": b}])
    await state.mark_like_searched(a)
    await state.save_brand_likes([{"company_id": a, "liked_by": "asha"}])
    likes = await state.brand_likes()
    assert [(x["company_id"], bool(x["searched_at"])) for x in likes] == [(a, True)]
    assert await state.brand_likes(unsearched_only=True) == []


@pytest.mark.asyncio
async def test_find_lookalikes_adds_sites_and_resolves_listed_names(state):
    liked = await state.add_company(Company(name="Glowshoe", domain="glowshoe.example",
                                            industry="Footwear brand", location="Mumbai"))
    await state.add_company(Company(name="Walk Co", domain="walkco.example"))      # already known
    await state.save_brand_likes([{"company_id": liked}])
    asked = []

    def handler(request):
        q = json.loads(request.content)["q"]
        asked.append(q)
        if q == "brands like Glowshoe India":
            return httpx.Response(200, json={"organic": [
                {"title": "Glowshoe - Official Site", "link": "https://glowshoe.example/"},
                {"title": "Stridely | Handmade Shoes", "link": "https://stridely.example/"},
                {"title": "Top 10 brands like Glowshoe", "link": "https://listicle.example/x",
                 "snippet": "Brands like Glowshoe include Peakstep, Walk Co and Stridely."}]})
        if q == "Peakstep official website":
            return httpx.Response(200, json={"organic": [{"title": "Peakstep", "link": "https://peakstep.example/"}]})
        return httpx.Response(200, json={"organic": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        counts = await lookalikes.find_lookalikes(state, CONFIG, "key", client=client)
    assert counts["added"] == 2 and counts["known"] == 0
    assert "Walk Co official website" in asked and "Stridely official website" not in asked
    new = await state.get_company_by_domain("peakstep.example")
    assert new.industry == "Footwear brand" and new.location == "Mumbai" and new.source == "lookalike"
    assert new.notes == "Similar to Glowshoe"
    assert await state.brand_likes(unsearched_only=True) == []         # once per like


def _obs(code, num=None, text=""):
    return {"signal_code": code, "value_num": num, "value_text": text, "observed_at": "2026-11-01"}


def test_fit_bonus_for_brands_like_a_liked_one():
    liked = [{"id": "L", "name": "Glowshoe", "industry": "Footwear brand", "traits": {"meta", "d2c"}}]
    twin = {"id": "c", "industry": "Footwear brand", "profile_checked_at": "x"}
    obs = [_obs("RUNNING_META_ADS", 1.0), _obs("TECH_STACK", text="Shopify")]
    _, reasons = brand_fit(twin, [], obs, CONFIG, liked=liked)
    assert "+10 like Glowshoe (liked)" in reasons
    _, reasons = brand_fit(twin, [], obs[:1], CONFIG, liked=liked)          # only one shared signal
    assert not any("liked" in r for r in reasons)
    found = {**twin, "source": "lookalike", "notes": "Similar to Glowshoe"}
    _, reasons = brand_fit(found, [], [], CONFIG, liked=liked)
    assert "+10 similar to Glowshoe (liked)" in reasons
