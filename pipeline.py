import json
from datetime import datetime

import os

import agent_audit
import analyzer
import db
import mailer
import scraper

LAST_CALL = {"google": None, "ai": None}

MODEL_PRICES_PER_MILLION_TOKENS = {
    "claude-opus-5": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
FALLBACK_PRICE = (5.0, 25.0)


def record_usage(purpose, model, input_tokens, output_tokens):
    db.add_usage(purpose, model, input_tokens, output_tokens)


analyzer.on_usage = record_usage
agent_audit.on_usage = record_usage


def price_for(model: str) -> tuple[float, float]:
    for known, price in MODEL_PRICES_PER_MILLION_TOKENS.items():
        if (model or "").startswith(known):
            return price
    return FALLBACK_PRICE


def estimate_cost_usd(usage_rows) -> float:
    total = 0.0
    for row in usage_rows:
        price_in, price_out = price_for(row["model"])
        total += row["input_tokens"] * price_in / 1_000_000
        total += row["output_tokens"] * price_out / 1_000_000
    return total


def spend_summary() -> dict:
    now = datetime.now()
    today_rows = db.usage_by_model(now.strftime("%Y-%m-%d"))
    month_rows = db.usage_by_model(now.strftime("%Y-%m"))
    return {
        "today_usd": round(estimate_cost_usd(today_rows), 3),
        "today_calls": sum(row["calls"] for row in today_rows),
        "month_usd": round(estimate_cost_usd(month_rows), 2),
        "month_calls": sum(row["calls"] for row in month_rows),
    }


def mark(service, ok, detail=""):
    LAST_CALL[service] = {
        "ok": ok,
        "detail": detail,
        "at": datetime.now().strftime("%H:%M:%S"),
    }


def import_leads(business_type, city, max_results, no_website=False,
                 profile_id=None, campaign_id=None, autopilot=False) -> dict:
    known_names = db.known_business_names(city)
    try:
        found = scraper.search_leads(business_type, city, max_results,
                                     no_website=no_website, known_names=known_names)
        mark("google", True, f"{len(found['leads'])} wyników: {business_type}, {city}")
    except Exception as error:
        mark("google", False, str(error)[:140])
        raise

    added = 0
    skipped = found["skipped_known"]
    for lead in found["leads"]:
        if db.lead_exists(lead["business_name"], city):
            skipped += 1
            continue
        website_data, email = enrich_contact(lead.get("website_url", ""))
        lead_id = db.add_lead(
            business_name=lead["business_name"],
            email=email,
            phone=lead.get("phone", ""),
            website_url=lead.get("website_url", ""),
            address=lead.get("address", ""),
            business_type=business_type,
            city=city,
            website_checks=json.dumps(website_data or {}),
            profile_id=profile_id,
            campaign_id=campaign_id,
            autopilot=1 if autopilot else 0,
        )
        if lead_id:
            added += 1
        else:
            skipped += 1
    return {
        "added": added,
        "skipped": skipped,
        "total_found": len(found["leads"]) + found["skipped_known"],
        "exhausted": found["exhausted"],
    }


def enrich_contact(website_url) -> tuple[dict | None, str]:
    if not website_url:
        return None, ""
    website_data = scraper.scrape_website(website_url)
    email = scraper.find_contact_email(website_url, website_data)
    return website_data, email


def resolve_profile(profile_id):
    profiles = db.get_profiles()
    wanted = next((p for p in profiles if p["id"] == profile_id), None)
    if wanted:
        return wanted
    return profiles[0] if profiles else None


def outsourced_platform_analysis(platform, pitch) -> str:
    return f"""## Strona na platformie {platform}

Ten biznes korzysta z **{platform}** zamiast własnej strony: {pitch}.

### Szansa sprzedażowa
To idealny lead do zaproponowania własnej strony. Argumenty:
- **Zero prowizji**: własna strona nie pobiera procentu od rezerwacji ani wizyt
- **Własna marka i domena**: niezależność od platformy
- **Lepsza widoczność w Google**: własne SEO, własna domena
- **Pełna kontrola** nad wyglądem, treścią i danymi klientów
- Platforma może zmienić warunki lub podnieść prowizje w każdej chwili

### Rekomendacja
Zaproponuj prostą stronę z formularzem kontaktowym lub systemem rezerwacji, dzięki której przestają płacić prowizje."""


def run_analysis(lead) -> dict:
    website_data = scraper.scrape_website(lead["website_url"])
    outsourced = (website_data or {}).get("outsourced_platform")
    if outsourced:
        pitch = (website_data or {}).get("outsourced_pitch", "korzystają z zewnętrznej platformy")
        analysis = outsourced_platform_analysis(outsourced, pitch)
        db.update_lead(lead["id"], ai_analysis=analysis, website_checks=json.dumps(website_data), generated_email="")
        return {"analysis": analysis, "scores": {}, "website_data": website_data}

    if os.getenv("AUDIT_MODE", "agent") == "agent":
        try:
            agent_result = agent_audit.audit(lead)
            mark("ai", True, f"audyt agentowy: {lead['business_name']}")
            primary_url = agent_result.get("primary_url") or lead["website_url"]
            if primary_url != lead["website_url"]:
                website_data = scraper.scrape_website(primary_url) or website_data
            stored = {key: agent_result[key] for key in ("analysis", "scores", "verdict", "story", "primary_url", "log")}
            db.update_lead(lead["id"], ai_analysis=json.dumps(stored, ensure_ascii=False),
                           website_checks=json.dumps(website_data or {}), generated_email="")
            return {"analysis": agent_result["analysis"], "scores": agent_result["scores"],
                    "website_data": website_data, "verdict": agent_result["verdict"], "story": agent_result["story"]}
        except Exception as error:
            mark("ai", False, f"audyt agentowy nie wyszedł, klasyczny w zamian: {str(error)[:100]}")

    screenshots = scraper.screenshot_website(lead["website_url"])
    impression = None
    try:
        impression = analyzer.first_impression(lead, screenshots)
    except Exception as error:
        mark("ai", False, f"pierwsze wrażenie: {str(error)[:120]}")
    try:
        result = analyzer.analyze_website_visually(
            lead, screenshots, website_data, impression=(impression or {}).get("text"))
        mark("ai", True, f"analiza: {lead['business_name']}")
    except Exception as error:
        mark("ai", False, str(error)[:140])
        raise
    if impression and impression.get("text"):
        result["analysis"] = (
            "Pierwsze wrażenie po trzech sekundach (desktop, laptop, telefon):\n"
            + impression["text"] + "\n\n" + result["analysis"]
        )
        scores = result.setdefault("scores", {})
        for key in ("first_impression", "design_year"):
            if impression.get(key) is not None:
                scores[key] = impression[key]
    db.update_lead(
        lead["id"],
        ai_analysis=json.dumps(result),
        website_checks=json.dumps(website_data or {}),
        generated_email="",
    )
    return {"analysis": result["analysis"], "scores": result.get("scores", {}), "website_data": website_data}


def cached_analysis(lead) -> dict:
    raw = lead.get("ai_analysis", "") or ""
    if not raw:
        return {"cached": False}
    try:
        website_data = json.loads(lead.get("website_checks") or "{}")
    except Exception:
        website_data = {}
    try:
        stored = json.loads(raw)
        if isinstance(stored, dict) and "analysis" in stored:
            return {"cached": True, "analysis": stored["analysis"],
                    "scores": stored.get("scores", {}), "website_data": website_data}
    except Exception:
        pass
    return {"cached": True, "analysis": raw, "scores": {}, "website_data": website_data}


def prepare_email(lead, profile_id=None, my_feedback=None, website_data=None) -> str:
    if website_data is None and lead.get("website_url"):
        website_data = scraper.scrape_website(lead["website_url"])
    profile = resolve_profile(profile_id or lead.get("profile_id"))
    try:
        email_text = analyzer.generate_email(
            lead, website_data,
            ai_analysis=lead.get("ai_analysis") or None,
            my_feedback=my_feedback or None,
            profile=profile,
        )
        mark("ai", True, f"email: {lead['business_name']}")
    except Exception as error:
        mark("ai", False, str(error)[:140])
        raise

    updates = {"generated_email": email_text}
    if profile:
        updates["profile_id"] = profile["id"]
    if my_feedback:
        observations = json.loads(lead.get("observations") or "[]")
        if my_feedback not in observations:
            observations.append(my_feedback)
        updates["observations"] = json.dumps(observations, ensure_ascii=False)
    db.update_lead(lead["id"], **updates)
    return email_text


def split_subject(email_text: str) -> tuple[str, str]:
    first_line, _, rest = (email_text or "").partition("\n")
    if first_line.lower().startswith("temat:"):
        return first_line[6:].strip(), rest.strip()
    return "", (email_text or "").strip()


def join_subject(subject: str, body: str) -> str:
    if not subject:
        return body
    return f"Temat: {subject}\n\n{body}"


def default_subject(lead) -> str:
    return f"Pytanie do {lead.get('business_name', 'firmy')}"


def queue_message(lead, kind, subject, body, send_now=False) -> int:
    db.cancel_queued_for_lead(lead["id"])
    in_reply_to = ""
    if kind == "followup":
        sent = db.sent_outbound_for_lead(lead["id"])
        in_reply_to = sent[-1]["message_id"] if sent else ""
    row_id = db.add_message(
        lead["id"], kind=kind, direction="out", subject=subject, body=body,
        status="queued", scheduled_at=db.now_iso(), in_reply_to=in_reply_to,
    )
    lead_updates = {"last_error": ""}
    if kind == "initial":
        lead_updates["status"] = "ready"
        lead_updates["generated_email"] = join_subject(subject, body)
    db.update_lead(lead["id"], **lead_updates)
    if send_now:
        deliver(db.get_message(row_id))
    return row_id


def prepare_lead(lead) -> int | None:
    website_data = None
    if lead.get("website_url") and not lead.get("ai_analysis"):
        analysis = run_analysis(lead)
        website_data = analysis["website_data"]
        if analysis.get("verdict") == "pomin":
            note = (lead.get("notes") or "").strip()
            reason = "Autopilot: strona jest dobra, mail pominięty. " + (analysis.get("story") or "")
            db.update_lead(lead["id"], status="skipped", notes=(note + "\n" + reason).strip())
            return None
        lead = db.get_lead(lead["id"])
    email_text = prepare_email(lead, website_data=website_data)
    subject, body = split_subject(email_text)
    if not subject:
        subject = default_subject(lead)
    return queue_message(lead, "initial", subject, body)


def followup_subject(lead) -> str:
    sent = db.sent_outbound_for_lead(lead["id"])
    if sent:
        subject = sent[0]["subject"]
    else:
        subject, _ = split_subject(lead.get("generated_email", ""))
    if not subject:
        subject = default_subject(lead)
    if subject.lower().startswith("re:"):
        return subject
    return "Re: " + subject


def create_followup(lead) -> dict:
    if not (lead.get("generated_email") or "").strip():
        raise ValueError("Najpierw wygeneruj pierwszy mail")
    followups = json.loads(lead.get("followups") or "[]")
    if len(followups) >= 2:
        raise ValueError("Maksymalnie 2 follow-upy, dalsze przypominanie to spam")
    number = len(followups) + 1
    try:
        text = analyzer.generate_followup(lead, followup_number=number)
        mark("ai", True, f"follow-up {number}: {lead['business_name']}")
    except Exception as error:
        mark("ai", False, str(error)[:140])
        raise
    followups.append(text)
    db.update_lead(lead["id"], followups=json.dumps(followups, ensure_ascii=False))
    return {"text": text, "number": number, "subject": followup_subject(lead)}


def deliver(message) -> str:
    try:
        return _deliver(message)
    except Exception as error:
        db.update_message(message["id"], status="failed", error=str(error)[:300])
        raise


def _deliver(message) -> str:
    lead = db.get_lead(message["lead_id"])
    if not lead or not lead.get("email"):
        raise ValueError("Lead nie ma adresu e-mail")
    profile = resolve_profile(lead.get("profile_id"))
    mailbox = mailer.mailbox_for(profile)
    if mailbox is None:
        profile_name = (profile or {}).get("name", "domyślny")
        raise ValueError(f"Profil {profile_name} nie ma skonfigurowanej skrzynki pocztowej")

    earlier = db.sent_outbound_for_lead(lead["id"])
    references = [m["message_id"] for m in earlier if m["message_id"]]
    in_reply_to = message.get("in_reply_to") or (references[-1] if references else "")
    message_id = mailer.send(
        mailbox, lead["email"], message["subject"], message["body"],
        in_reply_to=in_reply_to, references=references,
    )

    sent_at = db.now_iso()
    db.update_message(message["id"], status="sent", sent_at=sent_at, message_id=message_id, error="")
    lead_updates = {"status": "emailed", "last_error": ""}
    if message["kind"] == "initial":
        lead_updates["emailed_at"] = sent_at
        lead_updates["generated_email"] = join_subject(message["subject"], message["body"])
    db.update_lead(lead["id"], **lead_updates)
    return message_id
