import base64
import time
from pathlib import Path
from typing import Optional

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid02
from pywebpush import WebPushException, webpush

from . import db

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
VAPID_KEY_PATH = DATA_DIR / "vapid_private.pem"

# Push services want *someone* to contact about a misbehaving sender.
# Deployers who care can override this via VAPID_CONTACT_EMAIL.
import os

VAPID_CONTACT = os.environ.get("VAPID_CONTACT_EMAIL", "admin@example.com")

_vapid: Optional[Vapid02] = None


def _load_or_create_vapid() -> Vapid02:
    global _vapid
    if _vapid is not None:
        return _vapid

    # from_file() generates and persists a key pair itself when the file
    # doesn't exist yet, so this doubles as first-run setup.
    _vapid = Vapid02.from_file(str(VAPID_KEY_PATH))
    return _vapid


def public_key_b64() -> str:
    vapid = _load_or_create_vapid()
    raw = vapid.public_key.public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint
    )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def save_subscription(
    endpoint: str, p256dh: str, auth: str, user_id: Optional[int] = None
) -> None:
    """Upsert a subscription. A user_id, if given, attaches it to that
    account (without one, it's an anonymous device — still notifiable
    directly via /api/test-push, just not by the poller)."""
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO push_subscriptions (endpoint, p256dh, auth, user_id, created_at) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(endpoint) DO UPDATE SET p256dh = excluded.p256dh, "
            "auth = excluded.auth, "
            "user_id = COALESCE(excluded.user_id, push_subscriptions.user_id)",
            (endpoint, p256dh, auth, user_id, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


def attach_subscription(endpoint: Optional[str], user_id: int) -> None:
    """Links an already-subscribed device to an account, e.g. when
    someone tracks a title from a browser that already allowed
    notifications before signing in."""
    if not endpoint:
        return
    conn = db.get_connection()
    try:
        conn.execute(
            "UPDATE push_subscriptions SET user_id = ? WHERE endpoint = ?",
            (user_id, endpoint),
        )
        conn.commit()
    finally:
        conn.close()


def subscriptions_for_user(user_id: int) -> list:
    conn = db.get_connection()
    try:
        rows = conn.execute(
            "SELECT endpoint, p256dh, auth FROM push_subscriptions WHERE user_id = ?",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _subscription_info(endpoint: str, conn=None) -> Optional[dict]:
    owns_conn = conn is None
    conn = conn or db.get_connection()
    try:
        row = conn.execute(
            "SELECT * FROM push_subscriptions WHERE endpoint = ?", (endpoint,)
        ).fetchone()
    finally:
        if owns_conn:
            conn.close()
    if not row:
        return None
    return {
        "endpoint": row["endpoint"],
        "keys": {"p256dh": row["p256dh"], "auth": row["auth"]},
    }


def send(endpoint: str, payload_json: str) -> dict:
    """Send one push message. Returns {ok: True} or {ok: False, error: ...}."""
    info = _subscription_info(endpoint)
    if not info:
        return {"ok": False, "error": "not subscribed"}

    vapid = _load_or_create_vapid()
    try:
        webpush(
            subscription_info=info,
            data=payload_json,
            vapid_private_key=vapid,
            vapid_claims={"sub": f"mailto:{VAPID_CONTACT}"},
        )
        return {"ok": True}
    except WebPushException as e:
        status = getattr(e.response, "status_code", None)
        if status in (404, 410):
            # The browser dropped the subscription; stop trying it.
            conn = db.get_connection()
            try:
                conn.execute(
                    "DELETE FROM push_subscriptions WHERE endpoint = ?", (endpoint,)
                )
                conn.commit()
            finally:
                conn.close()
        return {"ok": False, "error": str(e)}
