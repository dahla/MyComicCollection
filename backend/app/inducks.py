"""Inducks integration: ISV bulk import + cover login/proxy.

Inducks exposes the entire catalogue as a tarball of caret-delimited text
files at https://inducks.org/inducks/isv.tgz (no auth needed). Cover images
require a free account and are fetched via an HR proxy on inducks.org.
"""
from __future__ import annotations

import re
import shutil
import sqlite3
import tarfile
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

import httpx

from . import cache
from .config import DB_PATH, SESSION_FILE

ISV_URL = "https://inducks.org/inducks/isv.tgz"
LOGIN_URL = "https://inducks.org/maccount.php"
HR_URL = "https://inducks.org/hr.php"

# Tables we import and the columns we keep. Column names must match what the
# Inducks createtables.sql declares — order is read from that file at import
# time, so adding/removing columns upstream won't corrupt the import.
WANTED_TABLES: dict[str, list[str]] = {
    "inducks_publication": ["publicationcode", "countrycode", "title", "languagecode"],
    "inducks_issue":       ["issuecode", "publicationcode", "issuenumber", "title", "oldestdate"],
    "inducks_country":     ["countrycode", "countryname"],
    "inducks_language":    ["languagecode", "languagename"],
    "inducks_entry":       ["entrycode", "issuecode", "position", "storyversioncode", "title"],
    "inducks_storyversion":["storyversioncode", "storycode"],
    "inducks_story":       ["storycode", "title"],
    "inducks_entryurl":    ["entrycode", "sitecode", "url"],
    "inducks_site":        ["sitecode", "urlbase", "images"],
}

INDEXES = [
    ("idx_pub_pubcode",      "inducks_publication",  "publicationcode"),
    ("idx_pub_country",      "inducks_publication",  "countrycode"),
    ("idx_pub_title",        "inducks_publication",  "title"),
    ("idx_issue_issuecode",  "inducks_issue",        "issuecode"),
    ("idx_issue_pub",        "inducks_issue",        "publicationcode"),
    ("idx_issue_title",      "inducks_issue",        "title"),
    ("idx_entry_issue",      "inducks_entry",        "issuecode"),
    ("idx_entry_sv",         "inducks_entry",        "storyversioncode"),
    ("idx_entry_title",      "inducks_entry",        "title"),
    ("idx_sv_svcode",        "inducks_storyversion", "storyversioncode"),
    ("idx_sv_story",         "inducks_storyversion", "storycode"),
    ("idx_story_storycode",  "inducks_story",        "storycode"),
    ("idx_story_title",      "inducks_story",        "title"),
    ("idx_eurl_entry",       "inducks_entryurl",     "entrycode"),
    ("idx_country_code",     "inducks_country",      "countrycode"),
    ("idx_language_code",    "inducks_language",     "languagecode"),
    ("idx_site_sitecode",    "inducks_site",         "sitecode"),
]

_progress: dict = {"status": "idle", "detail": "", "percent": 0, "error": None}
_lock = threading.Lock()


def get_progress() -> dict:
    return dict(_progress)


def _set(status: str, detail: str = "", percent: int | None = None, error: str | None = None):
    _progress["status"] = status
    _progress["detail"] = detail
    if percent is not None:
        _progress["percent"] = percent
    _progress["error"] = error


# ---------------------------------------------------------------- ISV import

def _parse_table_columns(sql_text: str, table: str) -> list[str]:
    """Return the ordered column list of `table` from the createtables.sql file.

    Inducks defines tables as `CREATE TABLE foo_temp (...)`. We only need
    column names, in declaration order — that's the order they appear in the
    matching .isv file.
    """
    # The body ends with ')' at the start of a line (the closing paren of the
    # column list). What follows on that line — `ENGINE=InnoDB ...;` — varies,
    # so anchor on the line-start ')' rather than ');'.
    pat = re.compile(
        rf"CREATE\s+TABLE\s+{table}_temp\s*\((.*?)^\)",
        re.IGNORECASE | re.DOTALL | re.MULTILINE,
    )
    m = pat.search(sql_text)
    if not m:
        return []
    cols = []
    for line in m.group(1).splitlines():
        line = line.strip().rstrip(",")
        if not line:
            continue
        if re.match(r"^(KEY|PRIMARY|UNIQUE|INDEX|CONSTRAINT|FOREIGN)\b", line, re.I):
            continue
        m2 = re.match(r"(\w+)\s+", line)
        if m2:
            cols.append(m2.group(1))
    return cols


def _stream_isv(path: Path, indices: list[int]):
    """Yield tuples of selected fields from an Inducks .isv file."""
    with open(path, "r", encoding="utf-8", errors="replace", buffering=1 << 20) as f:
        next(f, None)  # header
        for line in f:
            fields = line.rstrip("\n").split("^")
            yield tuple(fields[i] if i < len(fields) else "" for i in indices)


def _import_one(conn: sqlite3.Connection, isv_dir: Path, table: str,
                wanted_cols: list[str], all_cols: list[str]) -> int:
    isv_path = isv_dir / f"{table}.isv"
    if not isv_path.exists():
        return 0

    indices = [all_cols.index(c) for c in wanted_cols if c in all_cols]
    cols = [all_cols[i] for i in indices]
    if not cols:
        return 0

    conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.execute(f"CREATE TABLE {table} ({', '.join(f'[{c}] TEXT' for c in cols)})")

    placeholders = ",".join("?" * len(cols))
    sql = f"INSERT INTO {table} VALUES ({placeholders})"

    batch: list[tuple] = []
    n = 0
    for row in _stream_isv(isv_path, indices):
        batch.append(row)
        if len(batch) >= 50_000:
            conn.executemany(sql, batch)
            n += len(batch)
            batch.clear()
    if batch:
        conn.executemany(sql, batch)
        n += len(batch)
    return n


def _download(work: Path) -> Path:
    dest = work / "isv.tgz"
    _set("downloading", "Contacting inducks.org…", 0)
    with httpx.stream("GET", ISV_URL, timeout=300, follow_redirects=True) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        got = 0
        with open(dest, "wb") as f:
            for chunk in r.iter_bytes(1 << 20):
                f.write(chunk)
                got += len(chunk)
                if total:
                    _set("downloading",
                         f"Downloading: {got // (1 << 20)} MB / {total // (1 << 20)} MB",
                         got * 100 // total)
    return dest


def run_import() -> None:
    """Full ISV pipeline. Run this in a background thread."""
    work = Path(tempfile.mkdtemp(prefix="comicvault_isv_"))
    try:
        tgz = _download(work)

        _set("extracting", "Extracting archive…", None)
        with tarfile.open(tgz, "r:gz") as tar:
            tar.extractall(work, filter="data")
        isv_dir = work / "isv"
        if not isv_dir.exists():
            isv_dir = work

        sql_path = isv_dir / "createtables.sql"
        if not sql_path.exists():
            raise RuntimeError("createtables.sql missing from ISV archive")
        sql_text = sql_path.read_text(encoding="utf-8", errors="replace")

        conn = sqlite3.connect(DB_PATH, timeout=30)
        try:
            conn.execute("PRAGMA synchronous=OFF")
            conn.execute("PRAGMA journal_mode=MEMORY")
            conn.execute("PRAGMA cache_size=-64000")
            conn.execute("BEGIN")

            tables = list(WANTED_TABLES.items())
            for i, (tname, wanted) in enumerate(tables, 1):
                _set("importing", f"Importing {tname} ({i}/{len(tables)})",
                     int(i / len(tables) * 100))
                all_cols = _parse_table_columns(sql_text, tname)
                if not all_cols:
                    continue
                _import_one(conn, isv_dir, tname, wanted, all_cols)

            _set("indexing", "Building indexes…", 100)
            for name, table, col in INDEXES:
                try:
                    conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table}({col})")
                except sqlite3.OperationalError:
                    pass

            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.close()

        _set("done", "Sync complete.", 100)
    except Exception as e:
        _set("error", str(e), error=str(e))
        raise
    finally:
        shutil.rmtree(work, ignore_errors=True)


def start_import_async() -> bool:
    """Kick off run_import() in a background thread. Returns False if one is
    already running."""
    if not _lock.acquire(blocking=False):
        return False

    def _run():
        try:
            run_import()
        finally:
            _lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return True


# ---------------------------------------------------------------- Covers

def _load_cookie() -> str | None:
    if SESSION_FILE.exists():
        return SESSION_FILE.read_text().strip() or None
    return None


def _save_cookie(cookie: str) -> None:
    SESSION_FILE.write_text(cookie)


def login(username: str, password: str) -> None:
    """Authenticate against inducks.org and persist the session cookie."""
    with httpx.Client(follow_redirects=False, timeout=20) as client:
        # Seed cookies on the login page
        seed = client.get(LOGIN_URL)
        cookies = dict(seed.cookies)

        resp = client.post(
            LOGIN_URL,
            data={
                "login": username,
                "pass": password,
                "rememberme": "on",
                "redirect": "/maccount.php",
            },
            cookies=cookies,
        )
        merged = {**cookies, **dict(resp.cookies)}
        if not merged:
            raise RuntimeError("Inducks login failed — no cookies issued")

        _save_cookie("; ".join(f"{k}={v}" for k, v in merged.items()))


def is_logged_in() -> bool:
    return _load_cookie() is not None


def logout() -> None:
    SESSION_FILE.unlink(missing_ok=True)


def _resolve_cover_url(conn: sqlite3.Connection, issuecode: str) -> str | None:
    """Return the full image URL for a given issue, or None if absent."""
    row = conn.execute(
        """
        SELECT s.urlbase || eu.url
        FROM inducks_entryurl eu
        JOIN inducks_entry e   ON e.entrycode = eu.entrycode
        JOIN inducks_site s    ON s.sitecode  = eu.sitecode
        WHERE e.issuecode = ?
          AND eu.entrycode = ?
          AND s.images = 'Y'
          AND eu.sitecode NOT LIKE 'thumbnails%'
        LIMIT 1
        """,
        (issuecode, issuecode + "a"),
    ).fetchone()
    if row:
        return row[0]
    row = conn.execute(
        """
        SELECT s.urlbase || eu.url
        FROM inducks_entryurl eu
        JOIN inducks_entry e   ON e.entrycode = eu.entrycode
        JOIN inducks_site s    ON s.sitecode  = eu.sitecode
        WHERE e.issuecode = ?
          AND s.images = 'Y'
          AND eu.sitecode NOT LIKE 'thumbnails%'
        ORDER BY e.position
        LIMIT 1
        """,
        (issuecode,),
    ).fetchone()
    return row[0] if row else None


def fetch_cover_for_issue(conn: sqlite3.Connection, issuecode: str) -> tuple[Path, str] | None:
    """Return (path, mime) for a cached or freshly fetched cover. None if no
    image is registered in Inducks for the issue."""
    cached = cache.cache_path_for(issuecode)
    if cached:
        return cached, cache.mime_from_ext(cached)

    cookie = _load_cookie()
    if not cookie:
        raise PermissionError("Not logged in to inducks.org")

    full_url = _resolve_cover_url(conn, issuecode)
    if not full_url:
        return None

    hr_url = f"{HR_URL}?normalsize=1&image={urllib.parse.quote(full_url, safe='')}"
    with httpx.Client(follow_redirects=True, timeout=20) as client:
        resp = client.get(hr_url, headers={"Cookie": cookie})

    if resp.status_code == 401 or b"Please log in" in resp.content[:500]:
        logout()
        raise PermissionError("Inducks session expired")
    if resp.status_code != 200:
        raise RuntimeError(f"Inducks HR returned HTTP {resp.status_code}")

    ctype = resp.headers.get("content-type", "image/jpeg")
    path = cache.cache_write(issuecode, resp.content, ctype)
    return path, cache.mime_from_ext(path)


def fetch_icon_for_publication(conn: sqlite3.Connection, publicationcode: str) -> tuple[Path, str] | None:
    """Return a representative cover for a publication (its earliest issue)."""
    cached = cache.cache_path_for("pub:" + publicationcode)
    if cached:
        return cached, cache.mime_from_ext(cached)

    row = conn.execute(
        """
        SELECT issuecode FROM inducks_issue
        WHERE publicationcode = ?
        ORDER BY oldestdate, issuenumber
        LIMIT 1
        """,
        (publicationcode,),
    ).fetchone()
    if not row:
        return None

    result = fetch_cover_for_issue(conn, row[0])
    if not result:
        return None
    # Symlink-ish: copy bytes into a pub: cache key so we don't re-resolve next time
    src, mime = result
    dest = cache.cache_write("pub:" + publicationcode, src.read_bytes(), mime)
    return dest, cache.mime_from_ext(dest)
