import asyncio
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional, Sequence
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response

from scrapers import find_best_source, lookup_video, scrape_chapter, scrape_series
from scrapers import ytpost
from server import auth, banners, cache, captcha, comments, db, health, media, oauth, password_reset, poller, push, storyteller, turnstile, watch

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # A copy that only narrates (narrator/Dockerfile, on a Hugging Face
    # Space) leaves the site's background jobs to the main server.
    narration_only = os.environ.get("NARRATION_ONLY", "").strip().lower() in ("1", "true", "yes", "on")
    if not narration_only:
        poller.start()
        health.start(find_title, scrape_cached, image_loads)
    yield
    if not narration_only:
        await health.stop()
        poller.stop()
    if _proxy_client is not None:
        await _proxy_client.aclose()


# One long-lived client for the proxy, so its connections to an image CDN
# stay open between requests. A chapter can be ~200 images; opening a
# fresh TLS connection for every one made each image take 0.5-1.1s through
# the proxy against ~0.3s direct.
_proxy_client: Optional[httpx.AsyncClient] = None


def _get_proxy_client() -> httpx.AsyncClient:
    global _proxy_client
    if _proxy_client is None:
        _proxy_client = httpx.AsyncClient(
            timeout=20.0,
            limits=httpx.Limits(max_connections=64, max_keepalive_connections=32),
        )
    return _proxy_client


app = FastAPI(title="MangaWhere scraper API", lifespan=lifespan)

# CORS proxy for the frontend's own client-side API calls (AniList, Jikan,
# MangaUpdates, TikTok's oEmbed, Webtoons' RSS, ...) — those APIs don't
# send CORS headers a browser would accept, so the frontend routes them
# through here instead (see index.html's via()/PROXY). Without an
# allowlist this is an open relay anyone could point anywhere.
PROXY_ALLOWED_HOSTS = {
    "api.mangaupdates.com",
    "www.tiktok.com",
    "vm.tiktok.com",
    "www.youtube.com",
    "youtu.be",
    "api.jikan.moe",
    "graphql.anilist.co",
    # Search's last fallback (index.html's mdSearch). It was never listed,
    # so that step always got 403 and titles only MangaDex knows (Hand
    # Jumper) came back "Nothing found".
    "api.mangadex.org",
    "www.webtoons.com",
    "global.mangaplus.shueisha.co.jp",
    "manga.bilibili.com",
}
# comizy.io's page-image CDN is sharded across numbered subdomains
# (x1.cmzcdn.org, x7.cmzcdn.org, ...), so it needs a suffix match rather
# than the exact-hostname set above. It also enforces a strict Referer
# check (confirmed: only "https://comizy.io/" passes, not even our own
# domain), which is why these images need proxying at all — see
# PROXY_REFERER_OVERRIDES below.
PROXY_ALLOWED_SUFFIXES = ("cmzcdn.org",)
PROXY_REFERER_OVERRIDES = {"cmzcdn.org": "https://comizy.io/"}
PROXY_UA = "MangaWhere/1.0 (+https://mangawhere.example)"


def _proxy_host_allowed(hostname: str) -> bool:
    return hostname in PROXY_ALLOWED_HOSTS or any(
        hostname == suffix or hostname.endswith("." + suffix) for suffix in PROXY_ALLOWED_SUFFIXES
    )

# Hosts /api/video-lookup will run yt-dlp against — same allowlist idea as
# the proxy above, just narrower: this one downloads audio, so it's worth
# being stricter about what it'll fetch from.
VIDEO_LOOKUP_HOSTS = {
    "www.youtube.com",
    "youtube.com",
    "m.youtube.com",
    "youtu.be",
    "www.tiktok.com",
    "vm.tiktok.com",
    "m.tiktok.com",
}

# No cookies are used (auth is a bearer token the client stores itself), so
# a wildcard origin doesn't expose this to CSRF — it only lets a
# separately-hosted frontend (the "?api=" deployment mode) call in.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


def _current_user(authorization: Optional[str]):
    return auth.user_from_token(auth.bearer_token(authorization))


def _require_user(authorization: Optional[str]):
    user = _current_user(authorization)
    if not user:
        raise HTTPException(status_code=401, detail="Sign in required")
    return user


def _require_absolute_url(url: str) -> None:
    if not url:
        raise HTTPException(status_code=400, detail="Missing url query parameter")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="url must be an absolute http(s) URL")


async def _proxy(request: Request, target: str) -> Response:
    parsed = urlparse(target)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return JSONResponse({"error": "malformed url"}, status_code=400)
    if not parsed.hostname or not _proxy_host_allowed(parsed.hostname):
        return JSONResponse(
            {"error": "host not allowed", "host": parsed.hostname}, status_code=403
        )

    headers = {
        "User-Agent": PROXY_UA,
        "Accept": "application/json, text/xml, application/xml, */*",
    }
    for suffix, referer in PROXY_REFERER_OVERRIDES.items():
        if parsed.hostname == suffix or parsed.hostname.endswith("." + suffix):
            headers["Referer"] = referer
            break
    body = None
    if request.method == "POST":
        body = await request.body()
        headers["Content-Type"] = request.headers.get("content-type", "application/json")

    try:
        upstream = await _get_proxy_client().request(
            request.method, target, headers=headers, content=body
        )
    except Exception as e:
        return JSONResponse({"error": "upstream failed", "detail": str(e)}, status_code=502)

    media_type = upstream.headers.get("content-type", "application/json")
    response = Response(content=upstream.content, status_code=upstream.status_code, media_type=media_type)
    if upstream.status_code == 200 and media_type.startswith("image/"):
        # Chapter images never change at a given URL, so the reader's
        # browser (and any CDN in front of this server) can keep them —
        # going back a chapter or re-opening one doesn't refetch every page
        # through this server.
        response.headers["Cache-Control"] = "public, max-age=604800, immutable"
    return response


@app.get("/")
async def serve_index_or_proxy(request: Request, url: Optional[str] = Query(default=None)):
    if url:
        return await _proxy(request, url)
    return FileResponse(BASE_DIR / "index.html")


@app.post("/")
async def proxy_post(request: Request, url: Optional[str] = Query(default=None)):
    if not url:
        raise HTTPException(status_code=404, detail="Not found")
    return await _proxy(request, url)


# How long scraper results are reused before the source sites are asked
# again (see server/cache.py). Chapter pages almost never change once
# posted; chapter lists and title lookups change when a new chapter comes
# out, so those are kept short enough that "latest chapter" stays current.
# kaliscan's image URLs carry a signed token that expires (~11h), so its
# chapters are kept well inside that.
CHAPTER_TTL = 6 * 3600
KALISCAN_CHAPTER_TTL = 30 * 60
# MangaDex hands out image addresses on a volunteer-run server (MangaDex@Home)
# that are only promised for about 15 minutes; cached for hours they 404.
MANGADEX_CHAPTER_TTL = 10 * 60
SERIES_TTL = 30 * 60
FIND_TTL = 30 * 60          # an answer this old is refreshed in the background...
FIND_KEEP_TTL = 12 * 3600   # ...but still served instantly for up to this long
FIND_MISS_TTL = 10 * 60  # "not found anywhere" — rechecked sooner
# Once one site has found a title, how long the rest get before the reader
# is answered. Fast sites (APIs) answer in 1-3s; the slowest took 20-30s.
FIND_GRACE = 4.0


def _cacheable(response: JSONResponse, seconds: int) -> JSONResponse:
    # Lets the reader's browser (and any CDN in front of this server) reuse
    # the answer too, instead of asking again on every page view.
    response.headers["Cache-Control"] = f"public, max-age={seconds}"
    return response


def _chapter_ttl(url: str) -> float:
    lowered = url.lower()
    if "kaliscan" in lowered:
        return KALISCAN_CHAPTER_TTL
    if "mangadex" in lowered:
        return MANGADEX_CHAPTER_TTL
    return CHAPTER_TTL


async def scrape_cached(url: str):
    return await cache.cached(
        "chapter", url, _chapter_ttl(url),
        lambda: scrape_chapter(url),
        keep=lambda p: bool(p and p.get("images")),
    )


IMAGE_CHECK_UA = (
    "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/130.0 Mobile Safari/537.36"
)


# Image servers that refuse this server but serve readers' browsers, which
# load them directly (not through the proxy). MangaDex's @home nodes answer
# 404 to every request from Render while the same URLs load for anyone
# else, so fetching from here would hide titles that read fine. For these,
# the source's own API handing back the chapter's page list is the check.
IMAGE_CHECK_SKIPPED_SUFFIXES = ("mangadex.network",)


async def image_loads(url: str) -> bool:
    """Does a page image actually come back, the way the reader asks for it?
    Direct images go with no Referer (the reader's <img> uses no-referrer);
    proxied CDNs get the Referer the proxy would send. Only the headers are
    read, not the whole image."""
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return False
    if any(parsed.hostname == s or parsed.hostname.endswith("." + s) for s in IMAGE_CHECK_SKIPPED_SUFFIXES):
        return True
    headers = {"User-Agent": IMAGE_CHECK_UA, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8"}
    for suffix, referer in PROXY_REFERER_OVERRIDES.items():
        if parsed.hostname == suffix or parsed.hostname.endswith("." + suffix):
            headers["Referer"] = referer
            break
    try:
        async with _get_proxy_client().stream("GET", url, headers=headers, follow_redirects=True) as r:
            content_type = r.headers.get("content-type", "")
            if r.status_code == 200 and not content_type.startswith(("text/", "application/json")):
                return True
            problem = f"HTTP {r.status_code} {content_type}"
    except Exception as e:
        problem = repr(e)
    # Say why: "page images didn't load" alone can't tell a dead image from
    # an image server that refuses this server but serves readers fine.
    print(f"[health] image check failed: {problem} for {url[:120]}")
    return False


@app.get("/api/scrape")
async def api_scrape(url: str = Query(..., description="Chapter URL to scrape")):
    _require_absolute_url(url)

    try:
        payload = await scrape_cached(url)
    except Exception:
        raise HTTPException(
            status_code=502, detail="Couldn't reach that page — the site may be blocking us."
        )

    payload = dict(payload)
    payload["chapter_url"] = url
    payload["images"] = [img for img in payload.get("images", []) if img]

    response = JSONResponse(content=payload)
    response.headers["Referrer-Policy"] = "no-referrer"
    if payload["images"]:
        # Browsers may keep it no longer than the server does.
        _cacheable(response, int(min(_chapter_ttl(url), 3600)))
    return response


@app.get("/api/series")
async def api_series(url: str = Query(..., description="Series page URL to list chapters for")):
    _require_absolute_url(url)

    try:
        payload = await cache.cached(
            "series", url, SERIES_TTL, lambda: scrape_series(url),
            keep=lambda p: bool(p and p.get("chapters")),
        )
    except Exception:
        raise HTTPException(
            status_code=502, detail="Couldn't reach that page — the site may be blocking us."
        )

    response = JSONResponse(content=payload)
    response.headers["Referrer-Policy"] = "no-referrer"
    if payload.get("chapters"):
        _cacheable(response, 300)
    return response


MAX_FIND_ALTS = 3


def _find_names(title: str, alts: Sequence[str]) -> List[str]:
    """The title first, then up to MAX_FIND_ALTS other names it's known by.
    Names with no Latin letters (native Japanese/Korean/Chinese) are skipped:
    none of the sources index those, so they'd only cost a full search."""
    names = [title.strip()]
    seen = {names[0].lower()}
    for alt in alts:
        alt = (alt or "").strip()
        if not alt or len(alt) > 200 or alt.lower() in seen or not re.search(r"[A-Za-z]", alt):
            continue
        names.append(alt)
        seen.add(alt.lower())
        if len(names) > MAX_FIND_ALTS:
            break
    return names


async def find_title(title: str, alts: Sequence[str] = ()):
    """find_best_source under the title, then under each other name until
    one turns up. Sites list a series under different names (AniList's
    English title, the romanized original, a fan translation), and searching
    only the first missed series that were sitting right there. Each name is
    cached on its own, so a miss under the main title isn't searched twice."""
    for name in _find_names(title, alts):
        result = await cache.cached_swr(
            "find", name, FIND_TTL, FIND_KEEP_TTL,
            # A reader is waiting: answer once a site has it, and swap in
            # the full ranking when the slower sites finish.
            lambda n=name: find_best_source(
                n, grace=FIND_GRACE, on_complete=lambda full, n=n: _save_full_find(n, full)
            ),
            # Nobody is waiting on a background refresh: search every site.
            refresh=lambda n=name: find_best_source(n),
            miss_ttl=FIND_MISS_TTL,
        )
        if result:
            return result
    return None


async def _save_full_find(name: str, full) -> None:
    if full:
        await cache.store("find", name, full, FIND_KEEP_TTL)


@app.get("/api/find")
async def api_find(
    title: str = Query(..., description="Manga title to find a reading source for"),
    alt: List[str] = Query(default=[], description="Other names the title is known by"),
):
    if not title or not title.strip():
        raise HTTPException(status_code=400, detail="Missing title query parameter")

    try:
        result = await find_title(title, alt)
    except Exception:
        raise HTTPException(status_code=502, detail="Couldn't search for that title right now.")

    if not result:
        raise HTTPException(
            status_code=404, detail="Couldn't find this title on any of our sources."
        )

    response = JSONResponse(content=result)
    response.headers["Referrer-Policy"] = "no-referrer"
    return _cacheable(response, 300)


@app.post("/api/health/status")
async def api_health_status(request: Request):
    """Which of these titles open in the reader. Body: {"titles": [{"title",
    "alts"}, ...]}. Unknown titles are queued for a check and come back
    "pending"; the site hides only "broken" ones."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Expected JSON")
    items = body.get("titles") if isinstance(body, dict) else None
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="Expected a titles list")
    return {"statuses": health.statuses(items)}


@app.post("/api/health/report")
async def api_health_report(request: Request):
    """The reader couldn't open a title: have it re-checked soon."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Expected JSON")
    if not isinstance(body, dict) or not health.report(body.get("title"), body.get("alts")):
        raise HTTPException(status_code=400, detail="Expected a title")
    return {"ok": True}


@app.get("/api/health")
async def api_health():
    """How many titles open in the reader, and which don't and why."""
    return health.summary()


@app.get("/api/video-lookup")
async def api_video_lookup(
    url: str = Query(..., description="TikTok/YouTube video URL to extract a manga title from")
):
    """Fallback for when a video's caption alone doesn't name the manga
    (or oEmbed refuses it outright, e.g. embedding disabled): pulls the
    upload description and, for short clips, a speech-to-text transcript
    of the audio — either of which can carry the title even when the
    caption doesn't."""
    _require_absolute_url(url)
    host = urlparse(url).hostname or ""
    if host not in VIDEO_LOOKUP_HOSTS:
        raise HTTPException(status_code=400, detail="Only TikTok and YouTube links are supported")

    try:
        result = await lookup_video(url)
    except Exception:
        raise HTTPException(status_code=502, detail="Couldn't read that video right now.")

    response = JSONResponse(content=result)
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/api/post-lookup")
async def api_post_lookup(url: str = Query(..., description="YouTube community post URL")):
    """A YouTube community post's text and the titles it lists. Posts
    aren't videos, so the video path (oEmbed, yt-dlp) can't read them."""
    _require_absolute_url(url)
    post_id = ytpost.post_id_from_url(url)
    if not post_id:
        raise HTTPException(status_code=400, detail="That isn't a YouTube post link")
    try:
        result = await ytpost.lookup_post(post_id)
    except Exception as e:
        print(f"[post] lookup failed for {post_id}: {e!r}")
        raise HTTPException(status_code=502, detail="Couldn't read that post right now.")
    if not result["text"]:
        raise HTTPException(status_code=404, detail="That post is private, deleted, or has no text.")
    response = JSONResponse(content=result)
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


def _attach_endpoint_from_payload(result: dict, payload: dict) -> None:
    endpoint = payload.get("endpoint")
    if not endpoint or "token" not in result:
        return
    user = auth.user_from_token(result["token"])
    if user:
        push.attach_subscription(endpoint, user["id"])


@app.post("/api/register")
async def api_register(request: Request):
    payload = await request.json()
    turnstile_configured = turnstile.is_configured()
    turnstile_ok = (
        await turnstile.verify(payload.get("turnstile_token")) if turnstile_configured else True
    )
    result = auth.register(
        payload, captcha.verify, skip_captcha=turnstile_configured, turnstile_ok=turnstile_ok
    )
    _attach_endpoint_from_payload(result, payload)
    return JSONResponse(result)


@app.post("/api/forgot-password")
async def api_forgot_password(payload: dict):
    result = await asyncio.to_thread(
        password_reset.request_reset, str(payload.get("email") or ""), str(payload.get("link") or "")
    )
    return JSONResponse(result, status_code=200 if "message" in result else 400)


@app.post("/api/reset-password")
async def api_reset_password(payload: dict):
    result = password_reset.reset_password(str(payload.get("token") or ""), str(payload.get("password") or ""))
    return JSONResponse(result, status_code=200 if "token" in result else 400)


@app.post("/api/login")
async def api_login(request: Request):
    payload = await request.json()
    result = auth.login(payload)
    _attach_endpoint_from_payload(result, payload)
    return JSONResponse(result)


@app.post("/api/oauth/google")
async def api_oauth_google(request: Request):
    payload = await request.json()
    if not oauth.google_configured():
        raise HTTPException(status_code=503, detail="Google sign-in isn't set up on this server.")
    profile = await oauth.verify_google(payload.get("id_token") or "")
    if not profile:
        return JSONResponse({"error": "Couldn't verify that with Google. Try again."})
    result = auth.oauth_login("google", profile)
    _attach_endpoint_from_payload(result, payload)
    return JSONResponse(result)


@app.post("/api/oauth/facebook")
async def api_oauth_facebook(request: Request):
    payload = await request.json()
    if not oauth.facebook_configured():
        raise HTTPException(status_code=503, detail="Facebook sign-in isn't set up on this server.")
    profile = await oauth.verify_facebook(payload.get("access_token") or "")
    if not profile:
        return JSONResponse({"error": "Couldn't verify that with Facebook. Try again."})
    result = auth.oauth_login("facebook", profile)
    _attach_endpoint_from_payload(result, payload)
    return JSONResponse(result)


@app.post("/api/logout")
async def api_logout(authorization: Optional[str] = Header(default=None)):
    auth.logout(auth.bearer_token(authorization))
    return {"ok": True}


@app.get("/api/me")
async def api_me(authorization: Optional[str] = Header(default=None)):
    user = _current_user(authorization)
    if not user:
        return {"signed_in": False}
    return {
        "signed_in": True,
        "tracked": watch.count_for_user(user["id"]),
        "name": user["name"],
        "email": user["email"],
        "avatar_url": user["avatar_url"],
    }


@app.post("/api/avatar")
async def api_avatar(
    file: UploadFile = File(...), authorization: Optional[str] = Header(default=None)
):
    user = _require_user(authorization)
    content = await file.read()

    try:
        media.validate_avatar(file.content_type or "", content)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    try:
        avatar_url = await media.upload_avatar(user["id"], content, file.filename or "avatar")
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))

    auth.set_avatar(user["id"], avatar_url)
    return {"avatar_url": avatar_url}


@app.get("/api/config")
async def api_config():
    return {
        "vapid_public_key": push.public_key_b64(),
        "password_reset": password_reset.available(),
        "narration": await storyteller.availability(),
        # Storyteller mode on the listener's phone (/api/narrate/page and
        # /api/narrate/lines), wherever full narration isn't set up.
        "phone_narration": True,
    }


@app.post("/api/narrate")
async def api_narrate(payload: dict):
    """Start narrating a chapter (or return the job already narrating it).
    Takes the chapter's URL rather than a list of image URLs, and scrapes
    the images itself — so this can only ever fetch pages from the sites
    the scrapers support, never an arbitrary URL someone posts."""
    chapter_url = str(payload.get("chapter_url") or "")
    _require_absolute_url(chapter_url)
    available = await storyteller.availability()
    if not available["available"] or available.get("base"):
        raise HTTPException(status_code=503, detail="Narration isn't set up on this server.")

    try:
        chapter = await scrape_chapter(chapter_url)
    except Exception:
        raise HTTPException(status_code=502, detail="Couldn't reach that chapter.")
    images = [img for img in chapter.get("images", []) if img]
    if not images:
        raise HTTPException(status_code=404, detail="Couldn't find any pages in that chapter.")

    return await storyteller.start(chapter_url, images, str(payload.get("voice") or ""))


# Storyteller mode on the listener's own phone (ocr-worker.js): the phone
# reads each page's text and speaks it; the server only hands it the page
# images and tidies the text. Both are light enough for the free plan,
# where reading pages here isn't.
PHONE_NARRATION_MAX_LINES = 800
PHONE_NARRATION_MAX_TEXT = 300


def _image_type(data: bytes) -> str:
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"GIF8":
        return "image/gif"
    return "application/octet-stream"


@app.get("/api/trim")
async def api_trim(chapter_url: str = Query(..., description="Chapter to look for scanlation banners in")):
    """Rows of a chapter's first image (top) and last image (bottom) that are
    a scanlation group's banner rather than story (server/banners.py). The
    reader hides them once this answers."""
    _require_absolute_url(chapter_url)
    try:
        result = await banners.trim_for(chapter_url, scrape_cached)
    except Exception as e:
        print(f"[banners] trim failed for {chapter_url}: {e!r}")
        result = {"top": None, "bottom": None}
    # Nothing found may change once the banner is learned from another chapter.
    found = bool(result.get("top") or result.get("bottom"))
    return _cacheable(JSONResponse(content=result), 3600 if found else banners.RECHECK_AFTER)


@app.get("/api/narrate/page")
async def api_narrate_page(
    chapter_url: str = Query(..., description="Chapter whose page to fetch"),
    n: int = Query(..., ge=0, description="Page number, from 0"),
):
    """Page n of a chapter, for the phone to read. Image sites don't let a
    browser read their pixels, so the page comes through here; only pages
    of chapters the scrapers support, never an arbitrary URL."""
    _require_absolute_url(chapter_url)
    try:
        chapter = await scrape_cached(chapter_url)
    except Exception:
        raise HTTPException(status_code=502, detail="Couldn't reach that chapter.")
    images = [img for img in (chapter or {}).get("images", []) if img]
    if n >= len(images):
        raise HTTPException(status_code=404, detail="No such page.")
    data = await storyteller.fetch_image(_get_proxy_client(), images[n], chapter_url)
    if not data:
        raise HTTPException(status_code=502, detail="Couldn't load that page.")
    return Response(content=data, media_type=_image_type(data),
                    headers={"Cache-Control": "public, max-age=86400"})


@app.post("/api/narrate/lines")
async def api_narrate_lines(request: Request):
    """One page's raw OCR lines ([box, text, score], from the phone) as the
    lines to speak: grouped into speech bubbles, run-together words split,
    sound effects and scan credits dropped, each with the top of its bubble
    on the page (y) to scroll to. Text only: milliseconds."""
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Expected JSON.")
    raw = payload.get("lines") if isinstance(payload, dict) else None
    if not isinstance(raw, list) or len(raw) > PHONE_NARRATION_MAX_LINES:
        raise HTTPException(status_code=400, detail="Expected a list of lines.")
    lines = []
    for item in raw:
        try:
            box, text, score = item
            box = [[float(x), float(y)] for x, y in box][:4]
            if len(box) != 4 or not isinstance(text, str) or len(text) > PHONE_NARRATION_MAX_TEXT:
                raise ValueError
            lines.append([box, text, float(score)])
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Each line is [box, text, score].")
    return storyteller.speakable_lines(lines)


@app.get("/api/narrate/{job}")
async def api_narrate_status(job: str):
    state = storyteller.get_job(job)
    if not state:
        raise HTTPException(status_code=404, detail="No such narration.")
    return state


@app.get("/api/narrate/{job}/audio")
async def api_narrate_audio(job: str):
    state = storyteller.get_job(job)
    path = storyteller.audio_path(job)
    if not state or state.get("state") != "done" or not path.exists():
        raise HTTPException(status_code=404, detail="That narration isn't ready.")
    return FileResponse(path, media_type="audio/mpeg", filename=f"narration-{job}.mp3")


@app.post("/api/subscribe")
async def api_subscribe(request: Request, authorization: Optional[str] = Header(default=None)):
    payload = await request.json()
    endpoint = payload.get("endpoint")
    keys = payload.get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(status_code=400, detail="endpoint and keys are required")
    user = _current_user(authorization)
    push.save_subscription(endpoint, keys["p256dh"], keys["auth"], user_id=user["id"] if user else None)
    return {"ok": True, "attached": bool(user)}


@app.post("/api/test-push")
async def api_test_push(request: Request):
    payload = await request.json()
    endpoint = payload.get("endpoint")
    if not endpoint:
        return {"ok": False, "error": "no endpoint"}
    result = push.send(
        endpoint,
        json.dumps(
            {
                "title": "Manga Where",
                "body": "Test notification — it works.",
                "tag": "mangawhere-test",
            }
        ),
    )
    return result


@app.get("/api/captcha")
async def api_captcha():
    return captcha.generate()


@app.post("/api/watch")
async def api_watch(request: Request, authorization: Optional[str] = Header(default=None)):
    user = _require_user(authorization)
    payload = await request.json()
    watch.upsert(user["id"], payload)
    # If this browser already has push enabled, attach it for delivery —
    # tracking works without it, but a device that's already subscribed
    # shouldn't need a separate step to start actually receiving alerts.
    push.attach_subscription(payload.get("endpoint"), user["id"])
    return {"ok": True}


@app.post("/api/unwatch")
async def api_unwatch(request: Request, authorization: Optional[str] = Header(default=None)):
    user = _require_user(authorization)
    payload = await request.json()
    watch.remove(user["id"], payload.get("key") or "")
    return {"ok": True}


@app.post("/api/mark-read")
async def api_mark_read(request: Request, authorization: Optional[str] = Header(default=None)):
    user = _require_user(authorization)
    payload = await request.json()
    watch.mark_read(user["id"], payload.get("key") or "")
    return {"ok": True}


@app.get("/api/list")
async def api_list(authorization: Optional[str] = Header(default=None)):
    user = _require_user(authorization)
    return {"titles": watch.list_for_user(user["id"])}


@app.get("/api/comments")
async def api_comments_list(
    title: str = Query(..., description="Title key the comments belong to"),
    chapter: str = Query(..., description="Chapter key within that title"),
):
    if not title.strip() or not chapter.strip():
        raise HTTPException(status_code=400, detail="title and chapter are required")
    return {"comments": comments.list_for(title.strip(), chapter.strip())}


@app.post("/api/comments")
async def api_comments_post(request: Request, authorization: Optional[str] = Header(default=None)):
    user = _require_user(authorization)
    payload = await request.json()
    title_key = str(payload.get("title") or "")
    chapter_key = str(payload.get("chapter") or "")
    body = str(payload.get("body") or "")
    if not title_key.strip() or not chapter_key.strip() or not body.strip():
        raise HTTPException(status_code=400, detail="title, chapter, and body are required")

    try:
        comment = comments.add(
            user["id"], user["name"], user["avatar_url"], title_key, chapter_key, body
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return comment


# Only the frontend's own files are public. This used to be a StaticFiles
# mount of the whole project directory, which also served the source,
# .git and data/ — including the SQLite replica holding every account.
@app.get("/index.html")
async def serve_index_file():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/sw.js")
async def serve_service_worker():
    return FileResponse(BASE_DIR / "sw.js", media_type="application/javascript")


@app.get("/ocr-worker.js")
async def serve_ocr_worker():
    return FileResponse(BASE_DIR / "ocr-worker.js", media_type="application/javascript")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
