import sqlite3
from contextlib import contextmanager
from datetime import datetime

import os
DB_PATH = os.getenv("DB_PATH", "leads.db")

LEAD_STATUSES = ["new", "ready", "emailed", "replied", "converted", "skipped", "failed"]

SETTING_DEFAULTS = {
    "auto_send": "off",
    "daily_limit": "10",
    "send_from_hour": "9",
    "send_to_hour": "17",
    "weekdays_only": "1",
    "send_gap_minutes": "6",
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def init_db():
    with get_conn() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                business_name TEXT NOT NULL,
                email TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                website_url TEXT DEFAULT '',
                address TEXT DEFAULT '',
                business_type TEXT DEFAULT '',
                city TEXT DEFAULT '',
                status TEXT DEFAULT 'new',
                generated_email TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                ai_analysis TEXT DEFAULT '',
                website_checks TEXT DEFAULT '',
                emailed_at TEXT,
                created_at TEXT DEFAULT (datetime('now')),
                UNIQUE(business_name, city)
            )
        """)
        _migrate(conn)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                domain TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                experience TEXT DEFAULT '',
                realizations TEXT DEFAULT ''
            )
        """)
        _migrate_profiles(conn)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS campaigns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                business_type TEXT NOT NULL,
                city TEXT NOT NULL,
                target_count INTEGER DEFAULT 20,
                found_count INTEGER DEFAULT 0,
                no_website INTEGER DEFAULT 0,
                profile_id INTEGER,
                active INTEGER DEFAULT 1,
                last_run_at TEXT,
                last_error TEXT DEFAULT '',
                created_at TEXT DEFAULT (datetime('now'))
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lead_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                direction TEXT NOT NULL,
                subject TEXT DEFAULT '',
                body TEXT DEFAULT '',
                message_id TEXT DEFAULT '',
                in_reply_to TEXT DEFAULT '',
                status TEXT NOT NULL,
                scheduled_at TEXT,
                sent_at TEXT,
                error TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at TEXT NOT NULL,
                purpose TEXT DEFAULT '',
                model TEXT DEFAULT '',
                input_tokens INTEGER DEFAULT 0,
                output_tokens INTEGER DEFAULT 0
            )
        """)
        if conn.execute("SELECT COUNT(*) FROM profiles").fetchone()[0] == 0:
            conn.execute(
                "INSERT INTO profiles (name, domain, phone, experience, realizations) VALUES (?,?,?,?,?)",
                ("Szymon Laskowski", "szymonlaskowski.pl", "+48 731 531 571",
                 "ponad 3 lata komercyjnego doświadczenia jako programista",
                 "oficjalna strona urzędu miejskiego w Bielsku-Białej, sklep internetowy Mateusza Sochy"),
            )
            conn.execute("INSERT INTO profiles (name) VALUES (?)", ("Nikodem",))


def _migrate(conn):
    """Add new columns to existing databases without losing data."""
    for col, definition in [
        ("ai_analysis",    "TEXT DEFAULT ''"),
        ("website_checks", "TEXT DEFAULT ''"),
        ("mockup_html",    "TEXT DEFAULT ''"),
        ("mockup_image",   "BLOB"),
        ("observations",   "TEXT DEFAULT '[]'"),
        ("followups",      "TEXT DEFAULT '[]'"),
        ("profile_id",     "INTEGER"),
        ("last_error",     "TEXT DEFAULT ''"),
        ("autopilot",      "INTEGER DEFAULT 0"),
        ("campaign_id",    "INTEGER"),
    ]:
        try:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {definition}")
        except Exception:
            pass  # column already exists


def _migrate_profiles(conn):
    for col in ("mailbox_address", "mailbox_password", "smtp_host", "smtp_port"):
        try:
            conn.execute(f"ALTER TABLE profiles ADD COLUMN {col} TEXT DEFAULT ''")
        except Exception:
            pass


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def add_lead(**kwargs):
    """Zwraca id nowego rekordu albo None gdy duplikat (nazwa + miasto) lub błąd."""
    with get_conn() as conn:
        fields = list(kwargs.keys())
        placeholders = ", ".join("?" * len(fields))
        cols = ", ".join(fields)
        try:
            cur = conn.execute(
                f"INSERT OR IGNORE INTO leads ({cols}) VALUES ({placeholders})",
                list(kwargs.values()),
            )
            return cur.lastrowid if cur.rowcount else None
        except Exception as e:
            print(f"DB error: {e}")
            return None


def lead_exists(business_name, city):
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM leads WHERE business_name = ? AND city = ?",
            (business_name, city),
        ).fetchone()
        return row is not None


def known_business_names(city) -> set:
    with get_conn() as conn:
        rows = conn.execute("SELECT business_name FROM leads WHERE city = ?", (city,)).fetchall()
        return {row["business_name"] for row in rows}


def get_leads(status=None, city=None, business_type=None, search=None):
    with get_conn() as conn:
        query = "SELECT * FROM leads WHERE 1=1"
        params = []
        if status and status != "all":
            query += " AND status = ?"
            params.append(status)
        if city:
            query += " AND city LIKE ?"
            params.append(f"%{city}%")
        if business_type:
            query += " AND business_type LIKE ?"
            params.append(f"%{business_type}%")
        if search:
            query += " AND (business_name LIKE ? OR email LIKE ? OR city LIKE ?)"
            params.extend([f"%{search}%"] * 3)
        query += " ORDER BY created_at DESC"
        return [dict(row) for row in conn.execute(query, params).fetchall()]


def get_lead(lead_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM leads WHERE id = ?", (lead_id,)).fetchone()
        return dict(row) if row else None


def update_lead(lead_id, **kwargs):
    if not kwargs:
        return
    with get_conn() as conn:
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        values = list(kwargs.values()) + [lead_id]
        conn.execute(f"UPDATE leads SET {sets} WHERE id = ?", values)


def delete_lead(lead_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM leads WHERE id = ?", (lead_id,))
        conn.execute("DELETE FROM messages WHERE lead_id = ?", (lead_id,))


def leads_awaiting_preparation(limit=1):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM leads
               WHERE autopilot = 1 AND status = 'new' AND email != ''
                 AND COALESCE(generated_email, '') = ''
               ORDER BY id LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]


def count_awaiting_preparation() -> int:
    with get_conn() as conn:
        return conn.execute(
            """SELECT COUNT(*) FROM leads
               WHERE autopilot = 1 AND status = 'new' AND email != ''
                 AND COALESCE(generated_email, '') = ''"""
        ).fetchone()[0]


def adopt_new_leads_with_email() -> int:
    with get_conn() as conn:
        cur = conn.execute(
            "UPDATE leads SET autopilot = 1 WHERE status = 'new' AND email != '' AND autopilot = 0"
        )
        return cur.rowcount


def get_stats():
    with get_conn() as conn:
        stats = {}
        for status in LEAD_STATUSES:
            count = conn.execute(
                "SELECT COUNT(*) FROM leads WHERE status = ?", (status,)
            ).fetchone()[0]
            stats[status] = count
        stats["total"] = conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]
        return stats


def get_cities():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT city FROM leads WHERE city != '' ORDER BY city"
        ).fetchall()
        return [r["city"] for r in rows]


def get_business_types():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT DISTINCT business_type FROM leads WHERE business_type != '' ORDER BY business_type"
        ).fetchall()
        return [r["business_type"] for r in rows]


# ── Profile nadawców ──
PROFILE_FIELDS = (
    "name", "domain", "phone", "experience", "realizations",
    "mailbox_address", "mailbox_password", "smtp_host", "smtp_port",
)


def get_profiles():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM profiles ORDER BY id").fetchall()]


def get_public_profiles():
    public = []
    for profile in get_profiles():
        safe = dict(profile)
        safe["has_mailbox_password"] = bool(safe.pop("mailbox_password", ""))
        public.append(safe)
    return public


def get_profile(profile_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        return dict(row) if row else None


def add_profile(**kwargs):
    """Zwraca id albo None (brak nazwy / duplikat)."""
    kw = {k: (kwargs.get(k) or "").strip() for k in PROFILE_FIELDS if kwargs.get(k)}
    if not kw.get("name"):
        return None
    with get_conn() as conn:
        try:
            cur = conn.execute(
                f"INSERT INTO profiles ({', '.join(kw)}) VALUES ({', '.join('?' * len(kw))})",
                list(kw.values()),
            )
            return cur.lastrowid
        except Exception:
            return None


def update_profile(profile_id, **kwargs):
    kw = {k: str(kwargs.get(k) or "").strip() for k in PROFILE_FIELDS if k in kwargs}
    if not kw:
        return
    with get_conn() as conn:
        conn.execute(
            f"UPDATE profiles SET {', '.join(f'{k} = ?' for k in kw)} WHERE id = ?",
            list(kw.values()) + [profile_id],
        )


def delete_profile(profile_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        conn.execute("UPDATE leads SET profile_id = NULL WHERE profile_id = ?", (profile_id,))


# ── Ustawienia autopilota ──
def get_settings() -> dict:
    with get_conn() as conn:
        rows = conn.execute("SELECT key, value FROM settings").fetchall()
    stored = {row["key"]: row["value"] for row in rows}
    return {**SETTING_DEFAULTS, **stored}


def get_setting(key, default=None):
    with get_conn() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    if row is None:
        return SETTING_DEFAULTS.get(key, default)
    return row["value"]


def set_settings(**values):
    with get_conn() as conn:
        for key, value in values.items():
            conn.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, str(value)),
            )


# ── Kampanie ──
CAMPAIGN_FIELDS = (
    "business_type", "city", "target_count", "no_website", "profile_id",
    "active", "found_count", "last_run_at", "last_error",
)


def add_campaign(business_type, city, target_count=20, no_website=False, profile_id=None) -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO campaigns (business_type, city, target_count, no_website, profile_id)
               VALUES (?, ?, ?, ?, ?)""",
            (business_type, city, int(target_count), 1 if no_website else 0, profile_id),
        )
        return cur.lastrowid


def get_campaigns(active_only=False):
    query = "SELECT * FROM campaigns"
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY active DESC, id"
    with get_conn() as conn:
        return [dict(row) for row in conn.execute(query).fetchall()]


def get_campaign(campaign_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM campaigns WHERE id = ?", (campaign_id,)).fetchone()
        return dict(row) if row else None


def update_campaign(campaign_id, **fields):
    allowed = {k: v for k, v in fields.items() if k in CAMPAIGN_FIELDS}
    if not allowed:
        return
    with get_conn() as conn:
        sets = ", ".join(f"{k} = ?" for k in allowed)
        conn.execute(f"UPDATE campaigns SET {sets} WHERE id = ?", list(allowed.values()) + [campaign_id])


def delete_campaign(campaign_id):
    with get_conn() as conn:
        conn.execute("DELETE FROM campaigns WHERE id = ?", (campaign_id,))
        conn.execute("UPDATE leads SET campaign_id = NULL WHERE campaign_id = ?", (campaign_id,))


# ── Wiadomości: kolejka wysyłki i historia wątku ──
MESSAGE_FIELDS = ("subject", "body", "message_id", "in_reply_to", "status", "scheduled_at", "sent_at", "error")


def add_message(lead_id, kind, direction, subject, body, status,
                scheduled_at=None, message_id="", in_reply_to="") -> int:
    with get_conn() as conn:
        cur = conn.execute(
            """INSERT INTO messages
               (lead_id, kind, direction, subject, body, status, scheduled_at, message_id, in_reply_to, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (lead_id, kind, direction, subject, body, status, scheduled_at, message_id, in_reply_to, now_iso()),
        )
        return cur.lastrowid


def get_message(row_id):
    with get_conn() as conn:
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (row_id,)).fetchone()
        return dict(row) if row else None


def update_message(row_id, **fields):
    allowed = {k: v for k, v in fields.items() if k in MESSAGE_FIELDS}
    if not allowed:
        return
    with get_conn() as conn:
        sets = ", ".join(f"{k} = ?" for k in allowed)
        conn.execute(f"UPDATE messages SET {sets} WHERE id = ?", list(allowed.values()) + [row_id])


def get_queue():
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT m.*, l.business_name, l.email, l.city, l.status AS lead_status, l.profile_id
               FROM messages m JOIN leads l ON l.id = m.lead_id
               WHERE m.direction = 'out' AND m.status IN ('queued', 'failed')
               ORDER BY m.status DESC, m.scheduled_at, m.id"""
        ).fetchall()
        return [dict(row) for row in rows]


def count_queued() -> int:
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM messages WHERE direction = 'out' AND status = 'queued'"
        ).fetchone()[0]


def get_lead_messages(lead_id):
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE lead_id = ? ORDER BY created_at, id", (lead_id,)
        ).fetchall()
        return [dict(row) for row in rows]


def has_queued_message(lead_id) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM messages WHERE lead_id = ? AND direction = 'out' AND status = 'queued'",
            (lead_id,),
        ).fetchone()
        return row is not None


def cancel_queued_for_lead(lead_id):
    with get_conn() as conn:
        conn.execute(
            "UPDATE messages SET status = 'cancelled' WHERE lead_id = ? AND direction = 'out' AND status IN ('queued', 'failed')",
            (lead_id,),
        )


def next_due_message(now):
    with get_conn() as conn:
        row = conn.execute(
            """SELECT m.* FROM messages m JOIN leads l ON l.id = m.lead_id
               WHERE m.direction = 'out' AND m.status = 'queued' AND m.scheduled_at <= ?
                 AND l.status IN ('ready', 'emailed') AND l.email != ''
               ORDER BY m.scheduled_at, m.id LIMIT 1""",
            (now,),
        ).fetchone()
        return dict(row) if row else None


def count_sent_on(day) -> int:
    with get_conn() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM messages WHERE direction = 'out' AND status = 'sent' AND sent_at LIKE ?",
            (f"{day}%",),
        ).fetchone()[0]


def last_sent_at():
    with get_conn() as conn:
        row = conn.execute(
            "SELECT MAX(sent_at) AS last FROM messages WHERE direction = 'out' AND status = 'sent'"
        ).fetchone()
        return row["last"] if row else None


def sent_outbound_for_lead(lead_id):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT * FROM messages WHERE lead_id = ? AND direction = 'out' AND status = 'sent'
               ORDER BY sent_at, id""",
            (lead_id,),
        ).fetchall()
        return [dict(row) for row in rows]


# ── Zużycie API Anthropic ──
def add_usage(purpose, model, input_tokens, output_tokens):
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO api_usage (at, purpose, model, input_tokens, output_tokens) VALUES (?, ?, ?, ?, ?)",
            (now_iso(), purpose, model, int(input_tokens or 0), int(output_tokens or 0)),
        )


def usage_by_model(period_prefix):
    with get_conn() as conn:
        rows = conn.execute(
            """SELECT model, COUNT(*) AS calls,
                      SUM(input_tokens) AS input_tokens, SUM(output_tokens) AS output_tokens
               FROM api_usage WHERE at LIKE ? GROUP BY model""",
            (f"{period_prefix}%",),
        ).fetchall()
        return [dict(row) for row in rows]
