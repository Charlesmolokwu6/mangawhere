"""Forgotten-password flow: email a one-time link, then set a new password.

1. POST /api/forgot-password {email, link}: if an account has that email,
   store a hashed one-time token (1 hour) and email a link to the reader's
   own site with the token in it. The answer is the same whether or not
   the account exists, so this can't be used to find out who has one.
2. POST /api/reset-password {token, password}: if the token is valid,
   unused and unexpired, set the new password, sign out every existing
   session (in case someone else got in), and sign the reader in.

Accounts made with Google/Facebook have no password; this lets them set
one too. Email goes out over plain SMTP, so any provider works (Brevo,
Gmail with an app password, Resend, Amazon SES...) — see README.md.
"""
import hashlib
import hmac
import html
import os
import secrets
import smtplib
import ssl
import time
from email.message import EmailMessage
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from . import auth, db

TOKEN_TTL = 60 * 60  # 1 hour
MAX_REQUESTS_PER_EMAIL_PER_HOUR = 3
MIN_PASSWORD_LENGTH = 8

# The reset link points at the reader's own copy of the site (it sends its
# address with the request), but only at sites listed here — otherwise
# anyone could make this server email people a link to some other site.
DEFAULT_LINK_ORIGINS = (
    "https://charlesmolokwu6.github.io",
    "https://mangawhere.onrender.com",
    "http://localhost:8000",
)

SENT_MESSAGE = (
    "If there's an account for that email, a link to reset the password is on its way. "
    "Check your inbox (and spam folder)."
)
BAD_LINK_MESSAGE = "This reset link has expired or was already used. Ask for a new one."


def _smtp_settings() -> Optional[Dict[str, Any]]:
    host = os.environ.get("SMTP_HOST", "").strip()
    sender = os.environ.get("MAIL_FROM", "").strip()
    if not host or not sender:
        return None
    port = int(os.environ.get("SMTP_PORT", "587") or 587)
    return {
        "host": host,
        "port": port,
        # "starttls" (port 587, the usual), "ssl" (port 465), or "none" for a
        # relay on the same private network that doesn't do encryption.
        "security": (os.environ.get("SMTP_SECURITY", "").strip().lower() or ("ssl" if port == 465 else "starttls")),
        "username": os.environ.get("SMTP_USERNAME", "").strip(),
        "password": os.environ.get("SMTP_PASSWORD", ""),
        "sender": sender,
    }


def available() -> bool:
    return _smtp_settings() is not None


def _allowed_origins():
    extra = [o.strip().rstrip("/") for o in os.environ.get("RESET_LINK_ORIGINS", "").split(",") if o.strip()]
    return set(DEFAULT_LINK_ORIGINS) | set(extra)


def safe_link_base(link: str) -> Optional[str]:
    """The reader's page address, if it's on an allowed site; else None.
    The query string is always dropped: the page reads `?api=` to decide
    which server to talk to, so keeping it would let someone request a
    reset for another person's email with `?api=<their server>` on the
    link — a genuine email from us whose page then sends the new password
    and token to them."""
    parsed = urlparse(link or "")
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if origin not in _allowed_origins():
        return None
    return f"{origin}{parsed.path or '/'}"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


# The HTML version shows a button instead of the long link (email apps
# show HTML when they can; the plain text is for those that can't). Inline
# styles only: most email apps drop <style> blocks. Colours match the site.
_EMAIL_HTML = """\
<!doctype html>
<html><body style="margin:0;padding:0;background:#14101a">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#14101a">
<tr><td align="center" style="padding:32px 16px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="max-width:480px;background:#1e1826;border-radius:14px">
<tr><td style="padding:28px 24px;font-family:Arial,Helvetica,sans-serif;color:#f0ebf5">
<div style="font-size:22px;font-weight:bold;margin:0 0 18px">manga<span style="color:#3fe0c8">where</span></div>
<p style="font-size:16px;line-height:1.5;margin:0 0 22px">Someone asked to reset the password for your MangaWhere account.</p>
<table role="presentation" cellpadding="0" cellspacing="0"><tr>
<td style="border-radius:10px;background:#3fe0c8">
<a href="{link}" style="display:inline-block;padding:14px 28px;font-size:16px;font-weight:bold;color:#14101a;text-decoration:none;border-radius:10px">Reset password</a>
</td></tr></table>
<p style="font-size:14px;line-height:1.5;color:#a094ad;margin:22px 0 0">The button works for the next hour, once.
If that wasn't you, ignore this email. Your password won't change.</p>
</td></tr></table>
</td></tr></table>
</body></html>
"""


def build_message(sender: str, to: str, link: str) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = "Reset your MangaWhere password"
    message["From"] = sender
    message["To"] = to
    message.set_content(
        "Someone asked to reset the password for your MangaWhere account.\n\n"
        f"To choose a new password, open this link within the next hour:\n{link}\n\n"
        "If that wasn't you, ignore this email. Your password won't change."
    )
    message.add_alternative(_EMAIL_HTML.replace("{link}", html.escape(link, quote=True)), subtype="html")
    return message


def _send_email(to: str, link: str) -> None:
    settings = _smtp_settings()
    message = build_message(settings["sender"], to, link)
    context = ssl.create_default_context()
    if settings["security"] == "ssl":
        server = smtplib.SMTP_SSL(settings["host"], settings["port"], context=context, timeout=20)
    else:
        server = smtplib.SMTP(settings["host"], settings["port"], timeout=20)
        if settings["security"] != "none":
            server.starttls(context=context)
    try:
        if settings["username"]:
            server.login(settings["username"], settings["password"])
        server.send_message(message)
    finally:
        server.quit()


def request_reset(email: str, link: str) -> Dict[str, Any]:
    """Returns {"message": ...} or {"error": ...}. Never reveals whether an
    account exists. Sending happens here (blocking) — call it in a thread."""
    if not available():
        return {"error": "Password reset isn't set up on this server yet."}
    email = (email or "").strip().lower()
    if not auth.EMAIL_RE.match(email):
        return {"error": "Enter the email address you signed up with."}
    base = safe_link_base(link)
    if not base:
        return {"error": "Couldn't make a reset link for this page. Open the site from its usual address."}

    now = time.time()
    conn = db.get_connection()
    try:
        user = conn.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
        if not user:
            return {"message": SENT_MESSAGE}
        recent = conn.execute(
            "SELECT COUNT(*) AS n FROM password_resets WHERE user_id = ? AND created_at > ?",
            (user["id"], now - 3600),
        ).fetchone()["n"]
        if recent >= MAX_REQUESTS_PER_EMAIL_PER_HOUR:
            return {"message": SENT_MESSAGE}  # quietly stop; the earlier links still work
        token = secrets.token_urlsafe(32)
        conn.execute(
            "INSERT INTO password_resets (token_hash, user_id, created_at, expires_at, used) "
            "VALUES (?, ?, ?, ?, 0)",
            (_hash_token(token), user["id"], now, now + TOKEN_TTL),
        )
        conn.commit()
    finally:
        conn.close()

    try:
        _send_email(email, f"{base}#reset={token}")
    except Exception as e:
        print(f"[password-reset] sending email failed: {e}")
        return {"error": "Couldn't send the email right now. Try again in a few minutes."}
    return {"message": SENT_MESSAGE}


def reset_password(token: str, password: str) -> Dict[str, Any]:
    """Returns the same shape as auth.login on success, or {"error": ...}."""
    if len(password or "") < MIN_PASSWORD_LENGTH:
        return {"error": f"Password must be at least {MIN_PASSWORD_LENGTH} characters."}
    if not token:
        return {"error": BAD_LINK_MESSAGE}

    now = time.time()
    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT token_hash, user_id, expires_at, used FROM password_resets WHERE token_hash = ?",
            (_hash_token(token),),
        ).fetchone()
        if not row or row["used"] or row["expires_at"] < now or not hmac.compare_digest(
            row["token_hash"], _hash_token(token)
        ):
            return {"error": BAD_LINK_MESSAGE}
        user = conn.execute("SELECT * FROM users WHERE id = ?", (row["user_id"],)).fetchone()
        if not user:
            return {"error": BAD_LINK_MESSAGE}

        salt = secrets.token_hex(16)
        conn.execute(
            "UPDATE users SET password_hash = ?, salt = ? WHERE id = ?",
            (auth._hash_password(password, salt), salt, user["id"]),
        )
        # One link, one use — and every other link for this account goes too.
        conn.execute("UPDATE password_resets SET used = 1 WHERE user_id = ?", (user["id"],))
        # Whoever else might be signed in (the reason for a reset, sometimes)
        # is signed out everywhere.
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user["id"],))
        conn.commit()
    finally:
        conn.close()

    return {
        "token": auth.create_session(user["id"]),
        "email": user["email"],
        "name": user["name"],
        "avatar_url": user["avatar_url"],
    }
