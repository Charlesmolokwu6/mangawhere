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
    """True when CLOUDINARY_* env vars are set: avatars then go to
    Cloudinary. Otherwise they're kept in the app's own database
    (save_avatar)."""
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


# ---- avatars kept in the app's own database ----
#
# Without Cloudinary, a profile photo is shrunk to a small square and kept
# in the database (which, with Turso, survives redeploys), and served from
# /api/avatar/<user id>. ~15KB each, so even 10,000 photos are ~150MB.

AVATAR_SIZE = 256
MAX_AVATAR_PIXELS = 40_000_000   # a bigger picture is refused before it's decoded


def shrink_avatar(content: bytes) -> bytes:
    """The photo as a 256x256 WebP, centre-cropped and turned upright."""
    import io

    from PIL import Image, ImageOps

    try:
        image = Image.open(io.BytesIO(content))
        if image.width * image.height > MAX_AVATAR_PIXELS:
            raise ValueError("That picture is too big.")
        image.draft("RGB", (AVATAR_SIZE * 2, AVATAR_SIZE * 2))   # JPEGs decode smaller, faster
        image = ImageOps.exif_transpose(image).convert("RGB")
        image = ImageOps.fit(image, (AVATAR_SIZE, AVATAR_SIZE), Image.LANCZOS)
        out = io.BytesIO()
        image.save(out, "WEBP", quality=82)
        return out.getvalue()
    except ValueError:
        raise
    except Exception:
        raise ValueError("That picture couldn't be read. Try a JPEG or PNG.")


def save_avatar(user_id: int, image: bytes) -> str:
    """Keep the shrunk photo; returns its address (a path on this server,
    versioned so a new photo isn't hidden by a cached old one)."""
    import base64

    from server import db

    now = int(time.time())
    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO avatars (user_id, image, updated_at) VALUES (?, ?, ?)",
            (user_id, base64.b64encode(image).decode("ascii"), now),
        )
        conn.commit()
    finally:
        conn.close()
    return f"/api/avatar/{user_id}?v={now}"


def load_avatar(user_id: int):
    import base64

    from server import db

    conn = db.get_connection()
    try:
        row = conn.execute("SELECT image FROM avatars WHERE user_id = ?", (user_id,)).fetchone()
    finally:
        conn.close()
    return base64.b64decode(row["image"]) if row else None


async def upload_avatar(user_id: int, content: bytes, filename: str) -> str:
    if not configured():
        import asyncio

        image = await asyncio.to_thread(shrink_avatar, content)
        return await asyncio.to_thread(save_avatar, user_id, image)

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
