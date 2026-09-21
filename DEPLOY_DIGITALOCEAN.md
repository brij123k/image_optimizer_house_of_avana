# Putting the app live on DigitalOcean

You need: a DigitalOcean account, a **domain name** (about $10/year), and this project folder.
Below, `app.example.com` means the address you choose (for example `app.yourdomain.com`).

## 0. What you are building
```
Shopify / merchants  --HTTPS-->  nginx (port 443)  -->  gunicorn (port 8000)  -->  this Flask app  -->  SQLite file on the server's disk
```
The database is a single file, `/var/lib/image-optimizer/shops.db`, backed up every night.

## 1. Create the server (Droplet)
- DigitalOcean > Create > Droplet. Ubuntu **24.04**. Plan: **2 GB RAM** (about $12/month). The background-removal
  tool loads an AI model and needs the memory; 1 GB can run out.
- Choose a region close to your merchants, add your SSH key (or a password), create it. Note its IP address.

## 2. Point your domain at it
At your domain registrar, add an **A record**: name `app`, value = the Droplet's IP. Wait a few minutes.
Check: `ping app.example.com` shows your IP.

## 3. Set up the server
Log in: `ssh root@YOUR_IP`. Then run these one at a time:
```
apt update && apt upgrade -y
apt install -y python3-venv python3-pip nginx certbot python3-certbot-nginx sqlite3 ufw rsync
ufw allow OpenSSH && ufw allow 'Nginx Full' && ufw --force enable

adduser --system --group --home /opt/image-optimizer imageopt
mkdir -p /var/lib/image-optimizer/u2net && chown -R imageopt:imageopt /var/lib/image-optimizer
```

## 4. Upload the app (run on YOUR Mac, in the project folder)
```
rsync -av --exclude venv --exclude .env --exclude '.env.*' --exclude shops.db* --exclude backups \
      --exclude .shopify --exclude node_modules --exclude __pycache__ --exclude .git \
      ./ root@YOUR_IP:/opt/image-optimizer/
```
On the server:
```
chown -R imageopt:imageopt /opt/image-optimizer
cd /opt/image-optimizer
sudo -u imageopt python3 -m venv venv
sudo -u imageopt venv/bin/pip install -r requirements.txt
```

## 5. Add the settings
```
cp deploy/.env.production.example .env
nano .env                      # fill in every value; set SHOPIFY_APP_URL to https://app.example.com
chown imageopt:imageopt .env && chmod 600 .env
```
`APP_ENV=production` turns on the live-server protections. **Do not** add `SHOPIFY_STORE_DOMAIN`,
`SHOPIFY_ACCESS_TOKEN` or `FLASK_DEBUG` on the server.

## 6. Start the app, and keep it running
```
cp deploy/image-optimizer.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now image-optimizer
systemctl status image-optimizer          # should say "active (running)"
journalctl -u image-optimizer -f           # live log (Ctrl+C to leave)
```

## 7. HTTPS with nginx
```
cp deploy/nginx.conf /etc/nginx/sites-available/image-optimizer
sed -i 's/app.example.com/app.YOURDOMAIN.com/' /etc/nginx/sites-available/image-optimizer
ln -s /etc/nginx/sites-available/image-optimizer /etc/nginx/sites-enabled/
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx
certbot --nginx -d app.YOURDOMAIN.com      # follow the prompts; it renews itself
```
Check: open `https://app.YOURDOMAIN.com/privacy` in a browser. You should see the privacy policy.

## 8. Nightly database backup
```
chmod +x /opt/image-optimizer/deploy/backup.sh
( crontab -l 2>/dev/null; echo "15 3 * * * SHOP_DB_PATH=/var/lib/image-optimizer/shops.db /opt/image-optimizer/deploy/backup.sh" ) | crontab -
```
Backups land in `/var/backups/image-optimizer/`. Also turn on DigitalOcean's own **Droplet Backups** (weekly snapshot).

## 9. Tell Shopify the new address (on YOUR Mac, in the project folder)
```
cp shopify.app.production.toml.example shopify.app.production.toml
# edit it: replace app.example.com with your domain (3 places)
npx @shopify/cli@latest app config link         # choose the production config name when asked
npx @shopify/cli@latest app deploy --config production
```
This puts your server's address into Shopify for the app URL, the login redirect and the privacy webhooks.
ngrok is no longer used.

## 10. Test before you submit
1. Install the app on a development store from the Dev Dashboard. You should land on the app already logged in.
2. Run compression on one product, and check the ledger and the dashboard numbers.
3. Buy a pack: `SHOPIFY_BILLING_TEST=true` means no real money moves. Approve the test charge and check credits appear.
4. Uninstall the app, wait, and confirm the token was removed (`journalctl -u image-optimizer` shows the webhooks).
5. Only then set `SHOPIFY_BILLING_TEST=false` (in `.env`, then `systemctl restart image-optimizer`) and submit the listing.

## Updating later
Re-run the `rsync` from step 4, then `systemctl restart image-optimizer`. If `requirements.txt` changed, run the pip install line first.

## If something breaks
- App not starting: `journalctl -u image-optimizer -n 50`.
- 502 Bad Gateway from nginx: the app is not running; see the line above.
- Shopify says "redirect_uri is not whitelisted": the redirect URL in `shopify.app.production.toml` was not deployed (step 9).
- Cookies / login loop: check `SHOPIFY_APP_URL` is `https://...` exactly and matches the domain in the browser.
