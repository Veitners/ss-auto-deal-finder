"""SQLite storage: listings, price history, scores, crawl runs, settings."""
import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    ext_id TEXT NOT NULL,
    vertical TEXT NOT NULL,
    deal_type TEXT NOT NULL DEFAULT 'sell',
    url TEXT NOT NULL,
    title TEXT,
    description TEXT,
    price REAL,
    region TEXT,
    district TEXT,
    attrs TEXT NOT NULL DEFAULT '{}',
    posted_at TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    last_price_change TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    misses INTEGER NOT NULL DEFAULT 0,
    detail_at TEXT,
    views INTEGER,
    seller TEXT,
    UNIQUE (source, ext_id)
);
CREATE INDEX IF NOT EXISTS idx_listings_scope ON listings (vertical, deal_type, active);
CREATE INDEX IF NOT EXISTS idx_listings_seen ON listings (source, vertical, deal_type, last_seen);

CREATE TABLE IF NOT EXISTS price_history (
    listing_id INTEGER NOT NULL,
    ts TEXT NOT NULL,
    price REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_price_history ON price_history (listing_id, ts);

CREATE TABLE IF NOT EXISTS scores (
    listing_id INTEGER PRIMARY KEY,
    fair REAL,
    sigma REAL,
    conf REAL,
    discount REAL,
    z REAL,
    base REAL,
    method TEXT,
    group_key TEXT,
    group_n INTEGER,
    knn_fair REAL,
    comps TEXT,
    flags TEXT,
    text_mult REAL,
    extra TEXT,
    computed_at TEXT
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job TEXT NOT NULL,
    started TEXT NOT NULL,
    finished TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    pages INTEGER DEFAULT 0,
    seen INTEGER DEFAULT 0,
    new INTEGER DEFAULT 0,
    changed INTEGER DEFAULT 0,
    errors INTEGER DEFAULT 0,
    note TEXT
);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS alerts_sent (listing_id INTEGER PRIMARY KEY, ts TEXT NOT NULL);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def connect(path=None) -> sqlite3.Connection:
    path = path or config.DB_PATH
    if str(path) != ":memory:":
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    _migrate(conn)
    conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after a database was first created (CREATE IF NOT EXISTS skips them)."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(scores)")}
    for column, ddl in (("extra", "TEXT"), ("flags", "TEXT"), ("text_mult", "REAL")):
        if column not in have:
            conn.execute(f"ALTER TABLE scores ADD COLUMN {column} {ddl}")


@dataclass
class Upsert:
    id: int
    is_new: bool
    price_changed: bool
    old_price: float | None


def _merge_attrs(old_json: str, new: dict) -> str:
    merged = json.loads(old_json or "{}")
    for key, value in new.items():
        if value is not None:
            merged[key] = value
    return json.dumps(merged, ensure_ascii=False, sort_keys=True)


def upsert_listing(conn: sqlite3.Connection, rec: dict, now: str | None = None) -> Upsert:
    """Insert or refresh one listing and keep its price history.

    `rec` keys: source, ext_id, vertical, url, price, and optionally deal_type,
    title, description, region, district, attrs, posted_at, views, seller.
    """
    now = now or utcnow()
    deal_type = rec.get("deal_type", "sell")
    row = conn.execute(
        "SELECT id, price, attrs FROM listings WHERE source=? AND ext_id=?",
        (rec["source"], str(rec["ext_id"])),
    ).fetchone()

    if row is None:
        cur = conn.execute(
            """INSERT INTO listings (source, ext_id, vertical, deal_type, url, title, description,
                   price, region, district, attrs, posted_at, first_seen, last_seen,
                   last_price_change, active, misses, views, seller)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,0,?,?)""",
            (
                rec["source"], str(rec["ext_id"]), rec["vertical"], deal_type, rec["url"],
                rec.get("title"), rec.get("description"), rec.get("price"),
                rec.get("region"), rec.get("district"),
                json.dumps({k: v for k, v in (rec.get("attrs") or {}).items() if v is not None},
                           ensure_ascii=False, sort_keys=True),
                rec.get("posted_at"), now, now, now, rec.get("views"), rec.get("seller"),
            ),
        )
        lid = cur.lastrowid
        if rec.get("price") is not None:
            conn.execute("INSERT INTO price_history VALUES (?,?,?)", (lid, now, rec["price"]))
        return Upsert(lid, True, False, None)

    lid, old_price = row["id"], row["price"]
    new_price = rec.get("price")
    changed = new_price is not None and old_price is not None and abs(new_price - old_price) > 0.5
    conn.execute(
        """UPDATE listings SET last_seen=?, active=1, misses=0, url=?, title=COALESCE(?, title),
               description=COALESCE(?, description), price=COALESCE(?, price),
               region=COALESCE(?, region), district=COALESCE(?, district), attrs=?,
               posted_at=COALESCE(?, posted_at), views=COALESCE(?, views),
               seller=COALESCE(?, seller),
               last_price_change=CASE WHEN ? THEN ? ELSE last_price_change END
           WHERE id=?""",
        (
            now, rec["url"], rec.get("title"), rec.get("description"), new_price,
            rec.get("region"), rec.get("district"),
            _merge_attrs(row["attrs"], rec.get("attrs") or {}),
            rec.get("posted_at"), rec.get("views"), rec.get("seller"),
            1 if changed else 0, now, lid,
        ),
    )
    if changed:
        conn.execute("INSERT INTO price_history VALUES (?,?,?)", (lid, now, new_price))
    return Upsert(lid, False, changed, old_price)


def finish_scope(conn: sqlite3.Connection, source: str, vertical: str, deal_type: str,
                 run_started: str, inactive_after: int) -> int:
    """After a COMPLETE crawl, age out listings that were not seen this time."""
    conn.execute(
        """UPDATE listings SET misses = misses + 1
           WHERE source=? AND vertical=? AND deal_type=? AND active=1 AND last_seen < ?""",
        (source, vertical, deal_type, run_started),
    )
    cur = conn.execute(
        """UPDATE listings SET active=0
           WHERE source=? AND vertical=? AND deal_type=? AND active=1 AND misses >= ?""",
        (source, vertical, deal_type, inactive_after),
    )
    conn.commit()
    return cur.rowcount


def start_run(conn: sqlite3.Connection, job: str) -> int:
    cur = conn.execute("INSERT INTO runs (job, started) VALUES (?, ?)", (job, utcnow()))
    conn.commit()
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, **stats) -> None:
    conn.execute(
        """UPDATE runs SET finished=?, status=?, pages=?, seen=?, new=?, changed=?, errors=?, note=?
           WHERE id=?""",
        (utcnow(), status, stats.get("pages", 0), stats.get("seen", 0), stats.get("new", 0),
         stats.get("changed", 0), stats.get("errors", 0), stats.get("note"), run_id),
    )
    conn.commit()


def get_setting(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value)),
    )
    conn.commit()
