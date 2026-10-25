// MangaWhere ad-control bot — a single Cloudflare Worker, always on, free.
//
// It does two jobs:
//   GET  /ad-state  -> JSON {readerAds, allAds} the website reads on load to
//                      decide whether to show ads. Public, no secrets, CORS *.
//   POST /tg        -> Telegram webhook. Only the configured admin can drive
//                      it; everyone else is ignored. It writes the on/off
//                      state into KV, so the site picks it up on the next load.
//
// Bindings (set in the Cloudflare dashboard — never written in code):
//   AD_KV           KV namespace. Holds one document, "state".
//   ADMIN_ID        plain var — your numeric Telegram id (from @userinfobot).
//   BOT_TOKEN       secret    — the @BotFather token.
//   WEBHOOK_SECRET  secret    — any random string; also handed to Telegram's
//                               setWebhook so a forged POST can't drive the bot.
//
// State shape in KV ("state"): { reader: <until>, all: <until> } where <until>
// is 0/absent = ads on, a future epoch-ms = off until then, -1 = off forever.

const FOREVER = -1;
const DAY = 86400000;
const DURATIONS = { "24h": DAY, "7d": 7 * DAY, "30d": 30 * DAY, "forever": FOREVER };
const DURLABEL = { "24h": "24 hours", "7d": "1 week", "30d": "1 month", "forever": "indefinitely" };

async function readState(env) {
  try { return JSON.parse(await env.AD_KV.get("state")) || {}; }
  catch (e) { return {}; }
}
function writeState(env, state) {
  return env.AD_KV.put("state", JSON.stringify(state));
}

// Is a slot off, given its "until" value and the current time?
function isOff(until, now) {
  return until === FOREVER || (typeof until === "number" && until > now);
}

// What the website asks for: are reader ads / all ads currently enabled?
// "All off" implies reader off too.
function adState(state) {
  const now = Date.now();
  const allOff = isOff(state.all, now);
  return { readerAds: !(allOff || isOff(state.reader, now)), allAds: !allOff };
}

function human(ms) {
  const mins = Math.round(ms / 60000);
  if (mins < 60) return mins + (mins === 1 ? " minute" : " minutes");
  const hrs = Math.round(mins / 60);
  if (hrs < 48) return hrs + (hrs === 1 ? " hour" : " hours");
  const days = Math.round(hrs / 24);
  return days + (days === 1 ? " day" : " days");
}
function slotLine(until, now) {
  if (until === FOREVER) return "OFF (indefinitely)";
  if (typeof until === "number" && until > now) return "OFF (back on in " + human(until - now) + ")";
  return "ON";
}
function statusText(state) {
  const now = Date.now();
  return "MangaWhere ads\n\n" +
    "Reader ads: " + slotLine(state.reader, now) + "\n" +
    "All ads: " + slotLine(state.all, now);
}

function mainMenu() {
  return { inline_keyboard: [
    [{ text: "🔴 Turn OFF reader ads", callback_data: "off:reader" }],
    [{ text: "🔴 Turn OFF all ads", callback_data: "off:all" }],
    [{ text: "🟢 Turn everything ON", callback_data: "on" }],
    [{ text: "📊 Status", callback_data: "status" }],
  ] };
}
function durationMenu(scope) {
  return { inline_keyboard: [
    [{ text: "24 hours", callback_data: "set:" + scope + ":24h" },
     { text: "1 week", callback_data: "set:" + scope + ":7d" }],
    [{ text: "1 month", callback_data: "set:" + scope + ":30d" },
     { text: "Indefinitely", callback_data: "set:" + scope + ":forever" }],
    [{ text: "⬅ Back", callback_data: "menu" }],
  ] };
}

function tg(env, method, body) {
  return fetch("https://api.telegram.org/bot" + env.BOT_TOKEN + "/" + method, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
}

async function handleUpdate(env, update) {
  const cb = update.callback_query;
  const msg = update.message || update.edited_message;
  const from = cb ? cb.from : (msg ? msg.from : null);

  // Only the owner may drive the bot. Everyone else is ignored.
  if (!from || String(from.id) !== String(env.ADMIN_ID)) {
    if (cb) await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "Not authorized." });
    return;
  }

  if (msg && !cb) {
    await tg(env, "sendMessage", {
      chat_id: from.id,
      text: "MangaWhere ad control — choose:",
      reply_markup: mainMenu(),
    });
    return;
  }

  if (cb) {
    const data = cb.data || "";
    const chat = cb.message.chat.id, mid = cb.message.message_id;
    const edit = (text, markup) => tg(env, "editMessageText", {
      chat_id: chat, message_id: mid, text: text, reply_markup: markup,
    });

    if (data === "menu") {
      await edit("MangaWhere ad control — choose:", mainMenu());
    } else if (data === "status") {
      await edit(statusText(await readState(env)), mainMenu());
    } else if (data === "on") {
      await writeState(env, {});
      await edit("🟢 All ads are back ON.", mainMenu());
    } else if (data.indexOf("off:") === 0) {
      const scope = data.slice(4);
      await edit("Turn OFF " + (scope === "all" ? "all" : "reader") + " ads for how long?", durationMenu(scope));
    } else if (data.indexOf("set:") === 0) {
      const parts = data.split(":");
      const scope = parts[1], dur = parts[2];
      const ms = DURATIONS[dur];
      if (ms !== undefined) {
        const until = ms === FOREVER ? FOREVER : Date.now() + ms;
        const state = await readState(env);
        state[scope] = until;
        await writeState(env, state);
        await edit("🔴 " + (scope === "all" ? "All" : "Reader") + " ads OFF " + DURLABEL[dur] + ".\n\n" + statusText(state), mainMenu());
      }
    }
    await tg(env, "answerCallbackQuery", { callback_query_id: cb.id });
  }
}

function withCors(resp) {
  resp.headers.set("Access-Control-Allow-Origin", "*");
  resp.headers.set("Access-Control-Allow-Methods", "GET,OPTIONS");
  return resp;
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);

    if (req.method === "OPTIONS") return withCors(new Response(null, { status: 204 }));

    if (req.method === "GET" && url.pathname === "/ad-state") {
      const body = JSON.stringify(adState(await readState(env)));
      return withCors(new Response(body, {
        headers: { "content-type": "application/json", "cache-control": "no-store" },
      }));
    }

    if (req.method === "POST" && url.pathname === "/tg") {
      if (env.WEBHOOK_SECRET &&
          req.headers.get("x-telegram-bot-api-secret-token") !== env.WEBHOOK_SECRET) {
        return new Response("forbidden", { status: 403 });
      }
      let update;
      try { update = await req.json(); }
      catch (e) { return new Response("bad request", { status: 400 }); }
      // Swallow errors so Telegram gets its 200 and doesn't retry-storm.
      try { await handleUpdate(env, update); } catch (e) { /* logged by Workers */ }
      return new Response("ok");
    }

    return new Response("MangaWhere ad bot", { status: 200 });
  },
};
