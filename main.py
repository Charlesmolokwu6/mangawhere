import json
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from scrapers import find_best_source, scrape_chapter, scrape_series
from server import auth, captcha, comments, db, poller, push, watch

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
PROXY_UA = "MangaWhere/1.0 (+https://mangawhere.example)"

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
    if parsed.hostname not in PROXY_ALLOWED_HOSTS:
        return JSONResponse(
            {"error": "host not allowed", "host": parsed.hostname}, status_code=403
        )

    headers = {
        "User-Agent": PROXY_UA,
        "Accept": "application/json, text/xml, application/xml, */*",
    }
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


@app.get("/api/scrape")
async def api_scrape(url: str = Query(..., description="Chapter URL to scrape")):
    _require_absolute_url(url)

    try:
        payload = await scrape_chapter(url)
    except Exception:
        raise HTTPException(
            status_code=502, detail="Couldn't reach that page — the site may be blocking us."
        )

    payload["chapter_url"] = url
    payload["images"] = [img for img in payload.get("images", []) if img]

    response = JSONResponse(content=payload)
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/api/series")
async def api_series(url: str = Query(..., description="Series page URL to list chapters for")):
    _require_absolute_url(url)

    try:
        payload = await scrape_series(url)
    except Exception:
        raise HTTPException(
            status_code=502, detail="Couldn't reach that page — the site may be blocking us."
        )

    response = JSONResponse(content=payload)
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@app.get("/api/find")
async def api_find(title: str = Query(..., description="Manga title to find a reading source for")):
    if not title or not title.strip():
        raise HTTPException(status_code=400, detail="Missing title query parameter")

    try:
        result = await find_best_source(title.strip())
    except Exception:
        raise HTTPException(status_code=502, detail="Couldn't search for that title right now.")

    if not result:
        raise HTTPException(
            status_code=404, detail="Couldn't find this title on ToonGod or Asura Scans."
        )

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
    result = auth.register(payload, captcha.verify)
    _attach_endpoint_from_payload(result, payload)
    return JSONResponse(result)


@app.post("/api/login")
async def api_login(request: Request):
    payload = await request.json()
    result = auth.login(payload)
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
    return {"signed_in": True, "tracked": watch.count_for_user(user["id"])}


@app.get("/api/config")
async def api_config():
    return {"vapid_public_key": push.public_key_b64()}


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
        comment = comments.add(user["id"], user["name"], title_key, chapter_key, body)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return comment


app.mount("/", StaticFiles(directory=str(BASE_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
