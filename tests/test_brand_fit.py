"""Brand fit for the ICP: sector, ad spend, storefront, marketing push. Fictional data."""

from types import SimpleNamespace

from harvey.brand_fit import brand_fit


def _config(**brand):
    return SimpleNamespace(
        icp=SimpleNamespace(industries=["Footwear brand", "Consumer Technology startup"],
                            titles=["Head of Marketing"], geography=["Mumbai"],
                            hiring_signals=["Social Media Manager"], fit_model="brand",
                            brand_fit={"sectors": {"Footwear brand": 25}, **brand}),
        compliance=SimpleNamespace(do_not_contact=["blocked.example"]))


def obs(code, num=None, text="", at="2026-11-01"):
    return {"signal_code": code, "value_num": num, "value_text": text, "observed_at": at}


COMPANY = {"id": "c1", "domain": "glowshoes.example", "industry": "Footwear brand",
           "location": "Mumbai", "profile_checked_at": "2026-11-01"}


def test_strong_brand_scores_high_with_reasons():
    score, reasons = brand_fit(COMPANY, [], [
        obs("RUNNING_META_ADS", 1.0), obs("RUNNING_GOOGLE_ADS", 1.0),
        obs("TECH_STACK", text="Google Analytics, Shopify"),
        obs("HIRING_ROLE", 1.0, "Social Media Manager"), obs("LINKEDIN_PAGE", text="x")], _config())
    assert score == 25 + 30 + 5 + 10 + 15 + 5 + 5
    assert "+30 running Meta ads (spends on Instagram)" in reasons
    assert "+10 D2C store (Shopify)" in reasons


def test_latest_meta_reading_wins_and_instagram_is_the_fallback():
    score, reasons = brand_fit(COMPANY, [], [
        obs("RUNNING_META_ADS", 1.0, at="2026-10-01"), obs("RUNNING_META_ADS", 0.0, at="2026-11-01"),
        obs("SOCIAL_PROFILE", text="instagram: @glow")], _config())
    assert score == 25 + 12 + 5
    assert "+12 on Instagram, no Meta ads seen" in reasons


def test_weights_and_sectors_come_from_config():
    other = {**COMPANY, "industry": "Consumer Technology startup", "location": "Goa"}
    assert brand_fit(other, [], [], _config(other_icp_sector=7))[0] == 7
    outside = {**COMPANY, "industry": "Plumber", "location": ""}
    assert brand_fit(outside, [], [], _config())[0] == 0


def test_marketing_people_count_when_not_hiring():
    people = [{"first_name": "Asha", "title": "Brand Manager"}, {"first_name": "Ravi", "title": "Head of Growth"},
              {"first_name": "Lee", "title": "CFO"}]
    score, reasons = brand_fit(COMPANY, people, [], _config())
    assert "+10 marketing team (2 people found)" in reasons
    assert brand_fit(COMPANY, people[:1], [], _config())[0] == 25 + 5 + 5


def test_junk_and_do_not_contact_score_zero():
    assert brand_fit({**COMPANY, "junk": "Aggregator / marketplace"}, [], [obs("RUNNING_META_ADS", 1.0)],
                     _config()) == (0, ["junk: Aggregator / marketplace"])
    assert brand_fit({**COMPANY, "domain": "blocked.example"}, [], [], _config())[0] == 0


def test_unread_website_is_flagged():
    _, reasons = brand_fit({**COMPANY, "profile_checked_at": None}, [], [], _config())
    assert reasons[-1] == "website not read yet: score will change"
