from flask import Flask, render_template, request, jsonify, Response
from dotenv import load_dotenv
from datetime import datetime
import json
import os
import db
import scraper
import analyzer

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
    # Strip binary fields before JSON serialization
    for lead in leads:
        lead.pop("mockup_image", None)
    return jsonify(leads)


@app.route("/api/stats")
def get_stats():
    return jsonify(db.get_stats())


# ponytail: stan ostatniego wywołania trzymany w pamięci procesu — znika po
# restarcie i nie jest współdzielony między workerami. Wystarczy na panel LOG.
LAST_CALL = {"google": None, "ai": None}


def _mark(service, ok, detail=""):
    LAST_CALL[service] = {
        "ok": ok,
        "detail": detail,
        "at": datetime.now().strftime("%H:%M:%S"),
    }


def _service_status(service, env_key):
    if not os.getenv(env_key):
        return {"state": "no_key", "detail": f"brak {env_key}"}
    last = LAST_CALL[service]
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
    """Prawdziwe zapytanie do Google Maps — płatne, więc tylko na żądanie."""
    try:
        found = scraper.search_leads("kawiarnia", "Kraków", 1)
        _mark("google", True, f"test ok, {len(found)} wynik(ów)")
    except Exception as e:
        _mark("google", False, str(e)[:140])
    return health()


@app.route("/api/search", methods=["POST"])
def search():
    data = request.json or {}
    business_type = data.get("business_type", "").strip()
    city = data.get("city", "").strip()
    max_results = min(int(data.get("max_results", 10)), 60)
    no_website = bool(data.get("no_website"))

    if not business_type or not city:
        return jsonify({"error": "Podaj typ biznesu i miasto"}), 400

    try:
        leads = scraper.search_leads(business_type, city, max_results, no_website=no_website)
        _mark("google", True, f"{len(leads)} wyników: {business_type}, {city}")
    except ValueError as e:
        _mark("google", False, str(e)[:140])
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        _mark("google", False, str(e)[:140])
        return jsonify({"error": f"Błąd Google Maps API: {e}"}), 500

    added = 0
    skipped = 0
    results = []

    for lead in leads:
        # Duplikat sprawdzamy PRZED scrapowaniem, żeby nie palić minut na
        # PageSpeed i szukanie maila dla firmy, którą baza i tak odrzuci
        if db.lead_exists(lead["business_name"], city):
            skipped += 1
            continue

        website_data = None
        if lead.get("website_url"):
            website_data = scraper.scrape_website(lead["website_url"])
            email = scraper.find_contact_email(lead["website_url"], website_data)
        else:
            email = ""

        lead_id = db.add_lead(
            business_name=lead["business_name"],
            email=email,
            phone=lead.get("phone", ""),
            website_url=lead.get("website_url", ""),
            address=lead.get("address", ""),
            business_type=business_type,
            city=city,
            website_checks=json.dumps(website_data or {}),
        )

        if lead_id:
            added += 1
            results.append({"id": lead_id, "name": lead["business_name"]})
        else:
            skipped += 1

    return jsonify({"added": added, "skipped": skipped, "total_found": len(leads)})


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
        website_checks=json.dumps(website_data or {}),
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


@app.route("/api/lead/<int:lead_id>/analyze", methods=["GET", "POST"])
def analyze_lead(lead_id):
    lead = db.get_lead(lead_id)
    if not lead:
        return jsonify({"error": "Nie znaleziono"}), 404

    # GET — return cached result
    if request.method == "GET":
        raw = lead.get("ai_analysis", "") or ""
        checks_raw = lead.get("website_checks", "") or ""
        if not raw:
            return jsonify({"cached": False})
        try:
            website_data = json.loads(checks_raw) if checks_raw else {}
        except Exception:
            website_data = {}
        # Support both new format (JSON with scores) and old plain-text records
        try:
            stored = json.loads(raw)
            if isinstance(stored, dict) and "analysis" in stored:
                return jsonify({"cached": True, "analysis": stored["analysis"],
                                "scores": stored.get("scores", {}), "website_data": website_data})
        except Exception:
            pass
        return jsonify({"cached": True, "analysis": raw, "scores": {}, "website_data": website_data})

    # POST — run fresh analysis
    if not lead.get("website_url"):
        return jsonify({"error": "Brak strony do analizy"}), 400

    website_data = scraper.scrape_website(lead["website_url"])

    # Outsourced platform — skip Claude analysis, return instant pitch
    outsourced = (website_data or {}).get("outsourced_platform")
    if outsourced:
        pitch = (website_data or {}).get("outsourced_pitch", "korzystają z zewnętrznej platformy")
        analysis = f"""## Strona na platformie {outsourced}

Ten biznes korzysta z **{outsourced}** zamiast własnej strony — {pitch}.

### Szansa sprzedażowa
To idealny lead do zaproponowania własnej strony. Argumenty:
- **Zero prowizji** — własna strona nie pobiera % od rezerwacji/wizyt
- **Własna marka i domena** — niezależność od platformy
- **Lepsza widoczność w Google** — własne SEO, własna domena
- **Pełna kontrola** nad wyglądem, treścią i danymi klientów
- Platforma może zmienić warunki lub podnieść prowizje w każdej chwili

### Rekomendacja
Zaproponuj prostą stronę z formularzem kontaktowym lub systemem rezerwacji. 500 PLN za stronę która sprawi że przestają płacić prowizje."""
        db.update_lead(lead_id, ai_analysis=analysis, website_checks=json.dumps(website_data), generated_email="")
        return jsonify({"analysis": analysis, "website_data": website_data})

    screenshots = scraper.screenshot_website(lead["website_url"])
    # screenshots may be empty if Playwright/Chromium is unavailable — fall back to text-only

    try:
        result = analyzer.analyze_website_visually(lead, screenshots, website_data)
        _mark("ai", True, f"analiza: {lead['business_name']}")
        db.update_lead(
            lead_id,
            ai_analysis=json.dumps(result),
            website_checks=json.dumps(website_data or {}),
            generated_email="",
        )
        return jsonify({"analysis": result["analysis"], "scores": result.get("scores", {}), "website_data": website_data})
    except Exception as e:
        _mark("ai", False, str(e)[:140])
        import traceback
        traceback.print_exc()  # full traceback in Railway logs
        return jsonify({"error": str(e)}), 500


@app.route("/api/lead/<int:lead_id>/generate-email", methods=["POST"])
def generate_email(lead_id):
    lead = db.get_lead(lead_id)
    if not lead:
        return jsonify({"error": "Nie znaleziono"}), 404

    website_data = None
    if lead.get("website_url"):
        website_data = scraper.scrape_website(lead["website_url"])

    ai_analysis = lead.get("ai_analysis") or None
    payload = request.json or {}
    my_feedback = payload.get("my_feedback", "").strip()
    sender = payload.get("sender") or "szymon"

    try:
        email_text = analyzer.generate_email(lead, website_data, ai_analysis=ai_analysis, my_feedback=my_feedback or None, sender=sender)
        _mark("ai", True, f"email: {lead['business_name']}")
        updates = {"generated_email": email_text}
        if my_feedback:
            existing = json.loads(lead.get("observations") or "[]")
            if my_feedback not in existing:
                existing.append(my_feedback)
            updates["observations"] = json.dumps(existing, ensure_ascii=False)
        db.update_lead(lead_id, **updates)
        return jsonify({"email": email_text})
    except Exception as e:
        _mark("ai", False, str(e)[:140])
        return jsonify({"error": str(e)}), 500



@app.route("/api/lead/<int:lead_id>/generate-followup", methods=["POST"])
def generate_followup(lead_id):
    lead = db.get_lead(lead_id)
    if not lead:
        return jsonify({"error": "Nie znaleziono"}), 404
    if not (lead.get("generated_email") or "").strip():
        return jsonify({"error": "Najpierw wygeneruj pierwszy mail"}), 400
    followups = json.loads(lead.get("followups") or "[]")
    if len(followups) >= 2:
        return jsonify({"error": "Maksymalnie 2 follow-upy, dalsze przypominanie to spam"}), 400
    try:
        text = analyzer.generate_followup(lead, followup_number=len(followups) + 1)
        _mark("ai", True, f"follow-up {len(followups) + 1}: {lead['business_name']}")
        followups.append(text)
        db.update_lead(lead_id, followups=json.dumps(followups, ensure_ascii=False))
        return jsonify({"followup": text, "number": len(followups)})
    except Exception as e:
        _mark("ai", False, str(e)[:140])
        return jsonify({"error": str(e)}), 500


@app.route("/api/lead/<int:lead_id>/update", methods=["POST"])
def update_lead(lead_id):
    data = request.json or {}
    allowed = {"status", "notes", "email", "generated_email", "phone"}
    updates = {k: v for k, v in data.items() if k in allowed}

    if updates.get("status") == "emailed":
        updates["emailed_at"] = datetime.now().isoformat(timespec="seconds")

    db.update_lead(lead_id, **updates)
    return jsonify({"ok": True})


@app.route("/api/lead/<int:lead_id>", methods=["DELETE"])
def delete_lead(lead_id):
    with db.get_conn() as conn:
        conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
    return jsonify({"ok": True})


if __name__ == "__main__":
    db.init_db()
    port = int(os.getenv("PORT", 5001))
    app.run(host="0.0.0.0", debug=False, port=port)
