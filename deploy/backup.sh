#!/bin/sh
# Daily backup of the app's database. Run by cron (see DEPLOY_DIGITALOCEAN.md). Keeps 14 days.
set -e
DB="${SHOP_DB_PATH:-/var/lib/image-optimizer/shops.db}"
DIR=/var/backups/image-optimizer
mkdir -p "$DIR"
sqlite3 "$DB" ".backup '$DIR/shops-$(date +%F).db'"
find "$DIR" -name 'shops-*.db' -mtime +14 -delete
