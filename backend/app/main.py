"""ComicVault — minimal FastAPI app.

Serves a JSON API for browsing the Inducks Disney comic catalogue,
managing a personal collection (location + condition per issue), and
proxying cover images (lazy + cached) from inducks.org.

The Vue 3 SPA lives at /frontend/index.html; this app mounts it at /.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import auth, gcd, inducks
from .config import DB_PATH, DEFAULT_YEAR_PAGINATION_THRESHOLD
from .db import get_conn, has_inducks_data, init_collection_tables

def _find_frontend() -> Path:
    here = Path(__file__).resolve().parent  # backend/app
    for c in (here.parent / "frontend", here.parent.parent / "frontend"):
        if (c / "index.html").exists():
            return c
    return here.parent / "frontend"

FRONTEND_DIR = _find_frontend()

app = FastAPI(title="ComicVault")

init_collection_tables()


# --------------------------------------------------------------- auth gate

# Endpoints that must be reachable without a session (login flow + status).
_PUBLIC_ENDPOINTS = {
    "/api/auth/status",
    "/api/auth/setup",
    "/api/auth/login",
}


def _client_ip(request: Request) -> str:
    """Real client IP (handles X-Forwarded-For from a reverse proxy)."""
    fwd = request.headers.get("x-forwarded-for", "")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


@app.middleware("http")
async def require_auth(request: Request, call_next):
    path = request.url.path
    # Static frontend (HTML/JS/CSS) is public — the SPA itself decides what
    # to render based on /api/auth/status. Public auth endpoints obviously
    # also have to be reachable.
    if not path.startswith("/api/") or path in _PUBLIC_ENDPOINTS:
        return await call_next(request)
    # First-run: no password configured yet → let everything through so the
    # operator can set one. Once a password is set this branch is closed.
    if not auth.is_password_set():
        return await call_next(request)
    token = request.cookies.get(auth.SESSION_COOKIE)
    if auth.verify_session(token):
        return await call_next(request)
    return JSONResponse({"detail": "Unauthorized"}, status_code=401)


class AuthIn(BaseModel):
    password: str


@app.get("/api/auth/status")
def auth_status(request: Request):
    token = request.cookies.get(auth.SESSION_COOKIE)
    return {
        "password_set":   auth.is_password_set(),
        "authenticated": auth.verify_session(token),
    }


@app.post("/api/auth/setup")
def auth_setup(body: AuthIn, response: Response):
    if auth.is_password_set():
        raise HTTPException(409, "Password already set")
    try:
        auth.set_password(body.password)
    except ValueError as e:
        raise HTTPException(400, str(e))
    token = auth.create_session()
    response.set_cookie(
        auth.SESSION_COOKIE, token,
        httponly=True, samesite="lax",
        max_age=int(auth.SESSION_TTL.total_seconds()),
        path="/",
    )
    return {"ok": True}


@app.post("/api/auth/login")
def auth_login(body: AuthIn, request: Request, response: Response):
    ip = _client_ip(request)
    locked = auth.lockout_seconds(ip)
    if locked > 0:
        raise HTTPException(429, f"Too many attempts. Try again in {locked} s.")
    if not auth.verify_password(body.password):
        auth.record_failure(ip)
        # Re-check lockout in case THIS attempt was the one that tripped it.
        locked = auth.lockout_seconds(ip)
        if locked > 0:
            raise HTTPException(429, f"Too many attempts. Locked for {locked} s.")
        raise HTTPException(401, "Incorrect password")
    auth.reset_attempts(ip)
    token = auth.create_session()
    response.set_cookie(
        auth.SESSION_COOKIE, token,
        httponly=True, samesite="lax",
        max_age=int(auth.SESSION_TTL.total_seconds()),
        path="/",
    )
    return {"ok": True}


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response):
    auth.delete_session(request.cookies.get(auth.SESSION_COOKIE))
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    return {"ok": True}


class ChangePasswordIn(BaseModel):
    current: str
    new: str


@app.post("/api/auth/change-password")
def auth_change_password(body: ChangePasswordIn):
    if not auth.verify_password(body.current):
        raise HTTPException(401, "Current password is wrong")
    try:
        auth.set_password(body.new)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "must_relogin": True}


# ----------------------------------------------------------------- setup/sync

@app.get("/api/setup")
def setup_status():
    return {
        "synced": has_inducks_data(),
        "logged_in": inducks.is_logged_in(),
        "sync": inducks.get_progress(),
    }


class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def login(body: LoginIn):
    try:
        inducks.login(body.username, body.password)
    except Exception as e:
        raise HTTPException(401, str(e))
    return {"ok": True}


@app.post("/api/logout")
def logout():
    inducks.logout()
    return {"ok": True}


@app.post("/api/sync/start")
def sync_start():
    if not inducks.start_import_async():
        raise HTTPException(409, "Sync already running")
    return {"ok": True}


@app.get("/api/sync/status")
def sync_status():
    return inducks.get_progress()


# --------------------------------------------------------------- browse

def _owned_pubcodes(conn) -> set[str]:
    """Set of publication codes (Inducks pubcodes + cp:<id> for customs) the
    user owns at least one issue of."""
    inducks_owned = conn.execute(
        """
        SELECT DISTINCT i.publicationcode
        FROM collection_item ci
        JOIN inducks_issue i ON i.issuecode = ci.issuecode
        """
    ).fetchall()
    custom_owned = conn.execute(
        """
        SELECT DISTINCT 'cp:' || cii.publication_id
        FROM collection_item ci
        JOIN custom_issue cii ON ('ci:' || cii.id) = ci.issuecode
        """
    ).fetchall()
    return {r[0] for r in inducks_owned} | {r[0] for r in custom_owned}


def _is_custom_pub(code: str) -> bool:
    return code.startswith("cp:")


def _is_custom_issue(code: str) -> bool:
    return code.startswith("ci:")


def _custom_pub_id(code: str) -> int:
    return int(code.split(":", 1)[1])


def _custom_issue_id(code: str) -> int:
    return int(code.split(":", 1)[1])


def _custom_publication_detail(pub_id: int):
    with get_conn() as conn:
        pub = conn.execute(
            """
            SELECT cp.*, c.countryname
            FROM custom_publication cp
            LEFT JOIN inducks_country c ON c.countrycode = cp.countrycode
            WHERE cp.id = ?
            """,
            (pub_id,),
        ).fetchone()
        if not pub:
            raise HTTPException(404, "Publication not found")

        issues = conn.execute(
            """
            SELECT id, issuenumber, title, oldestdate
            FROM custom_issue
            WHERE publication_id = ?
            ORDER BY position
            """,
            (pub_id,),
        ).fetchall()

        owned = conn.execute(
            """
            SELECT ci.id, ci.issuecode, ci.condition, ci.location_id, ci.notes,
                   l.name AS location_name
            FROM collection_item ci
            LEFT JOIN collection_location l ON l.id = ci.location_id
            JOIN custom_issue cii ON ('ci:' || cii.id) = ci.issuecode
            WHERE cii.publication_id = ?
            """,
            (pub_id,),
        ).fetchall()

    owned_map: dict[str, list] = {}
    for o in owned:
        owned_map.setdefault(o["issuecode"], []).append({
            "id": o["id"],
            "condition": o["condition"],
            "location_id": o["location_id"],
            "location_name": o["location_name"],
            "notes": o["notes"],
        })

    return {
        "publicationcode": f"cp:{pub_id}",
        "title": pub["title"],
        "countrycode": pub["countrycode"],
        "country_name": pub["countryname"],
        "languagecode": pub["languagecode"],
        "publisher": pub["publisher"],
        "year_began": pub["year_began"],
        "year_ended": pub["year_ended"],
        "source": pub["source"],
        "total_issues": len(issues),
        "years": [],            # no year-tab pagination for custom (yet)
        "selected_year": None,
        "issues": [
            {
                "issuecode": f"ci:{r['id']}",
                "issuenumber": r["issuenumber"],
                "title": r["title"],
                "oldestdate": r["oldestdate"],
                "owned": owned_map.get(f"ci:{r['id']}", []),
            }
            for r in issues
        ],
    }


def _custom_issue_detail(issue_id: int):
    with get_conn() as conn:
        # Lazy-fetch GCD details (title/date/cover) on first detail view too,
        # so the detail page shows real data even before the cover loads.
        row = _get_custom_issue(conn, issue_id)
        if not row:
            raise HTTPException(404, "Issue not found")
        pub = conn.execute(
            """
            SELECT cp.id, cp.title, cp.countrycode, c.countryname
            FROM custom_publication cp
            LEFT JOIN inducks_country c ON c.countrycode = cp.countrycode
            WHERE cp.id = (SELECT publication_id FROM custom_issue WHERE id = ?)
            """,
            (issue_id,),
        ).fetchone()

        owned = conn.execute(
            """
            SELECT ci.id, ci.condition, ci.location_id, ci.notes, l.name AS location_name
            FROM collection_item ci
            LEFT JOIN collection_location l ON l.id = ci.location_id
            WHERE ci.issuecode = ?
            """,
            (f"ci:{issue_id}",),
        ).fetchall()

    return {
        "issuecode": f"ci:{issue_id}",
        "publicationcode": f"cp:{pub['id']}" if pub else None,
        "publication_title": pub["title"] if pub else None,
        "countrycode": pub["countrycode"] if pub else None,
        "country_name": pub["countryname"] if pub else None,
        "issuenumber": row["issuenumber"],
        "title": row["title"],
        "oldestdate": row["oldestdate"],
        "stories": [],            # GCD has stories too but we don't import them
        "owned": [
            {
                "id": o["id"],
                "condition": o["condition"],
                "location_id": o["location_id"],
                "location_name": o["location_name"],
                "notes": o["notes"],
            }
            for o in owned
        ],
    }


# SQL fragment: every publication, Inducks or custom, with a uniform shape.
_ALL_PUBS_SQL = """(
    SELECT publicationcode AS code, title, countrycode, languagecode
    FROM inducks_publication
    UNION ALL
    SELECT 'cp:' || id AS code, title, countrycode, languagecode
    FROM custom_publication
)"""


@app.get("/api/countries")
def list_countries(filter: str = "all"):
    """filter: 'all' | 'owned' (countries with ≥1 owned publication) | 'missing'
    (countries with ≥1 unowned publication)."""
    with get_conn() as conn:
        if not has_inducks_data():
            return []

        owned = _owned_pubcodes(conn) if filter != "all" else None
        if filter == "owned" and not owned:
            return []

        if filter == "all":
            where, params = "", ()
        elif filter == "owned":
            ph = ",".join("?" * len(owned))
            where = f"WHERE p.code IN ({ph})"
            params = tuple(owned)
        else:  # missing
            if not owned:
                where, params = "", ()
            else:
                ph = ",".join("?" * len(owned))
                where = f"WHERE p.code NOT IN ({ph})"
                params = tuple(owned)

        rows = conn.execute(
            f"""
            SELECT c.countrycode, c.countryname, COUNT(DISTINCT p.code)
            FROM inducks_country c
            JOIN {_ALL_PUBS_SQL} p ON p.countrycode = c.countrycode
            {where}
            GROUP BY c.countrycode, c.countryname
            ORDER BY c.countryname
            """,
            params,
        ).fetchall()
        return [{"code": r[0], "name": r[1], "count": r[2]} for r in rows if r[2] > 0]


@app.get("/api/publications")
def list_publications(
    country: Optional[str] = None,
    q: Optional[str] = None,
    filter: str = "all",
    limit: int = Query(120, le=500),
    offset: int = 0,
):
    with get_conn() as conn:
        if not has_inducks_data():
            return {"total": 0, "items": []}

        clauses = []
        params: list = []
        if country:
            clauses.append("p.countrycode = ?")
            params.append(country)
        if q:
            clauses.append("p.title LIKE ?")
            params.append(f"%{q}%")
        if filter == "owned":
            owned = _owned_pubcodes(conn)
            if not owned:
                return {"total": 0, "items": []}
            clauses.append(f"p.code IN ({','.join('?' * len(owned))})")
            params.extend(owned)
        elif filter == "missing":
            owned = _owned_pubcodes(conn)
            if owned:
                clauses.append(f"p.code NOT IN ({','.join('?' * len(owned))})")
                params.extend(owned)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        total = conn.execute(
            f"SELECT COUNT(*) FROM {_ALL_PUBS_SQL} p {where}", params
        ).fetchone()[0]

        rows = conn.execute(
            f"""
            SELECT p.code, p.title, p.countrycode, p.languagecode
            FROM {_ALL_PUBS_SQL} p
            {where}
            ORDER BY p.title
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()

        # Owned counts in one query that handles both Inducks and custom items.
        owned_counts: dict[str, int] = {}
        if rows:
            pcodes = [r[0] for r in rows]
            ph = ",".join("?" * len(pcodes))
            for code, n in conn.execute(
                f"""
                SELECT code, COUNT(DISTINCT issuecode) FROM (
                    SELECT i.publicationcode AS code, ci.issuecode
                    FROM collection_item ci
                    JOIN inducks_issue i ON i.issuecode = ci.issuecode
                    WHERE i.publicationcode IN ({ph})
                    UNION ALL
                    SELECT 'cp:' || cii.publication_id AS code, ci.issuecode
                    FROM collection_item ci
                    JOIN custom_issue cii ON ('ci:' || cii.id) = ci.issuecode
                    WHERE 'cp:' || cii.publication_id IN ({ph})
                ) GROUP BY code
                """,
                (*pcodes, *pcodes),
            ).fetchall():
                owned_counts[code] = n

    return {
        "total": total,
        "items": [
            {
                "publicationcode": r[0],
                "title": r[1],
                "countrycode": r[2],
                "languagecode": r[3],
                "owned_count": owned_counts.get(r[0], 0),
            }
            for r in rows
        ],
    }


def _get_setting(conn, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM app_setting WHERE key = ?", (key,)).fetchone()
    return row[0] if row else default


def _set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO app_setting (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


@app.get("/api/publications/{publicationcode:path}")
def publication_detail(publicationcode: str, year: Optional[str] = None):
    if _is_custom_pub(publicationcode):
        return _custom_publication_detail(_custom_pub_id(publicationcode))

    with get_conn() as conn:
        pub = conn.execute(
            """
            SELECT p.publicationcode, p.title, p.countrycode, p.languagecode,
                   c.countryname
            FROM inducks_publication p
            LEFT JOIN inducks_country c ON c.countrycode = p.countrycode
            WHERE p.publicationcode = ?
            """,
            (publicationcode,),
        ).fetchone()
        if not pub:
            raise HTTPException(404, "Publication not found")

        total = conn.execute(
            "SELECT COUNT(*) FROM inducks_issue WHERE publicationcode = ?",
            (publicationcode,),
        ).fetchone()[0]

        # Year breakdown — substr(oldestdate, 1, 4) yields YYYY when present.
        year_rows = conn.execute(
            """
            SELECT substr(oldestdate, 1, 4) AS yr, COUNT(*) AS n
            FROM inducks_issue
            WHERE publicationcode = ?
              AND oldestdate IS NOT NULL
              AND length(oldestdate) >= 4
            GROUP BY yr
            HAVING yr != ''
            ORDER BY yr
            """,
            (publicationcode,),
        ).fetchall()
        years = [{"year": r[0], "count": r[1]} for r in year_rows]
        dated = sum(y["count"] for y in years)

        # Auto-paginate when the run is large *and* most issues carry a year.
        threshold = int(_get_setting(
            conn, "year_pagination_threshold",
            str(DEFAULT_YEAR_PAGINATION_THRESHOLD),
        ))
        paginate = total > threshold and dated >= total // 2

        if paginate:
            selected_year = year or (years[-1]["year"] if years else None)
        else:
            selected_year = year       # honour explicit ?year=YYYY anyway
            years = []                  # no tabs in the UI

        issue_params: list = [publicationcode]
        issue_where = "WHERE i.publicationcode = ?"
        if selected_year:
            issue_where += " AND substr(i.oldestdate, 1, 4) = ?"
            issue_params.append(selected_year)

        issues = conn.execute(
            f"""
            SELECT i.issuecode, i.issuenumber, i.title, i.oldestdate
            FROM inducks_issue i
            {issue_where}
            -- Group by year-prefix so a partial date like '1968' doesn't
            -- sort before '1968-05-28'. Within a year, prefer the issue
            -- number's numeric value ('2' < '10') and fall back to a string
            -- compare for non-numeric tags ('1A' / 'supplement' / etc.).
            ORDER BY substr(COALESCE(i.oldestdate, '9999'), 1, 4),
                     CAST(i.issuenumber AS INTEGER),
                     i.issuenumber
            """,
            issue_params,
        ).fetchall()

        # Owned items for the issues on this page only — keeps the
        # owned_map join cheap when paginating a huge run.
        owned = conn.execute(
            f"""
            SELECT ci.id, ci.issuecode, ci.condition, ci.location_id, ci.notes,
                   l.name AS location_name
            FROM collection_item ci
            LEFT JOIN collection_location l ON l.id = ci.location_id
            JOIN inducks_issue i ON i.issuecode = ci.issuecode
            {issue_where.replace('i.publicationcode', 'i.publicationcode')}
            """,
            issue_params,
        ).fetchall()

    owned_map: dict[str, list] = {}
    for o in owned:
        owned_map.setdefault(o["issuecode"], []).append({
            "id": o["id"],
            "condition": o["condition"],
            "location_id": o["location_id"],
            "location_name": o["location_name"],
            "notes": o["notes"],
        })

    return {
        "publicationcode": pub["publicationcode"],
        "title": pub["title"],
        "countrycode": pub["countrycode"],
        "country_name": pub["countryname"],
        "languagecode": pub["languagecode"],
        "total_issues": total,
        "years": years,
        "selected_year": selected_year,
        "issues": [
            {
                "issuecode": r["issuecode"],
                "issuenumber": r["issuenumber"],
                "title": r["title"],
                "oldestdate": r["oldestdate"],
                "owned": owned_map.get(r["issuecode"], []),
            }
            for r in issues
        ],
    }


@app.get("/api/issues/{issuecode:path}")
def issue_detail(issuecode: str):
    if _is_custom_issue(issuecode):
        return _custom_issue_detail(_custom_issue_id(issuecode))

    with get_conn() as conn:
        issue = conn.execute(
            "SELECT issuecode, publicationcode, issuenumber, title, oldestdate FROM inducks_issue WHERE issuecode = ?",
            (issuecode,),
        ).fetchone()
        if not issue:
            raise HTTPException(404, "Issue not found")

        pub = conn.execute(
            """
            SELECT p.title, p.countrycode, c.countryname
            FROM inducks_publication p
            LEFT JOIN inducks_country c ON c.countrycode = p.countrycode
            WHERE p.publicationcode = ?
            """,
            (issue["publicationcode"],),
        ).fetchone()

        stories = conn.execute(
            """
            SELECT e.position, e.title AS entry_title, s.title AS story_title
            FROM inducks_entry e
            LEFT JOIN inducks_storyversion sv ON sv.storyversioncode = e.storyversioncode
            LEFT JOIN inducks_story s         ON s.storycode         = sv.storycode
            WHERE e.issuecode = ?
            ORDER BY e.position
            """,
            (issuecode,),
        ).fetchall()

        owned = conn.execute(
            """
            SELECT ci.id, ci.condition, ci.location_id, ci.notes, l.name AS location_name
            FROM collection_item ci
            LEFT JOIN collection_location l ON l.id = ci.location_id
            WHERE ci.issuecode = ?
            """,
            (issuecode,),
        ).fetchall()

    return {
        "issuecode": issue["issuecode"],
        "publicationcode": issue["publicationcode"],
        "publication_title": pub["title"] if pub else None,
        "countrycode": pub["countrycode"] if pub else None,
        "country_name": pub["countryname"] if pub else None,
        "issuenumber": issue["issuenumber"],
        "title": issue["title"],
        "oldestdate": issue["oldestdate"],
        "stories": [
            {"position": s["position"], "title": s["entry_title"], "original_title": s["story_title"]}
            for s in stories
        ],
        "owned": [
            {
                "id": o["id"],
                "condition": o["condition"],
                "location_id": o["location_id"],
                "location_name": o["location_name"],
                "notes": o["notes"],
            }
            for o in owned
        ],
    }


# ----------------------------------------------------------------- search

@app.get("/api/search")
def search(q: str = Query(..., min_length=2), limit: int = 30):
    like = f"%{q}%"
    with get_conn() as conn:
        has_inducks = has_inducks_data()

        # Publications: combine Inducks + custom (cp:<id>) so GCD imports
        # show up alongside Inducks publications.
        if has_inducks:
            publications = conn.execute(
                """
                SELECT * FROM (
                    SELECT publicationcode, title, countrycode
                    FROM inducks_publication WHERE title LIKE ?
                    UNION ALL
                    SELECT 'cp:' || id AS publicationcode, title, countrycode
                    FROM custom_publication WHERE title LIKE ?
                )
                ORDER BY title LIMIT ?
                """,
                (like, like, limit),
            ).fetchall()
            issues = conn.execute(
                """
                SELECT * FROM (
                    SELECT i.issuecode, i.title, i.issuenumber, i.publicationcode,
                           p.title AS pub_title
                    FROM inducks_issue i
                    LEFT JOIN inducks_publication p ON p.publicationcode = i.publicationcode
                    WHERE i.title LIKE ?
                    UNION ALL
                    SELECT 'ci:' || cii.id AS issuecode, cii.title, cii.issuenumber,
                           'cp:' || cp.id AS publicationcode, cp.title AS pub_title
                    FROM custom_issue cii
                    JOIN custom_publication cp ON cp.id = cii.publication_id
                    WHERE cii.title LIKE ?
                )
                ORDER BY title LIMIT ?
                """,
                (like, like, limit),
            ).fetchall()
            stories = conn.execute(
                """
                SELECT s.storycode, s.title, e.issuecode, p.title AS pub_title
                FROM inducks_story s
                JOIN inducks_storyversion sv ON sv.storycode = s.storycode
                JOIN inducks_entry e         ON e.storyversioncode = sv.storyversioncode
                LEFT JOIN inducks_issue i        ON i.issuecode = e.issuecode
                LEFT JOIN inducks_publication p  ON p.publicationcode = i.publicationcode
                WHERE s.title LIKE ?
                GROUP BY s.storycode
                ORDER BY s.title LIMIT ?
                """,
                (like, limit),
            ).fetchall()
        else:
            # No Inducks tables yet — search custom only.
            publications = conn.execute(
                """
                SELECT 'cp:' || id AS publicationcode, title, countrycode
                FROM custom_publication WHERE title LIKE ?
                ORDER BY title LIMIT ?
                """,
                (like, limit),
            ).fetchall()
            issues = conn.execute(
                """
                SELECT 'ci:' || cii.id AS issuecode, cii.title, cii.issuenumber,
                       'cp:' || cp.id AS publicationcode, cp.title AS pub_title
                FROM custom_issue cii
                JOIN custom_publication cp ON cp.id = cii.publication_id
                WHERE cii.title LIKE ?
                ORDER BY cii.title LIMIT ?
                """,
                (like, limit),
            ).fetchall()
            stories = []

    return {
        "publications": [dict(r) for r in publications],
        "issues":       [dict(r) for r in issues],
        "stories":      [dict(r) for r in stories],
    }


# ----------------------------------------------------------------- collection

class CollectionIn(BaseModel):
    issuecode: str
    location_id: Optional[int] = None
    condition: Optional[str] = None
    notes: Optional[str] = None


class CollectionPatch(BaseModel):
    location_id: Optional[int] = None
    condition: Optional[str] = None
    notes: Optional[str] = None


@app.get("/api/collection")
def collection_list():
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT ci.id, ci.issuecode, ci.condition, ci.location_id, ci.notes,
                   l.name AS location_name,
                   i.publicationcode, i.issuenumber, i.title AS issue_title,
                   p.title AS publication_title
            FROM collection_item ci
            LEFT JOIN collection_location l ON l.id = ci.location_id
            LEFT JOIN inducks_issue i        ON i.issuecode = ci.issuecode
            LEFT JOIN inducks_publication p  ON p.publicationcode = i.publicationcode
            ORDER BY p.title, i.issuenumber
            """
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/collection")
def collection_add(body: CollectionIn):
    with get_conn() as conn:
        cur = conn.execute(
            "INSERT INTO collection_item (issuecode, location_id, condition, notes) VALUES (?,?,?,?)",
            (body.issuecode, body.location_id, body.condition, body.notes),
        )
        new_id = cur.lastrowid
        conn.commit()
    return {"id": new_id}


@app.patch("/api/collection/{item_id}")
def collection_update(item_id: int, body: CollectionPatch):
    sets, params = [], []
    if body.location_id is not None:
        sets.append("location_id = ?"); params.append(body.location_id)
    if body.condition is not None:
        sets.append("condition = ?"); params.append(body.condition)
    if body.notes is not None:
        sets.append("notes = ?"); params.append(body.notes)
    if not sets:
        return {"ok": True}
    params.append(item_id)
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE collection_item SET {', '.join(sets)} WHERE id = ?", params
        )
        if cur.rowcount == 0:
            raise HTTPException(404, "Item not found")
        conn.commit()
    return {"ok": True}


@app.delete("/api/collection/{item_id}")
def collection_delete(item_id: int):
    with get_conn() as conn:
        cur = conn.execute("DELETE FROM collection_item WHERE id = ?", (item_id,))
        if cur.rowcount == 0:
            raise HTTPException(404, "Item not found")
        conn.commit()
    return {"ok": True}


# ----------------------------------------------------------------- locations

class LocationIn(BaseModel):
    name: str


@app.get("/api/locations")
def locations_list():
    with get_conn() as conn:
        rows = conn.execute("SELECT id, name FROM collection_location ORDER BY name").fetchall()
    return [dict(r) for r in rows]


@app.post("/api/locations")
def locations_add(body: LocationIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    with get_conn() as conn:
        try:
            cur = conn.execute("INSERT INTO collection_location (name) VALUES (?)", (name,))
            conn.commit()
            return {"id": cur.lastrowid, "name": name}
        except Exception:
            raise HTTPException(409, "Location already exists")


@app.delete("/api/locations/{loc_id}")
def locations_delete(loc_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM collection_location WHERE id = ?", (loc_id,))
        conn.commit()
    return {"ok": True}


class ConditionIn(BaseModel):
    name: str


# ---------- app-wide settings (key/value) ----------

class AppSettingsPatch(BaseModel):
    year_pagination_threshold: Optional[int] = None


@app.get("/api/app-settings")
def app_settings_get():
    with get_conn() as conn:
        threshold = _get_setting(
            conn, "year_pagination_threshold",
            str(DEFAULT_YEAR_PAGINATION_THRESHOLD),
        )
    return {"year_pagination_threshold": int(threshold)}


@app.patch("/api/app-settings")
def app_settings_patch(body: AppSettingsPatch):
    with get_conn() as conn:
        if body.year_pagination_threshold is not None:
            v = max(1, int(body.year_pagination_threshold))
            _set_setting(conn, "year_pagination_threshold", str(v))
    return {"ok": True}


@app.get("/api/conditions")
def conditions_list():
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, name FROM collection_condition ORDER BY position, name"
        ).fetchall()
    return [dict(r) for r in rows]


@app.post("/api/conditions")
def conditions_add(body: ConditionIn):
    name = body.name.strip()
    if not name:
        raise HTTPException(400, "Name required")
    with get_conn() as conn:
        max_pos = conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM collection_condition"
        ).fetchone()[0]
        try:
            cur = conn.execute(
                "INSERT INTO collection_condition (name, position) VALUES (?, ?)",
                (name, max_pos),
            )
            return {"id": cur.lastrowid, "name": name}
        except Exception:
            raise HTTPException(409, "Condition already exists")


@app.delete("/api/conditions/{cond_id}")
def conditions_delete(cond_id: int):
    with get_conn() as conn:
        conn.execute("DELETE FROM collection_condition WHERE id = ?", (cond_id,))
    return {"ok": True}


# --------------------------------------------------- GCD / custom publications

class GcdImportIn(BaseModel):
    series_id: str


class GcdLoginIn(BaseModel):
    username: str
    password: str


@app.get("/api/gcd/account")
def gcd_account():
    return {"logged_in": gcd.is_logged_in()}


@app.post("/api/gcd/login")
def gcd_login(body: GcdLoginIn):
    try:
        gcd.login(body.username, body.password)
    except Exception as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.post("/api/gcd/logout")
def gcd_logout():
    gcd.logout()
    return {"ok": True}


@app.get("/api/gcd/search")
def gcd_search(q: str = Query(..., min_length=2)):
    try:
        return gcd.search_series(q)
    except Exception as e:
        raise HTTPException(502, f"GCD search failed: {e}")


@app.get("/api/custom-publications")
def custom_publications_list():
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT cp.id, cp.source, cp.source_id, cp.title, cp.countrycode,
                   cp.languagecode, cp.publisher, cp.year_began, cp.year_ended,
                   COUNT(cii.id) AS issue_count,
                   SUM(CASE WHEN cii.cover_url IS NOT NULL AND cii.cover_url != ''
                            THEN 1 ELSE 0 END) AS enriched_count,
                   SUM(CASE WHEN cii.cover_url = ''
                            THEN 1 ELSE 0 END) AS no_cover_count,
                   SUM(CASE WHEN cii.cover_url IS NULL
                            THEN 1 ELSE 0 END) AS pending_count
            FROM custom_publication cp
            LEFT JOIN custom_issue cii ON cii.publication_id = cp.id
            GROUP BY cp.id
            ORDER BY cp.title
            """
        ).fetchall()
    return [
        dict(r) | {
            "publicationcode": f"cp:{r['id']}",
            "enrich_status": gcd.get_enrich_status(r["id"]),
        }
        for r in rows
    ]


@app.post("/api/custom-publications/import-gcd")
def custom_publications_import_gcd(body: GcdImportIn):
    try:
        series = gcd.fetch_series(body.series_id)
    except Exception as e:
        raise HTTPException(502, f"GCD fetch failed: {e}")

    with get_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM custom_publication WHERE source = 'gcd' AND source_id = ?",
            (series["source_id"],),
        ).fetchone()
        if existing:
            raise HTTPException(409, "This series is already imported")

        cur = conn.execute(
            """
            INSERT INTO custom_publication
                (source, source_id, title, countrycode, languagecode,
                 publisher, year_began, year_ended)
            VALUES ('gcd', ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                series["source_id"], series["title"],
                series["countrycode"], series["languagecode"],
                series["publisher"], series["year_began"], series["year_ended"],
            ),
        )
        pub_id = cur.lastrowid

        conn.executemany(
            """
            INSERT INTO custom_issue
                (publication_id, source_id, issuenumber, position)
            VALUES (?, ?, ?, ?)
            """,
            [
                (pub_id, i.get("source_id"), i.get("issuenumber", ""), i["position"])
                for i in series["issues"]
            ],
        )

    # Kick off the per-issue enrichment job — populates cover_url / title /
    # date for every issue in the background, throttled to GCD's rate limit.
    gcd.start_enrichment(pub_id, str(DB_PATH))
    return {"id": pub_id, "publicationcode": f"cp:{pub_id}", "issue_count": len(series["issues"])}


@app.post("/api/custom-publications/{pub_id}/enrich")
def custom_publications_enrich(pub_id: int):
    with get_conn() as conn:
        if not conn.execute(
            "SELECT 1 FROM custom_publication WHERE id = ?", (pub_id,)
        ).fetchone():
            raise HTTPException(404, "Publication not found")
    if not gcd.start_enrichment(pub_id, str(DB_PATH)):
        raise HTTPException(409, "Enrichment already running")
    return {"ok": True}


@app.get("/api/custom-publications/{pub_id}/enrich")
def custom_publications_enrich_status(pub_id: int):
    return gcd.get_enrich_status(pub_id)


@app.delete("/api/custom-publications/{pub_id}")
def custom_publications_delete(pub_id: int):
    with get_conn() as conn:
        # Drop any owned copies whose issuecode points at this publication's
        # custom issues, then drop the publication (cascade deletes the issues).
        conn.execute(
            """
            DELETE FROM collection_item
            WHERE issuecode IN (
                SELECT 'ci:' || id FROM custom_issue WHERE publication_id = ?
            )
            """,
            (pub_id,),
        )
        conn.execute("DELETE FROM custom_publication WHERE id = ?", (pub_id,))
    return {"ok": True}


# ----------------------------------------------------------------- covers

# Once a cover is fetched its bytes never change (filename is md5(issuecode)),
# so let the browser keep them for a year.
_COVER_CACHE_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}

# Returned in place of a 404 / 502 / auth error so the browser draws something
# instead of a broken-image icon. Short cache (60 s) means transient failures
# (e.g. GCD 429, brief network hiccups) recover on the next page load. Never
# written to the on-disk cover cache, so a real cover that *does* eventually
# show up is fetched and stored normally.
_PLACEHOLDER_SVG = (
    b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 320" '
    b'preserveAspectRatio="xMidYMid meet">'
    b'<rect width="240" height="320" fill="#f8fafc" stroke="#cbd5e1" '
    b'stroke-width="2" stroke-dasharray="6,4"/>'
    b'<g transform="translate(120 130)" fill="none" stroke="#94a3b8" '
    b'stroke-width="3" stroke-linecap="round" stroke-linejoin="round">'
    b'<rect x="-34" y="-44" width="68" height="84" rx="4"/>'
    b'<circle cx="-12" cy="-24" r="6" fill="#94a3b8"/>'
    b'<path d="M-32 32 L-10 4 L6 22 L20 -6 L34 32 Z" fill="#94a3b8"/>'
    b'</g>'
    b'<text x="120" y="220" font-family="sans-serif" font-size="14" '
    b'fill="#64748b" text-anchor="middle">Cover not available</text>'
    b'</svg>'
)
_PLACEHOLDER_HEADERS = {"Cache-Control": "public, max-age=60"}


def _placeholder() -> Response:
    return Response(
        content=_PLACEHOLDER_SVG,
        media_type="image/svg+xml",
        headers=_PLACEHOLDER_HEADERS,
    )


def _get_custom_issue(conn, issue_id: int) -> dict | None:
    """Return the stored custom_issue row as a dict, or None. Never hits the
    network — populating cover_url / title / date from GCD is the background
    enrichment job's responsibility, so request handlers stay snappy even
    while GCD is rate-limiting us."""
    row = conn.execute(
        "SELECT id, source_id, issuenumber, title, oldestdate, cover_url "
        "FROM custom_issue WHERE id = ?",
        (issue_id,),
    ).fetchone()
    return dict(row) if row else None


@app.get("/api/covers/issue/{issuecode:path}")
def cover_issue(issuecode: str):
    """Always returns an image (real or placeholder). Failures fall through
    to the placeholder so the browser shows *something* instead of a broken
    icon. The placeholder is never written to disk."""
    try:
        if _is_custom_issue(issuecode):
            with get_conn() as conn:
                row = _get_custom_issue(conn, _custom_issue_id(issuecode))
            if not row:
                return _placeholder()
            res = gcd.fetch_cover(issuecode, row["cover_url"] or "")
        else:
            with get_conn() as conn:
                res = inducks.fetch_cover_for_issue(conn, issuecode)
    except Exception:
        return _placeholder()

    if not res:
        return _placeholder()
    path, mime = res
    return FileResponse(path, media_type=mime, headers=_COVER_CACHE_HEADERS)


@app.get("/api/covers/publication/{publicationcode:path}")
def cover_publication(publicationcode: str):
    try:
        if _is_custom_pub(publicationcode):
            with get_conn() as conn:
                row = conn.execute(
                    "SELECT id FROM custom_issue WHERE publication_id = ? "
                    "ORDER BY position LIMIT 1",
                    (_custom_pub_id(publicationcode),),
                ).fetchone()
            if not row:
                return _placeholder()
            return cover_issue(f"ci:{row['id']}")

        with get_conn() as conn:
            res = inducks.fetch_icon_for_publication(conn, publicationcode)
    except Exception:
        return _placeholder()

    if not res:
        return _placeholder()
    path, mime = res
    return FileResponse(path, media_type=mime, headers=_COVER_CACHE_HEADERS)


# ----------------------------------------------------------------- frontend

@app.exception_handler(404)
async def spa_fallback(request, exc):
    # API 404s stay as JSON; everything else falls back to the SPA shell.
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    index = FRONTEND_DIR / "index.html"
    if index.exists():
        return FileResponse(index)
    return JSONResponse({"detail": "Not found"}, status_code=404)


if FRONTEND_DIR.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="spa")
