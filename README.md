# Battle.net Code Scanner

A tool to scan Battle.net checkout product codes, build a searchable catalog, and browse valid products with names, prices, images, and game tags.

- **Public catalog** — `/` search UI for browsing indexed products
- **Scanner** — `/scanner` admin UI for scanning codes and maintaining the library
- **Fast auto scan** — HTTP-first detection with automatic image/price/game enrichment when available
- **Browser enrichment** — “Fix Incomplete” fills missing metadata via full checkout pages

## Quick start (local)

```bash
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
playwright install chromium
python -m uvicorn app.main:app --host 127.0.0.1 --port 8787
```

Or double-click `run.bat` on Windows.

Open:

- **Catalog:** http://127.0.0.1:8787/
- **Scanner:** http://127.0.0.1:8787/scanner

## Scanner workflow

1. Open **Scanner** → **Open Login** → log in to Battle.net → **Save Session**
2. Set code range and start **Auto Scan**
3. Run **Fix Incomplete** for hits missing images, prices, or game tags (uses browser, slower)

Auto scans now try to pull images, prices, and game tags from checkout HTML over HTTP. Coin products (e.g. Overwatch Coins) and CP bundles are detected when present in the page.

## Hosting at bore.rip

GitHub Pages **cannot** run this app (it needs Python + Playwright). Use a small VPS or PaaS with Docker.

### Recommended: Railway + custom domain

1. Push this repo to GitHub (see below)
2. Create a project at [railway.app](https://railway.app) → **Deploy from GitHub**
3. Add a **Volume** mounted at `/app/data` (keeps your SQLite DB and images)
4. Set domain in Railway → **Custom Domain** → `bore.rip`
5. At your DNS provider, add the CNAME Railway gives you for `bore.rip`

The included `Dockerfile` and `railway.toml` handle the build. Health check: `/api/public/stats`.

### Alternatives

| Platform | Notes |
|----------|-------|
| **Render** | Docker deploy, add persistent disk for `/app/data` |
| **Fly.io** | `fly launch` + volume for `data/` |
| **VPS** | `docker build -t bore-rip . && docker run -p 8787:8787 -v $(pwd)/data:/app/data bore-rip` |

Point `bore.rip` CNAME/A record to your host. Use Cloudflare proxy if you want CDN + SSL.

### Production tips

- Mount `data/` persistently — library DB and images live there
- Keep `data/auth.json` private (never commit it)
- Use a reasonable scan delay; Battle.net may rate-limit aggressive scans
- After deploying, upload your local `data/library.db` and `data/images/` to the volume, or re-scan on the server

## Publish to GitHub

From the project folder:

```bash
git init
git add .
git commit -m "Initial public release"
git branch -M main
git remote add origin https://github.com/YOUR_USER/battlenet-code-library.git
git push -u origin main
```

**Do not commit:** `data/auth.json`, `data/library.db`, `venv/`, or local settings (already in `.gitignore`).

Install [GitHub CLI](https://cli.github.com/) to create the repo in one step:

```bash
gh repo create battlenet-code-library --public --source=. --push
```

## Data layout

| Path | Purpose |
|------|---------|
| `data/library.db` | SQLite product library |
| `data/images/` | Downloaded product thumbnails |
| `data/auth.json` | Saved Battle.net session (private) |
| `data/settings.json` | Scanner preferences |

## API

| Endpoint | Description |
|----------|-------------|
| `GET /api/public/stats` | Product count + games breakdown |
| `GET /api/public/library` | Paginated valid products (`search`, `game`, `sort`) |
| `GET /api/library/{code}` | Single product |
| `POST /api/scan/auto/start` | Start auto scan (scanner UI) |

## Disclaimer

Unofficial community tool. Not affiliated with Blizzard Entertainment. Use responsibly and respect Battle.net terms of service.

## License

MIT — see [LICENSE](LICENSE).
