import random
import secrets
import time

from . import db

# Excludes characters that are easy to confuse with each other (0/O, 1/I/l).
ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
LENGTH = 5
TTL = 600  # seconds


def _render_svg(text: str) -> str:
    width, height = 150, 50
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" role="img" aria-label="captcha">',
        f'<rect width="{width}" height="{height}" fill="#f1f1f4"/>',
    ]

    # A few faint noise lines so the text can't be lifted by a trivial
    # background-color filter.
    for _ in range(4):
        x1, y1 = random.randint(0, width), random.randint(0, height)
        x2, y2 = random.randint(0, width), random.randint(0, height)
        parts.append(
            f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" '
            f'stroke="#c7c7d1" stroke-width="1"/>'
        )

    step = width / (len(text) + 1)
    for i, ch in enumerate(text):
        x = step * (i + 1)
        y = height / 2 + random.randint(-6, 6)
        angle = random.randint(-25, 25)
        size = random.randint(22, 28)
        color = random.choice(["#2f2f3a", "#3a3a52", "#26263a"])
        parts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-size="{size}" '
            f'font-family="monospace" font-weight="bold" fill="{color}" '
            f'text-anchor="middle" transform="rotate({angle} {x:.1f} {y:.1f})">{ch}</text>'
        )

    parts.append("</svg>")
    return "".join(parts)


def generate() -> dict:
    answer = "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))
    captcha_id = secrets.token_urlsafe(12)
    now = time.time()

    conn = db.get_connection()
    try:
        conn.execute(
            "INSERT INTO captchas (id, answer, expires_at) VALUES (?, ?, ?)",
            (captcha_id, answer, now + TTL),
        )
        conn.commit()
    finally:
        conn.close()

    return {"id": captcha_id, "svg": _render_svg(answer)}


def verify(captcha_id, answer) -> bool:
    """Single-use: the row is gone after this call whether it matched or not."""
    if not captcha_id or not answer:
        return False

    conn = db.get_connection()
    try:
        row = conn.execute(
            "SELECT answer, expires_at FROM captchas WHERE id = ?", (captcha_id,)
        ).fetchone()
        conn.execute("DELETE FROM captchas WHERE id = ?", (captcha_id,))
        conn.commit()
    finally:
        conn.close()

    if not row or row["expires_at"] < time.time():
        return False
    return row["answer"] == answer.strip().upper()
