"""Cloudflare Turnstile verification for the sign-up form — a free,
privacy-friendly CAPTCHA replacement. Its site key (public) lives in
index.html; this module only ever needs the secret key, which stays
server-side.

Entirely optional: TURNSTILE_SECRET_KEY unset means is_configured() is
False, and auth.register() falls back to the custom SVG captcha exactly as
it always has.
"""
import os

import httpx

SECRET_KEY = os.environ.get("TURNSTILE_SECRET_KEY", "").strip()
VERIFY_URL = "https://challenge.cloudflare.com/turnstile/v0/siteverify"


def is_configured() -> bool:
    return bool(SECRET_KEY)


async def verify(token) -> bool:
    if not SECRET_KEY or not token:
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                VERIFY_URL, data={"secret": SECRET_KEY, "response": token}
            )
        return bool(response.json().get("success"))
    except Exception as e:
        print(f"[turnstile] verification request failed: {e}")
        return False
