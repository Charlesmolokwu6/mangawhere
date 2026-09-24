import hashlib
import os
import time
from typing import Dict

import httpx

CLOUDINARY_CLOUD_NAME = os.environ.get("CLOUDINARY_CLOUD_NAME", "")
CLOUDINARY_API_KEY = os.environ.get("CLOUDINARY_API_KEY", "")
CLOUDINARY_API_SECRET = os.environ.get("CLOUDINARY_API_SECRET", "")

MAX_AVATAR_BYTES = 5 * 1024 * 1024  # 5MB
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}


def configured() -> bool:
    """False until CLOUDINARY_* env vars are set on the server — the free-
    tier storage the avatar upload is backed by, since Render's own disk
    doesn't survive a redeploy. Callers should degrade to the default
    initials avatar rather than erroring when this is false."""
    return bool(CLOUDINARY_CLOUD_NAME and CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET)


def validate_avatar(content_type: str, content: bytes) -> None:
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise ValueError("Avatars must be a JPEG, PNG, WEBP, or GIF image.")
    if not content:
        raise ValueError("That file is empty.")
    if len(content) > MAX_AVATAR_BYTES:
        raise ValueError("Avatars must be 5MB or smaller.")


def _signature(params: Dict[str, str]) -> str:
    # Cloudinary's signed-upload scheme: sort every param that will be sent
    # (besides file/api_key/signature/resource_type) as "key=value", join
    # with "&", append the API secret, then SHA-1 the whole string.
    to_sign = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    return hashlib.sha1((to_sign + CLOUDINARY_API_SECRET).encode("utf-8")).hexdigest()


async def upload_avatar(user_id: int, content: bytes, filename: str) -> str:
    if not configured():
        raise RuntimeError(
            "Avatar uploads aren't set up yet — CLOUDINARY_CLOUD_NAME, "
            "CLOUDINARY_API_KEY and CLOUDINARY_API_SECRET need to be set."
        )

    # A fixed public_id per user (rather than a random one) means a new
    # upload overwrites their last avatar on Cloudinary's side instead of
    # accumulating an orphaned image there every time someone changes it.
    params = {
        "timestamp": str(int(time.time())),
        "public_id": f"avatars/user_{user_id}",
        "overwrite": "true",
    }
    data = {**params, "api_key": CLOUDINARY_API_KEY, "signature": _signature(params)}
    files = {"file": (filename or "avatar", content)}
    url = f"https://api.cloudinary.com/v1_1/{CLOUDINARY_CLOUD_NAME}/image/upload"

    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(url, data=data, files=files)

    if resp.status_code != 200:
        raise RuntimeError(f"Cloudinary rejected the upload (HTTP {resp.status_code}).")

    secure_url = (resp.json() or {}).get("secure_url")
    if not secure_url:
        raise RuntimeError("Cloudinary upload didn't return an image URL.")
    return secure_url
