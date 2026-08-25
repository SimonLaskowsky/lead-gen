import sqlite3
from contextlib import contextmanager
from datetime import datetime

import os
DB_PATH = os.getenv("DB_PATH", "leads.db")


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
    ]:
        try:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {col} {definition}")
        except Exception:
            pass  # column already exists


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
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


def get_stats():
    with get_conn() as conn:
        stats = {}
        for status in ["new", "emailed", "replied", "converted", "skipped"]:
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
PROFILE_FIELDS = ("name", "domain", "phone", "experience", "realizations")


def get_profiles():
    with get_conn() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM profiles ORDER BY id").fetchall()]


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
    kw = {k: (kwargs.get(k) or "").strip() for k in PROFILE_FIELDS if k in kwargs}
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
