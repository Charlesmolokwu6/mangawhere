# mangawhere

## Running it

Everything — the frontend (`index.html`), accounts, push notifications,
the ToonGod/Asura Scans reader, and the CORS proxy the frontend's own
client-side API calls go through — is served by one Python app,
`main.py`.

```
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port 8000
```

Then open `http://localhost:8000/`.

## Deploying to Render

`render.yaml` defines a free-tier web service. In the Render dashboard:
New → Blueprint → point it at this repo, and it picks up `render.yaml`
automatically.

**A real caveat on the free tier:** without `TURSO_DATABASE_URL` set (see
below), the app stores accounts, watch lists, and comments in a local
SQLite file (`data/`), and Render's free plan has no persistent disk — that
file is wiped on every redeploy. Fine for trying things out; set up Turso
(free, a few minutes) for anything you don't want to lose.

Optional environment variables (see `render.yaml`):
- `VAPID_CONTACT_EMAIL` — contact address push services can reach if
  your deployment misbehaves. Defaults to a placeholder; set a real one.
- `POLL_INTERVAL_SECONDS` — how often the background job checks tracked
  titles for new chapters. Defaults to 1800 (30 minutes).
- `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET`
  — enable profile picture uploads (see below). Everyone gets a default
  initials avatar with none of these set; uploading a custom photo just
  returns a clear error until all three are present.
- `YOUTUBE_COOKIES` — makes the video-link search flow's audio
  transcription reliable (see below). Left unset, that step still runs but
  frequently gets refused by YouTube's bot-check, and the app falls back to
  the paste-caption box.
- `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN` — move accounts/watch
  lists/comments off the ephemeral disk and onto a real database (see
  below). Strongly recommended; left unset, storage behaves exactly as it
  always has.

## Persistent storage (Turso)

`server/db.py` talks to [Turso](https://turso.tech) — a hosted,
SQLite-compatible database with a real free tier — as an "embedded
replica": a local file that mirrors a remote primary, so reads stay just
as fast as plain SQLite while writes are transparently forwarded to, and
made durable on, the remote database. Every existing query in
`auth.py`/`watch.py`/`comments.py`/`push.py`/`poller.py` runs completely
unchanged either way.

To turn it on:
1. Create a free account at [turso.tech](https://turso.tech), or install
   their CLI (`curl -sSfL https://get.tur.so/install.sh | bash`) and run
   `turso auth signup`.
2. Create a database: `turso db create mangawhere`.
3. Get its URL: `turso db show mangawhere --url` (starts with `libsql://`).
4. Create a token: `turso db tokens create mangawhere`.
5. Set those as `TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` in Render's
   environment variables for this service, then redeploy.

With both set, the app pulls down the real remote database on startup
(`init_db()`), so a redeploy no longer means starting over. Leave either
one unset and nothing changes — same local SQLite file as before this
existed.

## Profile pictures

Uploaded avatars can't live on Render's own disk for the same reason the
database can't — it's wiped on every redeploy. `POST /api/avatar`
instead uploads to [Cloudinary](https://cloudinary.com) (free tier: 25GB
storage, 25GB bandwidth/month), which is real persistent storage and
handles image hosting for you.

To turn it on:
1. Create a free Cloudinary account.
2. From its dashboard, copy the **Cloud name**, **API Key**, and **API
   Secret**.
3. Set them as `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, and
   `CLOUDINARY_API_SECRET` in Render's environment variables for this
   service, then redeploy.

Only the URL Cloudinary returns is stored in the app's own database
(`users.avatar_url`) — the image bytes themselves never touch the
ephemeral disk.

## Video-link audio transcription

When someone pastes a TikTok/YouTube link and the caption alone doesn't
name the manga, the backend also tries the upload description and (for
clips under 3 minutes) a speech-to-text transcript of the audio, via
yt-dlp and faster-whisper (`scrapers/video.py`). The description fetch
works without any setup. The audio *download* step, though, is what
YouTube's "Sign in to confirm you're not a bot" anti-bot check targets —
without a real signed-in session it fails intermittently, and the lookup
just falls back to the paste-caption box when that happens.

`YOUTUBE_COOKIES` fixes that: with a real account's session attached, the
request looks like a signed-in browser instead of a bare script, and
mostly avoids the challenge.

To set it up:
1. **Use a secondary/throwaway Google account for this, not your main
   one.** This exports that account's live session — automated use of it
   isn't something to risk a personal account over, and it's what YouTube's
   Terms of Service actually intend this check to discourage.
2. Sign into YouTube with that account in a normal browser, then export
   its cookies in Netscape format — e.g. the
   [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
   extension, from a youtube.com tab.
3. Paste the exported file's full contents as `YOUTUBE_COOKIES` in
   Render's environment variables for this service, then redeploy.

The cookie file is only ever read from that environment variable, written
to a private temp file at runtime, and handed straight to yt-dlp — it's
never logged, and never appears in any API response. It'll need
re-exporting occasionally once the session it holds expires or is signed
out.

## Tests

```
python -m unittest discover -s tests
```
