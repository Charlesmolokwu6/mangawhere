from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from scrapers import scrape_chapter

BASE_DIR = Path(__file__).resolve().parent
app = FastAPI(title="MangaWhere scraper API")


@app.get("/")
def serve_index():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/api/scrape")
async def api_scrape(url: str = Query(..., description="Chapter URL to scrape")):
    if not url:
        raise HTTPException(status_code=400, detail="Missing url query parameter")

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise HTTPException(status_code=400, detail="url must be an absolute http(s) URL")

    payload = await scrape_chapter(url)
    host = parsed.netloc.lower().replace("www.", "")

    if "toongod" in host:
        payload["domain"] = "toongod"
    elif "asurascans" in host or "asura" in host:
        payload["domain"] = "asurascans"

    payload["chapter_url"] = url
    payload["images"] = [img for img in payload.get("images", []) if img]

    response = JSONResponse(content=payload)
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


app.mount("/", StaticFiles(directory=str(BASE_DIR), html=True), name="static")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
