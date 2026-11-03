"""The Lead generator page: status counts, starting a step, the contacts list.

Fictional businesses and people only.
"""

import asyncio
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import harvey.dashboard as dash
from harvey.models.company import Company
from harvey.models.prospect import Prospect
from harvey.state import StateManager


@pytest.fixture
def client(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        db = Path(tmp) / "harvey.db"
        monkeypatch.setattr(dash, "DB_PATH", db)
        monkeypatch.setattr(dash, "WEB_DIR", Path(__file__).resolve().parent.parent / "harvey" / "web")
        monkeypatch.setattr("harvey.config.live_do_not_contact", lambda c: ["junk.example"])
        monkeypatch.setattr("harvey.config.load_config", lambda *a, **k: object())
        dash.token_store.configure(Path(tmp) / "data" / "dashboard-token")
        token = dash.token_store.get()
        with TestClient(dash.app, base_url="http://127.0.0.1:5555",
                        headers={"Authorization": f"Bearer {token}"}) as c:
            c.db_path = db
            yield c
        dash._leads_job = {}
        dash._leads_task = None


def _seed(db_path):
    async def go():
        state = StateManager(str(db_path))
        await state.init_db()
        cid = await state.add_company(Company(name="GlowCo", domain="glowco.example"))
        await state.add_company(Company(name="Junk", domain="junk.example"))
        await state.set_company_phone(cid, "+912223456789", "landline", "website")
        await state.add_prospect(Prospect(
            company_id=cid, company="GlowCo", first_name="Asha", last_name="Rao",
            title="Brand Manager", email="asha@glowco.example", email_status="verified",
            linkedin_url="https://www.linkedin.com/in/asha", source="linkedin_search"))
    asyncio.run(go())


def test_status_counts_brands_without_the_blocked_ones(client):
    _seed(client.db_path)
    s = client.get("/api/leads/status").json()
    assert (s["companies"], s["brands"], s["blocked"]) == (2, 1, 1)
    assert s["phones"] == 1
    assert s["contacts"] == 1 and s["named_contacts"] == 1 and s["linkedin_profiles"] == 1
    assert s["email_status"] == {"verified": 1}
    assert s["busy"] == "" and s["job"]["running"] is False


def test_contacts_carry_company_phone_and_linkedin(client):
    _seed(client.db_path)
    rows = client.get("/api/leads/contacts").json()["contacts"]
    assert rows[0]["linkedin_url"] == "https://www.linkedin.com/in/asha"
    assert rows[0]["phone"] == "+912223456789"


def test_reverify_step_runs_in_background_and_reports(client, monkeypatch):
    _seed(client.db_path)

    async def fake(state, env=None, limit=100):
        return {"verified": 2}
    monkeypatch.setattr("harvey.integrations.email_finder.reverify_guesses", fake)
    r = client.post("/api/leads/run", json={"step": "reverify"}).json()
    assert r["success"]
    for _ in range(50):
        job = client.get("/api/leads/status").json()["job"]
        if job.get("finished"):
            break
        time.sleep(0.05)
    assert job["result"]["email_status"] == {"verified": 2}
    assert "skipped" in job["result"]["crm_sync"]       # no key in tests: never the real CRM
    assert client.post("/api/leads/run", json={"step": "nope"}).status_code == 400


def test_brands_carry_phone_signals_and_people(client):
    _seed(client.db_path)
    brands = client.get("/api/leads/brands").json()["brands"]
    assert [b["domain"] for b in brands] == ["glowco.example"]     # junk.example is blocked
    b = brands[0]
    assert b["phone"] == "+912223456789" and b["phone_type"] == "landline"
    assert [(p["first_name"], p["email_status"]) for p in b["contacts"]] == [("Asha", "verified")]
    assert b["contacts"][0]["linkedin_url"] == "https://www.linkedin.com/in/asha"


def test_page_is_leads_only(client):
    html = client.get("/").text
    for tab in ('id="leads"', 'id="brands"', 'id="people"', 'id="find"'):
        assert tab in html
    # No sending or reply tracking: those tabs are gone from the page.
    for tab in ('id="outbox"', 'id="conversations"', 'id="campaigns"', 'id="controls"', 'id="today"'):
        assert tab not in html
