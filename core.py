"""Channel-agnostic core: DB, statuses, URL handling, date parsing.

Nothing here knows about Telegram, so a WhatsApp adapter can reuse it as-is.
"""
from __future__ import annotations

import os
import re
import sqlite3
from datetime import date, datetime, time, timedelta
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Jerusalem")

# ---------------------------------------------------------------- statuses

STATUSES: dict[str, tuple[str, str]] = {
    "new": ("🆕", "חדשה"),
    "contacted": ("📞", "דיברנו"),
    "scheduled": ("📅", "קבענו"),
    "seen": ("👀", "ראינו"),
    "no_answer": ("📵", "לא עונה"),
    "want": ("❤️", "רוצים"),
    "rejected": ("❌", "דחינו"),
}
FINAL = {"want", "rejected"}

# Allowed next statuses from each status.
# - no_answer: reachable from every open stage
# - rejected: reachable from every open stage
# - want: only after seen
# - scheduled -> scheduled = reschedule
TRANSITIONS: dict[str, list[str]] = {
    "new": ["contacted", "scheduled", "no_answer", "rejected"],
    "contacted": ["scheduled", "no_answer", "rejected"],
    "scheduled": ["seen", "scheduled", "no_answer", "rejected"],
    "seen": ["want", "rejected", "no_answer"],
    "no_answer": ["contacted", "scheduled", "rejected"],
    "want": [],
    "rejected": [],
}

# Display order for lists
ORDER = ["scheduled", "want", "seen", "contacted", "new", "no_answer", "rejected"]


def label(status: str) -> str:
    emoji, name = STATUSES[status]
    return f"{emoji} {name}"


def now() -> datetime:
    return datetime.now(TZ).replace(microsecond=0)


def now_iso() -> str:
    return now().isoformat()


# ---------------------------------------------------------------- URLs

URL_RE = re.compile(r"https?://[^\s<>\"']+")
_TRAILING = ".,;:!?)]}'\"״׳"
_DROP_PARAMS = {
    "fbclid", "mibextid", "rdid", "ref", "refsrc", "sfnsn", "igsh", "s",
    "__cft__", "__tn__", "share_url", "_rdr", "app", "opentracking",
}

_QUERYLESS = {"yad2.co.il", "madlan.co.il", "homeless.co.il", "komo.co.il"}

SOURCES = {
    "facebook.com": "פייסבוק",
    "fb.com": "פייסבוק",
    "fb.me": "פייסבוק",
    "yad2.co.il": "יד2",
    "madlan.co.il": "מדלן",
    "homeless.co.il": "הומלס",
    "komo.co.il": "קומו",
    "winwin.co.il": "ווין ווין",
}


def find_urls(text: str) -> list[str]:
    return [u.rstrip(_TRAILING) for u in URL_RE.findall(text or "")]


def normalize_url(url: str) -> str:
    parts = urlsplit(url.strip().rstrip(_TRAILING))
    host = parts.netloc.lower()
    for prefix in ("www.", "m.", "mobile.", "web."):
        if host.startswith(prefix):
            host = host[len(prefix):]
    if any(host == d or host.endswith("." + d) for d in _QUERYLESS):
        query = []  # listing sites: the item id is in the path, query is just tracking
    else:
        query = [
            (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
            if not k.lower().startswith("utm_") and k.lower() not in _DROP_PARAMS
        ]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit(("https", host, path, urlencode(query), ""))


def source_of(url: str | None) -> str | None:
    if not url:
        return None
    host = urlsplit(url).netloc.lower()
    for domain, name in SOURCES.items():
        if host == domain or host.endswith("." + domain):
            return name
    return host or None


# ---------------------------------------------------------------- dates

HE_WEEKDAYS = ["שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון"]  # python weekday() order
_WD_RE = re.compile(r"(?:^|[\s\-ב])(?:יום\s+)?(ראשון|שלישי|רביעי|חמישי|שישי|שני|שבת)(?![א-ת])")
_DATE_RE = re.compile(r"(?<![\d:])(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?(?![\d:])")
_HHMM_RE = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?!\d)")
_HOUR_RE = re.compile(r"(?:ב-?|בשעה\s*|שעה\s*)(\d{1,2})(?![\d./:])")


def _parse_time(text: str) -> time | None:
    m = _HHMM_RE.search(text)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
    else:
        m = _HOUR_RE.search(text)
        if not m:
            return None
        h, mi = int(m.group(1)), 0
        if 1 <= h <= 7:  # "ב-6" almost always means evening for a viewing
            h += 12
    if not (0 <= h <= 23 and 0 <= mi <= 59):
        return None
    return time(h, mi)


def parse_when(text: str, ref: datetime | None = None) -> str | None:
    """Deterministic parser for common Hebrew phrasings.

    Returns 'YYYY-MM-DDTHH:MM', 'YYYY-MM-DD' (no time), or None (unrecognized -> try LLM).
    """
    ref = ref or now()
    today = ref.date()
    t = _parse_time(text)
    d: date | None = None

    m = _DATE_RE.search(text)
    if m:
        day, month = int(m.group(1)), int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        if year < 100:
            year += 2000
        try:
            d = date(year, month, day)
        except ValueError:
            return None
        if not m.group(3) and d < today:
            d = date(year + 1, month, day)
    elif "מחרתיים" in text:
        d = today + timedelta(days=2)
    elif "מחר" in text:
        d = today + timedelta(days=1)
    elif "היום" in text or "הערב" in text:
        d = today
    else:
        wm = _WD_RE.search(text)
        if wm:
            wd = HE_WEEKDAYS.index(wm.group(1))
            delta = (wd - today.weekday()) % 7
            if delta == 0 and not (t and t > ref.time()):
                delta = 7
            d = today + timedelta(days=delta)

    if d is None:
        return None
    return f"{d.isoformat()}T{t.strftime('%H:%M')}" if t else d.isoformat()


def viewing_dt(s: str) -> tuple[datetime, bool]:
    """Returns (datetime, has_time). Date-only values are treated as end of day."""
    if "T" in s:
        return datetime.fromisoformat(s).replace(tzinfo=TZ), True
    d = date.fromisoformat(s)
    return datetime.combine(d, time(23, 59), tzinfo=TZ), False


def fmt_when(s: str | None) -> str:
    if not s:
        return ""
    dt, has_time = viewing_dt(s)
    out = f"{HE_WEEKDAYS[dt.weekday()]} {dt.day}.{dt.month}"
    return f"{out} · {dt.strftime('%H:%M')}" if has_time else out


# ---------------------------------------------------------------- store

SCHEMA = """
CREATE TABLE IF NOT EXISTS apartments(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    url TEXT UNIQUE,
    source TEXT,
    address TEXT,
    neighborhood TEXT,
    price INTEGER,
    rooms REAL,
    contact_name TEXT,
    phone TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    viewing_at TEXT,
    raw_text TEXT,
    added_by TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    apartment_id INTEGER NOT NULL REFERENCES apartments(id),
    user TEXT,
    type TEXT NOT NULL,          -- created | status | note | viewing | details
    value TEXT,
    created_at TEXT NOT NULL
);
-- every bot message tied to an apartment (cards, log lines, "when?" questions),
-- so a Reply to any of them lands on the right apartment
CREATE TABLE IF NOT EXISTS messages(
    chat_id INTEGER NOT NULL,
    msg_id INTEGER NOT NULL,
    apartment_id INTEGER NOT NULL REFERENCES apartments(id),
    kind TEXT NOT NULL,          -- card | log | ask_when
    PRIMARY KEY (chat_id, msg_id)
);
CREATE INDEX IF NOT EXISTS idx_events_apt ON events(apartment_id);
CREATE INDEX IF NOT EXISTS idx_messages_apt ON messages(apartment_id);
"""

DETAIL_FIELDS = ["address", "neighborhood", "price", "rooms", "contact_name", "phone"]


class TransitionError(ValueError):
    pass


class Store:
    def __init__(self, path: str):
        d = os.path.dirname(path)
        if d:
            os.makedirs(d, exist_ok=True)
        self.path = path
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(SCHEMA)
        self.db.commit()

    # -- events
    def _event(self, apt_id: int, user: str | None, type_: str, value: str | None = None):
        ts = now_iso()
        self.db.execute(
            "INSERT INTO events(apartment_id,user,type,value,created_at) VALUES (?,?,?,?,?)",
            (apt_id, user, type_, value, ts),
        )
        self.db.execute("UPDATE apartments SET updated_at=? WHERE id=?", (ts, apt_id))

    # -- apartments
    def create(self, *, url: str | None, raw_text: str | None, fields: dict | None, user: str) -> int:
        f = {k: (fields or {}).get(k) for k in DETAIL_FIELDS}
        ts = now_iso()
        cur = self.db.execute(
            """INSERT INTO apartments(url,source,address,neighborhood,price,rooms,contact_name,phone,
                                      status,raw_text,added_by,created_at,updated_at)
               VALUES (?,?,?,?,?,?,?,?,'new',?,?,?,?)""",
            (url, source_of(url), f["address"], f["neighborhood"], f["price"], f["rooms"],
             f["contact_name"], f["phone"], raw_text, user, ts, ts),
        )
        apt_id = cur.lastrowid
        self._event(apt_id, user, "created")
        self.db.commit()
        return apt_id

    def get(self, apt_id: int) -> dict | None:
        row = self.db.execute("SELECT * FROM apartments WHERE id=?", (apt_id,)).fetchone()
        return dict(row) if row else None

    def by_url(self, url: str) -> dict | None:
        row = self.db.execute("SELECT * FROM apartments WHERE url=?", (url,)).fetchone()
        return dict(row) if row else None

    def set_url(self, apt_id: int, url: str) -> bool:
        if self.by_url(url):
            return False
        self.db.execute("UPDATE apartments SET url=?, source=? WHERE id=?", (url, source_of(url), apt_id))
        self.db.commit()
        return True

    def fill_details(self, apt_id: int, fields: dict, raw_text: str | None, user: str) -> list[str]:
        """Fill only missing fields. Returns the names of fields that were filled."""
        apt = self.get(apt_id)
        filled = []
        for k in DETAIL_FIELDS:
            v = fields.get(k)
            if v not in (None, "") and apt.get(k) in (None, ""):
                self.db.execute(f"UPDATE apartments SET {k}=? WHERE id=?", (v, apt_id))
                filled.append(k)
        if raw_text:
            merged = "\n\n".join(x for x in (apt.get("raw_text"), raw_text) if x)
            self.db.execute("UPDATE apartments SET raw_text=? WHERE id=?", (merged, apt_id))
        self._event(apt_id, user, "details", ",".join(filled))
        self.db.commit()
        return filled

    def missing_details(self, apt: dict) -> bool:
        return not apt.get("address") and not apt.get("price")

    def set_status(self, apt_id: int, status: str, user: str, viewing_at: str | None = None):
        apt = self.get(apt_id)
        if not apt:
            raise TransitionError("not found")
        if status not in TRANSITIONS[apt["status"]]:
            raise TransitionError(f"{apt['status']} -> {status} not allowed")
        if status == "scheduled":
            if not viewing_at:
                raise TransitionError("scheduled requires viewing_at")
            self.db.execute("UPDATE apartments SET viewing_at=? WHERE id=?", (viewing_at, apt_id))
            self._event(apt_id, user, "viewing", viewing_at)
        self.db.execute("UPDATE apartments SET status=? WHERE id=?", (status, apt_id))
        self._event(apt_id, user, "status", f"{apt['status']}>{status}")
        self.db.commit()

    def reopen(self, apt_id: int, user: str) -> str:
        """Undo a final decision: go back to the status before it."""
        apt = self.get(apt_id)
        if apt["status"] not in FINAL:
            raise TransitionError("not final")
        row = self.db.execute(
            "SELECT value FROM events WHERE apartment_id=? AND type='status' ORDER BY id DESC LIMIT 1",
            (apt_id,),
        ).fetchone()
        prev = row["value"].split(">")[0] if row else "new"
        if prev in FINAL:
            prev = "new"
        self.db.execute("UPDATE apartments SET status=? WHERE id=?", (prev, apt_id))
        self._event(apt_id, user, "status", f"{apt['status']}>{prev}")
        self.db.commit()
        return prev

    def add_note(self, apt_id: int, text: str, user: str):
        self._event(apt_id, user, "note", text)
        self.db.commit()

    def notes(self, apt_id: int, limit: int = 3) -> list[dict]:
        rows = self.db.execute(
            "SELECT user,value,created_at FROM events WHERE apartment_id=? AND type='note' "
            "ORDER BY id DESC LIMIT ?",
            (apt_id, limit),
        ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def list(self, statuses: list[str] | None = None) -> list[dict]:
        rows = [dict(r) for r in self.db.execute("SELECT * FROM apartments").fetchall()]
        if statuses is not None:
            rows = [r for r in rows if r["status"] in statuses]
        rows.sort(key=lambda r: (ORDER.index(r["status"]), r["viewing_at"] or "", -r["id"]))
        return rows

    def upcoming(self, days: int = 7) -> list[dict]:
        start = datetime.combine(now().date(), time(0), tzinfo=TZ)
        end = start + timedelta(days=days + 1)
        out = []
        for r in self.list(["scheduled"]):
            if r["viewing_at"]:
                dt, _ = viewing_dt(r["viewing_at"])
                if start <= dt < end:
                    out.append(r)
        out.sort(key=lambda r: r["viewing_at"])
        return out

    def digest(self, stale_days: int = 2) -> dict[str, list[dict]]:
        n = now()
        today = n.date()
        stale_before = n - timedelta(days=stale_days)
        out = {"today": [], "tomorrow": [], "passed": [], "new_stale": [], "no_answer_stale": [], "seen_stale": []}
        for r in self.list():
            upd = datetime.fromisoformat(r["updated_at"])
            if r["status"] == "scheduled" and r["viewing_at"]:
                dt, _ = viewing_dt(r["viewing_at"])
                if dt.date() == today:
                    out["today"].append(r)
                elif dt.date() == today + timedelta(days=1):
                    out["tomorrow"].append(r)
                elif dt.date() < today:
                    out["passed"].append(r)
            elif r["status"] == "new" and upd < stale_before:
                out["new_stale"].append(r)
            elif r["status"] == "no_answer" and upd < stale_before:
                out["no_answer_stale"].append(r)
            elif r["status"] == "seen" and upd < stale_before:
                out["seen_stale"].append(r)
        for k in ("today", "tomorrow", "passed"):
            out[k].sort(key=lambda r: r["viewing_at"])
        return out

    # -- message <-> apartment mapping
    def register_msg(self, chat_id: int, msg_id: int, apt_id: int, kind: str):
        self.db.execute(
            "INSERT OR REPLACE INTO messages(chat_id,msg_id,apartment_id,kind) VALUES (?,?,?,?)",
            (chat_id, msg_id, apt_id, kind),
        )
        self.db.commit()

    def lookup_msg(self, chat_id: int, msg_id: int) -> tuple[int, str] | None:
        row = self.db.execute(
            "SELECT apartment_id,kind FROM messages WHERE chat_id=? AND msg_id=?", (chat_id, msg_id)
        ).fetchone()
        return (row["apartment_id"], row["kind"]) if row else None

    def cards_of(self, apt_id: int) -> list[tuple[int, int]]:
        rows = self.db.execute(
            "SELECT chat_id,msg_id FROM messages WHERE apartment_id=? AND kind='card'", (apt_id,)
        ).fetchall()
        return [(r["chat_id"], r["msg_id"]) for r in rows]

    def backup_to(self, path: str):
        dst = sqlite3.connect(path)
        with dst:
            self.db.backup(dst)
        dst.close()
