// MangaWhere at Cloudflare: storyteller pages and Comizy page images,
// cached so the Render server (100GB/month free bandwidth) doesn't carry them.
//
// /api/narrate/page  In phone storyteller mode every listener's phone fetches
//                    each page of the chapter from the Render server. The
//                    first request goes to Render; Cloudflare keeps the page
//                    for a day for everyone after.
// /img?url=...       comizy.io's image CDN (*.cmzcdn.org) only answers
//                    requests with comizy's own Referer, which a reader's
//                    <img> can't send, so the reader loads those images
//                    through here. Only cmzcdn.org images, nothing else, and
//                    each is kept for a week (chapter images never change).
//
// Deployed as a Cloudflare Worker (free plan: 100,000 requests a day). To use
// it, set NARRATION_PAGE_BASE in index.html to this Worker's address.

const ORIGIN = "https://mangawhere.onrender.com";
const PAGE_PATH = "/api/narrate/page";
const PAGE_KEEP_SECONDS = 86400;

const IMAGE_PATH = "/img";
const IMAGE_HOST_SUFFIX = "cmzcdn.org";
const IMAGE_REFERER = "https://comizy.io/";
const IMAGE_KEEP_SECONDS = 7 * 86400;
const IMAGE_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " +
  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36";

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
};

function withCors(response) {
  response = new Response(response.body, response);
  for (const [name, value] of Object.entries(CORS)) response.headers.set(name, value);
  return response;
}

// cache: the request to cache under. load: how to fetch it on a miss.
async function cached(ctx, key, keepSeconds, load) {
  const cache = caches.default;
  let response = await cache.match(key);
  if (!response) {
    const upstream = await load();
    response = new Response(upstream.body, upstream);
    if (upstream.ok) {
      response.headers.set("Cache-Control", `public, max-age=${keepSeconds}`);
      ctx.waitUntil(cache.put(key, response.clone()));
    }
  }
  return withCors(response);
}

function comizyImage(url) {
  let target;
  try {
    target = new URL(url.searchParams.get("url") || "");
  } catch (e) {
    return null;
  }
  const host = target.hostname.toLowerCase();
  const ok = target.protocol === "https:" &&
    (host === IMAGE_HOST_SUFFIX || host.endsWith("." + IMAGE_HOST_SUFFIX));
  return ok ? target : null;
}

export default {
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
    const url = new URL(request.url);
    if (request.method !== "GET") return new Response("Not found", { status: 404, headers: CORS });

    if (url.pathname === PAGE_PATH) {
      const target = new Request(ORIGIN + PAGE_PATH + url.search);
      return cached(ctx, target, PAGE_KEEP_SECONDS, () => fetch(target));
    }

    if (url.pathname === IMAGE_PATH) {
      const target = comizyImage(url);
      if (!target) return new Response("Not allowed", { status: 403, headers: CORS });
      // Cached under the image's own address, without our headers.
      const key = new Request(target.toString());
      return cached(ctx, key, IMAGE_KEEP_SECONDS, () => fetch(target.toString(), {
        headers: { "Referer": IMAGE_REFERER, "User-Agent": IMAGE_UA },
      }));
    }

    return new Response("Not found", { status: 404, headers: CORS });
  },
};
