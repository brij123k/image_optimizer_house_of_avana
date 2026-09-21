# Image Optimizer — House of Avana

Local Flask tool that compresses Shopify product images in place.
Scope by entire store or by collection, with per-product selection
and optional WebP conversion.

## Setup

    python3 -m venv venv
    source venv/bin/activate
    pip install -r requirements.txt
    cp .env.example .env    # then fill in your credentials
    python3 app.py

Open http://localhost:5056

## Storefront speed features (theme app extension)

`extensions/one-globe-image-optimizer/` adds an **app embed** that runs on the store's
pages: LazyLoad, responsive images (srcset), main-image preload, critical CSS
inlining, deferred CSS, and best-effort JS defer / app-script delay. Everything
is off until the merchant enables it in the theme editor.

It is separate from the Flask app and needs the Shopify CLI to deploy:

    npm install -g @shopify/cli
    shopify auth login
    shopify app config link      # links this folder to your existing app; creates shopify.app.toml
    shopify app deploy           # publishes the extension

Then, in the store admin: Online Store > Themes > Customize > App embeds >
turn on "one-globe-image-optimizer" and pick the options.

`assets/speed.js` is the minified build of `extension-src/one-globe-image-optimizer/speed.src.js`. After editing
the source, rebuild it:

    npx terser extension-src/one-globe-image-optimizer/speed.src.js --compress --mangle \
        -o extensions/one-globe-image-optimizer/assets/speed.js

If `shopify app deploy` rejects `shopify.extension.toml`, generate a fresh one
with `shopify app generate extension --template theme_app_extension` and copy
`blocks/speed.liquid` and `assets/speed.js` into it.

## Going live (DigitalOcean + Shopify App Store)

- Server setup, HTTPS, database backups and switching Shopify to the real address: **[DEPLOY_DIGITALOCEAN.md](DEPLOY_DIGITALOCEAN.md)**
- Server files are in `deploy/` (gunicorn, systemd, nginx, backup, `.env.production.example`).
- `APP_ENV=production` turns on the live-server protections: only a logged-in session or a request Shopify signed identifies a
  store (a bare `?shop=` does nothing), forged requests are blocked, cookies are HTTPS-only, and the local test-store settings
  (`SHOPIFY_STORE_DOMAIN`, `SHOPIFY_ACCESS_TOKEN`) are ignored.
- Shopify's mandatory privacy webhooks are handled in `compliance.py`; the public pages are `/privacy` and `/support`.
