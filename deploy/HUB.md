# bore.rip project hub setup

## Architecture

| URL | Host | What |
|-----|------|------|
| `bore.rip/` | **Vercel** (`Boregu/bore.rip`) | Portfolio / project hub |
| `bore.rip/battlenetcodes` | **Railway** → proxied by Vercel | Battle.net catalog (this app) |
| `bore.rip/battlenetcodes/scanner` | Railway | Scanner admin UI |

Cloudflare sits in front of both (DNS + SSL).

## 1. Deploy Battle.net app to Railway

1. Go to [railway.app](https://railway.app) → **New Project** → **Deploy from GitHub**
2. Select **`Boregu/Battle.net-Code-Scanner`**
3. Add a **Volume** mounted at `/app/data`
4. Copy your local `data/library.db` and `data/images/` to the volume (or re-scan on server)
5. Note the public URL, e.g. `battlenet-code-scanner-production.up.railway.app`

## 2. Proxy from Vercel (bore.rip repo)

In your **`Boregu/bore.rip`** repo, add `vercel.json`:

```json
{
  "rewrites": [
    {
      "source": "/battlenetcodes",
      "destination": "https://YOUR-RAILWAY-APP.up.railway.app/"
    },
    {
      "source": "/battlenetcodes/:path*",
      "destination": "https://YOUR-RAILWAY-APP.up.railway.app/:path*"
    }
  ]
}
```

Push to `main` → Vercel redeploys automatically.

## 3. Link from portfolio homepage

Add a project card on bore.rip:

```html
<a href="/battlenetcodes">Battle.net Code Catalog</a>
```

## 4. Cloudflare DNS

Keep pointing `bore.rip` to Vercel:

| Type | Name | Content |
|------|------|---------|
| A | `@` | `76.76.21.21` |
| CNAME | `www` | `cname.vercel-dns.com` |

## Adding more projects later

Same pattern for each app:

1. Deploy app to Railway/Fly/Render
2. Add Vercel rewrite: `/myproject/:path*` → app URL
3. Add mount prefix to `static/init.js` `MOUNT_PREFIXES` if the app lives in this repo, or use that project's own init script

Examples:
- `bore.rip/tools` → some tool
- `bore.rip/games` → game project
