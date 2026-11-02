"""Named users, sessions and the audit log (all in the master database).

- Passwords: PBKDF2-HMAC-SHA256, 240k iterations, per-user salt (`pbkdf2$<iter>$<salt>$<hash>`).
- Sessions: a random 256-bit token; only its SHA-256 is stored. Browsers get it in an HttpOnly, SameSite=Strict
  cookie (`ro_session`); scripts send `Authorization: Bearer <token>`. Tokens never travel in URLs.
- Roles: admin (setup + users + everything) > manager > server, kitchen.
- Lockout: LOGIN_MAX_FAILURES failed logins per username within LOGIN_LOCK_MIN minutes -> 429.
- First run: when there are no users, an admin is created from ADMIN_USERNAME / ADMIN_PASSWORD (or a random password
  written to data/FIRST_RUN_ADMIN.txt, mode 600), plus demo users when DEMO_USERS is on.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from restaurant_ops.config import settings

ROLES = ("admin", "manager", "server", "kitchen")
RANK = {"admin": 3, "manager": 2, "server": 1, "kitchen": 1}
ITERATIONS = 240_000
_failures: dict[str, list[float]] = {}


class AuthError(ValueError):
    pass


def _now() -> datetime:
    return datetime.now(timezone.utc)


def hash_password(pw: str, salt: str | None = None, iterations: int = ITERATIONS) -> str:
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), bytes.fromhex(salt), iterations).hex()
    return f"pbkdf2${iterations}${salt}${h}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        _, it, salt, h = stored.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(pw, salt, int(it)).split("$")[3], h)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def public_user(r) -> dict:
    return {"id": r["id"], "username": r["username"], "display_name": r["display_name"], "role": r["role"], "active": bool(r["active"])}


def create_user(conn: sqlite3.Connection, username: str, password: str, role: str, display_name: str = "") -> dict:
    username = username.strip().lower()
    if not (3 <= len(username) <= 32) or not username.replace("_", "").replace(".", "").isalnum():
        raise AuthError("username: 3-32 letters, digits, dot or underscore")
    if role not in ROLES:
        raise AuthError(f"role must be one of {ROLES}")
    if len(password) < 8:
        raise AuthError("password must be at least 8 characters")
    if conn.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
        raise AuthError("username taken")
    uid = conn.execute("INSERT INTO users (username, display_name, role, pw_hash, active, created) VALUES (?,?,?,?,1,?)",
                       (username, display_name or username, role, hash_password(password), _now().isoformat())).lastrowid
    conn.commit()
    return public_user(conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone())


def update_user(conn: sqlite3.Connection, user_id: int, role: str | None = None, active: bool | None = None,
                password: str | None = None, display_name: str | None = None) -> dict:
    """Validates everything first, then writes, so a rejected change leaves nothing half-applied. The last active
    admin can neither be demoted nor deactivated (nobody could reach Setup any more)."""
    r = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    if r is None:
        raise AuthError("unknown user")
    if role is not None and role not in ROLES:
        raise AuthError(f"role must be one of {ROLES}")
    if password is not None and len(password) < 8:
        raise AuthError("password must be at least 8 characters")
    loses_admin = r["role"] == "admin" and r["active"] and ((role is not None and role != "admin") or active is False)
    if loses_admin and conn.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND active=1").fetchone()[0] <= 1:
        raise AuthError("can't demote or deactivate the last admin")
    if role is not None:
        conn.execute("UPDATE users SET role=? WHERE id=?", (role, user_id))
    if active is not None:
        conn.execute("UPDATE users SET active=? WHERE id=?", (int(active), user_id))
    if password is not None:
        conn.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_password(password), user_id))
    if active is False or password is not None or (role is not None and role != r["role"]):
        conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))   # sign out everywhere
    if display_name is not None:
        conn.execute("UPDATE users SET display_name=? WHERE id=?", (display_name, user_id))
    conn.commit()
    return public_user(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())


def list_users(conn: sqlite3.Connection) -> list[dict]:
    return [public_user(r) for r in conn.execute("SELECT * FROM users ORDER BY role, username")]


def login(conn: sqlite3.Connection, username: str, password: str) -> tuple[str, dict]:
    username = (username or "").strip().lower()
    now = time.time()
    recent = [t for t in _failures.get(username, []) if now - t < settings.LOGIN_LOCK_MIN * 60]
    if len(recent) >= settings.LOGIN_MAX_FAILURES:
        raise AuthError("locked")
    r = conn.execute("SELECT * FROM users WHERE username=? AND active=1", (username,)).fetchone()
    if r is None or not verify_password(password or "", r["pw_hash"]):
        _failures[username] = recent + [now]
        raise AuthError("invalid username or password")
    _failures.pop(username, None)
    token = secrets.token_urlsafe(32)
    exp = _now() + timedelta(hours=settings.SESSION_HOURS)
    conn.execute("INSERT INTO sessions (token_hash, user_id, created, expires) VALUES (?,?,?,?)",
                 (_token_hash(token), r["id"], _now().isoformat(), exp.isoformat()))
    conn.execute("DELETE FROM sessions WHERE expires < ?", (_now().isoformat(),))
    conn.commit()
    return token, public_user(r)


def resolve(conn: sqlite3.Connection, token: str | None) -> dict | None:
    if not token:
        return None
    r = conn.execute("SELECT u.*, s.expires FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND u.active=1",
                     (_token_hash(token),)).fetchone()
    if r is None or r["expires"] < _now().isoformat():
        return None
    return public_user(r)


def logout(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash=?", (_token_hash(token),))
        conn.commit()


def allowed(role: str | None, needed: tuple[str, ...]) -> bool:
    """`needed` lists the roles that may act besides admin/manager; an empty tuple means manager or admin only;
    ('any',) means any signed-in user; ('admin',) means admin only."""
    if role is None:
        return False
    if needed == ("admin",):
        return role == "admin"
    if role in ("admin", "manager"):
        return True
    return "any" in needed or role in needed


def bootstrap(conn: sqlite3.Connection) -> dict:
    """First run: create the admin (and demo users). Returns what was created (never the admin password)."""
    if conn.execute("SELECT 1 FROM users").fetchone():
        return {"created": []}
    created = []
    pw = settings.ADMIN_PASSWORD
    note = None
    if not pw:
        pw = secrets.token_urlsafe(12)
        path = settings.DATA_DIR / "FIRST_RUN_ADMIN.txt"
        path.write_text(f"username: {settings.ADMIN_USERNAME}\npassword: {pw}\n(change it in Setup > Users, then delete this file)\n")
        os.chmod(path, 0o600)
        note = str(path)
    create_user(conn, settings.ADMIN_USERNAME, pw, "admin", "Administrator")
    created.append(settings.ADMIN_USERNAME)
    if settings.DEMO_USERS:
        for role in ("manager", "server", "kitchen"):
            create_user(conn, role, f"{role}-demo", role, role.title())
            created.append(role)
    return {"created": created, "admin_password_file": note}


# =========================================================================== audit log


SENSITIVE = {"password", "token", "new_password"}


def audit(conn: sqlite3.Connection, user: dict | None, method: str, path: str, status: int, detail: dict | None = None) -> None:
    clean = {k: ("***" if k in SENSITIVE else v) for k, v in (detail or {}).items()}
    conn.execute("INSERT INTO audit_log (ts, username, role, method, path, status, detail) VALUES (?,?,?,?,?,?,?)",
                 (_now().isoformat(timespec="seconds"), (user or {}).get("username"), (user or {}).get("role"), method, path, status,
                  json.dumps(clean, default=str)[:2000]))
    conn.commit()


def audit_rows(conn: sqlite3.Connection, limit: int = 200, username: str | None = None) -> list[dict]:
    q, args = "SELECT * FROM audit_log", []
    if username:
        q += " WHERE username=?"
        args.append(username)
    return [dict(r) for r in conn.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit))]
