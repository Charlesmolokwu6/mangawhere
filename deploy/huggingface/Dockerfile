# MangaWhere's server on a Hugging Face Space (Docker, free CPU basic:
# 2 cores, 16GB memory). The Space holds only this file; the code comes
# from GitHub, fresh on every rebuild (Settings > Factory rebuild).
# Debian 12: the newest the pinned Playwright (1.47) installs its browser
# libraries on.
FROM python:3.11-slim-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates \
 && rm -rf /var/lib/apt/lists/*

# Changes whenever GitHub's main branch does, so a rebuild never reuses an
# old copy of the code.
ADD https://api.github.com/repos/Charlesmolokwu6/mangawhere/git/refs/heads/main /tmp/version.json
RUN git clone --depth 1 https://github.com/Charlesmolokwu6/mangawhere.git /app

WORKDIR /app
RUN pip install --no-cache-dir -r requirements.txt

# The headless browser some scrapers fall back to, with the system
# libraries it needs, somewhere the app's user can read it.
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN playwright install --with-deps chromium && chmod -R a+rx /ms-playwright

# Spaces run as user 1000.
RUN useradd -m -u 1000 user && chown -R user:user /app
USER user
ENV HOME=/home/user \
    PYTHONUNBUFFERED=1 \
    SERVER_OCR=1 \
    HEAVY_SLOTS=2 \
    NARRATION_ENABLED=0

EXPOSE 7860
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860", "--proxy-headers", "--forwarded-allow-ips", "*"]
