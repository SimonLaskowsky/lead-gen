from flask import Flask, render_template, request, jsonify, Response
from dotenv import load_dotenv
from datetime import datetime
import json
import os
import db
import mailer
import pipeline
import scraper
import worker

from pathlib import Path
load_dotenv(Path(__file__).parent / ".env")

app = Flask(__name__)
db.init_db()


@app.before_request
def auth_check():
    password = os.getenv("APP_PASSWORD", "")
    if not password:
        return  # local dev, no auth
    auth = request.authorization
    if not auth or auth.password != password:
        return Response(
            "Wymagane logowanie",
            401,
            {"WWW-Authenticate": 'Basic realm="Lead Gen"'},
        )


@app.route("/")
def index():
    stats = db.get_stats()
    cities = db.get_cities()
    business_types = db.get_business_types()
    return render_template("index.html", stats=stats, cities=cities, business_types=business_types)


@app.route("/api/leads")
def get_leads():
    status = request.args.get("status", "all")
    city = request.args.get("city", "")
    business_type = request.args.get("business_type", "")
    search = request.args.get("search", "")
    leads = db.get_leads(
        status=status,
        city=city or None,
        business_type=business_type or None,
        search=search or None,
    )
    for lead in leads:
        lead.pop("mockup_image", None)
    return jsonify(leads)


@app.route("/api/stats")
def get_stats():
    stats = db.get_stats()
    stats["costs"] = pipeline.COST_ESTIMATES_USD
    return jsonify(stats)


def _service_status(service, env_key):
    if not os.getenv(env_key):
        return {"state": "no_key", "detail": f"brak {env_key}"}
    last = pipeline.LAST_CALL[service]
    if not last:
        return {"state": "idle", "detail": "klucz jest, brak wywołań"}
    return {
        "state": "ok" if last["ok"] else "error",
        "detail": last["detail"],
        "at": last["at"],
    }


@app.route("/api/health")
def health():
    return jsonify({
        "google": _service_status("google", "GOOGLE_MAPS_API_KEY"),
        "ai": _service_status("ai", "ANTHROPIC_API_KEY"),
        "leads": db.get_stats()["total"],
    })


@app.route("/api/health/probe", methods=["POST"])
def health_probe():
    """Prawdziwe zapytanie do Google Maps, płatne, więc tylko na żądanie."""
    try:
        found = scraper.search_leads("kawiarnia", "Kraków", 1)
        pipeline.mark("google", True, f"test ok, {len(found['leads'])} wynik(ów)")
    except Exception as e:
        pipeline.mark("google", False, str(e)[:140])
    return health()


@app.route("/api/search", methods=["POST"])
def search():
    data = request.json or {}
    business_type = data.get("business_type", "").strip()
    city = data.get("city", "").strip()
    max_results = min(int(data.get("max_results", 10)), 60)
    no_website = bool(data.get("no_website"))
    autopilot = bool(data.get("autopilot"))

    if not business_type or not city:
        return jsonify({"error": "Podaj typ biznesu i miasto"}), 400

    try:
        result = pipeline.import_leads(business_type, city, max_results, no_website=no_website, autopilot=autopilot)
    except ValueError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"Błąd Google Maps API: {e}"}), 500
    return jsonify(result)


# ── Profile nadawców ──
@app.route("/api/profiles", methods=["GET", "POST"])
def profiles_collection():
    if request.method == "GET":
        return jsonify(db.get_public_profiles())
    pid = db.add_profile(**(request.json or {}))
    if not pid:
        return jsonify({"error": "Podaj unikalne imię i nazwisko"}), 400
    return jsonify({"id": pid})


@app.route("/api/profiles/<int:profile_id>/update", methods=["POST"])
def update_profile(profile_id):
    fields = dict(request.json or {})
    if not fields.get("mailbox_password"):
        fields.pop("mailbox_password", None)
    db.update_profile(profile_id, **fields)
    return jsonify({"ok": True})


@app.route("/api/profiles/<int:profile_id>", methods=["DELETE"])
def delete_profile(profile_id):
    db.delete_profile(profile_id)
    return jsonify({"ok": True})


@app.route("/api/profiles/<int:profile_id>/mailbox-test", methods=["POST"])
def test_profile_mailbox(profile_id):
    profile = db.get_profile(profile_id)
    if not profile:
        return jsonify({"error": "Nie znaleziono profilu"}), 404
    mailbox = mailer.mailbox_for(profile)
    if mailbox is None:
        return jsonify({"error": "Uzupełnij adres skrzynki i hasło aplikacji w profilu"}), 400
    result = mailer.test_connection(mailbox)
    result["description"] = mailer.describe(mailbox)
    return jsonify(result)


# ── Leady ──
@app.route("/api/lead/manual", methods=["POST"])
def add_manual_lead():
    data = request.json or {}
    name = data.get("business_name", "").strip()
    if not name:
        return jsonify({"error": "Podaj nazwę firmy"}), 400

    url = data.get("website_url", "").strip()
    email = data.get("email", "").strip()
    website_data = scraper.scrape_website(url) if url else None
    if url and not email:
        email = scraper.find_contact_email(url, website_data)

    lead_id = db.add_lead(
        business_name=name,
        email=email,
        phone=data.get("phone", "").strip(),
        website_url=url,
        address=data.get("address", "").strip(),
        business_type=data.get("business_type", "").strip(),
        city=data.get("city", "").strip(),
        notes=data.get("notes", "").strip(),
        profile_id=data.get("profile_id") or None,
        website_checks=json.dumps(website_data or {}),
        autopilot=1 if data.get("autopilot") else 0,
    )
    if not lead_id:
        return jsonify({"error": "Taki lead już jest w bazie (ta sama nazwa i miasto)"}), 409
    return jsonify({"id": lead_id, "email": email})


@app.route("/api/lead/<int:lead_id>")
def get_lead(lead_id):
    lead = db.get_lead(lead_id)
    if not lead:
        return jsonify({"error": "Nie znaleziono"}), 404
    lead["has_mockup"] = bool(lead.pop("mockup_image", None))
    try:
        lead["observations"] = json.loads(lead.get("observations") or "[]")
    except Exception:
        lead["observations"] = []
    return jsonify(lead)


@app.route("/api/lead/<int:lead_id>/messages")
def lead_messages(lead_id):
    return jsonify(db.get_lead_messages(lead_id))


@app.route("/api/lead/<int:lead_id>/analyze", methods=["GET", "POST"])
def analyze_lead(lead_id):
    lead = db.get_lead(lead_id)
    if not lead:
        return jsonify({"error": "Nie znaleziono"}), 404

    if request.method == "GET":
        return jsonify(pipeline.cached_analysis(lead))

    if not lead.get("website_url"):
        return jsonify({"error": "Brak strony do analizy"}), 400
    try:
        return jsonify(pipeline.run_analysis(lead))
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/api/lead/<int:lead_id>/generate-email", methods=["POST"])
def generate_email(lead_id):
    lead = db.get_lead(lead_id)
    if not lead:
        return jsonify({"error": "Nie znaleziono"}), 404
    payload = request.json or {}
    my_feedback = payload.get("my_feedback", "").strip()
    try:
        email_text = pipeline.prepare_email(lead, profile_id=payload.get("profile_id"), my_feedback=my_feedback)
        return jsonify({"email": email_text})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/lead/<int:lead_id>/generate-followup", methods=["POST"])
def generate_followup(lead_id):
    lead = db.get_lead(lead_id)
    if not lead:
        return jsonify({"error": "Nie znaleziono"}), 404
    try:
        result = pipeline.create_followup(lead)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"followup": result["text"], "number": result["number"], "subject": result["subject"]})


@app.route("/api/lead/<int:lead_id>/queue", methods=["POST"])
def queue_lead_message(lead_id):
    lead = db.get_lead(lead_id)
    if not lead:
        return jsonify({"error": "Nie znaleziono"}), 404
    if not lead.get("email"):
        return jsonify({"error": "Lead nie ma adresu e-mail, uzupełnij go w notatkach"}), 400
    payload = request.json or {}
    subject = (payload.get("subject") or "").strip() or pipeline.default_subject(lead)
    body = (payload.get("body") or "").strip()
    if not body:
        return jsonify({"error": "Treść maila jest pusta"}), 400
    kind = "followup" if payload.get("kind") == "followup" else "initial"
    already_contacted = lead["status"] in ("emailed", "replied") or db.sent_outbound_for_lead(lead_id)
    if already_contacted:
        kind = "followup"
    if payload.get("profile_id"):
        db.update_lead(lead_id, profile_id=payload["profile_id"])
        lead = db.get_lead(lead_id)
    send_now = bool(payload.get("send_now"))
    try:
        row_id = pipeline.queue_message(lead, kind, subject, body, send_now=send_now)
    except Exception as e:
        return jsonify({"error": str(e), "queued": True}), 500
    return jsonify({"id": row_id, "sent": send_now})


@app.route("/api/lead/<int:lead_id>/autopilot", methods=["POST"])
def set_lead_autopilot(lead_id):
    enabled = bool((request.json or {}).get("enabled"))
    db.update_lead(lead_id, autopilot=1 if enabled else 0)
    return jsonify({"ok": True, "enabled": enabled})


@app.route("/api/lead/<int:lead_id>/update", methods=["POST"])
def update_lead(lead_id):
    data = request.json or {}
    allowed = {"status", "notes", "email", "generated_email", "phone", "profile_id"}
    updates = {k: v for k, v in data.items() if k in allowed}

    if updates.get("status") == "emailed":
        updates["emailed_at"] = datetime.now().isoformat(timespec="seconds")
    if updates.get("status") in ("new", "replied", "converted", "skipped"):
        db.cancel_queued_for_lead(lead_id)

    db.update_lead(lead_id, **updates)
    return jsonify({"ok": True})


@app.route("/api/lead/<int:lead_id>", methods=["DELETE"])
def delete_lead(lead_id):
    db.delete_lead(lead_id)
    return jsonify({"ok": True})


# ── Autopilot ──
SETTING_LIMITS = {
    "daily_limit": (1, 200),
    "send_from_hour": (0, 23),
    "send_to_hour": (1, 24),
    "send_gap_minutes": (1, 240),
}


@app.route("/api/autopilot")
def autopilot_status():
    return jsonify(worker.status())


@app.route("/api/autopilot/settings", methods=["POST"])
def autopilot_settings():
    data = request.json or {}
    updates = {}
    if "auto_send" in data:
        updates["auto_send"] = "on" if data["auto_send"] in (True, "on", 1, "1") else "off"
    if "weekdays_only" in data:
        updates["weekdays_only"] = "1" if data["weekdays_only"] in (True, "1", 1, "on") else "0"
    for key, (low, high) in SETTING_LIMITS.items():
        if key not in data:
            continue
        try:
            value = int(data[key])
        except (TypeError, ValueError):
            return jsonify({"error": f"{key}: podaj liczbę"}), 400
        if not (low <= value <= high):
            return jsonify({"error": f"{key}: dozwolony zakres {low} do {high}"}), 400
        updates[key] = str(value)
    if updates:
        db.set_settings(**updates)
    if updates.get("auto_send") == "on":
        worker.STATE["consecutive_send_failures"] = 0
    return jsonify(db.get_settings())


@app.route("/api/autopilot/tick", methods=["POST"])
def autopilot_tick():
    started = worker.run_tick_in_background()
    return jsonify({"started": started})


@app.route("/api/autopilot/adopt", methods=["POST"])
def autopilot_adopt():
    count = db.adopt_new_leads_with_email()
    return jsonify({"adopted": count})


# ── Kampanie ──
@app.route("/api/campaigns", methods=["GET", "POST"])
def campaigns_collection():
    if request.method == "GET":
        return jsonify(db.get_campaigns())
    data = request.json or {}
    business_type = (data.get("business_type") or "").strip()
    city = (data.get("city") or "").strip()
    if not business_type or not city:
        return jsonify({"error": "Podaj typ biznesu i miasto"}), 400
    target_count = max(1, min(int(data.get("target_count") or 20), 500))
    campaign_id = db.add_campaign(
        business_type, city, target_count,
        no_website=bool(data.get("no_website")),
        profile_id=data.get("profile_id") or None,
    )
    return jsonify({"id": campaign_id})


@app.route("/api/campaigns/<int:campaign_id>/update", methods=["POST"])
def update_campaign(campaign_id):
    data = request.json or {}
    updates = {}
    if "active" in data:
        updates["active"] = 1 if data["active"] else 0
        updates["last_error"] = ""
    if "target_count" in data:
        updates["target_count"] = max(1, min(int(data["target_count"]), 500))
    db.update_campaign(campaign_id, **updates)
    return jsonify({"ok": True})


@app.route("/api/campaigns/<int:campaign_id>", methods=["DELETE"])
def delete_campaign(campaign_id):
    db.delete_campaign(campaign_id)
    return jsonify({"ok": True})


# ── Kolejka wysyłki ──
@app.route("/api/queue")
def queue_list():
    return jsonify(db.get_queue())


@app.route("/api/messages/<int:row_id>/send", methods=["POST"])
def send_message_now(row_id):
    message = db.get_message(row_id)
    if not message:
        return jsonify({"error": "Nie znaleziono wiadomości"}), 404
    if message["status"] == "sent":
        return jsonify({"error": "Ta wiadomość już poszła"}), 400
    try:
        message_id = pipeline.deliver(message)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True, "message_id": message_id})


@app.route("/api/messages/<int:row_id>/update", methods=["POST"])
def update_message(row_id):
    message = db.get_message(row_id)
    if not message:
        return jsonify({"error": "Nie znaleziono wiadomości"}), 404
    if message["status"] == "sent":
        return jsonify({"error": "Wysłanej wiadomości nie da się edytować"}), 400
    data = request.json or {}
    updates = {}
    if "subject" in data:
        updates["subject"] = (data["subject"] or "").strip()
    if "body" in data:
        updates["body"] = (data["body"] or "").strip()
    if "scheduled_at" in data:
        updates["scheduled_at"] = data["scheduled_at"] or db.now_iso()
    db.update_message(row_id, **updates)
    if message["kind"] == "initial" and ("subject" in updates or "body" in updates):
        fresh = db.get_message(row_id)
        db.update_lead(message["lead_id"], generated_email=pipeline.join_subject(fresh["subject"], fresh["body"]))
    return jsonify({"ok": True})


@app.route("/api/messages/<int:row_id>/cancel", methods=["POST"])
def cancel_message(row_id):
    message = db.get_message(row_id)
    if not message:
        return jsonify({"error": "Nie znaleziono wiadomości"}), 404
    db.update_message(row_id, status="cancelled")
    lead = db.get_lead(message["lead_id"])
    if lead and lead["status"] == "ready" and message["kind"] == "initial":
        db.update_lead(lead["id"], status="new")
    return jsonify({"ok": True})


@app.route("/api/messages/<int:row_id>/requeue", methods=["POST"])
def requeue_message(row_id):
    message = db.get_message(row_id)
    if not message:
        return jsonify({"error": "Nie znaleziono wiadomości"}), 404
    if message["status"] == "sent":
        return jsonify({"error": "Ta wiadomość już poszła"}), 400
    db.update_message(row_id, status="queued", error="", scheduled_at=db.now_iso())
    if message["kind"] == "initial":
        db.update_lead(message["lead_id"], status="ready", last_error="")
    return jsonify({"ok": True})


if os.getenv("AUTOPILOT_WORKER", "1") == "1":
    worker.start(interval_seconds=int(os.getenv("AUTOPILOT_INTERVAL_SECONDS", "60")))


if __name__ == "__main__":
    db.init_db()
    port = int(os.getenv("PORT", 5001))
    app.run(host="0.0.0.0", debug=False, port=port)
