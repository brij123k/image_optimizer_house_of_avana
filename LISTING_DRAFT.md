# Shopify App Store listing — draft to paste into the Partner Dashboard

Written against Shopify's App Store requirements. Limits are in brackets and were measured.
Files: icon `listing/app-icon-1200.png` (1200×1200), feature image `listing/feature-image-1600x900.png`, screenshots `listing/screenshots/` (1600×900).

## Name — must be the SAME in the listing and in the app's TOML file
**App name** [≤30, 25]: **One Globe Image Optimizer**
The TOML file (`shopify.app.image-optimizer.toml`) now uses this exact name. Brand first ("One Globe"), then what it does.
Before submitting, search the App Store for this name to be sure nobody else uses it or something very similar.

## Text (benefit-led, no data claims or guarantees)
**App card subtitle** [≤62, 53]
Help product pages load faster and be found in search

**App introduction** [≤100, 95]
Lighter product images and clear ALT text can help your pages load faster and appear in search.

**App details** [≤500, 418]
Compress product images in bulk, by store, collection or product, and convert them to WebP. AI writes a specific ALT text and file name for each image from what it shows, and suggests three keywords per image you can reuse in your content. Each ALT text is checked so keywords are not overused. Every change asks for your confirmation before it is saved to your store. Originals are replaced, so try one product first.

**Feature list** [≤80 each]
1. Compress images in bulk and convert them to WebP   (48)
2. AI-written ALT text and file names, specific to each image   (58)
3. Three keyword ideas per image to reuse in your content   (54)
4. An automatic check keeps ALT text natural, not keyword-stuffed   (62)
5. A confirmation before any change is saved to your store   (55)

**Search terms** [max 5, one idea each]: image optimizer, compress images, alt text, image seo, webp

**Integrations**: none. (Do not list Shopify itself.)

## Feature media and screenshots — alt text is required
| File | Alt text |
|---|---|
| `feature-image-1600x900.png` | App icon beside the words: Lighter product images. Clearer ALT text. |
| `01-dashboard.png` | App dashboard showing image credits, images compressed, storage saved and store coverage |
| `02-ai-alt-text-and-keywords.png` | AI labeling screen with options to write ALT text and rename image files |
| `03-compress-images.png` | Compress Images screen with quality setting, WebP option and scope by store or collection |
| `04-store-speed.png` | Store speed screen listing storefront image features and a button to check the storefront |
| `05-save-confirmation.png` | Confirmation window asking to save changes to the Shopify store, with Don't save selected first |

- Use the feature image as the **feature media** (or record a 2–3 minute promotional video, no more than 25% screencast).
- Screenshots: 3–6 desktop images, at least one showing the app. Already checked: no browser frame, no personal data, **no pricing**, no reviews, no outcome claims.
  The old plans screenshot was removed because it showed prices.

## Pricing
- Billing method: **One-time payment** (charged through Shopify Billing). The app has no subscription.
- **100 images — $2.00**, **500 images — $10.00**, **1000 images — $18.00**. Credits never expire. Merchants can buy another pack at any time from inside the app, with no reinstall and no need to contact support.
- Say in the pricing details: "Every store starts with 100 free images."
- Link the pricing page: `https://YOUR-DOMAIN/support` (or a pricing page of your own).

## Categories, eligibility and links
- **Category / tags**: choose the closest for image optimization and SEO. Pick features that are really true (compression, ALT text, file names).
- **Install eligibility**: requires the **Online Store** sales channel. No country, shipping or currency limits.
- **Privacy policy (required)**: `https://YOUR-DOMAIN/privacy`
- **Support / FAQ**: `https://YOUR-DOMAIN/support` · Support email: (set `SUPPORT_EMAIL` on the server)
- **Emergency developer contact**: (name, email, phone)
- **Demo store**: `mystore-123456789789457569.myshopify.com` — link straight to the app's Compress Images page and add: "Pick one product, press Optimize images, then Save to Shopify." Remove the store's password or give it to reviewers.

## App review instructions [paste]
1. Install the app on a development store and open it from Apps in the Shopify admin. You are taken to Shopify's approval screen first, then to the Dashboard. No account or store address is needed.
2. **Compress Images** → choose **A collection** → pick ONE product → **Optimize images**. A "Save to Shopify?" window appears. Press **Save to Shopify**. Expected: the image is replaced by a smaller one. Pressing **Don't save** changes nothing.
3. **Image ALT text** → tick **Update ALT text** → choose one product → **Run AI labeling** → confirm. Expected: each image gets its own ALT text. **Keyword suggestions** then lists 3 keywords for the product.
4. **Plans** → **Buy this pack** on the $2.00 pack → approve the test charge on Shopify's page. Expected: you return to the app and 100 credits are added.
5. Uninstall the app (Settings → Apps). Expected: the store's data is deleted through Shopify's privacy webhooks. Reinstalling starts the approval screen again.
Note: the app changes product images, so use a test store. English screencast: show every step above and the expected result of each.

## Requirement check (from Shopify's document)
- [x] **Authentication**: opening the app for a store that isn't logged in goes straight to Shopify's approval screen, also after an uninstall and reinstall (the saved login is checked with Shopify first).
- [x] **Permissions**: only `write_products` (it includes reading products). Nothing else is requested.
- [x] **No pop-up windows** for approval or payment; both use normal page redirects.
- [x] **Billing** uses Shopify's billing system; more credits can be bought any time without reinstalling.
- [x] **Cookies** are `SameSite=Lax`, `Secure` and `HttpOnly` on the live server.
- [x] **Privacy policy**, mandatory privacy webhooks and webhook signature checks in place.
- [ ] **Performance**: the storefront extension must not lower a store's Lighthouse score by more than 10 points (home 17%, product 40%, collection 43%). See the note below.
- [ ] Real HTTPS address deployed to Shopify, live test on a development store, support email set, screencast recorded.

## About the storefront extension and the performance rule
The extension (LazyLoad, responsive images, preloading and so on) changes how pages of real stores load, so Shopify will measure it. It has not been measured yet.
Safest plan: **submit the first version without the extension**, then add it later after testing with Lighthouse on a real theme. The listing above does not mention it.
