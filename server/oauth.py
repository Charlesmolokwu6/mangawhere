"""Sign in with Google / Facebook.

Both providers' JS SDKs hand the *frontend* a token directly (Google: an ID
token from Google Identity Services; Facebook: an access token from the
Facebook SDK) — no server-side redirect dance needed, which fits this
app's static-frontend-plus-API architecture far better than the
authorization-code flow. This module just verifies whichever token the
frontend already obtained.

Google's ID token is a signed JWT, but rather than pull in a JWT/crypto
library to verify the signature ourselves, this uses Google's own
`tokeninfo` endpoint — it validates the signature server-side and hands
back the decoded claims, which is officially supported for exactly this.
Facebook's access token is verified implicitly: calling the Graph API with
it only succeeds if Facebook itself considers the token valid.

Apple ("Sign in with Apple") isn't implemented here yet — verifying its
identity token needs real JWT/JWKS handling (a different shape of problem
from the other two), and there's no point building it before the paid
Apple Developer account it depends on exists. See README.md.

Entirely optional, same as every other integration in this app: the site
IDs below are public/frontend config (see index.html), and both provider
functions here degrade to "not configured" rather than erroring when
their corresponding ID is unset.
"""
import os
from typing import Optional

import httpx

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
FACEBOOK_APP_ID = os.environ.get("FACEBOOK_APP_ID", "").strip()
FACEBOOK_APP_SECRET = os.environ.get("FACEBOOK_APP_SECRET", "").strip()

GOOGLE_TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
FACEBOOK_GRAPH_URL = "https://graph.facebook.com/me"
FACEBOOK_DEBUG_TOKEN_URL = "https://graph.facebook.com/debug_token"


def google_configured() -> bool:
    return bool(GOOGLE_CLIENT_ID)


def facebook_configured() -> bool:
    return bool(FACEBOOK_APP_ID and FACEBOOK_APP_SECRET)


async def verify_google(id_token: str) -> Optional[dict]:
    """Returns {"subject", "email", "name", "picture"} on success, or None
    on any failure (wrong audience, expired, malformed, network error —
    all treated the same: sign-in didn't work, try again)."""
    if not GOOGLE_CLIENT_ID or not id_token:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(GOOGLE_TOKENINFO_URL, params={"id_token": id_token})
        if response.status_code != 200:
            return None
        claims = response.json()
        # tokeninfo verifies the signature but not *which app* the token
        # was issued for — that's on us, or anyone else's Google sign-in
        # token would work here too.
        if claims.get("aud") != GOOGLE_CLIENT_ID:
            return None
        if claims.get("email_verified") not in ("true", True):
            return None
        return {
            "subject": claims.get("sub"),
            "email": (claims.get("email") or "").lower(),
            "name": claims.get("name") or "",
            "picture": claims.get("picture") or "",
        }
    except Exception as e:
        print(f"[oauth] Google token verification failed: {e}")
        return None


async def verify_facebook(access_token: str) -> Optional[dict]:
    """Returns {"subject", "email", "name", "picture"} on success, or None
    on any failure."""
    if not FACEBOOK_APP_ID or not FACEBOOK_APP_SECRET or not access_token:
        return None
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            # Confirms the token was actually issued for *this* app, not
            # just that it's some valid Facebook token.
            debug = await client.get(
                FACEBOOK_DEBUG_TOKEN_URL,
                params={
                    "input_token": access_token,
                    "access_token": f"{FACEBOOK_APP_ID}|{FACEBOOK_APP_SECRET}",
                },
            )
            debug_data = debug.json().get("data", {})
            if not debug_data.get("is_valid") or debug_data.get("app_id") != FACEBOOK_APP_ID:
                return None

            profile = await client.get(
                FACEBOOK_GRAPH_URL,
                params={"fields": "id,name,email,picture", "access_token": access_token},
            )
            if profile.status_code != 200:
                return None
            data = profile.json()
        email = (data.get("email") or "").lower()
        if not email:
            # Facebook accounts can lack a verified email entirely (no
            # email on file, or the user declined that permission) --
            # nothing to link an account by, so treat it as a failure
            # rather than creating an unreachable account.
            return None
        picture = ((data.get("picture") or {}).get("data") or {}).get("url") or ""
        return {
            "subject": data.get("id"),
            "email": email,
            "name": data.get("name") or "",
            "picture": picture,
        }
    except Exception as e:
        print(f"[oauth] Facebook token verification failed: {e}")
        return None
