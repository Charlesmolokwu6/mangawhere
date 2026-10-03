# mangawhere

## Running it

Everything — the frontend (`index.html`), accounts, push notifications,
the manga reader and its narration, and the CORS proxy the frontend's own
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
- `TURNSTILE_SECRET_KEY` — verifies Cloudflare Turnstile on sign-up (see
  below). Left unset, sign-up keeps using the custom SVG captcha.
- `GOOGLE_CLIENT_ID` — enables "Continue with Google" (see below). Also
  needed in `index.html` (it's public). Left unset, that button just
  doesn't render.
- `OLLAMA_HOST`, `NARRATION_MODEL` — the optional AI storyteller for
  chapter narration (see below). Left unset, narration just reads the
  dialogue aloud.
- `FACEBOOK_APP_ID`, `FACEBOOK_APP_SECRET` — enables "Continue with
  Facebook" (see below). The app ID is also needed in `index.html` (public);
  the secret stays here only. Left unset, that button just doesn't render.

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

## Storyteller mode (chapter narration)

The reader has a floating **🎧 Listen** button that turns the current
chapter into an MP3 — entirely free, no paid API (`server/storyteller.py`):

1. **OCR** ([RapidOCR](https://github.com/RapidAI/RapidOCR), runs locally on
   the CPU) reads every speech bubble, in reading order. Sound effects
   (KRR, WOO…) are recognised and left out of the dialogue.
2. **The script** is built from that text alone — every line, in order,
   word for word, with a short attribution ("someone whispers"). If an
   [Ollama](https://ollama.com) server is reachable, a small local AI model
   picks how each line is delivered (shouts, whispers, asks…) from a fixed
   list; without it, punctuation decides. The model is never allowed to
   write text itself: letting llama3.2:3b write the narration freely was
   tried, and it invented dialogue, creatures and settings that weren't in
   the chapter.
3. **Voice**: [Kokoro](https://huggingface.co/hexgrad/Kokoro-82M), an
   open-source voice model (Apache 2.0: free, including commercial use, with
   no per-use limits) records it on the narration machine itself.
   Listeners pick the narrator: Fenrir, Echo or Eric (male), Emma or
   Jessica (female); the reader remembers their choice. Each chapter's
   script is shared, and a voice is only recorded for a chapter the first
   time someone picks it. The model (~330MB, plus 28MB of voices) downloads
   into `data/kokoro/` on first use; set `KOKORO_MODEL=kokoro-v1.0.int8.onnx`
   for a smaller (~90MB) but slower version.

A long chapter takes a few minutes the first time (it runs as a background
job with a progress bar); after that the MP3 is cached in
`data/narration/`.

Why OCR rather than a vision model like moondream: moondream was tried
first on real pages, and it paraphrased dialogue, invented lines that
weren't on the page, and misdescribed scenes. OCR reads what's actually
written.

**Running it locally, with the AI storyteller:**
```
pip install -r requirements.txt
ollama pull llama3.2:3b      # ~2GB, one-time; needs Ollama installed and running
uvicorn main:app --port 8000
```

**On Render's free plan, narration is off.** Reading a chapter's pages
peaks at ~600MB of memory (measured), over the free plan's 512MB — running
it there would crash the whole site, not just narration. So on Render
(which sets `RENDER=true`) the Listen button doesn't appear unless
narration is pointed somewhere else:

1. Run a second copy of this app on a machine with at least ~2GB of RAM
   (4GB+ with Ollama) — e.g. Oracle Cloud's Always Free VM, a small VPS,
   or your own computer behind a Cloudflare Tunnel. Same
   `pip install -r requirements.txt` and `uvicorn main:app`; add Ollama
   there if you want it.
2. Set `NARRATION_URL` on Render to that copy's public address (e.g.
   `https://narrator.example.com`) and redeploy.

The reader then sends narration requests straight to that machine;
everything else keeps running on Render.

Environment variables (all optional):
- `NARRATION_URL` — address of another deployment of this app that does
  the narrating (see above). Set this on Render.
- `NARRATION_ENABLED` — `true`/`false` to force narration on or off on
  this server. Unset, it's on everywhere except Render.
- `OLLAMA_HOST` — where Ollama is. Defaults to `http://localhost:11434`.
- `NARRATION_MODEL` — which Ollama model picks each line's delivery.
  Defaults to `llama3.2:3b`.

The Listen button only appears when the server reports narration as
available (`/api/config`), so a server without these dependencies just
doesn't show it.

## Password reset (email)

The sign-in page has a "Forgot password?" link that emails a one-time
link (valid 1 hour) to choose a new password (`server/password_reset.py`).
Saving it signs the reader in and signs them out on every other device.
It also lets people who signed up with Google/Facebook add a password.
The link only appears once email sending is set up.

Any email provider that offers SMTP works. Free options include Brevo
(a free plan with a daily sending limit), Gmail (with an "app password",
fine for small volumes), Resend and Amazon SES; check each one's current
free limits. For lots of users, use a provider with your own domain
verified, so the emails don't land in spam.

Set these in Render's environment variables, then redeploy:
- `SMTP_HOST`, e.g. `smtp-relay.brevo.com` or `smtp.gmail.com`
- `SMTP_PORT`, usually `587` (or `465`)
- `SMTP_USERNAME`, `SMTP_PASSWORD`: from the provider (for Gmail, an app
  password, never your normal one)
- `MAIL_FROM`, e.g. `MangaWhere <no-reply@yourdomain.com>`; it must be a
  sender the provider has verified
- `RESET_LINK_ORIGINS` (optional): extra site addresses the reset link may
  point to, comma-separated, e.g. a custom domain. GitHub Pages and the
  Render address are allowed already.

## Advertising

Ads are set in `index.html`'s `ADS` block (near `AMAZON_TAG`): paste an
ad network's code for each place you want ads, and leave the rest empty.

| Place | Where it shows |
|---|---|
| `home` | Home page, below Trending |
| `detail` | A title's page, below its chapter list |
| `readerPages` | Between a chapter's pages, after every `AD_EVERY_PAGES` (default 4) pages |
| `readerEnd` | End of a chapter, just above Prev / Next |

Every ad runs inside its own sandboxed frame (`adFrame()`), which matters
for three reasons: network code that uses `document.write` (Adsterra's
banners do) can't wipe the page; ad scripts can't read the page or the
reader's sign-in token; and frames load only when scrolled near, so ads
never slow down chapter pages.

**Adsterra** accepts sites with mature titles (Google AdSense doesn't,
since `HIDE_ADULT` is off; it risks the whole AdSense account).
1. Sign up as a publisher at [adsterra.com](https://adsterra.com) and add
   your site.
2. Create ad units. **Banner** units fit these places best: 300×250 for
   `home`/`detail`/`readerPages`, 320×50 or 468×60 for `readerEnd`. Native
   Banners work too (set `AD_NATIVE_HEIGHT` to their height).
3. For each unit, copy its code ("Get code") and paste it between the
   quotes for the place you want it, then push. The frame sizes itself
   from the `width`/`height` in a banner's code. Write each closing
   script tag in the pasted code as `<\/script>`: a plain one would end
   `index.html`'s own script block and break the page. The live units
   (`ADSTERRA_300x250`, `ADSTERRA_320x50`) show the format.

Popunder and Social Bar units aren't supported on purpose: they run
across the whole page (not in a frame), cover content, and are the formats
readers most dislike.

## Sign in with Google / Facebook

`server/oauth.py` verifies whichever token the provider's own JS SDK hands
the frontend — no server-side redirect flow, which fits this app's
static-frontend-plus-API split far better than the authorization-code
dance. Accounts link by email: signing in with Google using the same
address an existing password (or Facebook) account already uses attaches
that provider to the same account instead of creating a duplicate. Both
providers are entirely independent — set up one, both, or neither.

**Google** (free):
1. Go to [console.cloud.google.com](https://console.cloud.google.com) →
   **APIs & Services** → **Credentials** → **Create Credentials** →
   **OAuth client ID** → type **Web application**.
2. Under **Authorized JavaScript origins**, add every origin the frontend
   is actually served from (e.g. `https://charlesmolokwu6.github.io` and,
   once it exists, your custom domain).
3. Copy the **Client ID** it gives you (no secret needed — this flow never
   uses one). Paste it as `index.html`'s `GOOGLE_CLIENT_ID`, and set the
   same value as `GOOGLE_CLIENT_ID` in Render's environment variables,
   then redeploy both.

**Facebook** (free):
1. Go to [developers.facebook.com](https://developers.facebook.com) →
   **Create App** → choose **Consumer** → add the **Facebook Login**
   product.
2. In **App Settings** → **Basic**, add your site's domain(s) under
   **App Domains**.
3. Copy the **App ID** and **App Secret**. The App ID is public — paste it
   as both `index.html`'s `FACEBOOK_APP_ID` and Render's `FACEBOOK_APP_ID`.
   The App Secret is private — set it only as Render's
   `FACEBOOK_APP_SECRET`. Redeploy both.
4. While the app is in **Development mode**, only accounts added as
   testers/admins under **Roles** can actually sign in with it. Facebook's
   **App Review** is what lifts that restriction for real users — plan for
   that step before expecting it to work for the public.

**Apple ("Sign in with Apple") isn't implemented yet.** Verifying its
identity token needs real JWT/JWKS handling — a different shape of problem
from the HTTP-based checks Google and Facebook use here — and it requires
a **paid Apple Developer Program membership ($99/year)** to even get the
credentials it needs, so there was no point building it ahead of that
being confirmed as a cost worth taking on.

## Bot detection

Sign-up already has three layers: a honeypot field, a too-fast-to-be-human
timing check, and a custom distorted-text captcha (`server/captcha.py`).
Cloudflare adds a fourth, stronger option — in two independent pieces,
since only one of them needs a domain you own.

### Cloudflare Turnstile (sign-up form)

[Turnstile](https://developers.cloudflare.com/turnstile/) is Cloudflare's
free CAPTCHA replacement — usually no interaction needed at all, just a
background check. Once configured, it **replaces** the custom captcha on
sign-up rather than stacking with it (`server/turnstile.py`,
`server/auth.py`'s `register()`); the honeypot and timing checks stay on
regardless. Doesn't need a custom domain — it works fine against the
`onrender.com`/GitHub Pages URLs as they are today.

To turn it on:
1. In the [Cloudflare dashboard](https://dash.cloudflare.com), go to
   **Turnstile** → **Add widget**. Give it the domain the frontend is
   actually served from (e.g. your `github.io` page, or `localhost` while
   testing), and leave the widget mode on **Managed**.
2. It gives you two keys. The **site key** is public — paste it into
   `index.html`'s `TURNSTILE_SITE_KEY` near the top of the `<script>`
   block, then redeploy the frontend (push to GitHub Pages).
3. The **secret key** is private — set it as `TURNSTILE_SECRET_KEY` in
   Render's environment variables for the backend service, then redeploy.

Both need to be set for Turnstile to activate; either one missing and
sign-up falls back to the custom captcha exactly as it always has.

### Cloudflare in front of the whole site (needs a domain)

This is the heavier option — routing all traffic through Cloudflare's edge
(WAF, Bot Fight Mode, DDoS protection) before it ever reaches Render — and
it specifically requires a domain you control, since it works by pointing
that domain's DNS through Cloudflare. The current `mangawhere.onrender.com`
address can't be proxied this way. Nothing to configure in this repo until
you have one; when you do, the setup is:

1. Buy or already own a domain, and add it to a Cloudflare account (free
   plan is fine) — Cloudflare becomes its DNS provider.
2. In Render, add that domain as a **Custom Domain** on the `mangawhere`
   service ([Render's docs](https://render.com/docs/custom-domains)) —
   it'll give you a CNAME target to point at.
3. In Cloudflare's DNS settings, add that CNAME record with the proxy
   status **on** (the orange cloud, not grey/DNS-only) — that's what
   actually routes traffic through Cloudflare's edge instead of straight to
   Render.
4. Set Cloudflare's SSL/TLS mode to **Full (strict)** — Render already
   serves real, valid HTTPS, so Cloudflare can verify it end-to-end.
5. Turn on **Bot Fight Mode** (free) or a **WAF** rule, under Cloudflare's
   Security settings, for the actual bot-blocking.

## Tests

```
python -m unittest discover -s tests
```
