import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from scrapers import find_best_source, lookup_video, scrape_chapter, scrape_series
from server import auth, cache, captcha, comments, db, media, oauth, poller, push, storyteller, turnstile, watch

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    poller.start()
    yield
    poller.stop()


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
        async with httpx.AsyncClient(timeout=20.0) as client:
            upstream = await client.request(request.method, target, headers=headers, content=body)
    except Exception as e:
        return JSONResponse({"error": "upstream failed", "detail": str(e)}, status_code=502)

    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/json"),
    )


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
SERIES_TTL = 30 * 60
FIND_TTL = 30 * 60
FIND_MISS_TTL = 10 * 60  # "not found anywhere" — rechecked sooner


def _cacheable(response: JSONResponse, seconds: int) -> JSONResponse:
    # Lets the reader's browser (and any CDN in front of this server) reuse
    # the answer too, instead of asking again on every page view.
    response.headers["Cache-Control"] = f"public, max-age={seconds}"
    return response


@app.get("/api/scrape")
async def api_scrape(url: str = Query(..., description="Chapter URL to scrape")):
    _require_absolute_url(url)
    kaliscan = "kaliscan" in url.lower()

    try:
        payload = await cache.cached(
            "chapter", url, KALISCAN_CHAPTER_TTL if kaliscan else CHAPTER_TTL,
            lambda: scrape_chapter(url),
            keep=lambda p: bool(p and p.get("images")),
        )
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
        _cacheable(response, 300 if kaliscan else 3600)
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


@app.get("/api/find")
async def api_find(title: str = Query(..., description="Manga title to find a reading source for")):
    if not title or not title.strip():
        raise HTTPException(status_code=400, detail="Missing title query parameter")

    try:
        result = await cache.cached(
            "find", title, FIND_TTL, lambda: find_best_source(title.strip()),
            miss_ttl=FIND_MISS_TTL,
        )
    except Exception:
        raise HTTPException(status_code=502, detail="Couldn't search for that title right now.")

    if not result:
        raise HTTPException(
            status_code=404, detail="Couldn't find this title on any of our sources."
        )

    response = JSONResponse(content=result)
    response.headers["Referrer-Policy"] = "no-referrer"
    return _cacheable(response, 300)


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
        "narration": await storyteller.availability(),
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


app.mount("/", StaticFiles(directory=str(BASE_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
