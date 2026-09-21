# Deploying the storefront speed extension

The extension lives in `extensions/one-globe-image-optimizer/`. Until it is deployed, the
**Store speed** page shows only Image compression and Minification.

## Before you start
- Node 18+ (you have 22) and a Shopify Partner / Dev Dashboard login.
- `shopify.app.toml` is pre-filled from `.env`. **`shopify app deploy` pushes it to Shopify**,
  replacing the app's URL and scopes in the Dev Dashboard.
  - If the dashboard currently shows different URLs or scopes, run `shopify app config link`
    first. It rewrites `shopify.app.toml` from the dashboard, then re-check the values.
  - The toml asks for the scopes `read_products,write_products`. Do not add `read_metafields` or
    `write_metafields`: they are not real Shopify scopes and make the deploy fail. The app saves its
    "already optimized" record without them.
- The `application_url` is your ngrok address. It changes whenever ngrok restarts —
  update `SHOPIFY_APP_URL` in `.env` and the two URLs in `shopify.app.toml`, then deploy again.

## Steps
1. Install the CLI: `npm install -g @shopify/cli`
2. Log in: `shopify auth login`
3. From this folder: `shopify app config link`  (choose your app; keep or compare the toml)
4. Deploy: `shopify app deploy`  (confirm the extension "one-globe-image-optimizer" is listed)
5. Tell the app the extension exists — add to `.env` and restart the server:
   `SPEED_EXTENSION_LIVE=true`
6. In the store admin: Online Store > Themes > Customize > **App embeds** >
   switch on **one-globe-image-optimizer**, choose the options, Save.
7. In the app open **Store speed** and press **Check my storefront**. Each storefront
   feature should now show On or Off.

## If something goes wrong
- `deploy` rejects `shopify.extension.toml`: generate a fresh one with
  `shopify app generate extension --template theme_app_extension`, then copy
  `blocks/speed.liquid` and `assets/speed.js` into the new folder.
- The embed doesn't appear in the theme editor: the store must have the app installed, and
  the theme must be an Online Store 2.0 theme.
- Install shows "This app is under review": the app is set to public distribution and is not
  approved yet. Use custom distribution for a named store, or your development store.
- Rebuilding `assets/speed.js` after editing `extension-src/one-globe-image-optimizer/speed.src.js`:
  `npx terser extension-src/one-globe-image-optimizer/speed.src.js --compress --mangle -o extensions/one-globe-image-optimizer/assets/speed.js`
