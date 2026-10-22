# RelayDrop — HTML / CSS / JS front-end + Python back-end

Temporary file sharing with an 8-digit code, a link, a QR code and a Code 128 barcode.
Same look (the CSS file is byte-for-byte the original) and same behaviour and limits as before:
1–20 files, 50 MiB per file, 100 MiB total, expiry 1 h / 24 h / 3 d / 7 d, optional password, revoke, ZIP download,
support/abuse/copyright tickets, operator inbox.

```
frontend/                 the UI — plain HTML, CSS and JS, no build step
  index.html about.html privacy.html terms.html copyright.html
  guides.html guides/*.html  contact.html report.html admin.html 404.html
  css/style.css           the original stylesheet
  js/app.js               send / receive / history / support / admin behaviour
  js/icons.js             SVG icons
  js/config.js            ← API address (empty = same server)
backend/                  Python only (Flask + SQLite)
  run.py  requirements.txt  .env.example
  relaydrop/              api.py, core.py, qr.py, barcode128.py
  tests/
.github/workflows/        ci.yml (tests) · pages.yml (publish the UI to GitHub Pages)
.devcontainer/            one-click GitHub Codespaces
Dockerfile                deploy anywhere that runs containers
```

## Run it

**On GitHub (Codespaces)** — push this repo, then *Code ▸ Codespaces ▸ Create codespace*. It installs everything and
starts the app; the forwarded port 5000 opens the site. Operator inbox: `/admin`, key = `ADMIN_KEY` in `backend/.env`.

**On your computer**
```bash
cd backend
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                                   # then change ADMIN_KEY
python run.py                                          # → http://127.0.0.1:5000
```
The Python server serves both the API and the `frontend/` folder, so no CORS or extra setup is needed.
Tests: `cd backend && python -m unittest discover -s tests -v` (also run by GitHub Actions on every push).

## Deploy on Netlify (UI) + Render (API)

Netlify serves static files and JS/Go/Node functions only — it can't run Flask or keep uploaded files — so the UI goes on
Netlify and the Python API on a container host. Order matters:

1. **API first.** Render ▸ *New ▸ Blueprint* ▸ select this repo (uses `render.yaml` + `Dockerfile`). Note the URL, e.g.
   `https://relaydrop-api.onrender.com`, and open `/health` to check it. Any Docker host (Fly.io, Railway, a VPS) works the same:
   set `CORS_ORIGINS`, `ADMIN_KEY`, `TRUST_PROXY=1`, keep `/data` on a persistent volume.
2. **UI on Netlify.** *Add new site ▸ Import from Git* ▸ this repo (`netlify.toml` sets publish dir `frontend`). Add the environment
   variable `RELAYDROP_API_URL` = the API URL from step 1, then deploy.
3. **Allow the UI.** On the API set `CORS_ORIGINS=https://YOUR-SITE.netlify.app` (your custom domain too, comma-separated) and redeploy.

Done: visitors open the Netlify site to send/receive; files go straight to your API. Operator inbox: `https://YOUR-SITE.netlify.app/admin.html`.
Free-tier hosts without a disk lose files on restart; uploads are temporary by design, but use a disk for anything real.

## GitHub Pages (UI only) + API elsewhere

GitHub Pages hosts static files only, so it can show the UI but **cannot run the Python API**. To use Pages:

1. Run the API somewhere (Codespaces port made public, Render/Railway/Fly using the `Dockerfile`, your own server) and set
   `CORS_ORIGINS=https://<your-user>.github.io` on it (add `SERVE_FRONTEND=0` if you don't want it serving the UI too).
2. In the repo: *Settings ▸ Pages ▸ Source = GitHub Actions*, and *Settings ▸ Secrets and variables ▸ Actions ▸ Variables* →
   add `RELAYDROP_API_URL` = the API's `https://…` address.
3. Push to `main`; `pages.yml` publishes `frontend/` with `js/config.js` pointing at your API.

Note: the operator inbox login uses a cookie, so in this split setup the API must be on HTTPS; the sender/receiver flows use a
header token and work regardless.

## API

| Method & path | Purpose |
|---|---|
| `GET /api/session` | issue/confirm the anonymous sender token |
| `POST /api/transfers` · `PUT /api/transfers/<id>/files/<file_id>` · `POST /api/transfers/<id>/finalize` | create → upload → publish |
| `GET /api/transfers` · `DELETE /api/transfers/<id>` | my transfers · revoke |
| `POST /api/receive` · `GET /download?token=…[&file_id=…]` | resolve code/link → file or streamed ZIP |
| `POST /api/tickets` · `GET /api/tickets/<id>?receipt=…` | contact / abuse / copyright requests |
| `POST /api/admin/login` · `/logout` · `GET /api/admin/tickets` · `PATCH /api/admin/tickets/<id>` | operator inbox |
| `POST /api/qr` · `POST /api/barcode` | PNG/SVG images |
| `GET /health` | status and limits |

Files are not end-to-end encrypted or malware-scanned. For production use HTTPS, a strong `ADMIN_KEY`, and a real WSGI server
(the Dockerfile uses `waitress`).
