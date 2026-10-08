# MangaWhere's storyteller server, for a free Hugging Face Space (Docker,
# 16GB RAM): the Listen button's OCR + voice needs ~600MB, more than
# Render's free plan has. Paste this file into the Space as "Dockerfile";
# it builds the app straight from GitHub, with narration on and the
# site's background jobs off. Then set Render's NARRATION_URL to the
# Space's address (https://<user>-<space>.hf.space). See README.md.
# Debian 12: Playwright 1.47 (requirements.txt) has no browser deps for 13.
FROM python:3.11-slim-bookworm

RUN apt-get update && apt-get install -y --no-install-recommends git libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# Spaces run the container as user 1000.
RUN useradd -m -u 1000 user
ENV HOME=/home/user \
    NARRATION_ENABLED=true \
    NARRATION_ONLY=true \
    PLAYWRIGHT_BROWSERS_PATH=/home/user/pw

# "Factory rebuild" in the Space's settings pulls the latest code.
ARG MANGAWHERE_REF=main
RUN git clone --depth 1 --branch "$MANGAWHERE_REF" https://github.com/Charlesmolokwu6/mangawhere.git /home/user/app \
    && pip install --no-cache-dir -r /home/user/app/requirements.txt \
    && python -m playwright install --with-deps chromium \
    && cd /home/user/app \
    && python -c "from server import storyteller as s; s._download(s.KOKORO_MODEL); s._download(s.KOKORO_VOICES_FILE)" \
    && chown -R user:user /home/user

USER user
WORKDIR /home/user/app
EXPOSE 7860
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "7860"]
