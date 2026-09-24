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

**A real caveat on the free tier:** the app stores accounts, watch
lists, and push subscriptions in a local SQLite file (`data/`). Render's
free plan has no persistent disk, so that file is wiped on every
redeploy. Fine for trying things out; for anything you don't want to
lose, either add a paid Render persistent disk mounted at `data/`, or
move the storage to a managed database (Render's own Postgres, for
instance) — neither is set up here yet.

Optional environment variables (see `render.yaml`):
- `VAPID_CONTACT_EMAIL` — contact address push services can reach if
  your deployment misbehaves. Defaults to a placeholder; set a real one.
- `POLL_INTERVAL_SECONDS` — how often the background job checks tracked
  titles for new chapters. Defaults to 1800 (30 minutes).
- `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET`
  — enable profile picture uploads (see below). Everyone gets a default
  initials avatar with none of these set; uploading a custom photo just
  returns a clear error until all three are present.

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

## Tests

```
python -m unittest discover -s tests
```
