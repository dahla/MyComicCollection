"""Grand Comics Database client — search + series + cover proxy.

GCD exposes a public read-only REST API at https://www.comics.org/api/.
No authentication is required. The series-detail endpoint conveniently
returns the full list of issue IDs for the run, so we can import a whole
series with two HTTP calls (search + series detail) and fetch each cover
lazily later, the first time it's viewed.

A single covers cache directory is shared with the Inducks side; collisions
are avoided because GCD covers are keyed under synthetic ``ci:<id>`` issue
codes whose md5 differs from any real Inducks issuecode.
"""
from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path

import httpx

from . import cache
from .config import DATA_DIR

API_BASE = "https://www.comics.org/api"
CREDS_FILE = DATA_DIR / "gcd_credentials.json"

# GCD will return 429 if we hammer it. Keep at least this many seconds between
# successive API calls (image-CDN fetches don't go through here so they aren't
# throttled). 2 s ≈ 30 requests/min, which has been safe in practice.
_API_MIN_INTERVAL = 2.0
_api_lock = threading.Lock()
_last_api_call = [0.0]
# When we hit a 429, set this to monotonic() + Retry-After. The _unblocked
# Event is cleared during the cooldown so all threads wait together (instead
# of queueing through a single lock).
_blocked_until = [0.0]
_unblocked = threading.Event()
_unblocked.set()


def throttle_seconds_remaining() -> int:
    """Seconds until the next GCD API call may proceed (0 when not throttled)."""
    return max(0, int(_blocked_until[0] - time.monotonic() + 0.999))


def _set_cooldown(seconds: int):
    _blocked_until[0] = time.monotonic() + seconds
    _unblocked.clear()
    threading.Timer(seconds, _unblocked.set).start()


def _throttle():
    # All callers wait on the cooldown event together — no lock held during sleep.
    _unblocked.wait()
    with _api_lock:
        wait = _API_MIN_INTERVAL - (time.monotonic() - _last_api_call[0])
        if wait > 0:
            time.sleep(wait)
        _last_api_call[0] = time.monotonic()


def _api_get(client: httpx.Client, url: str) -> httpx.Response:
    """GET a GCD API URL, respecting global throttle and Retry-After on 429.
    Retries up to 4 times when rate-limited."""
    last_resp = None
    for _ in range(4):
        _throttle()
        last_resp = client.get(url)
        if last_resp.status_code != 429:
            return last_resp
        try:
            wait = int(last_resp.headers.get("Retry-After", "30") or "30")
        except ValueError:
            wait = 30
        _set_cooldown(max(wait, 5))
    return last_resp


def _load_credentials() -> dict | None:
    if not CREDS_FILE.exists():
        return None
    try:
        data = json.loads(CREDS_FILE.read_text())
        if data.get("username") and data.get("password"):
            return data
    except Exception:
        pass
    return None


def _save_credentials(username: str, password: str) -> None:
    CREDS_FILE.write_text(json.dumps({"username": username, "password": password}))


def is_logged_in() -> bool:
    return _load_credentials() is not None


def login(username: str, password: str) -> None:
    """Persist GCD credentials. They're attached as HTTP Basic auth on every
    subsequent API call, which gives us a much higher per-hour quota than the
    anonymous limit. Credentials are saved to DATA_DIR in plaintext JSON —
    same trust model as the Inducks session cookie."""
    if not username or not password:
        raise ValueError("Username and password required")
    _save_credentials(username, password)


def logout() -> None:
    CREDS_FILE.unlink(missing_ok=True)


def _client() -> httpx.Client:
    creds = _load_credentials()
    auth = (creds["username"], creds["password"]) if creds else None
    return httpx.Client(
        timeout=20,
        follow_redirects=True,
        headers={"Accept": "application/json", "User-Agent": "ComicVault/1.0"},
        auth=auth,
    )


def search_series(name: str) -> list[dict]:
    """Search GCD by series name. Returns a list of series summaries."""
    with _client() as client:
        resp = _api_get(client, f"{API_BASE}/series/name/{name}/")
        resp.raise_for_status()
        data = resp.json()
    # GCD returns a paginated 'results' list or a bare list depending on the
    # endpoint flavour; handle both.
    items = data.get("results", data) if isinstance(data, dict) else data
    out = []
    for it in items:
        out.append({
            "id": _id_from_url(it.get("api_url", "")),
            "name": it.get("name") or "",
            "country": it.get("country") or "",
            "language": it.get("language") or "",
            "year_began": it.get("year_began"),
            "year_ended": it.get("year_ended"),
            "issue_count": len(it.get("active_issues") or []),
            "publisher_url": it.get("publisher", ""),
        })
    return out


def fetch_series(series_id: str) -> dict:
    """Get full series record including the list of issue IDs."""
    with _client() as client:
        resp = _api_get(client, f"{API_BASE}/series/{series_id}/")
        resp.raise_for_status()
        series = resp.json()

        publisher_name = ""
        pub_url = series.get("publisher")
        if pub_url:
            try:
                p = _api_get(client, pub_url).json()
                publisher_name = p.get("name") or ""
            except Exception:
                pass

    issues = []
    for i, url in enumerate(series.get("active_issues") or []):
        issues.append({
            "source_id": _id_from_url(url),
            "position": i,
        })
    descriptors = series.get("issue_descriptors") or []
    for i, num in enumerate(descriptors):
        if i < len(issues):
            issues[i]["issuenumber"] = str(num)

    return {
        "source_id": str(series_id),
        "title": series.get("name") or "",
        "countrycode": series.get("country") or "",
        "languagecode": series.get("language") or "",
        "year_began": _safe_int(series.get("year_began")),
        "year_ended": _safe_int(series.get("year_ended")),
        "publisher": publisher_name,
        "issues": issues,
    }


def fetch_issue(issue_source_id: str) -> dict:
    """Fetch a single issue's metadata (cover URL, date, title)."""
    with _client() as client:
        resp = _api_get(client, f"{API_BASE}/issue/{issue_source_id}/")
        resp.raise_for_status()
        data = resp.json()
    return {
        "issuenumber": data.get("number") or "",
        "title": data.get("title") or "",
        "oldestdate": data.get("publication_date") or "",
        "cover_url": data.get("cover") or "",
    }


# ---- background enrichment ------------------------------------------------

# Per-publication enrichment status, keyed by custom_publication.id.
# {pub_id: {"running": bool, "done": int, "total": int, "errors": int}}
_enrich_status: dict[int, dict] = {}
_enrich_lock = threading.Lock()


def get_enrich_status(pub_id: int) -> dict:
    base = _enrich_status.get(
        pub_id, {"running": False, "done": 0, "total": 0, "errors": 0}
    )
    return {**base, "throttled_seconds": throttle_seconds_remaining()}


def _enrich_worker(pub_id: int, db_path: str) -> None:
    """Walk every issue of a custom publication and fill in title/date/cover_url
    from GCD, throttled by `_api_get`. Idempotent — issues that already have a
    cover_url are skipped."""
    import sqlite3
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        # Only retry rows we haven't asked GCD about yet (cover_url IS NULL).
        # An empty string means GCD told us there's no cover for that issue —
        # re-asking would just consume rate-limit quota for the same answer.
        rows = conn.execute(
            "SELECT id, source_id FROM custom_issue "
            "WHERE publication_id = ? AND cover_url IS NULL "
            "ORDER BY position",
            (pub_id,),
        ).fetchall()
        with _enrich_lock:
            _enrich_status[pub_id] = {"running": True, "done": 0, "total": len(rows), "errors": 0}

        for r in rows:
            try:
                meta = fetch_issue(r["source_id"])
                conn.execute(
                    """
                    UPDATE custom_issue
                    SET cover_url = ?,
                        title = COALESCE(NULLIF(title, ''), ?),
                        oldestdate = COALESCE(NULLIF(oldestdate, ''), ?)
                    WHERE id = ?
                    """,
                    (meta["cover_url"], meta["title"], meta["oldestdate"], r["id"]),
                )
                conn.commit()
            except Exception:
                with _enrich_lock:
                    _enrich_status[pub_id]["errors"] += 1
            with _enrich_lock:
                _enrich_status[pub_id]["done"] += 1
    finally:
        with _enrich_lock:
            if pub_id in _enrich_status:
                _enrich_status[pub_id]["running"] = False
        conn.close()


def start_enrichment(pub_id: int, db_path: str) -> bool:
    """Kick off `_enrich_worker` in a daemon thread. Returns False if a job
    for this publication is already running."""
    with _enrich_lock:
        if _enrich_status.get(pub_id, {}).get("running"):
            return False
        _enrich_status[pub_id] = {"running": True, "done": 0, "total": 0, "errors": 0}
    threading.Thread(target=_enrich_worker, args=(pub_id, db_path), daemon=True).start()
    return True


def fetch_cover(issuecode: str, cover_url: str) -> tuple[Path, str] | None:
    """Return (path, mime) for a cached or freshly fetched GCD cover."""
    cached = cache.cache_path_for(issuecode)
    if cached:
        return cached, cache.mime_from_ext(cached)

    if not cover_url:
        return None

    with _client() as client:
        resp = client.get(cover_url)
    if resp.status_code != 200:
        return None
    ctype = resp.headers.get("content-type", "image/jpeg")
    path = cache.cache_write(issuecode, resp.content, ctype)
    return path, cache.mime_from_ext(path)


# ---- helpers --------------------------------------------------------------

_TRAILING_ID = re.compile(r"/(\d+)/?$")


def _id_from_url(url: str) -> str:
    m = _TRAILING_ID.search(url or "")
    return m.group(1) if m else ""


def _safe_int(v) -> int | None:
    try:
        return int(v) if v is not None and v != "" else None
    except (TypeError, ValueError):
        return None
