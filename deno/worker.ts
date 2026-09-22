/**
 * Manga Where — proxy + notifications, one file.
 *
 * Replaces both the old proxy and the Python backend. Deno Deploy gives
 * us a database (Deno KV) and a scheduler (Deno.cron) built in, so there
 * is no Postgres to provision and no separate cron service to run.
 *
 * Deploy: paste this into your Deno playground and hit Save & Deploy.
 * Then set three environment variables (Settings -> Environment):
 *   VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY, VAPID_CLAIM_EMAIL
 *
 * The URL stays the same as your existing proxy, so the front end keeps
 * working without changes.
 */

import webpush from "npm:web-push@3.6.7";

// ---------------------------------------------------------------- config

const VAPID_PUBLIC_KEY = Deno.env.get("VAPID_PUBLIC_KEY") ?? "";
const VAPID_PRIVATE_KEY = Deno.env.get("VAPID_PRIVATE_KEY") ?? "";
const VAPID_CLAIM_EMAIL = Deno.env.get("VAPID_CLAIM_EMAIL") ?? "mailto:you@example.com";
const PUSH_READY = Boolean(VAPID_PUBLIC_KEY && VAPID_PRIVATE_KEY);

if (PUSH_READY) {
  webpush.setVapidDetails(VAPID_CLAIM_EMAIL, VAPID_PUBLIC_KEY, VAPID_PRIVATE_KEY);
}

// Proxy allowlist. Without it this is an open proxy anyone could point
// anywhere, which gets it shut down with your name attached.
const ALLOWED_HOSTS = [
  "api.mangaupdates.com",
  "www.tiktok.com",
  "vm.tiktok.com",
  "www.youtube.com",
  "youtu.be",
  "api.jikan.moe",
  "graphql.anilist.co",
  "www.webtoons.com",
  "global.mangaplus.shueisha.co.jp",
  "manga.bilibili.com",
];

const CORS: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  // Authorization must be listed, or the browser refuses to send the
  // bearer token on any cross-origin call and every signed-in request
  // silently fails before it leaves the page.
  "Access-Control-Allow-Headers": "Content-Type, Authorization",
  "Access-Control-Max-Age": "86400",
};

const UA = "MangaWhere/1.0 (+https://mangawhere.example)";

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { ...CORS, "Content-Type": "application/json" },
  });
}

// ------------------------------------------------------------- storage
//
// Deno KV. Keys are arrays, which act like folders:
//   ["sub", endpoint]            -> a subscribed device
//   ["title", key]               -> a manga we're tracking
//   ["watch", accountId, key]    -> that account follows that manga
//   ["watchers", key, accountId] -> reverse index, so the poller can find
//                                   everyone following one title without
//                                   scanning every account
//   ["device-account", endpoint] -> which account a push device belongs to

/* On Deno Deploy a KV database has to be provisioned in the Databases tab
   and linked to this app; until then openKv throws. Because this runs at
   the top level, letting it throw kills the entire app — including the
   proxy, which the live site depends on. So we degrade instead: proxy
   keeps serving, notification endpoints report why they're unavailable. */
let kv: Deno.Kv | null = null;
let kvError = "";
try {
  kv = await Deno.openKv();
} catch (e) {
  kvError = String(e).slice(0, 200);
  console.warn("Deno KV unavailable — proxy still works, notifications don't:", kvError);
}

interface Sub {
  endpoint: string;
  p256dh: string;
  auth: string;
  created: number;
  failures?: number;
}

interface TitleRec {
  key: string;
  name: string;
  cover?: string;
  kind?: string;
  country?: string;
  links?: Array<{ site: string; url: string; official?: boolean }>;
  latest?: number | null;
  source?: string;
  readUrl?: string;
  checked?: number;
}

/* An account is a set of devices that share one list. Created lazily the
   first time someone asks to sync, so the no-signup flow is untouched for
   everyone who never needs a second device.

   Deliberately no password. A password would mean hashing, reset email,
   and a breach to worry about, to protect a list of comics — and it would
   put a form in front of the one feature people actually came for. */
interface Account {
  id: string;
  code: string;
  created: number;
  devices: string[];
  email?: string;
  salt?: string;
  hash?: string;
  displayName?: string;
}

/* A watch belongs to an ACCOUNT, not to a device.

   It was keyed by push endpoint at first, which meant signing up without
   allowing notifications left you with an account that had nowhere to put
   a tracked title. Devices are only delivery addresses; the list is the
   account's. */
interface WatchRec {
  accountId: string;
  key: string;
  seen: number | null;
  notified: number | null;
  created: number;
}

// ------------------------------------------------------------------ auth
//
// Email and password, hashed with PBKDF2 through Web Crypto — no npm
// dependency, and no plaintext password ever stored or logged.
//
// 210,000 iterations is OWASP's current floor for PBKDF2-SHA256. It costs
// a few hundred milliseconds per login, which nobody notices, and makes
// bulk cracking of a stolen database expensive.

const PBKDF2_ITERATIONS = 210_000;

function b64(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes));
}
function unb64(s: string): Uint8Array<ArrayBuffer> {
  return Uint8Array.from(atob(s), (c) => c.charCodeAt(0));
}

async function hashPassword(password: string, saltIn?: Uint8Array) {
  const salt: Uint8Array<ArrayBuffer> = saltIn
    ? new Uint8Array(saltIn)
    : crypto.getRandomValues(new Uint8Array(16));
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits(
    { name: "PBKDF2", salt, iterations: PBKDF2_ITERATIONS, hash: "SHA-256" },
    key, 256);
  return { salt: b64(salt), hash: b64(new Uint8Array(bits)) };
}

async function verifyPassword(password: string, salt: string, expected: string) {
  const { hash } = await hashPassword(password, unb64(salt));
  // Constant-time compare, so response timing can't be used to guess.
  if (hash.length !== expected.length) return false;
  let diff = 0;
  for (let i = 0; i < hash.length; i++) diff |= hash.charCodeAt(i) ^ expected.charCodeAt(i);
  return diff === 0;
}

function newToken(): string {
  return b64(crypto.getRandomValues(new Uint8Array(32)))
    .replace(/\+/g, "-").replace(/\//g, "_").replace(/=/g, "");
}

function normalizeEmail(e: string): string {
  return String(e || "").trim().toLowerCase();
}

function validEmail(e: string): boolean {
  return /^[^@\s]+@[^@\s.]+\.[^@\s]{2,}$/.test(e);
}

const SESSION_DAYS = 180;

/* Bot protection without a CAPTCHA.

   A CAPTCHA taxes every real person to stop a problem most sites don't
   have yet, and the cheap ones are solved by services for fractions of a
   cent anyway. These three catch scripted signups — which is what you
   actually get — and cost a real user nothing:

     honeypot  a field the form hides; humans never fill it, bots fill
               everything they find
     timing    a person needs seconds to type an email and password; a
               script posts instantly
     rate cap  a handful of accounts per address per hour

   If you ever get hit by something that beats all three, add Cloudflare
   Turnstile — it's free and drops in at the same place. */

const SIGNUPS_PER_IP_PER_HOUR = 5;
const MIN_FORM_SECONDS = 2;

/* ---- CAPTCHA ----

   Drawn here as an SVG rather than pulled from Turnstile or hCaptcha, so
   there's no third-party account to hold, no key to leak, and nothing
   about your users leaving your server.

   The answer never goes to the browser — only an id does. The browser
   sends back what the person typed and the server compares. Each challenge
   works once and expires after ten minutes, so a solved one can't be
   replayed across a run of signups. */

const CAPTCHA_CHARS = "ABCDEFGHJKMNPQRSTUVWXYZ23456789";  // no O/0, no I/1/L
const CAPTCHA_LEN = 5;
const CAPTCHA_TTL_MS = 10 * 60 * 1000;

function randInt(min: number, max: number): number {
  const r = crypto.getRandomValues(new Uint32Array(1))[0] / 4294967296;
  return Math.floor(r * (max - min + 1)) + min;
}

function makeCaptchaText(): string {
  let out = "";
  for (let i = 0; i < CAPTCHA_LEN; i++) {
    out += CAPTCHA_CHARS[randInt(0, CAPTCHA_CHARS.length - 1)];
  }
  return out;
}

/* Characters are rotated, shifted and drawn over noise so that simple
   image-to-text passes struggle, while staying comfortably readable. */
function captchaSvg(text: string): string {
  const W = 220, H = 74;
  const parts: string[] = [];

  parts.push(`<rect width="${W}" height="${H}" rx="10" fill="#1b1830"/>`);

  // background noise: curves behind the text
  for (let i = 0; i < 5; i++) {
    const y1 = randInt(6, H - 6), y2 = randInt(6, H - 6), y3 = randInt(6, H - 6);
    parts.push(
      `<path d="M0 ${y1} Q ${W / 2} ${y2} ${W} ${y3}" stroke="#3de7c3" ` +
      `stroke-opacity="0.${randInt(12, 30)}" stroke-width="${randInt(1, 2)}" fill="none"/>`
    );
  }
  for (let i = 0; i < 26; i++) {
    parts.push(
      `<circle cx="${randInt(0, W)}" cy="${randInt(0, H)}" r="${randInt(1, 2)}" ` +
      `fill="#edebf5" fill-opacity="0.${randInt(10, 26)}"/>`
    );
  }

  const step = (W - 44) / CAPTCHA_LEN;
  for (let i = 0; i < text.length; i++) {
    const x = 26 + i * step + randInt(-3, 3);
    const y = H / 2 + randInt(-4, 8);
    const rot = randInt(-26, 26);
    const size = randInt(29, 37);
    const colour = i % 2 === 0 ? "#edebf5" : "#3de7c3";
    parts.push(
      `<text x="${x}" y="${y}" fill="${colour}" font-size="${size}" ` +
      `font-family="Georgia,serif" font-weight="bold" ` +
      `transform="rotate(${rot} ${x} ${y})" ` +
      `dominant-baseline="middle">${text[i]}</text>`
    );
  }

  // a line across the top of the glyphs, which breaks naive segmentation
  parts.push(
    `<path d="M4 ${randInt(20, 54)} Q ${W / 2} ${randInt(10, 64)} ${W - 4} ` +
    `${randInt(20, 54)}" stroke="#edebf5" stroke-opacity="0.35" ` +
    `stroke-width="2" fill="none"/>`
  );

  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}" ` +
         `width="${W}" height="${H}" role="img" aria-label="Type the characters shown">` +
         parts.join("") + `</svg>`;
}

async function issueCaptcha() {
  const id = crypto.randomUUID();
  const text = makeCaptchaText();
  await kv!.set(["captcha", id], { text, created: Date.now() },
    { expireIn: CAPTCHA_TTL_MS });
  return { id, svg: captchaSvg(text) };
}

/** One use only: the challenge is deleted whether or not it matched. */
async function checkCaptcha(id: string, answer: string): Promise<boolean> {
  if (!id || !answer) return false;
  const rec = await kv!.get<{ text: string }>(["captcha", id]);
  if (!rec.value) return false;
  await kv!.delete(["captcha", id]);
  return rec.value.text.toUpperCase() === String(answer).trim().toUpperCase();
}

function clientIp(req: Request): string {
  const fwd = req.headers.get("x-forwarded-for") || "";
  return fwd.split(",")[0].trim() || req.headers.get("cf-connecting-ip") || "unknown";
}

/** Returns true when this address has had enough for now. */
async function rateLimited(ip: string): Promise<boolean> {
  const hour = Math.floor(Date.now() / 3_600_000);
  const key = ["ratelimit", "signup", ip, String(hour)];
  const current = (await kv!.get<number>(key)).value ?? 0;
  if (current >= SIGNUPS_PER_IP_PER_HOUR) return true;
  // Expires on its own, so old counters never accumulate.
  await kv!.set(key, current + 1, { expireIn: 3_600_000 });
  return false;
}

interface Session { accountId: string; created: number; expires: number; }

/** Resolves a bearer token to an account id, or null. */
async function sessionAccount(req: Request, url: URL): Promise<string | null> {
  const auth = req.headers.get("authorization") || "";
  const token = auth.startsWith("Bearer ")
    ? auth.slice(7)
    : url.searchParams.get("token") || "";
  if (!token) return null;
  const s = await kv!.get<Session>(["session", token]);
  if (!s.value) return null;
  if (s.value.expires < Date.now()) {
    await kv!.delete(["session", token]);
    return null;
  }
  return s.value.accountId;
}

// -------------------------------------------------------------- accounts

/* Ambiguous characters are left out: no O/0, no I/1/L. People read these
   off one screen and type them into another, often on a phone. */
const CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789";

function makeCode(): string {
  const bytes = crypto.getRandomValues(new Uint8Array(8));
  let out = "";
  for (let i = 0; i < 8; i++) {
    out += CODE_ALPHABET[bytes[i] % CODE_ALPHABET.length];
    if (i === 3) out += "-";
  }
  return out;
}

/** The account a device belongs to, if it has ever synced. */
async function accountFor(endpoint: string): Promise<Account | null> {
  const link = await kv!.get<{ id: string }>(["device-account", endpoint]);
  if (!link.value) return null;
  const acc = await kv!.get<Account>(["account", link.value.id]);
  return acc.value ?? null;
}

/** Every push endpoint an account can be reached on. */
async function accountDevices(accountId: string): Promise<Sub[]> {
  const acc = (await kv!.get<Account>(["account", accountId])).value;
  if (!acc) return [];
  const out: Sub[] = [];
  for (const ep of acc.devices) {
    const s = await kv!.get<Sub>(["sub", ep]);
    if (s.value) out.push(s.value);
  }
  return out;
}

/** Attaches a push subscription to an account, if it isn't already. */
async function attachDevice(accountId: string, endpoint: string) {
  const acc = (await kv!.get<Account>(["account", accountId])).value;
  if (!acc) return;
  if (!acc.devices.includes(endpoint)) {
    acc.devices.push(endpoint);
    await kv!.set(["account", accountId], acc);
  }
  await kv!.set(["device-account", endpoint], { id: accountId });
}

// --------------------------------------------------------------- sources

/** .../en/action/tower-of-god/list?title_no=95 -> .../rss?title_no=95 */
function webtoonRss(seriesUrl: string): string | null {
  try {
    const u = new URL(seriesUrl);
    if (!u.hostname.includes("webtoons.com")) return null;
    const titleNo = u.searchParams.get("title_no");
    if (!titleNo) return null;
    const parts = u.pathname.split("/").filter(Boolean);
    if (parts.length < 3) return null;
    return `${u.origin}/${parts.slice(0, 3).join("/")}/rss?title_no=${titleNo}`;
  } catch {
    return null;
  }
}

/** The publisher's own number. Authoritative, unlike release trackers. */
async function webtoonLatest(links: TitleRec["links"]) {
  for (const l of links ?? []) {
    const feed = webtoonRss(l.url ?? "");
    if (!feed) continue;
    try {
      const r = await fetch(feed, { headers: { "User-Agent": UA } });
      if (!r.ok) continue;
      const xml = await r.text();
      // Episode numbers live in each item's link. Regex beats pulling in
      // an XML parser for one attribute.
      let best = 0, url = "";
      for (const m of xml.matchAll(/episode_no=(\d+)/g)) {
        const n = Number(m[1]);
        if (n > best) best = n;
      }
      const link = xml.match(new RegExp(`<link>([^<]*episode_no=${best}[^<]*)</link>`));
      if (link) url = link[1];
      if (best > 0) return { chapter: best, source: "WEBTOON", url };
    } catch { /* try the next link */ }
  }
  return null;
}

function similar(a: string, b: string): number {
  const norm = (s: string) =>
    (s || "").toLowerCase().replace(/[^a-z0-9 ]/g, "").trim();
  const A = norm(a), B = norm(b);
  if (!A || !B) return 0;
  if (A === B) return 1;
  const bg = (s: string) => {
    const out: string[] = [];
    for (let i = 0; i < s.length - 1; i++) out.push(s.slice(i, i + 2));
    return out;
  };
  const x = bg(A), y = bg(B);
  if (!x.length || !y.length) return 0;
  const seen: Record<string, number> = {};
  for (const g of x) seen[g] = (seen[g] ?? 0) + 1;
  let hit = 0;
  for (const g of y) if (seen[g] > 0) { seen[g]--; hit++; }
  return (2 * hit) / (x.length + y.length);
}

function seasonOf(title: string): number {
  const t = title ?? "";
  for (const re of [/season\s*(\d+)/i, /\bs(\d+)\b/i, /\bpart\s*(\d+)/i]) {
    const m = t.match(re);
    if (m) return Number(m[1]);
  }
  return 1;
}

// Novels run far longer than their manhwa adaptations, so matching one
// gives a wildly wrong chapter number.
function isComic(rec: Record<string, unknown>): boolean {
  const t = String(rec?.type ?? "").toLowerCase();
  if (!t) return true;
  return !t.includes("novel") && !t.includes("artbook") && !t.includes("doujin");
}

/** Fallback source. Tracks fan releases, so it stalls once licensed. */
async function mangaUpdatesLatest(title: string) {
  try {
    const r = await fetch("https://api.mangaupdates.com/v1/series/search", {
      method: "POST",
      headers: { "Content-Type": "application/json", "User-Agent": UA },
      body: JSON.stringify({ search: title }),
    });
    if (!r.ok) return null;
    const results = (await r.json())?.results ?? [];
    if (!results.length) return null;

    const scored: Array<{ score: number; id: number }> = [];
    for (const x of results) {
      const rec = x.record ?? x;
      if (!rec || !isComic(rec)) continue;
      const id = rec.series_id ?? rec.id;
      if (!id) continue;
      const names = [rec.title ?? "", ...(rec.associated ?? []).map((a: { title: string }) => a.title)];
      const score = Math.max(...names.map((n: string) => similar(title, n)), 0);
      if (score >= 0.5) scored.push({ score, id });
    }
    if (!scored.length) return null;
    scored.sort((a, b) => b.score - a.score);

    const found: Array<{ chapter: number; season: number }> = [];
    for (const { id } of scored.slice(0, 4)) {
      const d = await fetch(`https://api.mangaupdates.com/v1/series/${id}`,
        { headers: { "User-Agent": UA } });
      if (!d.ok) continue;
      const data = await d.json();
      if (!isComic(data)) continue;
      let raw = data.latest_chapter;
      if (raw == null) {
        const m = String(data.status ?? "").match(/(\d{1,4})\s*chapters?/i);
        raw = m ? m[1] : null;
      }
      const n = Number(raw);
      if (n > 0) found.push({ chapter: n, season: seasonOf(data.title ?? "") });
    }
    if (!found.length) return null;
    // Newest season, not biggest number — S3 ch24 comes after S2 ch180.
    // Two matched records can land on the same season (a raw scanlation
    // entry and an official one, say), so keep the highest chapter count
    // per season before picking the newest one — otherwise a same-season
    // tie could pick whichever happened to be scanned last instead of
    // whichever actually has more chapters.
    const bySeason = new Map<number, number>();
    for (const f of found) {
      const prevMax = bySeason.get(f.season);
      if (prevMax === undefined || f.chapter > prevMax) bySeason.set(f.season, f.chapter);
    }
    const newestSeason = Math.max(...bySeason.keys());
    return { chapter: bySeason.get(newestSeason)!, source: "MangaUpdates", url: "" };
  } catch {
    return null;
  }
}

async function latestFor(t: TitleRec) {
  return (await webtoonLatest(t.links)) ?? (await mangaUpdatesLatest(t.name));
}

// ------------------------------------------------------------------ push

async function sendPush(sub: Sub, payload: Record<string, unknown>) {
  if (!PUSH_READY) {
    console.log(`[no VAPID keys] would notify: ${payload.title}`);
    return { ok: false, dead: false };
  }
  try {
    await webpush.sendNotification(
      { endpoint: sub.endpoint, keys: { p256dh: sub.p256dh, auth: sub.auth } },
      JSON.stringify(payload),
      { TTL: 86400 },
    );
    return { ok: true, dead: false };
  } catch (e) {
    // 404/410 mean the browser threw the subscription away — uninstalled,
    // cleared data, permission revoked. Permanent; stop sending.
    const code = (e as { statusCode?: number })?.statusCode;
    if (code === 404 || code === 410) return { ok: false, dead: true };
    console.log("push failed:", code, String(e).slice(0, 120));
    return { ok: false, dead: false };
  }
}

// ------------------------------------------------------------------ poll

async function pollOnce(verbose = true) {
  if (!kv) return { checked: 0, moved: 0, notified: 0, dropped: 0, error: "no database linked" };
  let checked = 0, moved = 0, sent = 0, dropped = 0;

  for await (const entry of kv!.list<TitleRec>({ prefix: ["title"] })) {
    const t = entry.value;

    // Only check titles somebody actually follows. You're not monitoring
    // all of manga — just what your users asked for.
    const watchers: WatchRec[] = [];
    for await (const w of kv!.list<{ accountId?: string; endpoint?: string }>(
      { prefix: ["watchers", t.key] })) {
      // Entries written before watches moved from devices to accounts carry
      // an endpoint instead of an accountId. Translate the ones we can and
      // drop the rest, so an old row can't crash the whole run.
      let accountId = w.value?.accountId;
      if (!accountId && w.value?.endpoint) {
        const link = await kv!.get<{ id: string }>(["device-account", w.value.endpoint]);
        accountId = link.value?.id;
        if (accountId) {
          await kv!.set(["watchers", t.key, accountId], { accountId });
        }
        await kv!.delete(w.key);          // stale either way
      }
      if (!accountId) continue;
      const rec = await kv!.get<WatchRec>(["watch", accountId, t.key]);
      if (rec.value) watchers.push(rec.value);
    }
    if (!watchers.length) continue;

    checked++;
    const found = await latestFor(t);
    if (!found?.chapter) continue;

    const previous = t.latest ?? null;
    const updated: TitleRec = {
      ...t,
      latest: found.chapter,
      source: found.source,
      readUrl: found.url || t.readUrl,
      checked: Date.now(),
    };
    await kv!.set(["title", t.key], updated);

    if (previous !== null && found.chapter > previous) {
      moved++;
      if (verbose) console.log(`+ ${t.name}: ${previous} -> ${found.chapter} (${found.source})`);
    }

    // Notifying is a per-person decision. Someone who followed at chapter
    // 300 is owed an alert for 337 even on our very first check.
    for (const w of watchers) {
      if (w.notified !== null && w.notified >= found.chapter) continue;

      // An account may be signed in on a phone and a laptop; both should
      // hear about it. An account with no device at all still has its
      // watch marked, so the list shows the update next time they look.
      const devices = await accountDevices(w.accountId);
      let delivered = devices.length === 0;

      for (const sub of devices) {
        const res = await sendPush(sub, {
          title: t.name,
          body: `Chapter ${found.chapter} is out${found.source ? ` on ${found.source}` : ""}`,
          url: updated.readUrl || "/",
          icon: t.cover ?? "",
          tag: t.key,
        });
        if (res.dead) {
          await kv!.delete(["sub", sub.endpoint]);
          await kv!.delete(["device-account", sub.endpoint]);
          const acc = (await kv!.get<Account>(["account", w.accountId])).value;
          if (acc) {
            acc.devices = acc.devices.filter((e) => e !== sub.endpoint);
            await kv!.set(["account", w.accountId], acc);
          }
          dropped++;
          continue;
        }
        if (res.ok) { delivered = true; sent++; }
      }

      if (delivered) {
        await kv!.set(["watch", w.accountId, t.key], { ...w, notified: found.chapter });
      }
    }
  }

  const result = { checked, moved, notified: sent, dropped };
  if (verbose) console.log("poll done:", result);
  return result;
}

// Runs on Deno Deploy with no extra service. Hourly is plenty — manga
// doesn't move faster than that, and it keeps us gentle with the APIs.
try {
  Deno.cron("check chapters", "0 * * * *", async () => { await pollOnce(); });
} catch {
  // Deno.cron is unavailable when running locally; not an error.
}

// ------------------------------------------------------------------ http

async function handleApi(req: Request, url: URL): Promise<Response | null> {
  const path = url.pathname;

  if (path === "/api/config") {
    return json({ vapid_public_key: VAPID_PUBLIC_KEY, push_enabled: PUSH_READY });
  }

  if (path === "/api/health") {
    if (!kv) {
      return json({ status: "degraded", push_configured: PUSH_READY, database: false,
        detail: "No Deno KV database linked to this app. Provision one in the " +
                "Databases tab and link it, then redeploy.", error: kvError });
    }
    let subs = 0, titles = 0;
    for await (const _ of kv.list({ prefix: ["sub"] })) subs++;
    for await (const _ of kv.list({ prefix: ["title"] })) titles++;
    return json({ status: "ok", push_configured: PUSH_READY, database: true,
                  subscribers: subs, titles });
  }

  // Everything past here stores or reads data.
  if (path.startsWith("/api/") && !kv) {
    return json({ error: "no database linked",
      detail: "Provision a Deno KV database in the Databases tab and link it to " +
              "this app, then redeploy." }, 503);
  }

  if (path === "/api/subscribe" && req.method === "POST") {
    const b = await req.json();
    if (!b?.endpoint || !b?.keys?.p256dh) return json({ error: "bad subscription" }, 400);
    const existing = await kv!.get<Sub>(["sub", b.endpoint]);
    await kv!.set(["sub", b.endpoint], {
      endpoint: b.endpoint,
      p256dh: b.keys.p256dh,
      auth: b.keys.auth,
      created: existing.value?.created ?? Date.now(),
      failures: 0,
    });
    // Signed in? Then this device becomes one of theirs.
    const accountId = await sessionAccount(req, url);
    if (accountId) await attachDevice(accountId, b.endpoint);
    return json({ ok: true, attached: Boolean(accountId) });
  }

  if (path === "/api/watch" && req.method === "POST") {
    const b = await req.json();
    // The signed-in account owns the list, so that's what we authenticate.
    // Notifications are optional; tracking is not tied to them.
    const accountId = await sessionAccount(req, url);
    if (!accountId) return json({ error: "Sign in to track series" }, 401);
    if (!b?.title?.key) return json({ error: "missing fields" }, 400);
    // If this browser has push enabled, quietly attach it for delivery.
    if (b?.endpoint) await attachDevice(accountId, b.endpoint);

    const prior = await kv!.get<TitleRec>(["title", b.title.key]);
    // Titles are shared across every account tracking them (keyed by
    // ["title", key], not per-account), so a client with thinner cached
    // data — no links, no cover — must not blank out what an earlier
    // watcher already supplied. links especially: it's how latestFor()
    // finds the official Webtoons RSS feed, so losing it silently
    // degrades chapter tracking for everyone following this title, not
    // just whoever's request this is.
    const t: TitleRec = {
      key: b.title.key,
      name: b.title.name || prior.value?.name || "",
      cover: b.title.cover || prior.value?.cover || "",
      kind: b.title.kind || prior.value?.kind || "",
      country: b.title.country || prior.value?.country || "",
      links: (b.title.links && b.title.links.length) ? b.title.links : (prior.value?.links ?? []),
      latest: prior.value?.latest ?? null,
      source: prior.value?.source ?? "",
      readUrl: prior.value?.readUrl ?? "",
    };
    await kv!.set(["title", t.key], t);

    const seen = b.seen_chapter ?? t.latest ?? null;
    // One write, whatever device it came from — the account holds the list,
    // so every signed-in device sees it without any copying.
    await kv!.set(["watch", accountId, t.key],
      { accountId, key: t.key, seen, notified: seen, created: Date.now() });
    await kv!.set(["watchers", t.key, accountId], { accountId });
    return json({ ok: true, title: t.name, from_chapter: seen });
  }

  if (path === "/api/unwatch" && req.method === "POST") {
    const b = await req.json();
    const accountId = await sessionAccount(req, url);
    if (!accountId) return json({ error: "Sign in first" }, 401);
    await kv!.delete(["watch", accountId, b.key]);
    await kv!.delete(["watchers", b.key, accountId]);
    return json({ ok: true });
  }

  if (path === "/api/list") {
    const accountId = await sessionAccount(req, url);
    if (!accountId) return json({ count: 0, unread: 0, titles: [] });
    const out = [];
    for await (const w of kv!.list<WatchRec>({ prefix: ["watch", accountId] })) {
      const t = await kv!.get<TitleRec>(["title", w.value.key]);
      if (!t.value) continue;
      const latest = t.value.latest ?? null;
      const seen = w.value.seen ?? null;
      out.push({
        key: t.value.key, name: t.value.name, cover: t.value.cover,
        kind: t.value.kind, latest, seen,
        new: latest && seen && latest > seen ? latest - seen : 0,
        source: t.value.source, read_url: t.value.readUrl,
      });
    }
    out.sort((a, b) => b.new - a.new || a.name.localeCompare(b.name));
    return json({ count: out.length, unread: out.filter((x) => x.new).length, titles: out });
  }

  if (path === "/api/mark-read" && req.method === "POST") {
    const b = await req.json();
    const accountId = await sessionAccount(req, url);
    if (!accountId) return json({ error: "Sign in first" }, 401);
    const w = await kv!.get<WatchRec>(["watch", accountId, b.key]);
    const t = await kv!.get<TitleRec>(["title", b.key]);
    if (w.value && t.value) {
      await kv!.set(["watch", accountId, b.key],
        { ...w.value, seen: t.value.latest ?? null, notified: t.value.latest ?? null });
    }
    return json({ ok: true });
  }

  // Lets someone confirm notifications actually reach their phone.
  if (path === "/api/test-push" && req.method === "POST") {
    const b = await req.json();
    const sub = await kv!.get<Sub>(["sub", b.endpoint]);
    if (!sub.value) return json({ error: "not subscribed" }, 404);
    const res = await sendPush(sub.value, {
      title: "Manga Where",
      body: "Notifications are working. We'll ping you when a chapter drops.",
      url: "/",
    });
    if (res.dead) {
      await kv!.delete(["sub", b.endpoint]);
      const acc = await accountFor(b.endpoint);
      if (acc) {
        acc.devices = acc.devices.filter((e) => e !== b.endpoint);
        await kv!.set(["account", acc.id], acc);
      }
      await kv!.delete(["device-account", b.endpoint]);
    }
    return json({ ok: res.ok, dead: res.dead });
  }

  /* Hand out a fresh challenge. Also used by the reload button. */
  if (path === "/api/captcha") {
    const c = await issueCaptcha();
    return json({ id: c.id, svg: c.svg });
  }

  /* ---- sign up ---- */
  if (path === "/api/register" && req.method === "POST") {
    const b = await req.json();
    const email = normalizeEmail(b?.email);
    const password = String(b?.password ?? "");

    if (!validEmail(email)) return json({ error: "Enter a valid email address" }, 400);
    if (password.length < 8) {
      return json({ error: "Password must be at least 8 characters" }, 400);
    }

    // Honeypot: a real person never sees this field, so anything in it
    // came from something filling the form blind.
    if (b?.website) return json({ error: "Something went wrong. Try again." }, 400);

    // Filled in faster than a person can type it.
    const elapsed = Number(b?.elapsed ?? 0);
    if (elapsed > 0 && elapsed < MIN_FORM_SECONDS) {
      return json({ error: "That was too quick — please try again." }, 400);
    }

    if (!(await checkCaptcha(b?.captcha_id, b?.captcha))) {
      return json({ error: "Those characters didn't match. Try again.",
                    captcha_failed: true }, 400);
    }

    if (await rateLimited(clientIp(req))) {
      return json({ error: "Too many accounts created from here. Try again later." }, 429);
    }

    const taken = await kv!.get<{ id: string }>(["email", email]);
    if (taken.value) {
      return json({ error: "An account already exists for that email" }, 409);
    }

    const { salt, hash } = await hashPassword(password);
    const id = crypto.randomUUID();
    const acc: Account = {
      id, code: makeCode(), created: Date.now(), devices: [],
      email, salt, hash, displayName: email.split("@")[0],
    };

    if (b?.endpoint) {
      acc.devices.push(b.endpoint);
      await kv!.set(["device-account", b.endpoint], { id });
    }

    await kv!.set(["account", id], acc);
    await kv!.set(["email", email], { id });
    await kv!.set(["code", acc.code], { id });

    const token = newToken();
    await kv!.set(["session", token], {
      accountId: id, created: Date.now(),
      expires: Date.now() + SESSION_DAYS * 86400_000,
    });
    return json({ ok: true, token, email, name: acc.displayName, code: acc.code });
  }

  /* ---- log in ---- */
  if (path === "/api/login" && req.method === "POST") {
    const b = await req.json();
    const email = normalizeEmail(b?.email);
    const password = String(b?.password ?? "");

    const found = await kv!.get<{ id: string }>(["email", email]);
    const acc = found.value
      ? (await kv!.get<Account>(["account", found.value.id])).value
      : null;

    // Same message either way — saying "no such email" would let anyone
    // test which addresses are registered.
    const bad = json({ error: "Email or password is incorrect" }, 401);
    if (!acc || !acc.salt || !acc.hash) return bad;
    if (!(await verifyPassword(password, acc.salt, acc.hash))) return bad;

    // Attach the device they're signing in from, so notifications reach it.
    // Nothing to copy — the list belongs to the account already.
    if (b?.endpoint) await attachDevice(acc.id, b.endpoint);

    const token = newToken();
    await kv!.set(["session", token], {
      accountId: acc.id, created: Date.now(),
      expires: Date.now() + SESSION_DAYS * 86400_000,
    });
    return json({ ok: true, token, email: acc.email,
                  name: acc.displayName, code: acc.code });
  }

  /* ---- who am I ---- */
  if (path === "/api/me") {
    const accountId = await sessionAccount(req, url);
    if (!accountId) return json({ signed_in: false });
    const acc = (await kv!.get<Account>(["account", accountId])).value;
    if (!acc) return json({ signed_in: false });
    let tracked = 0;
    for await (const _ of kv!.list({ prefix: ["watch", accountId] })) tracked++;
    return json({ signed_in: true, email: acc.email, name: acc.displayName,
                  code: acc.code, devices: acc.devices.length, tracked });
  }

  /* ---- log out ---- */
  if (path === "/api/logout" && req.method === "POST") {
    const auth = req.headers.get("authorization") || "";
    const token = auth.startsWith("Bearer ") ? auth.slice(7) : "";
    if (token) await kv!.delete(["session", token]);
    return json({ ok: true });
  }

  // Manual trigger, handy before waiting an hour for the cron.
  if (path === "/api/poll" && req.method === "POST") {
    return json(await pollOnce(false));
  }

  return null;
}

Deno.serve(async (req: Request) => {
  if (req.method === "OPTIONS") return new Response(null, { status: 204, headers: CORS });

  const url = new URL(req.url);

  const api = await handleApi(req, url);
  if (api) return api;

  // ---- proxy (unchanged, so the existing front end keeps working) ----
  const target = url.searchParams.get("url");
  if (!target) {
    return json({ ok: true, service: "mangawhere", push: PUSH_READY, allowed: ALLOWED_HOSTS });
  }

  let dest: URL;
  try {
    dest = new URL(target);
  } catch {
    return json({ error: "malformed url" }, 400);
  }
  if (!ALLOWED_HOSTS.includes(dest.hostname)) {
    return json({ error: "host not allowed", host: dest.hostname }, 403);
  }

  const init: RequestInit = {
    method: req.method,
    headers: { "User-Agent": UA, "Accept": "application/json, text/xml, application/xml, */*" },
  };
  if (req.method === "POST") {
    init.body = await req.text();
    (init.headers as Record<string, string>)["Content-Type"] =
      req.headers.get("content-type") ?? "application/json";
  }

  try {
    const upstream = await fetch(dest.toString(), init);
    const body = await upstream.text();
    return new Response(body, {
      status: upstream.status,
      headers: { ...CORS, "Content-Type": upstream.headers.get("content-type") ?? "application/json" },
    });
  } catch (err) {
    return json({ error: "upstream failed", detail: String(err) }, 502);
  }
});
