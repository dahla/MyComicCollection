"""Single-password gate for the whole app.

- Password is hashed with stdlib scrypt (no extra dependency, ~50 ms/check
  on a Pi — slow enough to make brute-force expensive).
- Sessions are random tokens stored in SQLite, so they survive container
  restarts. The cookie is HttpOnly + SameSite=Lax.
- Brute-force protection: after MAX_FAILS failed attempts from the same
  IP, lock that IP for LOCKOUT minutes. Counter is per-IP and persisted in
  SQLite so it survives a restart too.
"""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from .db import get_conn

SESSION_COOKIE = "comicvault_session"
SESSION_TTL    = timedelta(days=30)
MAX_FAILS      = 5
LOCKOUT        = timedelta(minutes=15)

# scrypt parameters — n=2**14 r=8 p=1 is "interactive auth" strength,
# RFC-7914 baseline; ~50 ms on ARMv8.
_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _DK_LEN = 2**14, 8, 1, 64


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _now().isoformat()


# ---- password hashing ------------------------------------------------------

def _hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                       n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DK_LEN)
    return f"scrypt$1${salt.hex()}${h.hex()}"


def _verify_hash(password: str, stored: str) -> bool:
    try:
        scheme, _ver, salt_hex, hash_hex = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
        actual = hashlib.scrypt(password.encode("utf-8"), salt=salt,
                                n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DK_LEN)
        return secrets.compare_digest(actual, expected)
    except Exception:
        return False


def is_password_set() -> bool:
    with get_conn() as c:
        r = c.execute("SELECT value FROM app_setting WHERE key = 'password_hash'").fetchone()
    return bool(r and r[0])


def set_password(password: str) -> None:
    if not password or len(password) < 4:
        raise ValueError("Password must be at least 4 characters")
    h = _hash(password)
    with get_conn() as c:
        c.execute(
            "INSERT INTO app_setting (key, value) VALUES ('password_hash', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (h,),
        )
        # Forcing a re-login on every existing session whenever the password
        # changes is the right call here — protects against the case where
        # someone sets a password to lock out an active session attacker.
        c.execute("DELETE FROM auth_session")


def verify_password(password: str) -> bool:
    with get_conn() as c:
        r = c.execute("SELECT value FROM app_setting WHERE key = 'password_hash'").fetchone()
    return bool(r and r[0]) and _verify_hash(password, r[0])


# ---- sessions --------------------------------------------------------------

def create_session() -> str:
    token = secrets.token_urlsafe(32)
    with get_conn() as c:
        c.execute("INSERT INTO auth_session (token) VALUES (?)", (token,))
    return token


def verify_session(token: str | None) -> bool:
    if not token:
        return False
    with get_conn() as c:
        r = c.execute(
            "SELECT last_used_at FROM auth_session WHERE token = ?",
            (token,),
        ).fetchone()
        if not r:
            return False
        try:
            last = datetime.fromisoformat(r[0])
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            last = _now()
        if _now() - last > SESSION_TTL:
            c.execute("DELETE FROM auth_session WHERE token = ?", (token,))
            return False
        c.execute(
            "UPDATE auth_session SET last_used_at = ? WHERE token = ?",
            (_now_iso(), token),
        )
    return True


def delete_session(token: str | None) -> None:
    if not token:
        return
    with get_conn() as c:
        c.execute("DELETE FROM auth_session WHERE token = ?", (token,))


# ---- per-IP brute-force lockout -------------------------------------------

def lockout_seconds(ip: str) -> int:
    """Seconds remaining until this IP can try logging in again, or 0."""
    with get_conn() as c:
        r = c.execute("SELECT locked_until FROM auth_attempts WHERE ip = ?", (ip,)).fetchone()
    if not r or not r[0]:
        return 0
    try:
        until = datetime.fromisoformat(r[0])
        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return 0
    return max(0, int((until - _now()).total_seconds()))


def record_failure(ip: str) -> None:
    with get_conn() as c:
        r = c.execute("SELECT fail_count FROM auth_attempts WHERE ip = ?", (ip,)).fetchone()
        count = (r[0] if r else 0) + 1
        if count >= MAX_FAILS:
            c.execute(
                "INSERT INTO auth_attempts (ip, fail_count, locked_until) VALUES (?, 0, ?) "
                "ON CONFLICT(ip) DO UPDATE SET fail_count = 0, locked_until = excluded.locked_until",
                (ip, (_now() + LOCKOUT).isoformat()),
            )
        else:
            c.execute(
                "INSERT INTO auth_attempts (ip, fail_count, locked_until) VALUES (?, ?, NULL) "
                "ON CONFLICT(ip) DO UPDATE SET fail_count = excluded.fail_count, locked_until = NULL",
                (ip, count),
            )


def reset_attempts(ip: str) -> None:
    with get_conn() as c:
        c.execute("DELETE FROM auth_attempts WHERE ip = ?", (ip,))
