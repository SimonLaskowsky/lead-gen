# Testy autopilota bez sieci: python test_autopilot.py
import os
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace

os.environ["DB_PATH"] = os.path.join(tempfile.mkdtemp(), "autopilot.db")
os.environ["AUTOPILOT_WORKER"] = "0"
os.environ.pop("SMTP_USER", None)
os.environ.pop("SMTP_PASSWORD", None)
os.environ["ANTHROPIC_API_KEY"] = "test-key"
os.environ["GOOGLE_MAPS_API_KEY"] = "test-key"

import analyzer
import db
import mailer
import pipeline
import scraper
import worker
import app as flask_app

SENT = []


def fake_search_leads(business_type, city, max_results, no_website=False, known_names=None):
    candidates = [
        {"business_name": "Salon Ania", "phone": "111", "website_url": "https://salon-ania.pl", "address": "ul. A 1"},
        {"business_name": "Fryzjer Bez Strony", "phone": "222", "website_url": "", "address": "ul. B 2"},
        {"business_name": "Studio Booksy", "phone": "333", "website_url": "https://booksy.com/pl/x", "address": "ul. C 3"},
    ]
    known = known_names or set()
    fresh = [c for c in candidates if c["business_name"] not in known][:max_results]
    return {"leads": fresh, "skipped_known": len(candidates) - len(fresh), "exhausted": True}


def fake_scrape_website(url):
    if not url:
        return None
    if "booksy" in url:
        return {"outsourced_platform": "Booksy", "outsourced_pitch": "płacą prowizję", "tech_stack": []}
    return {"has_ssl": True, "has_mobile_viewport": False, "tech_stack": ["WordPress"], "title": "Salon"}


def fake_find_contact_email(url, data):
    return "kontakt@" + scraper._domain_of(url) if url else ""


def fake_analyze(lead, screenshots, website_data, impression=None):
    return {"scores": {"design": 4}, "analysis": "Strona do poprawy."}


def fake_generate_email(lead, website_data, ai_analysis=None, my_feedback=None, profile=None):
    return f"Temat: Uwagi do strony {lead['business_name']}\n\nDzień dobry,\ntreść maila.\n\n{profile['name']}"


def fake_generate_followup(lead, followup_number=1):
    return f"Dzień dobry, follow-up numer {followup_number}."


def fake_send(mailbox, to_address, subject, body, in_reply_to="", references=None):
    message_id = f"<msg{len(SENT) + 1}@test.local>"
    SENT.append({"to": to_address, "subject": subject, "body": body, "in_reply_to": in_reply_to,
                 "references": list(references or []), "from": mailbox.address, "message_id": message_id})
    return message_id


scraper.search_leads = fake_search_leads
scraper.scrape_website = fake_scrape_website
scraper.find_contact_email = fake_find_contact_email
scraper.screenshot_website = lambda url: {}
analyzer.analyze_website_visually = fake_analyze
analyzer.first_impression = lambda lead, screenshots: None
analyzer.generate_email = fake_generate_email
analyzer.generate_followup = fake_generate_followup
mailer.send = fake_send


def setup_mailbox():
    profile = db.get_profiles()[0]
    db.update_profile(profile["id"], mailbox_address="szymon@gmail.com", mailbox_password="app-pass")
    return profile["id"]


def allow_sending_now():
    db.set_settings(auto_send="on", weekdays_only="0", send_from_hour="0", send_to_hour="24", send_gap_minutes="0")
    worker.STATE["next_send_after"] = None
    worker.STATE["consecutive_send_failures"] = 0


def test_settings_defaults_and_roundtrip():
    settings = db.get_settings()
    assert settings["auto_send"] == "off"
    assert settings["daily_limit"] == "10"
    db.set_settings(daily_limit=5)
    assert db.get_settings()["daily_limit"] == "5"
    db.set_settings(daily_limit=10)


def test_mailbox_defaults_from_provider():
    box = mailer.mailbox_for({"name": "Szymon", "mailbox_address": "x@gmail.com", "mailbox_password": "p"})
    assert (box.smtp_host, box.smtp_port) == ("smtp.gmail.com", 587)
    box = mailer.mailbox_for({"name": "Szymon", "mailbox_address": "x@wp.pl", "mailbox_password": "p"})
    assert box.smtp_port == 465 and box.smtp_host == "smtp.wp.pl"
    custom = mailer.mailbox_for({"name": "S", "mailbox_address": "x@firma.pl", "mailbox_password": "p",
                                 "smtp_host": "mail.firma.pl", "smtp_port": "465"})
    assert custom.smtp_host == "mail.firma.pl" and custom.smtp_port == 465
    broken_port = mailer.mailbox_for({"name": "S", "mailbox_address": "x@gmail.com", "mailbox_password": "p", "smtp_port": "abc"})
    assert broken_port.smtp_port == 587
    assert mailer.mailbox_for({"name": "Nikodem"}) is None
    assert "brak hosta SMTP" in mailer.describe(mailer.mailbox_for({"name": "S", "mailbox_address": "x@firma.pl", "mailbox_password": "p"}))


def test_build_message_threading_headers():
    box = mailer.mailbox_for({"name": "Szymon Laskowski", "mailbox_address": "x@gmail.com", "mailbox_password": "p"})
    first = mailer.build_message(box, "a@b.pl", "Temat", "Treść")
    assert first["Message-ID"].startswith("<") and first["Message-ID"].endswith("@gmail.com>")
    assert "In-Reply-To" not in first
    followup = mailer.build_message(box, "a@b.pl", "Re: Temat", "Ping", in_reply_to="<1@x>", references=["<1@x>", "<2@x>"])
    assert followup["In-Reply-To"] == "<1@x>"
    assert followup["References"] == "<1@x> <2@x>"
    assert "Szymon Laskowski" in followup["From"]


def test_send_allowed_rules():
    settings = dict(db.SETTING_DEFAULTS)
    monday_noon = datetime(2026, 9, 7, 12, 0)
    assert worker.send_allowed(settings, monday_noon, 0, None)[0]
    assert not worker.send_allowed(settings, datetime(2026, 9, 5, 12, 0), 0, None)[0]
    assert not worker.send_allowed(settings, datetime(2026, 9, 7, 7, 0), 0, None)[0]
    assert not worker.send_allowed(settings, monday_noon, 10, None)[0]
    assert not worker.send_allowed(settings, monday_noon, 0, monday_noon - timedelta(minutes=2))[0]
    assert worker.send_allowed(settings, monday_noon, 0, monday_noon - timedelta(minutes=10))[0]
    settings["weekdays_only"] = "0"
    assert worker.send_allowed(settings, datetime(2026, 9, 5, 12, 0), 0, None)[0]


def test_split_and_join_subject():
    subject, body = pipeline.split_subject("Temat: Cześć\n\nDzień dobry,\nx")
    assert subject == "Cześć" and body == "Dzień dobry,\nx"
    assert pipeline.split_subject("bez tematu") == ("", "bez tematu")
    assert pipeline.join_subject("A", "B") == "Temat: A\n\nB"


def test_usage_recording_and_cost():
    fake_message = SimpleNamespace(model="claude-opus-5", usage=SimpleNamespace(input_tokens=1000, output_tokens=500))
    analyzer._record(fake_message, "test")
    analyzer._record(SimpleNamespace(model="claude-opus-5", usage=None), "test")
    spend = pipeline.spend_summary()
    assert spend["today_calls"] == 1
    assert abs(spend["today_usd"] - (1000 * 5 + 500 * 25) / 1_000_000) < 0.001, spend
    assert pipeline.price_for("claude-sonnet-5") == (2.0, 10.0)
    assert pipeline.price_for("nieznany-model") == pipeline.FALLBACK_PRICE


def test_full_autopilot_cycle():
    db.init_db()
    profile_id = setup_mailbox()
    campaign_id = db.add_campaign("fryzjer", "Kraków", target_count=3, profile_id=profile_id)

    worker.tick()
    campaign = db.get_campaign(campaign_id)
    assert campaign["found_count"] == 3, campaign
    assert campaign["active"] == 0, "cel osiągnięty: kampania ma się wyłączyć"
    assert db.count_awaiting_preparation() == 2, "tylko leady z e-mailem czekają na mail"

    worker.tick()
    lead = db.get_leads(search="Salon Ania")[0]
    assert lead["status"] == "ready", lead["status"]
    assert lead["generated_email"].startswith("Temat: Uwagi do strony Salon Ania")
    assert lead["ai_analysis"] != ""
    assert db.count_awaiting_preparation() == 1

    worker.tick()
    booksy = db.get_leads(search="Studio Booksy")[0]
    assert booksy["status"] == "ready" and "Booksy" in booksy["ai_analysis"]
    assert db.count_awaiting_preparation() == 0
    assert [m["kind"] for m in db.get_queue()] == ["initial", "initial"]

    worker.tick()
    assert SENT == [], "auto-wysyłka wyłączona: nic nie wychodzi"

    allow_sending_now()
    worker.tick()
    assert len(SENT) == 1, SENT
    assert SENT[0]["to"] == "kontakt@salon-ania.pl"
    assert SENT[0]["from"] == "szymon@gmail.com"
    lead = db.get_lead(lead["id"])
    assert lead["status"] == "emailed" and lead["emailed_at"]
    assert len(db.get_queue()) == 1, "odstęp między mailami: drugi czeka"

    worker.STATE["next_send_after"] = None
    worker.tick()
    assert len(SENT) == 2 and SENT[1]["to"] == "kontakt@booksy.com"
    assert db.get_queue() == []

    db.set_settings(daily_limit="2")
    worker.STATE["next_send_after"] = None
    extra = db.add_lead(business_name="Trzeci", city="Kraków", email="t@t.pl", status="new", autopilot=1)
    pipeline.queue_message(db.get_lead(extra), "initial", "T", "B")
    worker.tick()
    assert len(SENT) == 2, "dzienny limit blokuje trzeci mail"
    assert "limit" in worker.STATE["send_note"]
    db.set_settings(daily_limit="10")


def test_manual_followup_goes_in_thread():
    client = flask_app.app.test_client()
    lead = db.get_leads(search="Salon Ania")[0]
    res = client.post(f"/api/lead/{lead['id']}/generate-followup", json={})
    data = res.get_json()
    assert res.status_code == 200, data
    assert data["number"] == 1 and data["subject"] == "Re: Uwagi do strony Salon Ania"
    assert db.get_queue() == [] or all(m["lead_id"] != lead["id"] for m in db.get_queue()), "sam follow-up nie idzie do kolejki"

    allow_sending_now()
    sent_before = len(SENT)
    res = client.post(f"/api/lead/{lead['id']}/queue", json={"subject": data["subject"], "body": data["followup"], "kind": "followup", "send_now": True})
    assert res.status_code == 200, res.get_json()
    assert len(SENT) == sent_before + 1
    assert SENT[-1]["in_reply_to"] == "<msg1@test.local>"
    assert SENT[-1]["references"] == ["<msg1@test.local>"]
    assert db.get_lead(lead["id"])["status"] == "emailed"


def test_missing_keys_pause_steps_without_failing_leads():
    lead_id = db.add_lead(business_name="Bez Klucza", city="Opole", email="x@bezklucza.pl", status="new", autopilot=1)
    saved = os.environ.pop("ANTHROPIC_API_KEY")
    try:
        worker.step_prepare()
        worker.step_prepare()
    finally:
        os.environ["ANTHROPIC_API_KEY"] = saved
    assert db.get_lead(lead_id)["status"] == "new", "bez klucza lead nie może dostać statusu błąd"
    warnings = [entry for entry in worker.LOG if "ANTHROPIC_API_KEY" in entry["text"]]
    assert len(warnings) == 1, "ostrzeżenie o braku klucza ma pojawić się raz"
    db.update_lead(lead_id, autopilot=0)


def test_send_failures_switch_auto_send_off():
    allow_sending_now()
    for leftover in db.get_queue():
        db.update_message(leftover["id"], status="cancelled")
    lead_id = db.add_lead(business_name="Bez Skrzynki", city="Łódź", email="x@y.pl", status="new", autopilot=1)
    nikodem = db.get_profiles()[1]
    db.update_lead(lead_id, profile_id=nikodem["id"])
    pipeline.queue_message(db.get_lead(lead_id), "initial", "T", "B")
    for _ in range(3):
        worker.STATE["next_send_after"] = None
        queued = [m for m in db.get_queue() if m["lead_id"] == lead_id]
        if queued:
            db.update_message(queued[0]["id"], status="queued")
        worker.step_send()
    assert db.get_settings()["auto_send"] == "off"
    failed = [m for m in db.get_lead_messages(lead_id) if m["status"] == "failed"]
    assert failed and "skrzynki" in failed[0]["error"]


def test_api_queue_and_campaign_routes():
    client = flask_app.app.test_client()
    res = client.post("/api/campaigns", json={"business_type": "mechanik", "city": "Gdańsk", "target_count": 5})
    campaign_id = res.get_json()["id"]
    assert any(c["id"] == campaign_id for c in client.get("/api/campaigns").get_json())
    client.post(f"/api/campaigns/{campaign_id}/update", json={"active": False})
    assert db.get_campaign(campaign_id)["active"] == 0

    lead_id = db.add_lead(business_name="Ręczny Lead", city="Gdańsk", email="r@l.pl")
    res = client.post(f"/api/lead/{lead_id}/queue", json={"subject": "Hej", "body": "Treść", "kind": "initial"})
    assert res.status_code == 200, res.get_json()
    assert db.get_lead(lead_id)["status"] == "ready"
    row = next(m for m in client.get("/api/queue").get_json() if m["lead_id"] == lead_id)
    res = client.post(f"/api/messages/{row['id']}/update", json={"subject": "Hej 2", "body": "Nowa treść"})
    assert res.status_code == 200
    assert db.get_lead(lead_id)["generated_email"] == "Temat: Hej 2\n\nNowa treść"
    sent_before = len(SENT)
    res = client.post(f"/api/messages/{row['id']}/send")
    assert res.status_code == 200, res.get_json()
    assert len(SENT) == sent_before + 1 and SENT[-1]["subject"] == "Hej 2"
    assert db.get_lead(lead_id)["status"] == "emailed"

    status = client.get("/api/autopilot").get_json()
    assert "log" in status and "mailboxes" in status and "spend" in status
    res = client.post("/api/autopilot/settings", json={"daily_limit": 999})
    assert res.status_code == 400
    res = client.post("/api/autopilot/settings", json={"daily_limit": 15, "auto_send": False})
    assert res.get_json()["daily_limit"] == "15" and res.get_json()["auto_send"] == "off"

    profiles = client.get("/api/profiles").get_json()
    assert "mailbox_password" not in profiles[0] and profiles[0]["has_mailbox_password"] is True
    client.post(f"/api/profiles/{profiles[0]['id']}/update", json={"mailbox_password": "", "name": profiles[0]["name"]})
    assert db.get_profile(profiles[0]["id"])["mailbox_password"] == "app-pass"


if __name__ == "__main__":
    db.init_db()
    test_settings_defaults_and_roundtrip()
    test_mailbox_defaults_from_provider()
    test_build_message_threading_headers()
    test_send_allowed_rules()
    test_split_and_join_subject()
    test_usage_recording_and_cost()
    test_full_autopilot_cycle()
    test_manual_followup_goes_in_thread()
    test_missing_keys_pause_steps_without_failing_leads()
    test_send_failures_switch_auto_send_off()
    test_api_queue_and_campaign_routes()
    print("OK, wszystkie testy autopilota przeszły")
