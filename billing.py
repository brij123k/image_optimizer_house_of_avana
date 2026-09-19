"""Shopify Billing API — one-time image-credit packs.

Every shop gets billing_store.FREE_IMAGES optimizations for free, once. Past
that, they buy a pack (a one-time Shopify charge, not a recurring
subscription) and the images in it never expire.

Flow:
    1. Frontend calls POST /api/billing/purchase with a plan_id
    2. We ask Shopify to create the charge, record it as 'pending', and hand
       back the confirmation_url Shopify gave us
    3. Frontend sends the browser there; the merchant approves (or declines)
       on Shopify's own page
    4. Shopify redirects back to /billing/callback — we don't trust that
       redirect alone, we re-query Shopify for the charge's real status
       before crediting anything
"""
import os

from flask import Blueprint, jsonify, redirect, request

import billing_store
import token_store
from shopify_auth import current_shop, api_version, fresh_token
from shopify_client import ShopifyClient

billing_bp = Blueprint("billing", __name__)

PLANS = [
    {
        "id": "pack_100", "label": "100 images", "images": 100, "price": "2.00",
        "features": [
            "100 image credits", "Compression + WebP conversion",
            "AI ALT text labeling", "AI image file renaming", "Never expires",
        ],
    },
    {
        "id": "pack_500", "label": "500 images", "images": 500, "price": "10.00",
        "features": [
            "500 image credits", "Compression + WebP conversion",
            "AI ALT text labeling", "AI image file renaming", "Never expires", "Better price per image",
        ],
    },
    {
        "id": "pack_1000", "label": "1000 images", "images": 1000, "price": "18.00",
        "features": [
            "1000 image credits", "Compression + WebP conversion",
            "AI ALT text labeling", "AI image file renaming", "Never expires", "Best price per image",
        ],
    },
]
PLANS_BY_ID = {p["id"]: p for p in PLANS}

_PURCHASE_MUTATION = """
mutation($name: String!, $price: MoneyInput!, $returnUrl: URL!, $test: Boolean!) {
  appPurchaseOneTimeCreate(name: $name, price: $price, returnUrl: $returnUrl, test: $test) {
    appPurchaseOneTime { id }
    confirmationUrl
    userErrors { field message }
  }
}
"""

_PURCHASES_QUERY = """
{ currentAppInstallation { oneTimePurchases(first: 20, sortKey: CREATED_AT, reverse: true) {
    edges { node { id status } } } } }
"""


def _app_url():
    return os.environ.get("SHOPIFY_APP_URL", "").rstrip("/")


def _test_mode():
    # Real money only moves when this is explicitly turned off, so a dev
    # store or local testing never bills anyone by accident.
    return os.environ.get("SHOPIFY_BILLING_TEST", "true").lower() != "false"


def _client_for(shop):
    token = fresh_token(shop)
    if not token:
        raise PermissionError(f"{shop} hasn't installed the app.")
    return ShopifyClient(shop, token, api_version())


@billing_bp.route("/api/billing/plans")
def plans():
    return jsonify({"ok": True, "plans": PLANS})


@billing_bp.route("/api/billing/usage")
def usage():
    shop = current_shop()
    if not shop:
        return jsonify({"ok": False, "error": "No shop in session."}), 401
    return jsonify({"ok": True, **billing_store.get_usage(shop)})


@billing_bp.route("/api/billing/purchase", methods=["POST"])
def purchase():
    shop = current_shop()
    if not shop:
        return jsonify({"ok": False, "error": "No shop in session."}), 401

    plan = PLANS_BY_ID.get((request.get_json(silent=True) or {}).get("plan_id"))
    if not plan:
        return jsonify({"ok": False, "error": "Unknown plan."}), 400

    try:
        client = _client_for(shop)
        data = client.graphql(_PURCHASE_MUTATION, {
            "name": f"Image Compactor — {plan['label']}",
            "price": {"amount": plan["price"], "currencyCode": "USD"},
            "returnUrl": f"{_app_url()}/billing/callback",
            "test": _test_mode(),
        })
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"Could not start checkout: {exc}"}), 502

    result = data["appPurchaseOneTimeCreate"]
    errors = result.get("userErrors") or []
    if errors:
        return jsonify({"ok": False, "error": errors[0]["message"]}), 400

    charge_id = result["appPurchaseOneTime"]["id"]
    billing_store.record_purchase(charge_id, shop, plan["id"], plan["images"], plan["price"])
    return jsonify({"ok": True, "confirmation_url": result["confirmationUrl"]})


@billing_bp.route("/billing/callback")
def billing_callback():
    shop = current_shop()
    if not shop:
        return redirect("/")

    pending = billing_store.pending_charge_ids(shop)
    if pending:
        try:
            client = _client_for(shop)
            data = client.graphql(_PURCHASES_QUERY)
            for edge in data["currentAppInstallation"]["oneTimePurchases"]["edges"]:
                node = edge["node"]
                if node["id"] in pending and node["status"] == "ACTIVE":
                    billing_store.credit_purchase(node["id"])
        except Exception:  # noqa: BLE001 - the merchant still lands on the app either way
            pass

    return redirect(f"/?shop={shop}&billing=done")
