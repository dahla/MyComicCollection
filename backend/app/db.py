import sqlite3
from contextlib import contextmanager
from .config import DB_PATH


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


DEFAULT_CONDITIONS = [
    "Mint", "Near Mint", "Very Fine", "Fine",
    "Very Good", "Good", "Fair", "Poor",
]


def init_collection_tables() -> None:
    with get_conn() as c:
        c.execute("""
            CREATE TABLE IF NOT EXISTS collection_location (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS collection_condition (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                position INTEGER NOT NULL DEFAULT 0
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS collection_item (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                issuecode TEXT NOT NULL,
                location_id INTEGER REFERENCES collection_location(id) ON DELETE SET NULL,
                condition TEXT,
                notes TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_ci_issuecode ON collection_item(issuecode)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS app_setting (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        # Non-Inducks publications (e.g. imported from the Grand Comics Database).
        c.execute("""
            CREATE TABLE IF NOT EXISTS custom_publication (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,           -- 'gcd' or 'manual'
                source_id TEXT,                 -- GCD series id, NULL for manual
                title TEXT NOT NULL,
                countrycode TEXT,
                languagecode TEXT,
                publisher TEXT,
                year_began INTEGER,
                year_ended INTEGER,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(source, source_id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS custom_issue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                publication_id INTEGER NOT NULL
                    REFERENCES custom_publication(id) ON DELETE CASCADE,
                source_id TEXT,
                issuenumber TEXT,
                title TEXT,
                oldestdate TEXT,
                cover_url TEXT,
                position INTEGER NOT NULL DEFAULT 0
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_custom_issue_pub ON custom_issue(publication_id)")

        # Seed default conditions on first run
        if c.execute("SELECT COUNT(*) FROM collection_condition").fetchone()[0] == 0:
            for i, name in enumerate(DEFAULT_CONDITIONS):
                c.execute(
                    "INSERT INTO collection_condition (name, position) VALUES (?, ?)",
                    (name, i),
                )


def has_inducks_data() -> bool:
    with get_conn() as c:
        row = c.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='inducks_publication'"
        ).fetchone()
        if not row:
            return False
        n = c.execute("SELECT COUNT(*) FROM inducks_publication").fetchone()[0]
        return n > 0
