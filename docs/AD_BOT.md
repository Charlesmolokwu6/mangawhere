# Ad-control Telegram bot

A private Telegram bot, served by this backend (`server/adcontrol.py`), that
lets the site owner switch ads off and back on. The current on/off state is
stored in the database and reported in `/api/config`, which the front end reads
on load to decide whether to show ads.

## What it does

Open the bot in Telegram and tap buttons:

- **🔴 Turn OFF reader ads** — hides only the ads shown while reading a chapter.
- **🔴 Turn OFF all ads** — hides every ad (reader, browse grid, home, detail).
- **🟢 Turn everything ON**
- **📊 Status** — what's off and for how long.

After a "turn off" button it asks for how long: **24 hours · 1 week · 1 month ·
Indefinitely**. Only the configured owner can use it; everyone else is ignored.

If the backend is unreachable, the page shows ads as normal (fail-open), and the
last answer is cached in the browser so a reload applies instantly.

## Setup

### 1. Create the bot

In Telegram, message **@BotFather** → `/newbot`. It gives you a **token**. Get
your numeric id from **@userinfobot**.

### 2. Set the environment variables on the backend service

In the Render service's **Environment**:

| Variable | Value |
| --- | --- |
| `TELEGRAM_BOT_TOKEN` | the @BotFather token |
| `TELEGRAM_ADMIN_ID` | your numeric Telegram id |
| `TELEGRAM_WEBHOOK_SECRET` | any random string (a long password) |

Save and let it redeploy.

### 3. Point Telegram at the backend

Tell Telegram to send updates to this backend, protected by the same secret:

```sh
curl "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook?url=https://<backend-url>/api/telegram&secret_token=<WEBHOOK_SECRET>"
```

(`<backend-url>` is this service's address, e.g. `https://mangawhere-y9lw.onrender.com`.)

### 4. Test

Message the bot `/start`, tap **Turn OFF all ads → 24 hours**, reload the site —
ads should be gone. Tap **Turn everything ON**, reload — they're back.

To stop the bot receiving updates: `.../deleteWebhook`.
