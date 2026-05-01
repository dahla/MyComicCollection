"""ComicVault — minimal FastAPI app.

Serves a JSON API for browsing the Inducks Disney comic catalogue,
managing a personal collection (location + condition per issue), and
proxying cover images (lazy + cached) from inducks.org.

The Vue 3 SPA lives at /frontend/index.html; this app mounts it at /.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import inducks
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
    rows = conn.execute(
        """
        SELECT DISTINCT i.publicationcode
        FROM collection_item ci
        JOIN inducks_issue i ON i.issuecode = ci.issuecode
        """
    ).fetchall()
    return {r[0] for r in rows}


@app.get("/api/countries")
def list_countries(filter: str = "all"):
    """filter: 'all' | 'owned' (countries with ≥1 owned publication) | 'missing'
    (countries with ≥1 unowned publication)."""
    with get_conn() as conn:
        if not has_inducks_data():
            return []

        if filter == "all":
            rows = conn.execute(
                """
                SELECT c.countrycode, c.countryname, COUNT(p.publicationcode)
                FROM inducks_country c
                JOIN inducks_publication p ON p.countrycode = c.countrycode
                GROUP BY c.countrycode, c.countryname
                ORDER BY c.countryname
                """
            ).fetchall()
        else:
            owned = _owned_pubcodes(conn)
            if filter == "owned":
                if not owned:
                    return []
                ph = ",".join("?" * len(owned))
                rows = conn.execute(
                    f"""
                    SELECT c.countrycode, c.countryname, COUNT(DISTINCT p.publicationcode)
                    FROM inducks_country c
                    JOIN inducks_publication p ON p.countrycode = c.countrycode
                    WHERE p.publicationcode IN ({ph})
                    GROUP BY c.countrycode, c.countryname
                    ORDER BY c.countryname
                    """,
                    tuple(owned),
                ).fetchall()
            else:  # missing
                if not owned:
                    rows = conn.execute(
                        """
                        SELECT c.countrycode, c.countryname, COUNT(p.publicationcode)
                        FROM inducks_country c
                        JOIN inducks_publication p ON p.countrycode = c.countrycode
                        GROUP BY c.countrycode, c.countryname
                        ORDER BY c.countryname
                        """
                    ).fetchall()
                else:
                    ph = ",".join("?" * len(owned))
                    rows = conn.execute(
                        f"""
                        SELECT c.countrycode, c.countryname, COUNT(DISTINCT p.publicationcode)
                        FROM inducks_country c
                        JOIN inducks_publication p ON p.countrycode = c.countrycode
                        WHERE p.publicationcode NOT IN ({ph})
                        GROUP BY c.countrycode, c.countryname
                        ORDER BY c.countryname
                        """,
                        tuple(owned),
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
            clauses.append(f"p.publicationcode IN ({','.join('?' * len(owned))})")
            params.extend(owned)
        elif filter == "missing":
            owned = _owned_pubcodes(conn)
            if owned:
                clauses.append(f"p.publicationcode NOT IN ({','.join('?' * len(owned))})")
                params.extend(owned)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""

        total = conn.execute(
            f"SELECT COUNT(*) FROM inducks_publication p {where}", params
        ).fetchone()[0]

        rows = conn.execute(
            f"""
            SELECT p.publicationcode, p.title, p.countrycode, p.languagecode
            FROM inducks_publication p
            {where}
            ORDER BY p.title
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()

        # Owned counts in one shot
        owned_counts: dict[str, int] = {}
        if rows:
            pcodes = [r[0] for r in rows]
            ph = ",".join("?" * len(pcodes))
            for pc, n in conn.execute(
                f"""
                SELECT i.publicationcode, COUNT(DISTINCT ci.issuecode)
                FROM collection_item ci
                JOIN inducks_issue i ON i.issuecode = ci.issuecode
                WHERE i.publicationcode IN ({ph})
                GROUP BY i.publicationcode
                """,
                pcodes,
            ).fetchall():
                owned_counts[pc] = n

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


@app.get("/api/publications/{publicationcode:path}")
def publication_detail(publicationcode: str):
    with get_conn() as conn:
        pub = conn.execute(
            "SELECT publicationcode, title, countrycode, languagecode FROM inducks_publication WHERE publicationcode = ?",
            (publicationcode,),
        ).fetchone()
        if not pub:
            raise HTTPException(404, "Publication not found")

        country = conn.execute(
            "SELECT countryname FROM inducks_country WHERE countrycode = ?",
            (pub["countrycode"],),
        ).fetchone()

        issues = conn.execute(
            """
            SELECT i.issuecode, i.issuenumber, i.title, i.oldestdate
            FROM inducks_issue i
            WHERE i.publicationcode = ?
            ORDER BY i.oldestdate, i.issuenumber
            """,
            (publicationcode,),
        ).fetchall()

        owned = conn.execute(
            """
            SELECT ci.id, ci.issuecode, ci.condition, ci.location_id, ci.notes,
                   l.name AS location_name
            FROM collection_item ci
            LEFT JOIN collection_location l ON l.id = ci.location_id
            JOIN inducks_issue i ON i.issuecode = ci.issuecode
            WHERE i.publicationcode = ?
            """,
            (publicationcode,),
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
        "country_name": country["countryname"] if country else None,
        "languagecode": pub["languagecode"],
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
        if not has_inducks_data():
            return {"publications": [], "issues": [], "stories": []}

        publications = conn.execute(
            """
            SELECT publicationcode, title, countrycode
            FROM inducks_publication
            WHERE title LIKE ?
            ORDER BY title LIMIT ?
            """,
            (like, limit),
        ).fetchall()

        issues = conn.execute(
            """
            SELECT i.issuecode, i.title, i.issuenumber, i.publicationcode, p.title AS pub_title
            FROM inducks_issue i
            LEFT JOIN inducks_publication p ON p.publicationcode = i.publicationcode
            WHERE i.title LIKE ?
            ORDER BY i.title LIMIT ?
            """,
            (like, limit),
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


# ----------------------------------------------------------------- covers

# Once a cover is fetched its bytes never change (filename is md5(issuecode)),
# so let the browser keep them for a year.
_COVER_CACHE_HEADERS = {"Cache-Control": "public, max-age=31536000, immutable"}


@app.get("/api/covers/issue/{issuecode:path}")
def cover_issue(issuecode: str):
    try:
        with get_conn() as conn:
            res = inducks.fetch_cover_for_issue(conn, issuecode)
    except PermissionError as e:
        raise HTTPException(401, str(e))
    except Exception as e:
        raise HTTPException(502, str(e))
    if not res:
        raise HTTPException(404, "No cover registered for this issue")
    path, mime = res
    return FileResponse(path, media_type=mime, headers=_COVER_CACHE_HEADERS)


@app.get("/api/covers/publication/{publicationcode:path}")
def cover_publication(publicationcode: str):
    try:
        with get_conn() as conn:
            res = inducks.fetch_icon_for_publication(conn, publicationcode)
    except PermissionError as e:
        raise HTTPException(401, str(e))
    except Exception as e:
        raise HTTPException(502, str(e))
    if not res:
        raise HTTPException(404, "No cover available for this publication")
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
