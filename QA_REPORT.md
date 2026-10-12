# MangaWhere — QA & Readiness Report (target: ~10,000 users)

Tested against the current `main.py` FastAPI app running locally (`uvicorn main:app`),
with the default configuration (local SQLite, no Turso/Cloudinary/Turnstile).
Each finding below was reproduced, not just read from the code.

Severity key: 🔴 High · 🟠 Medium · 🟡 Low / polish

---

## 🔴 1. SSRF — the scraper endpoints will fetch arbitrary internal URLs

**Where:** `scrapers/core.py:_detect_domain()` + `/api/scrape`, `/api/series`,
`/api/narrate/page`, `/api/trim` in `main.py`.

`_detect_domain()` decides which scraper to run by doing a **plain substring match
on the entire URL**, including the query string:

```python
if "toongod" in lowered: return "toongod"
```

The endpoints accept any absolute `http(s)` URL (`_require_absolute_url` only checks
the scheme), so the host is attacker-controlled. If the URL merely *contains* a known
site name anywhere, the matched scraper calls `fetch_html_httpx(url)` against that host.

**Reproduced:**
```
GET /api/scrape?url=http://127.0.0.1:8000/sw.js?x=toongod   → 200
server log: "GET /sw.js?x=toongod HTTP/1.1" 200 OK   ← server made the internal request
```
The server issued a GET to an internal address because `x=toongod` was in the query.
On a real host this reaches link-local metadata (`169.254.169.254`), internal services,
and anything else the box can route to. (I deliberately only pointed it at localhost.)

**Fix:** derive the host from `urlparse(url).hostname` and match it against an
**allowlist of known source domains** (the same pattern `PROXY_ALLOWED_HOSTS` already
uses in `main.py`). Reject anything else before scraping. Match on the hostname, never
on the raw URL string.

---

## 🔴 2. Password hashing blocks the event loop → login/register don't scale

**Where:** `main.py` `api_login` (line ~614) and `api_register` (line ~590) call
`auth.login()` / `auth.register()` **directly**, and those run PBKDF2 at 200,000
iterations synchronously. (`/api/forgot-password` already does the right thing with
`await asyncio.to_thread(...)` on line ~599 — login/register were missed.)

FastAPI runs these `async def` routes on the single event-loop thread, so each hash
blocks *every other request* on that instance while it runs.

**Reproduced:** 30 concurrent logins →
```
wall = 4.94s, per-request avg = 2.78s, max = 4.85s
```
Latency climbs linearly because requests serialize behind the CPU-bound hash. At 10k
users this caps login throughput at roughly one hash at a time per instance and stalls
unrelated traffic (reading, comments) during every sign-in.

**Fix:** run the hashing off the loop, e.g.
`result = await asyncio.to_thread(auth.login, payload)` (and the same for `register`,
`oauth_login`). Cheap, isolated change.

---

## 🟠 3. No rate limiting on expensive / abusable endpoints

`SCALING.md` step 7 already lists this as a TODO; flagging it as a launch blocker for 10k.
Nothing currently limits per-IP request rate on:
- `/api/find`, `/api/video-lookup` (run scrapers / yt-dlp — very expensive),
- `/api/register`, `/api/login` (CPU-bound hashing, see #2),
- `/api/comments` POST (no per-user post rate limit; only a 2000-char body cap),
- `/api/scrape`, `/api/series` (outbound fetches).

One script can exhaust the scrapers or the single worker. If Cloudflare is put in front
(as SCALING.md plans), its rate-limiting rules cover most of this without code; otherwise
add per-IP limits in the app.

---

## 🟠 4. The fallback SVG captcha offers ~no bot protection

**Where:** `server/captcha.py:_render_svg()`.

Each answer character is rendered as its own `<text>…</text>` element, so the answer is
present in plaintext in the SVG markup handed to the client.

**Reproduced:** extracted the answer from the returned SVG with one regex
(`>([A-Z0-9])</text>`), then used it to register successfully — no OCR needed.

This only matters when Turnstile is **not** configured (`TURNSTILE_SECRET_KEY` unset),
which is the default. When Turnstile is on it replaces this and is fine.

**Fix:** either require Turnstile for public deployments, or render the captcha as a
rasterized image / merge glyphs into a single path so the characters aren't individually
readable from the markup. Treat the custom captcha as "better than nothing" only.

---

## 🟠 5. Fresh DB connection per query, no pooling; SQLite has no WAL / busy_timeout

**Where:** `server/db.py:get_connection()`.

Every query opens a brand-new connection and closes it. With Turso configured, each
`get_connection()` does `libsql.connect(sync_url=...)` — and `user_from_token()` runs on
essentially every authenticated request, so that's a new embedded-replica connection per
call. For local SQLite there's no `PRAGMA journal_mode=WAL` and no `busy_timeout`, so
concurrent writers can hit "database is locked" instead of waiting.

40 concurrent comment writes succeeded locally (fast local disk), so this is a **latent
risk**, not a reproduced failure — but at 10k users with network-backed Turso it's worth
hardening: reuse connections (or a small pool) and set `WAL` + `busy_timeout` for the
SQLite path.

---

## 🟡 6. `/api/register` returns HTTP 500 on malformed JSON

**Reproduced:** `POST /api/register` with a non-JSON body → **500** (unhandled
`await request.json()`). `/api/login`, `/api/health/*`, `/api/reddit-read` all catch this
and return 400. Inconsistent and surfaces a stack trace path.

**Fix:** wrap the `await request.json()` in `api_register` (and `api_login`,
`api_oauth_*`, which share the pattern) in a try/except returning 400, matching the others.

---

## 🟡 7. `/api/watch` silently succeeds when the payload has no title key

**Reproduced:** `POST /api/watch` with `{}` → `{"ok":true}`, but `watch.upsert()` returns
early (no `key`) and stores nothing. The client is told the title was tracked when it
wasn't.

**Fix:** return 400 when `title.key` is missing instead of a success response.

---

## What's already solid (tested, no issue found)

- **Stored-XSS in comments:** bodies are stored raw but the frontend escapes both
  `c.body` and `c.name` with `esc()` in `renderCommentRow` — rendered inert. Good.
- **Proxy open-relay:** `/?url=` enforces `PROXY_ALLOWED_HOSTS`; a disallowed host
  (metadata IP) correctly returned **403**. (This is the correct pattern the scraper
  endpoints in #1 are missing.)
- **Auth basics:** protected endpoints 401 without a token; wrong password and unknown
  email give the same generic error; session tokens are stored only as SHA-256 hashes;
  duplicate-email registration is rejected.
- **Avatar upload** correctly returns 503 (not a crash) when Cloudinary isn't configured.
- **Existing test suite:** 340 passing. 2 failures in `tests/test_storyteller.py` are
  environmental (missing `numpy`, and a narration-enable flag) — not product bugs.

---

## Suggested priority order

1. #1 SSRF (host allowlist) — security, do before any public launch.
2. #2 hashing off the event loop — one-line-ish, big scaling win.
3. #3 rate limiting (Cloudflare or app-level).
4. #4 captcha / require Turnstile publicly.
5. #5 DB connection reuse + WAL/busy_timeout.
6. #6, #7 validation polish.
