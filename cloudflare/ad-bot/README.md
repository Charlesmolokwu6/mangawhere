# MangaWhere ad-control Telegram bot

A tiny Cloudflare Worker that lets the site owner switch ads off (and back on)
from a private Telegram bot. It's always on, free, and completely separate from
the backend server, so it keeps working even if the backend is down.

## What it does

Open the bot in Telegram and you get buttons:

- **🔴 Turn OFF reader ads** — hides only the ads shown while reading a chapter.
- **🔴 Turn OFF all ads** — hides every ad (reader, browse grid, home, detail).
- **🟢 Turn everything ON**
- **📊 Status** — shows what's off and for how long.

After tapping a "turn off" button it asks for how long: **24 hours · 1 week ·
1 month · Indefinitely**. Only the owner (the configured Telegram id) can use it;
everyone else is ignored.

The website reads the current on/off state from this Worker's `/ad-state`
endpoint when a page loads. If the Worker can't be reached, ads show as normal
(fail-open — a problem here never leaves the site with no content).

## One-time setup

### 1. Create the bot

In Telegram, message **@BotFather** → `/newbot` → give it a name and a username
ending in `bot`. It hands you a **token** like `123456:ABC-...`. Keep it secret.

Get your own numeric id from **@userinfobot** (it replies with a number). That's
your `ADMIN_ID` — not secret.

### 2. Deploy the Worker

```sh
cd cloudflare/ad-bot
npx wrangler kv namespace create AD_KV     # prints an id — paste it into wrangler.jsonc
# edit wrangler.jsonc: set the KV id and your ADMIN_ID
npx wrangler secret put BOT_TOKEN          # paste the @BotFather token
npx wrangler secret put WEBHOOK_SECRET     # paste any random string, e.g. a long password
npx wrangler deploy                        # prints the Worker URL, e.g. https://mangawhere-adbot.<you>.workers.dev
```

(Prefer the dashboard? Create a Worker, paste `ad-bot-worker.js`, add a KV
binding named `AD_KV`, add the `ADMIN_ID` variable, and add `BOT_TOKEN` and
`WEBHOOK_SECRET` as secrets.)

### 3. Point Telegram at the Worker

Tell Telegram to send updates to the Worker, protected by the same secret:

```sh
curl "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://<worker-url>/tg&secret_token=<WEBHOOK_SECRET>"
```

### 4. Point the website at the Worker

In `index.html`, set:

```js
var AD_BOT_URL = "https://<worker-url>";
```

Push to `main`; GitHub Pages republishes and the site starts reading the switch.
Until this is set, ads always show (nothing changes).

## Test

Message your bot `/start`, tap **Turn OFF all ads → 24 hours**, reload the site —
the ads should be gone. Tap **Turn everything ON**, reload — they're back.
