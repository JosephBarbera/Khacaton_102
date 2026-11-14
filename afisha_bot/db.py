"""Хранилище событий: SQLite. Схема соответствует разделу 8.1 ТЗ."""
import hashlib
import json
import os
import re
import sqlite3
import unicodedata
import uuid
from contextlib import closing
from datetime import datetime, timedelta

DB_PATH = os.getenv("DB_PATH", "events.db")

CATEGORIES = ("концерт", "спектакль", "выставка", "фестиваль", "кино", "экскурсия", "другое")
STATUSES = ("опубликовано", "черновик", "отменено")
REQUIRED_FIELDS = ("title", "date_start", "city")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    normalized_title TEXT NOT NULL,
    search_text TEXT NOT NULL DEFAULT '',
    category TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    date_start TEXT NOT NULL,
    date_end TEXT,
    duration_min INTEGER,
    city TEXT NOT NULL,
    city_norm TEXT NOT NULL DEFAULT '',
    venue TEXT,
    address TEXT,
    lat REAL,
    lon REAL,
    description TEXT NOT NULL DEFAULT '',
    price_min INTEGER,
    price_max INTEGER,
    is_free INTEGER NOT NULL DEFAULT 0,
    age_limit INTEGER NOT NULL DEFAULT 0,
    language TEXT,
    org_link TEXT,
    source_url TEXT,
    source_type TEXT NOT NULL DEFAULT 'импорт',
    source_hash TEXT NOT NULL,
    verified_at TEXT,
    confidence REAL NOT NULL DEFAULT 1.0,
    status TEXT NOT NULL DEFAULT 'опубликовано',
    dedup_key TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_city_date ON events(city, date_start);
CREATE INDEX IF NOT EXISTS idx_events_category ON events(category);

CREATE TABLE IF NOT EXISTS subscriptions (
    user_id INTEGER NOT NULL,
    city TEXT NOT NULL,
    category TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (user_id, city, category)
);

CREATE TABLE IF NOT EXISTS change_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    entity_id TEXT,
    details TEXT
);
"""


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(get_conn()) as conn:
        conn.executescript(_SCHEMA)
        conn.commit()


def normalize_title(text):
    text = unicodedata.normalize("NFKC", str(text or "")).lower().strip()
    return re.sub(r"\s+", " ", text)


def parse_price(value):
    """Разбор цены из текста: «бесплатно», «от 300 до 800 Р», «1000 ₽»."""
    if value is None or value == "":
        return None, None
    if isinstance(value, (int, float)):
        return int(value), int(value)
    text = str(value).lower()
    if "бесплат" in text or "свободн" in text or "без вход" in text:
        return 0, 0
    numbers = [int(float(x)) for x in re.findall(r"\d+(?:[.,]\d+)?", text)]
    if not numbers:
        return None, None
    return min(numbers), max(numbers)


def parse_datetime(value):
    """Приводит дату к ISO 8601. Поддерживает относительный формат «+3 19:00»."""
    if value is None or str(value).strip() == "":
        return None, "не заполнено обязательное поле «date_start»"
    if isinstance(value, datetime):
        return value.isoformat(), None
    text = str(value).strip()
    m = re.fullmatch(r"\+?(\d+)\s*(?:d|дн\w*)?\s*(\d{1,2}:\d{2})?", text)
    if m:
        base = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        base += timedelta(days=int(m.group(1)))
        hh, mm = (int(x) for x in (m.group(2) or "00:00").split(":"))
        return base.replace(hour=hh, minute=mm).isoformat(), None
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y-%m-%d", "%d.%m.%Y %H:%M", "%d.%m.%Y"):
        try:
            return datetime.strptime(text, fmt).isoformat(), None
        except ValueError:
            continue
    return None, "не удалось разобрать дату «" + text + "»"


def normalize_event(raw):
    """Валидация и нормализация записи источника.

    Возвращает (событие, ошибка). Поля схемы из раздела 8.1 ТЗ.
    """
    rec = {k: (v.strip() if isinstance(v, str) else v) for k, v in raw.items() if v not in (None, "")}
    errors = []
    for field in REQUIRED_FIELDS:
        if not rec.get(field):
            errors.append("не заполнено обязательное поле «" + field + "»")
    if errors:
        return None, "; ".join(errors)

    title = rec["title"]
    category = str(rec.get("category", "другое")).strip().lower()
    if category not in CATEGORIES:
        category = "другое"

    date_start, err = parse_datetime(rec.get("date_start"))
    if err:
        return None, err
    date_end = None
    if rec.get("date_end"):
        date_end, _ = parse_datetime(rec.get("date_end"))

    if rec.get("price_min") is not None or rec.get("price_max") is not None:
        def to_int(v):
            try:
                return int(float(v)) if v is not None else None
            except (TypeError, ValueError):
                return None
        price_min, price_max = to_int(rec.get("price_min")), to_int(rec.get("price_max"))
    else:
        price_min, price_max = parse_price(rec.get("price"))

    try:
        age_limit = int(float(rec.get("age_limit", 0) or 0))
    except (TypeError, ValueError):
        age_limit = 0

    tags = rec.get("tags", [])
    if isinstance(tags, str):
        tags = [t.strip() for t in re.split(r"[;,]", tags) if t.strip()]
    tags = " ".join(tags)

    is_free = bool(rec.get("is_free")) or (price_min == 0 and price_max == 0)

    now = datetime.now().isoformat(timespec="seconds")
    norm_title = normalize_title(title)
    city = str(rec["city"]).strip()
    venue = str(rec.get("venue") or "").strip()
    dedup_key = "|".join([norm_title, date_start[:10], city.lower(), venue.lower()])
    source_hash = hashlib.sha256(
        json.dumps(rec, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()
    search_text = normalize_title(" ".join([
        title, str(rec.get("description") or ""), city, venue, tags,
    ]))

    return {
        "id": str(rec.get("id") or uuid.uuid4()),
        "title": title,
        "normalized_title": norm_title,
        "search_text": search_text,
        "category": category,
        "tags": tags,
        "date_start": date_start,
        "date_end": date_end,
        "duration_min": rec.get("duration_min"),
        "city": city,
        "city_norm": normalize_title(city),
        "venue": venue,
        "address": rec.get("address"),
        "lat": rec.get("lat"),
        "lon": rec.get("lon"),
        "description": str(rec.get("description") or ""),
        "price_min": price_min,
        "price_max": price_max,
        "is_free": int(is_free),
        "age_limit": age_limit,
        "language": rec.get("language"),
        "org_link": rec.get("org_link") or rec.get("source_url"),
        "source_url": rec.get("source_url"),
        "source_type": rec.get("source_type") or "импорт",
        "source_hash": source_hash,
        "verified_at": now,
        "confidence": 1.0,
        "status": rec.get("status") if rec.get("status") in STATUSES else "опубликовано",
        "dedup_key": dedup_key,
        "created_at": now,
        "updated_at": now,
    }, None


def log_change(actor, action, entity_id, details):
    with closing(get_conn()) as conn:
        conn.execute(
            "INSERT INTO change_log (ts, actor, action, entity_id, details) VALUES (?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), actor, action, entity_id, details),
        )
        conn.commit()


def upsert_event(event, actor="import"):
    """Идемпотентная запись: без дублей по dedup_key, обновление по source_hash."""
    now = datetime.now().isoformat(timespec="seconds")
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT id, source_hash FROM events WHERE dedup_key = ?", (event["dedup_key"],)
        ).fetchone()
        if row is None:
            event["created_at"] = event["updated_at"] = now
            cols = ", ".join(event)
            placeholders = ", ".join("?" for _ in event)
            conn.execute(
                "INSERT INTO events (" + cols + ") VALUES (" + placeholders + ")",
                list(event.values()),
            )
            conn.commit()
            log_change(actor, "add", event["id"], event["title"])
            return "added"
        if row["source_hash"] == event["source_hash"]:
            return "skipped"
        event["id"] = row["id"]
        event["updated_at"] = now
        assignments = ", ".join(k + " = ?" for k in event if k != "created_at")
        values = [v for k, v in event.items() if k != "created_at"]
        conn.execute("UPDATE events SET " + assignments + " WHERE id = ?", values + [row["id"]])
        conn.commit()
        log_change(actor, "update", event["id"], event["title"])
        return "updated"


def period_range(key, custom=None):
    """Границы периода для фильтра дат (строки YYYY-MM-DD)."""
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    if key == "custom" and custom:
        return custom, custom
    if key == "today":
        return today.date().isoformat(), today.date().isoformat()
    if key == "tomorrow":
        t = today + timedelta(days=1)
        return t.date().isoformat(), t.date().isoformat()
    if key == "weekend":
        start = today if today.weekday() in (5, 6) else today + timedelta(days=(5 - today.weekday()) % 7)
        return start.date().isoformat(), (start + timedelta(days=1)).date().isoformat()
    if key == "week":
        return today.date().isoformat(), (today + timedelta(days=7)).date().isoformat()
    return today.date().isoformat(), None  # по умолчанию — предстоящие


def search_events(filters, offset=0, limit=5):
    """Поиск по фильтрам. Событие попадает в период, если пересекается с ним."""
    where = ["status = 'опубликовано'"]
    args = []
    if filters.get("city"):
        where.append("city_norm = ?")
        args.append(normalize_title(filters["city"]))
    if filters.get("category"):
        where.append("category = ?")
        args.append(filters["category"])
    d1, d2 = period_range(filters.get("period") or "upcoming", filters.get("custom_date"))
    if d2 is not None:
        where.append("date(date_start) <= ? AND date(COALESCE(date_end, date_start)) >= ?")
        args += [d2, d1]
    elif d1 is not None:
        where.append("date(COALESCE(date_end, date_start)) >= ?")
        args.append(d1)
    price = filters.get("price")
    if price == "free":
        where.append("(is_free = 1 OR COALESCE(price_min, 0) = 0)")
    elif price and price != "any":
        where.append("COALESCE(price_min, 0) <= ?")
        args.append(int(price))
    if filters.get("kids"):
        where.append("COALESCE(age_limit, 0) <= 12")
    text = filters.get("text")
    if text:
        query = normalize_title(text).replace("%", "").replace("_", "")
        where.append("search_text LIKE ?")
        args.append("%" + query + "%")
    where_sql = " AND ".join(where)
    with closing(get_conn()) as conn:
        total = conn.execute(
            "SELECT COUNT(*) FROM events WHERE " + where_sql, args
        ).fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM events WHERE " + where_sql
            + " ORDER BY date_start LIMIT ? OFFSET ?",
            args + [limit, offset],
        ).fetchall()
    return [dict(r) for r in rows], total


def get_event(event_id):
    with closing(get_conn()) as conn:
        row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return dict(row) if row else None


def cities():
    with closing(get_conn()) as conn:
        rows = conn.execute(
            "SELECT DISTINCT city FROM events WHERE status = 'опубликовано' ORDER BY city"
        ).fetchall()
    return [r["city"] for r in rows]


def find_similar(event_id, limit=3):
    ev = get_event(event_id)
    if not ev:
        return []
    d = ev["date_start"][:10]
    d1 = (datetime.fromisoformat(d) - timedelta(days=3)).date().isoformat()
    d2 = (datetime.fromisoformat(d) + timedelta(days=3)).date().isoformat()
    with closing(get_conn()) as conn:
        rows = conn.execute(
            "SELECT * FROM events WHERE status = 'опубликовано' AND id != ?"
            " AND category = ? AND city_norm = ? AND date(date_start) BETWEEN ? AND ?"
            " ORDER BY date_start LIMIT ?",
            (event_id, ev["category"], ev["city_norm"], d1, d2, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def add_subscription(user_id, city, category):
    with closing(get_conn()) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO subscriptions (user_id, city, category, created_at)"
            " VALUES (?,?,?,?)",
            (user_id, city, category, datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()


def remove_subscriptions(user_id):
    with closing(get_conn()) as conn:
        cur = conn.execute("DELETE FROM subscriptions WHERE user_id = ?", (user_id,))
        conn.commit()
    return cur.rowcount


def list_subscriptions(user_id):
    with closing(get_conn()) as conn:
        rows = conn.execute(
            "SELECT city, category FROM subscriptions WHERE user_id = ? ORDER BY created_at",
            (user_id,),
        ).fetchall()
    return [dict(r) for r in rows]
