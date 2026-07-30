# Product site deployment

The public MealCircuit product site is a dependency-free static website in `site/`. It is designed for Cloudflare Pages and does not expose the loopback-only MealCircuit Web UI, private user data, a Pages Function, or any runtime secret.

## What is deployed

- `/` — English product page
- `/zh/` — Simplified Chinese product page
- `/404.html` — explicit not-found page, so Cloudflare Pages does not apply SPA fallback behavior
- `/_headers` — security and asset-cache policies interpreted by Cloudflare Pages
- `/robots.txt`, `/site.webmanifest`, and `/favicon.svg` — public site metadata

`wrangler.jsonc` keeps the Pages project name, static output directory, compatibility date, and Wrangler telemetry preference in source control. There are no Pages bindings or environment variables.

## Local verification

From the repository root:

```bash
python -m http.server 4173 --directory site
```

Open <http://127.0.0.1:4173/> and <http://127.0.0.1:4173/zh/>. Stop the server with `Ctrl+C`.

If Wrangler is already available, its Pages-compatible local server can be used instead:

```bash
npx wrangler@latest pages dev site
```

This command may download Wrangler into the package runner cache; Wrangler is intentionally not a repository dependency.

## Cloudflare Pages Git configuration

1. In the Cloudflare dashboard, open **Workers & Pages** and create a **Pages** application using **Connect to Git**.
2. Authorize and select `QianQIUlp/meal-circuit`.
3. Use the following settings:

| Setting | Value |
| --- | --- |
| Project name | `meal-circuit` |
| Production branch | `main` |
| Framework preset | `None` |
| Root directory | Leave blank (repository root) |
| Build command | `exit 0` |
| Build output directory | `site` |

No environment variables or secrets are required. The dashboard values intentionally match `wrangler.jsonc`; keep the file as the configuration source of truth.

Cloudflare uploads the contents of `site/` after a successful build. A push to `main` updates production, while other enabled branches can receive preview deployments. Cloudflare documents this flow in [Git integration](https://developers.cloudflare.com/pages/configuration/git-integration/), [build configuration](https://developers.cloudflare.com/pages/configuration/build-configuration/), and [static HTML deployment](https://developers.cloudflare.com/pages/framework-guides/deploy-anything/).

## Build watch paths

If build watch paths are enabled in **Settings → Builds**, include:

```text
site/*
wrangler.jsonc
```

Leave excludes empty. This keeps desktop, Android, sync-server, and documentation-only commits from redeploying an unchanged product site.

## Optional custom domain

After the generated `*.pages.dev` deployment is healthy:

1. Open the Pages project and choose **Custom domains → Set up a domain**.
2. Enter the chosen hostname.
3. Let Cloudflare create the DNS record when the zone is in the same account. If DNS is hosted elsewhere, finish the Pages domain association before creating the instructed CNAME.
4. Wait for the domain status to become **Active**, then verify HTTPS.

The repository intentionally does not declare a canonical hostname or sitemap before a real custom domain is selected. After selection, add absolute canonical, `og:url`, sitemap, and `robots.txt` sitemap URLs together so preview and production metadata do not point at an invented address.

## Post-deployment checks

- `/` renders the English page and `/zh/` renders the Chinese page.
- Both language links switch routes and all GitHub/release links reach `QianQIUlp/meal-circuit`.
- An unknown route returns the custom 404 page instead of the homepage.
- Response headers include the CSP, frame, permissions, content-type, and referrer policies from `site/_headers`.
- Desktop, tablet, and phone widths have no page-level horizontal overflow.
- A non-`main` branch preview does not replace the production deployment.

Direct upload remains available for maintainers who deliberately choose it:

```bash
npx wrangler@latest pages deploy site --project-name meal-circuit
```

This is an external production write and should only be run with an authenticated Cloudflare account and explicit release intent.
