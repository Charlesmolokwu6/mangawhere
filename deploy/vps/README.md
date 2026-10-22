# Running MangaWhere on your own VPS

The whole app (frontend, accounts, reader, poller) in Docker, with Caddy
in front for HTTPS. Works on any Linux VPS with Docker; 2GB of memory is
the comfortable minimum (4GB+ to turn narration on).

## First time

1. Point the domain at the server. With Cloudflare (recommended, see the
   main README's "Cloudflare in front of the whole site"): an `A` record
   for the domain to the VPS's IP, proxy **on**, SSL/TLS mode
   **Full (strict)**. Without Cloudflare: the same `A` record at your
   registrar's DNS.
2. On the VPS, install Docker (`curl -fsSL https://get.docker.com | sh`)
   and open ports 80 and 443 in its firewall.
3. Get the code and settings:
   ```
   git clone https://github.com/Charlesmolokwu6/mangawhere.git
   cd mangawhere
   cp deploy/vps/.env.example deploy/vps/.env
   nano deploy/vps/.env      # set SITE_DOMAIN and PUBLIC_SITE_URL at least
   ```
4. Start it:
   ```
   docker compose -f deploy/vps/docker-compose.yml up -d --build
   ```
   Caddy fetches the HTTPS certificate on the first request, which can
   take a minute.

## Deploying an update

```
git pull
docker compose -f deploy/vps/docker-compose.yml up -d --build
```

## Checking it works

```
docker compose -f deploy/vps/docker-compose.yml ps     # app shows (healthy)
curl -I https://<your domain>/                         # 200, server: Caddy
docker compose -f deploy/vps/docker-compose.yml logs -f app
```

## Backups

Without Turso, accounts, watch lists and comments are in the `app-data`
volume, in one SQLite file. Copy it off the machine regularly, e.g. daily
from cron:

```
docker compose -f deploy/vps/docker-compose.yml exec -T app \
  python -c "import sqlite3; sqlite3.connect('data/mangawhere.db').backup(sqlite3.connect('data/backup.db'))"
docker compose -f deploy/vps/docker-compose.yml cp app:/app/data/backup.db ./mangawhere-$(date +%F).db
```

With `TURSO_DATABASE_URL` set, the data lives in Turso instead and a new
server picks it up on its first start, so moving to another VPS is just
steps 2–4 above on the new machine and pointing DNS at it.

## Visitors' addresses

Sign-up, comment and account rate limits count requests per visitor.
Behind Cloudflare, Caddy takes the visitor's address from
`CF-Connecting-IP`, but only on requests that really come from
Cloudflare's addresses (listed in `Caddyfile`), so the header can't be
faked by going to the server directly. If Cloudflare publishes new
ranges (https://www.cloudflare.com/ips/), add them there.
