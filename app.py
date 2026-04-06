from flask import Flask, render_template, request, jsonify, Response
from dotenv import load_dotenv
from datetime import datetime
from functools import wraps
import os
import db
import scraper
import analyzer

from pathlib import Path
load_dotenv(Path(__file__).parent / ".env")

app = Flask(__name__)


def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        password = os.getenv("APP_PASSWORD", "")
        if not password:
            return f(*args, **kwargs)  # no password set = open access (local dev)
        auth = request.authorization
        if not auth or auth.password != password:
            return Response(
                "Wymagane logowanie",
                401,
                {"WWW-Authenticate": 'Basic realm="Lead Gen"'},
            )
        return f(*args, **kwargs)
    return decorated


app.before_request_funcs[None] = []


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


@app.before_request
def setup():
    db.init_db()


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
    return jsonify(leads)


@app.route("/api/stats")
def get_stats():
    return jsonify(db.get_stats())


@app.route("/api/search", methods=["POST"])
def search():
    data = request.json or {}
    business_type = data.get("business_type", "").strip()
    city = data.get("city", "").strip()
    max_results = min(int(data.get("max_results", 10)), 60)

    if not business_type or not city:
        return jsonify({"error": "Podaj typ biznesu i miasto"}), 400

    try:
        leads = scraper.search_leads(business_type, city, max_results)
    except ValueError as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        return jsonify({"error": f"Błąd Google Maps API: {e}"}), 500

    added = 0
    skipped = 0
    results = []

    for lead in leads:
        website_data = None
        if lead.get("website_url"):
            website_data = scraper.scrape_website(lead["website_url"])
            email = scraper.extract_email_from_website(website_data)
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
        )

        if lead_id:
            added += 1
            results.append({"id": lead_id, "name": lead["business_name"]})
        else:
            skipped += 1

    return jsonify({"added": added, "skipped": skipped, "total_found": len(leads)})


@app.route("/api/lead/<int:lead_id>")
def get_lead(lead_id):
    lead = db.get_lead(lead_id)
    if not lead:
        return jsonify({"error": "Nie znaleziono"}), 404
    return jsonify(lead)


@app.route("/api/lead/<int:lead_id>/analyze", methods=["POST"])
def analyze_lead(lead_id):
    lead = db.get_lead(lead_id)
    if not lead:
        return jsonify({"error": "Nie znaleziono"}), 404
    if not lead.get("website_url"):
        return jsonify({"error": "Brak strony do analizy"}), 400

    website_data = scraper.scrape_website(lead["website_url"])
    screenshots = scraper.screenshot_website(lead["website_url"])

    if not screenshots.get("desktop") and not screenshots.get("mobile"):
        return jsonify({"error": "Nie udało się zrobić zrzutu ekranu strony"}), 500

    try:
        analysis = analyzer.analyze_website_visually(lead, screenshots, website_data)
        # Store analysis in notes if empty, otherwise prepend
        existing_notes = lead.get("notes", "") or ""
        prefix = f"[AI Analiza]\n{analysis}\n\n"
        db.update_lead(lead_id, notes=prefix + existing_notes if existing_notes else prefix.strip())
        return jsonify({"analysis": analysis, "website_data": website_data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/lead/<int:lead_id>/generate-email", methods=["POST"])
def generate_email(lead_id):
    lead = db.get_lead(lead_id)
    if not lead:
        return jsonify({"error": "Nie znaleziono"}), 404

    website_data = None
    if lead.get("website_url"):
        website_data = scraper.scrape_website(lead["website_url"])

    # Use AI analysis from notes if already done
    ai_analysis = None
    notes = lead.get("notes", "") or ""
    if notes.startswith("[AI Analiza]"):
        # Extract just the analysis part
        ai_analysis = notes.replace("[AI Analiza]\n", "").split("\n\n")[0]

    try:
        email_text = analyzer.generate_email(lead, website_data, ai_analysis=ai_analysis)
        db.update_lead(lead_id, generated_email=email_text)
        return jsonify({"email": email_text})
    except Exception as e:
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
    import sqlite3
    from db import get_conn

    with get_conn() as conn:
        conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
    return jsonify({"ok": True})


if __name__ == "__main__":
    db.init_db()
    port = int(os.getenv("PORT", 5001))
    app.run(host="0.0.0.0", debug=False, port=port)
