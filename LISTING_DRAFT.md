# Shopify App Store listing — draft to paste into the Partner Dashboard

Partner Dashboard → App distribution → All apps → the app → App Store listing. Character limits are in brackets.
Files: icon `listing/app-icon-1200.png` (1200×1200), screenshots `listing/screenshots/` (1600×900).

## Text

**App name** [≤30, 25]
One Globe Image Optimizer

**App introduction** [≤100, 91]
Compress product images, write AI ALT text and get keyword ideas, without keyword stuffing.

**Tagline / subtitle** [74]
Smaller images, AI ALT text and file names, and 3 keyword ideas per image.

**App details** [≤500, 385]
Big product photos slow your store down. One Globe Image Optimizer compresses your images (and can convert them to WebP), then uses AI to write a specific ALT text and file name for every image, plus 3 keyword ideas you can reuse in blogs and content. Every ALT text is checked so keywords are not stuffed. Preview, then choose to save: nothing changes in your store until you confirm.

**Feature list** [≤80 each]
1. Compress images in bulk and convert to WebP, by store, collection or product   (76)
2. AI ALT text and file names written for each image, not one text for all   (71)
3. 3 keyword ideas per image to reuse in blogs, descriptions and content   (69)
4. Keyword-stuffing checker rewrites over-optimised ALT text automatically   (71)
5. Asks 'Save to Shopify?' before every change; shows your storefront image weight   (79)

**Search terms**: image optimizer, compress images, alt text, image seo, webp, file names, keywords

## Pricing (one-time purchases, no subscription)
- **Free** — first 100 images on every store (compression, AI ALT text, file renaming, keyword ideas)
- **100 images — $2.00**, **500 images — $10.00**, **1000 images — $18.00** (credits never expire)

## Screenshots (1600×900) — suggested captions
1. `01-dashboard.png` — See credits, savings and store coverage at a glance
2. `02-ai-alt-text-and-keywords.png` — AI ALT text, file names and 3 keywords per image, with anti-stuffing
3. `03-compress-images.png` — Compress a whole store, a collection or a single product
4. `04-store-speed.png` — Check how much your storefront images weigh
5. `05-plans.png` — Simple one-time credit packs, 100 images free

## Links and contacts (fill in before submitting)
- Privacy policy: `https://YOUR-DOMAIN/privacy`
- Support: `https://YOUR-DOMAIN/support`  ·  Support email: **(set SUPPORT_EMAIL)**
- Emergency developer contact: **(name, email, phone)**
- Demo store: `mystore-123456789789457569.myshopify.com`  (remove its storefront password, or give the reviewers the password)

## Test instructions for Shopify's reviewers [paste]
1. Install the app on a development store, then open it from Apps in the Shopify admin. You land on the Dashboard, already logged in. No account or store address is needed.
2. Open **Compress Images**, choose **A collection**, pick ONE product, press **Optimize images**. A "Save to Shopify?" window appears — press **Save to Shopify**. The image is replaced by a smaller one.
3. Open **Image ALT text**, tick **Update ALT text**, choose one product, press **Run AI labeling**, confirm. Each image gets its own ALT text. Open **Keyword suggestions** to see the 3 keywords per product.
4. Open **Plans** and press **Buy this pack** on the $2.00 pack. Approve the (test) charge on Shopify's page. You return to the app and the credits are added.
5. Uninstall the app from Settings → Apps. The store's data is deleted through Shopify's privacy webhooks.
Notes: the app changes product images in the store, so use a test store. "Don't save" in the confirmation window changes nothing.

## Screencast outline (about 3 minutes)
Install → open from admin → Dashboard tour (20s) → compress one product with the confirm window (40s) → AI ALT text + keywords (50s) → Plans and a test purchase (40s) → uninstall (10s).

## Before you submit — status
- [x] Data-deletion webhooks, privacy page, support page, install flow, security fixes (in the code)
- [ ] App on a real HTTPS server and the live address deployed to Shopify (see DEPLOY_DIGITALOCEAN.md)
- [ ] `SUPPORT_EMAIL` set; privacy policy read by someone
- [ ] Full test on the live server with a development store (install, compress, test purchase, uninstall)
- [ ] `SHOPIFY_BILLING_TEST=false` just before submitting
- [ ] Theme extension tested in a real theme — leave the six storefront features OUT of the listing until then
- [ ] Screencast recorded, emergency contact added
