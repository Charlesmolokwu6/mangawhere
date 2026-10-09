// MangaWhere storyteller pages, cached at Cloudflare.
//
// In phone storyteller mode every listener's phone fetches each page of the
// chapter from the Render server (/api/narrate/page), which has a 100GB/month
// free bandwidth allowance. Deployed as a free Cloudflare Worker (100,000
// requests a day), this sits in front: the first listener's request goes to
// Render, and Cloudflare keeps the page for a day for everyone after.
//
// It only ever fetches that one endpoint of the MangaWhere server, so it
// can't be used to fetch anything else. To use it, set NARRATION_PAGE_BASE
// in index.html to this Worker's address.

const ORIGIN = "https://mangawhere.onrender.com";
const PATH = "/api/narrate/page";
const KEEP_SECONDS = 86400;

const CORS = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
};

export default {
  async fetch(request, env, ctx) {
    if (request.method === "OPTIONS") return new Response(null, { headers: CORS });
    const url = new URL(request.url);
    if (request.method !== "GET" || url.pathname !== PATH) {
      return new Response("Not found", { status: 404, headers: CORS });
    }

    const target = new Request(ORIGIN + PATH + url.search);
    const cache = caches.default;
    let response = await cache.match(target);
    if (!response) {
      const upstream = await fetch(target);
      response = new Response(upstream.body, upstream);
      if (upstream.ok) {
        response.headers.set("Cache-Control", `public, max-age=${KEEP_SECONDS}`);
        ctx.waitUntil(cache.put(target, response.clone()));
      }
    }
    response = new Response(response.body, response);
    for (const [name, value] of Object.entries(CORS)) response.headers.set(name, value);
    return response;
  },
};
