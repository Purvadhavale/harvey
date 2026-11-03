"""Syncing brands and contacts to Supabase. Fictional data; no network."""

import json
import os
import tempfile
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio

from harvey import supabase_sync as ss
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


CONFIG = SimpleNamespace(
    icp=SimpleNamespace(industries=["Footwear brand"], titles=["Head of Marketing", "Founder"],
                        hiring_signals=[], company_size="", geography=["Mumbai"]),
    compliance=SimpleNamespace(do_not_contact=["junk.example"]),
)


async def _seed(state):
    await seed_signal_catalog(state)
    cid = await state.add_company(Company(name="GlowCo", domain="glowco.example",
                                          industry="Footwear brand", location="Mumbai"))
    await state.add_company(Company(name="Junk", domain="junk.example"))
    await state.set_company_phone(cid, "+912223456789", "landline", "website")
    await state.add_observations([{"signal_code": "RUNNING_META_ADS", "company_id": cid,
        "collector": "profile", "value_num": 1, "value_text": "", "confidence": 1.0,
        "evidence_url": ""}], run_id="r1")
    await state.add_prospect(Prospect(company_id=cid, company="GlowCo", first_name="Asha",
        last_name="Rao", title="Head of Marketing", email="asha@glowco.example",
        email_status="verified", linkedin_url="https://www.linkedin.com/in/asha"))
    await state.add_prospect(Prospect(company_id=cid, company="GlowCo", title="General inbox",
        email="hello@glowco.example", email_status="verified", source="company_inbox"))
    return cid


@pytest.mark.asyncio
async def test_rows_carry_fit_next_step_signals_and_junk_flag(state, monkeypatch):
    monkeypatch.setattr("harvey.config.live_do_not_contact", lambda c: ["junk.example"])
    cid = await _seed(state)
    brands, contacts = await ss.build_rows(state, CONFIG)
    by_domain = {b["domain"]: b for b in brands}
    glow = by_domain["glowco.example"]
    assert glow["id"] == cid and glow["sector"] == "Footwear" and glow["city"] == "Mumbai"
    assert glow["next_step"] == "Email Asha"
    assert glow["meta_ads"] is True and glow["phone"] == "+912223456789"
    assert glow["fit"] > 0 and not glow["is_junk"]
    assert by_domain["junk.example"]["is_junk"] is True
    assert sorted((c["first_name"], c["is_inbox"]) for c in contacts) == [("", True), ("Asha", False)]


def test_next_step_falls_back_in_order():
    assert ss.next_step([], {"instagram": "@glowco"}, "", []) == "DM @glowco on Instagram"
    assert ss.next_step([], {}, "mobile", []) == "WhatsApp / call brand number"
    assert ss.next_step([{"first_name": "Ravi", "email_status": "invalid",
                          "linkedin_url": "https://www.linkedin.com/in/r"}], {}, "", []) == \
        "Connect with Ravi on LinkedIn"


@pytest.mark.asyncio
async def test_push_upserts_brands_before_contacts():
    calls = []

    def handler(request):
        calls.append((request.url.path, request.url.params.get("on_conflict"),
                      request.headers["Prefer"], len(json.loads(request.content))))
        return httpx.Response(201)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        counts = await ss.push([{"id": "b1"}], [{"id": "c1"}, {"id": "c2"}],
                               "https://proj.example", "secret", client=client)
    assert counts == (1, 2)
    assert [c[0] for c in calls] == ["/rest/v1/brands", "/rest/v1/contacts"]
    assert all(c[1] == "id" and "merge-duplicates" in c[2] for c in calls)


@pytest.mark.asyncio
async def test_push_reports_a_refused_upsert():
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda r: httpx.Response(401, text="bad key"))) as client:
        with pytest.raises(RuntimeError, match="HTTP 401"):
            await ss.push([{"id": "b1"}], [], "https://proj.example", "x", client=client)
