"""Shopify's mandatory privacy webhooks.

Every public app must answer three requests, all signed by Shopify:
  customers/data_request  – a shopper asks what data the app holds about them
  customers/redact        – delete a shopper's data
  shop/redact             – 48 hours after uninstall, delete the store's data

This app never stores anything about a store's shoppers (no customer records,
no order data), so the two customer requests need no action beyond an
acknowledgement. shop/redact removes everything the app keeps about the store.

Billing records (the `purchases` table) are kept: they are financial records the
business must retain. They hold only the charge id, plan, price and store domain.
"""
import sqlite3

from flask import Blueprint, current_app, request

from shopify_auth import valid_shop, verify_webhook_hmac
from token_store import conn as _conn

compliance_bp = Blueprint("compliance", __name__)

# table -> column that holds the shop domain
SHOP_TABLES = {
    "shops": "shop",
    "usage": "shop",
    "stats": "shop",
    "image_keywords": "shop",
    "access_log": "shop",
    "speed_checks": "shop",
}


def delete_shop_data(shop):
    """Removes everything stored about a shop, except billing records.
    Returns {table: rows_deleted}."""
    c = _conn()
    removed = {}
    for table, col in SHOP_TABLES.items():
        try:
            cur = c.execute(f"DELETE FROM {table} WHERE {col} = ?", (shop,))
            removed[table] = cur.rowcount
        except sqlite3.OperationalError:      # table was never created — nothing to delete
            continue
    c.commit()
    return removed


@compliance_bp.route("/webhooks/compliance", methods=["POST"])
def compliance():
    if not verify_webhook_hmac(request.get_data(), request.headers.get("X-Shopify-Hmac-Sha256")):
        return "", 401

    topic = (request.headers.get("X-Shopify-Topic") or "").strip().lower()
    payload = request.get_json(silent=True) or {}
    shop = (payload.get("shop_domain") or request.headers.get("X-Shopify-Shop-Domain") or "").strip().lower()

    if topic == "shop/redact" and valid_shop(shop):
        removed = delete_shop_data(shop)
        current_app.logger.info("shop/redact for %s: %s", shop, removed)
    elif topic in ("customers/data_request", "customers/redact"):
        # No shopper data is stored, so there is nothing to export or delete.
        current_app.logger.info("%s for %s: no customer data held", topic, shop)
    else:
        return "", 400
    return "", 200
