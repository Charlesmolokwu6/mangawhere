import hashlib
import hmac
import re
import secrets
import time
from typing import Any, Dict, Optional

from . import db

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
SESSION_TTL = 60 * 60 * 24 * 30  # 30 days
PBKDF2_ITERATIONS = 200_000
GENERIC_ERROR = "Something went wrong. Try again."


def _hash_password(password: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), PBKDF2_ITERATIONS
    ).hex()


def _user_row_to_dict(row) -> Dict[str, Any]:
    return {
        "id": row["id"],
        "email": row["email"],
        "name": row["name"],
        "avatar_url": row["avatar_url"],
    }


def register(
    payload: Dict[str, Any],
    verify_captcha,
    skip_captcha: bool = False,
    turnstile_ok: bool = True,
) -> Dict[str, Any]:
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""
    website = payload.get("website") or ""
    elapsed = payload.get("elapsed")
    captcha_id = payload.get("captcha_id")
    captcha_answer = payload.get("captcha") or ""

    if not EMAIL_RE.match(email):
        return {"error": "Enter a valid email address."}
    if len(password) < 8:
        return {"error": "Password must be at least 8 characters."}

    # Honeypot filled in, or the form submitted implausibly fast: treat both
    # as bot traffic without saying which check tripped it.
    if website.strip() or (isinstance(elapsed, (int, float)) and elapsed < 1.5):
        return {"error": GENERIC_ERROR, "captcha_failed": True}

    # skip_captcha is set once Cloudflare Turnstile is configured — its
    # result (verified against Cloudflare's API before this function is
    # ever called, since that's an async HTTP call this sync function can't
    # make itself) replaces the custom captcha rather than stacking with it.
    if skip_captcha:
        if not turnstile_ok:
            return {"error": "That didn't match. Try again.", "captcha_failed": True}
    elif not verify_captcha(captcha_id, captcha_answer):
        return {"error": "That didn't match. Try again.", "captcha_failed": True}

    conn = db.get_connection()
    try:
        existing = conn.execute(
            "SELECT id FROM users WHERE email = ?", (email,)
        ).fetchone()
        if existing:
            return {"error": "An account with that email already exists."}

        salt = secrets.token_hex(16)
        password_hash = _hash_password(password, salt)
        name = email.split("@", 1)[0]
        now = time.time()
        cur = conn.execute(
            "INSERT INTO users (email, name, password_hash, salt, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (email, name, password_hash, salt, now),
        )
        user_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    token = create_session(user_id)
    return {"token": token, "email": email, "name": name, "avatar_url": None}


def login(payload: Dict[str, Any]) -> Dict[str, Any]:
    email = (payload.get("email") or "").strip().lower()
    password = payload.get("password") or ""

    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM users WHERE email = ?", (email,)
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return {"error": "Incorrect email or password."}

    candidate = _hash_password(password, row["salt"])
    if not hmac.compare_digest(candidate, row["password_hash"]):
        return {"error": "Incorrect email or password."}

    token = create_session(row["id"])
    return {
        "token": token,
        "email": row["email"],
        "name": row["name"],
        "avatar_url": row["avatar_url"],
    }


def create_session(user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = time.time()
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO sessions (token, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?)",
            (token, user_id, now, now + SESSION_TTL),
        )
        conn.commit()
    finally:
        conn.close()
    return token


def logout(token: str) -> None:
    if not token:
        return
    conn = db.get_connection()
    try:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()


def user_from_token(token: Optional[str]) -> Optional[Dict[str, Any]]:
    if not token:
        return None
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id "
            "WHERE sessions.token = ? AND sessions.expires_at > ?",
            (token, time.time()),
        ).fetchone()
    finally:
        conn.close()
    return _user_row_to_dict(row) if row else None


def set_avatar(user_id: int, url: str) -> None:
    conn = db.get_connection()
    try:
        conn.execute("UPDATE users SET avatar_url = ? WHERE id = ?", (url, user_id))
        conn.commit()
    finally:
        conn.close()


def bearer_token(authorization_header: Optional[str]) -> Optional[str]:
    if not authorization_header:
        return None
    parts = authorization_header.split(" ", 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip()
    return None
