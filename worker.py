import os
import random
import threading
import time
from collections import deque
from datetime import datetime, timedelta

import db
import mailer
import pipeline

LOG = deque(maxlen=150)
STATE = {
    "started": False,
    "last_tick_at": None,
    "busy_with": "",
    "send_note": "",
    "consecutive_send_failures": 0,
    "next_send_after": None,
    "warned_missing_keys": set(),
}
TICK_LOCK = threading.Lock()

DISCOVERY_BATCH = 5
PREPARE_BUFFER = 5
MAX_SEND_FAILURES_IN_A_ROW = 3


def log(text):
    LOG.appendleft({"at": datetime.now().strftime("%H:%M:%S"), "text": text})


def start(interval_seconds=60):
    if STATE["started"]:
        return
    STATE["started"] = True
    thread = threading.Thread(target=_run_forever, args=(interval_seconds,), daemon=True, name="autopilot")
    thread.start()
    log("autopilot wystartował")


def _run_forever(interval_seconds):
    while True:
        tick()
        time.sleep(interval_seconds)


def tick() -> bool:
    if not TICK_LOCK.acquire(blocking=False):
        return False
    try:
        STATE["last_tick_at"] = db.now_iso()
        for step in (step_send, step_prepare, step_discover):
            _run_step(step)
    finally:
        STATE["busy_with"] = ""
        TICK_LOCK.release()
    return True


def run_tick_in_background() -> bool:
    if TICK_LOCK.locked():
        return False
    threading.Thread(target=tick, daemon=True, name="autopilot-manual").start()
    return True


def _run_step(step):
    try:
        step()
    except Exception as error:
        log(f"błąd w kroku {step.__name__}: {str(error)[:200]}")


def missing_key(env_name) -> bool:
    if os.getenv(env_name):
        return False
    if env_name not in STATE["warned_missing_keys"]:
        STATE["warned_missing_keys"].add(env_name)
        log(f"brak {env_name} w .env, ten krok autopilota czeka")
    return True


def parse_iso(stamp) -> datetime | None:
    if not stamp:
        return None
    try:
        return datetime.fromisoformat(stamp)
    except ValueError:
        return None


# ── Krok 1: wysyłka z kolejki ──
def step_send():
    settings = db.get_settings()
    if settings["auto_send"] != "on":
        STATE["send_note"] = "auto-wysyłka wyłączona, maile czekają w kolejce"
        return
    now = datetime.now()
    allowed, reason = send_allowed(settings, now, db.count_sent_on(now.strftime("%Y-%m-%d")), parse_iso(db.last_sent_at()))
    STATE["send_note"] = reason
    if not allowed:
        return
    hold_until = STATE["next_send_after"]
    if hold_until and now < hold_until:
        STATE["send_note"] = f"następny mail po {hold_until:%H:%M}"
        return
    message = db.next_due_message(now.isoformat(timespec="seconds"))
    if message is None:
        STATE["send_note"] = "kolejka pusta"
        return
    lead = db.get_lead(message["lead_id"])
    STATE["busy_with"] = f"wysyłam: {lead['business_name']}"
    try:
        pipeline.deliver(message)
    except Exception as error:
        STATE["consecutive_send_failures"] += 1
        log(f"błąd wysyłki do {lead['business_name']}: {str(error)[:150]}")
        if STATE["consecutive_send_failures"] >= MAX_SEND_FAILURES_IN_A_ROW:
            db.set_settings(auto_send="off")
            log("3 błędy wysyłki z rzędu, auto-wysyłka wyłączona do czasu sprawdzenia skrzynki")
        return
    STATE["consecutive_send_failures"] = 0
    gap_minutes = int(settings["send_gap_minutes"])
    STATE["next_send_after"] = now + timedelta(minutes=gap_minutes * (1 + random.random()))
    log(f"wysłano {message['kind']}: {lead['business_name']} <{lead['email']}>")


def send_allowed(settings, now, sent_today, last_sent) -> tuple[bool, str]:
    if int(settings["weekdays_only"]) and now.weekday() >= 5:
        return False, "weekend, wysyłka wraca w poniedziałek"
    from_hour = int(settings["send_from_hour"])
    to_hour = int(settings["send_to_hour"])
    if not (from_hour <= now.hour < to_hour):
        return False, f"poza oknem wysyłki {from_hour}:00 do {to_hour}:00"
    daily_limit = int(settings["daily_limit"])
    if sent_today >= daily_limit:
        return False, f"dzienny limit {daily_limit} osiągnięty"
    gap = timedelta(minutes=int(settings["send_gap_minutes"]))
    if last_sent and now - last_sent < gap:
        return False, f"odstęp między mailami, następny po {(last_sent + gap):%H:%M}"
    return True, "wysyłka aktywna"


# ── Krok 2: analiza i pisanie maili dla nowych leadów ──
def step_prepare():
    if missing_key("ANTHROPIC_API_KEY"):
        return
    for lead in db.leads_awaiting_preparation(limit=1):
        STATE["busy_with"] = f"piszę mail: {lead['business_name']}"
        try:
            pipeline.prepare_lead(lead)
            log(f"mail gotowy: {lead['business_name']}")
        except Exception as error:
            db.update_lead(lead["id"], status="failed", last_error=str(error)[:300])
            log(f"nie udało się przygotować {lead['business_name']}: {str(error)[:120]}")


# ── Krok 3: szukanie nowych firm dla kampanii ──
def step_discover():
    if missing_key("GOOGLE_MAPS_API_KEY"):
        return
    if db.count_awaiting_preparation() >= PREPARE_BUFFER:
        return
    for campaign in db.get_campaigns(active_only=True):
        label = f"{campaign['business_type']} / {campaign['city']}"
        remaining = campaign["target_count"] - campaign["found_count"]
        if remaining <= 0:
            db.update_campaign(campaign["id"], active=0, last_error="")
            log(f"kampania {label}: cel osiągnięty")
            continue
        STATE["busy_with"] = f"szukam firm: {label}"
        try:
            result = pipeline.import_leads(
                campaign["business_type"], campaign["city"], min(DISCOVERY_BATCH, remaining),
                no_website=bool(campaign["no_website"]), profile_id=campaign["profile_id"],
                campaign_id=campaign["id"], autopilot=True,
            )
        except Exception as error:
            db.update_campaign(campaign["id"], last_error=str(error)[:200], last_run_at=db.now_iso())
            log(f"kampania {label}: {str(error)[:150]}")
            return
        found_count = campaign["found_count"] + result["added"]
        updates = {"found_count": found_count, "last_run_at": db.now_iso(), "last_error": ""}
        if found_count >= campaign["target_count"]:
            updates["active"] = 0
        elif result["added"] == 0 and result["exhausted"]:
            updates["active"] = 0
            updates["last_error"] = "Google nie zwraca już nowych firm dla tego zapytania"
        db.update_campaign(campaign["id"], **updates)
        log(f"kampania {label}: +{result['added']} firm ({found_count}/{campaign['target_count']})")
        if updates.get("active") == 0 and not updates["last_error"]:
            log(f"kampania {label}: cel osiągnięty")
        return


def status() -> dict:
    settings = db.get_settings()
    today = datetime.now().strftime("%Y-%m-%d")
    mailboxes = []
    for profile in db.get_profiles():
        mailboxes.append({"profile": profile["name"], "state": mailer.describe(mailer.mailbox_for(profile))})
    return {
        "started": STATE["started"],
        "last_tick_at": STATE["last_tick_at"],
        "busy_with": STATE["busy_with"],
        "send_note": STATE["send_note"],
        "auto_send": settings["auto_send"] == "on",
        "settings": settings,
        "sent_today": db.count_sent_on(today),
        "queued": db.count_queued(),
        "awaiting_preparation": db.count_awaiting_preparation(),
        "campaigns_active": len(db.get_campaigns(active_only=True)),
        "mailboxes": mailboxes,
        "spend": pipeline.spend_summary(),
        "log": list(LOG)[:40],
    }
