# Moving the backend to another Render account

The backend (`main.py`) runs as the Render web service
`mangawhere.onrender.com`. Moving it means creating a new service in the
other account, copying its settings, and pointing everything at the new
address. The old service keeps running until the last step, so nothing is
down in between.

## 1. Create the service in the new account

1. Sign in to Render with the new account.
2. Connect GitHub to it, with access to this repository.
3. **New → Blueprint**, pick this repository. It reads `render.yaml` and
   creates the `mangawhere` service. If that name is taken, Render picks a
   different address (for example `mangawhere-abcd.onrender.com`). Note the
   address down: it's the **new URL** used below.

## 2. Copy the environment variables

In the old service: **Environment**. Copy every value you have set into the
new service's **Environment** (the ones marked `sync: false` in
`render.yaml` are never copied for you):

- `TURSO_DATABASE_URL`, `TURSO_AUTH_TOKEN`: use the same values, so
  accounts, watch lists and comments carry over. Without Turso they live
  in the old service's `data/` folder and don't move.
- `VAPID_CONTACT_EMAIL`, `CLOUDINARY_*`, `YOUTUBE_COOKIES`,
  `TURNSTILE_SECRET_KEY`, `GOOGLE_CLIENT_ID`, `FACEBOOK_APP_ID`,
  `FACEBOOK_APP_SECRET`, `OLLAMA_HOST`, `NARRATION_MODEL`,
  `NARRATION_URL`, `SMTP_*`, `MAIL_FROM`, `RESET_LINK_ORIGINS`: whichever
  you use.

Then **Manual Deploy** and wait until `https://<new URL>/api/config` loads.

Password reset links already allow the new address: Render tells the app
its own URL (`RENDER_EXTERNAL_URL`).

## 3. Point everything at the new URL

| Where | What to change |
| --- | --- |
| `index.html` | `var BACKEND_URL = "..."` (one line; the CORS proxy and the API both use it) |
| `wrangler.jsonc` and `cloudflare/wrangler.jsonc` | `"vars": { "BACKEND_URL": "..." }` |
| GitHub → Settings → Secrets and variables → Actions → **Variables** | add `BACKEND_URL` = the new URL (the keep-awake ping uses it) |

Push the two files to `main`: GitHub Pages republishes `index.html`, and
Cloudflare redeploys the page-cache Worker.

## 4. Things tied to the address

- **Google / Facebook sign-in**: if the Google Cloud or Facebook app lists
  the backend's address anywhere (authorized origins, redirect URIs), add
  the new one. The frontend's GitHub Pages address doesn't change.
- **Push notifications**: the signing key is kept in the service's `data/`
  folder, so the new service makes its own. Readers who turned on new-chapter
  alerts need to turn them off and on again.

## 5. Retire the old service

After a day or so of the new one working, suspend or delete the old service
in the old account. The `"https://mangawhere.onrender.com"` defaults left
in the code (password reset origins, the Worker and workflow fallbacks)
are harmless; swap them for the new URL whenever convenient.
