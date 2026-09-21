import json
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from scrapers import find_best_source, scrape_chapter, scrape_series
from server import auth, captcha, db, poller, push, watch

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="MangaWhere scraper API")

# No cookies are used (auth is a bearer token the client stores itself), so
# a wildcard origin doesn't expose this to CSRF — it only lets a
# separately-hosted frontend (the "?api=" deployment mode) call in.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
async def on_startup():
    db.init_db()
    poller.start()


@app.on_event("shutdown")
async def on_shutdown():
    poller.stop()


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


@app.get("/")
def serve_index():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/api/scrape")
async def api_scrape(url: str = Query(..., description="Chapter URL to scrape")):
    _require_absolute_url(url)

    try:
        payload = await scrape_chapter(url)
    except Exception:
        raise HTTPException(
            status_code=502, detail="Couldn't reach that page — the site may be blocking us."
        )
    host = urlparse(url).netloc.lower().replace("www.", "")

    if "toongod" in host:
        payload["domain"] = "toongod"
    elif "asurascans" in host or "asura" in host:
        payload["domain"] = "asurascans"

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


@app.post("/api/register")
async def api_register(request: Request):
    payload = await request.json()
    return JSONResponse(auth.register(payload, captcha.verify))


@app.post("/api/login")
async def api_login(request: Request):
    payload = await request.json()
    return JSONResponse(auth.login(payload))


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
async def api_subscribe(request: Request):
    payload = await request.json()
    endpoint = payload.get("endpoint")
    keys = payload.get("keys") or {}
    if not endpoint or not keys.get("p256dh") or not keys.get("auth"):
        raise HTTPException(status_code=400, detail="endpoint and keys are required")
    push.save_subscription(endpoint, keys["p256dh"], keys["auth"])
    return {"ok": True}


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


app.mount("/", StaticFiles(directory=str(BASE_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
