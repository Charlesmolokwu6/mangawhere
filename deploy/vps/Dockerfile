# MangaWhere's server on your own VPS (see deploy/vps/README.md). Built
# from this checkout, so `git pull && docker compose up -d --build` is a
# deploy.
# Debian 12: the newest the pinned Playwright (1.47) installs its browser
# libraries on.
FROM python:3.11-slim-bookworm

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# The headless browser some scrapers fall back to, with the system
# libraries it needs, somewhere the app's user can read it.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps chromium && chmod -R a+rx /ms-playwright

COPY . .
RUN useradd -m -u 1000 app && mkdir -p data && chown -R app:app /app
USER app
ENV HOME=/home/app \
    PYTHONUNBUFFERED=1

EXPOSE 8000
# Only Caddy can reach this port (it isn't published), so the address
# Caddy forwards is the visitor's real one.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
