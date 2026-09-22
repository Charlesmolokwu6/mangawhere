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

## Tests

```
python -m unittest discover -s tests
```
