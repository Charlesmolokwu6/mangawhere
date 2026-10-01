# Scaling plan: ~100,000 active users

Where MangaWhere stands today, what breaks first as traffic grows, and the
order to fix it in. Numbers marked *measured* were measured on this
codebase; the rest are estimates to check against real traffic.

## What's already done

- **Shared scraper cache** (`server/cache.py`). A title lookup, chapter
  list or chapter's pages is fetched from the source sites once, then
  reused for every reader until it expires: 30 min for lookups and chapter
  lists (so new chapters still show up), 6 h for a chapter's pages (30 min
  for kaliscan, whose image links expire). *Measured:* a Gosu lookup took
  16 s the first time and 0.004 s for every reader after that, including
  after a server restart. Failed scrapes aren't remembered, so a site
  that's briefly down is retried. Many readers asking for the same title
  at once share one fetch.
- **Source fallback.** If a chapter won't load from one site, the reader
  switches to the next site that has it.
- **Narration can't crash the site.** It's off on Render unless pointed at a
  separate machine (`NARRATION_URL`), because it needs ~600 MB *measured*.

## Step 1: before launch (required)

1. **Turso for the database.** Set `TURSO_DATABASE_URL` and
   `TURSO_AUTH_TOKEN` (README → Persistent storage). Without it, accounts,
   watch lists, comments and the scraper cache live on Render's disk and
   are wiped on every deploy. With it, the cache is also shared between
   server instances.
2. **Render paid plan.** The free plan sleeps after ~15 minutes idle (the
   first visitor after that waits for it to boot) and has 512 MB of memory.
   Start with one paid instance, watch memory and response times in
   Render's metrics, and add instances as traffic grows. Every instance
   shares the Turso-backed cache.
3. **Cloudflare in front of the site** (README → Bot detection → Cloudflare
   in front of the whole site). Needs your own domain. Gives DDoS
   protection, bot blocking, and edge caching: `/api/find`, `/api/series`
   and `/api/scrape` now send `Cache-Control: public, max-age=…`, so a
   Cloudflare cache rule for `/api/*` lets the edge answer repeat requests
   without reaching Render at all.
4. **Frontend on a CDN.** `index.html` is a single static file; serve it
   from Cloudflare Pages or GitHub Pages and point it at the API with
   `?api=` / `API_BASE`, so page loads don't cost server time.

## Step 2: as traffic grows

5. **Run the new-chapter poller once, not once per instance.**
   `server/poller.py` already checks each watched title once per cycle,
   however many people follow it. But every server instance starts its own
   poller, so with two or more instances readers get each notification
   twice or more. Before adding a second instance, run the poller on only
   one (an env var such as `RUN_POLLER=true` on one instance) or as a
   separate Render background worker. With thousands of distinct watched
   titles, also check that one cycle still finishes inside
   `POLL_INTERVAL_SECONDS`.
6. **Comizy image proxy bandwidth.** comizy.io's images must go through this
   server (they check the Referer). At scale that is real bandwidth. Prefer
   other sources for the same chapter when they're equally up to date, or
   move the proxy to a Cloudflare Worker (free tier: 100,000 requests/day;
   check current limits).
7. **Rate limits.** Add per-IP limits on `/api/find` and `/api/video-lookup`
   (the expensive ones), so one script can't exhaust the scrapers. Cloudflare
   rate-limiting rules can do this without code.
8. **Monitoring.** Watch for sources that start failing (`[toongod]`,
   `[match] no candidates found` lines in the logs). A source site changing
   its layout silently drops it from results; the cache then hides that
   until entries expire.

## Step 3: narration at scale

The design that holds up at 100k users is **generate once per chapter,
store it, serve it to everyone**:

| Part | Choice | Cost at scale |
|---|---|---|
| Script (what to say) | Gemini Flash reading the page images | Free tier for testing; paid per chapter at scale |
| Voice | Kokoro (Apache 2.0) | Free licence; needs CPU: ~1× real time on 4 cores *measured* |
| Storage + playback | Cloudflare R2 | 10 GB + 10M plays/month free, bandwidth free |
| Worker machine | Separate server running the narration copy of this app | Oracle free tier (may be reclaimed when idle) or a small paid VPS |

Cost grows with the number of *distinct chapters narrated*, not with
listeners. Start with the most-read series only, and queue requests so
the worker never runs more chapters at once than it has CPU for.

## Risks to plan for

- **Source sites blocking the server.** Caching cuts traffic to them
  sharply, but a busy site still sends regular requests. Expect to lose
  sources occasionally and to need new ones.
- **Copyright.** At 100k users the site is visible to publishers. Serving
  scanlations already carries DMCA risk; generating and storing narrated
  audio of those chapters adds to it. Decide how you'll handle takedown
  requests before launch.
