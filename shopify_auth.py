"""Shopify OAuth (authorization code grant) for a non-embedded Flask app.

Flow:
    1. Merchant hits /auth?shop=their-store.myshopify.com
    2. We validate the shop domain, mint a nonce, and redirect to Shopify
    3. Shopify sends them back to /auth/callback with a code
    4. We verify the HMAC and the nonce, swap the code for a token, store it

Every parameter arriving from Shopify is attacker-controllable until proven
otherwise, so each one is validated before it's used.
"""
import hashlib
import hmac
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import requests
from flask import Blueprint, current_app, jsonify, redirect, request, session

import token_store

auth_bp = Blueprint("auth", __name__)

# Only these hostnames are ever redirected to or called. Without this check a
# crafted ?shop= parameter would turn the app into an open redirect and leak
# the authorization code to whoever asked.
SHOP_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9\-]*\.myshopify\.com$")

STATE_KEY = "shopify_oauth_state"
STATE_SHOP_KEY = "shopify_oauth_shop"


def _cfg(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"Missing {name}. Set it in .env or your host's environment.")
    return value


def api_version():
    return _cfg("SHOPIFY_API_VERSION", "2026-07")


def valid_shop(shop):
    return bool(shop) and bool(SHOP_RE.match(shop))


def verify_query_hmac(args):
    """Confirms a redirect from Shopify really came from Shopify.

    The signature covers every query parameter except hmac/signature, sorted by
    key and joined as k=v pairs.
    """
    secret = _cfg("SHOPIFY_API_SECRET", required=True)
    provided = args.get("hmac", "")
    if not provided:
        return False
    pairs = sorted(
        (k, v) for k, v in args.items(multi=False) if k not in ("hmac", "signature")
    )
    message = "&".join(f"{k}={v}" for k, v in pairs)
    digest = hmac.new(secret.encode(), message.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(digest, provided)


def verify_webhook_hmac(raw_body, header_hmac):
    """Webhooks sign the raw request body and send it base64-encoded — a
    different scheme from the hex query-string HMAC above."""
    import base64
    secret = _cfg("SHOPIFY_API_SECRET", required=True)
    digest = base64.b64encode(
        hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(digest, header_hmac or "")


# --------------------------------------------------------------------------
# Install
# --------------------------------------------------------------------------

@auth_bp.route("/auth")
def begin_auth():
    """Entry point. Send merchants here to install:
        https://your-app.com/auth?shop=their-store.myshopify.com
    """
    shop = (request.args.get("shop") or "").strip().lower()
    if not valid_shop(shop):
        return jsonify({
            "ok": False,
            "error": "Pass a valid ?shop=your-store.myshopify.com",
        }), 400

    # If Shopify itself sent the merchant here it signs the request; verify when
    # a signature is present, but allow a bare link with no hmac.
    if request.args.get("hmac") and not verify_query_hmac(request.args):
        return jsonify({"ok": False, "error": "Request signature failed."}), 401

    nonce = secrets.token_urlsafe(24)
    session[STATE_KEY] = nonce
    session[STATE_SHOP_KEY] = shop

    query = urlencode({
        "client_id": _cfg("SHOPIFY_API_KEY", required=True),
        "scope": _cfg("SHOPIFY_SCOPES", "read_products,write_products,read_metafields,write_metafields"),
        "redirect_uri": _cfg("SHOPIFY_APP_URL", required=True).rstrip("/") + "/auth/callback",
        "state": nonce,
        # Omit grant_options[] to get an offline token — one that keeps working
        # for background jobs after the merchant closes the browser.
    })
    return redirect(f"https://{shop}/admin/oauth/authorize?{query}")


@auth_bp.route("/auth/callback")
def callback():
    """Shopify redirects here after the merchant approves the scopes."""
    shop = (request.args.get("shop") or "").strip().lower()
    code = request.args.get("code")
    state = request.args.get("state")

    if not valid_shop(shop):
        return jsonify({"ok": False, "error": "Invalid shop domain."}), 400
    if not code:
        return jsonify({"ok": False, "error": "Missing authorization code."}), 400

    # Nonce check: proves this callback belongs to a flow we started, not one an
    # attacker triggered in the merchant's browser.
    expected = session.pop(STATE_KEY, None)
    expected_shop = session.pop(STATE_SHOP_KEY, None)
    if not expected or not state or not hmac.compare_digest(expected, state):
        return jsonify({"ok": False, "error": "State mismatch — start the install again."}), 401
    if expected_shop != shop:
        return jsonify({"ok": False, "error": "Shop mismatch — start the install again."}), 401

    if not verify_query_hmac(request.args):
        return jsonify({"ok": False, "error": "Request signature failed."}), 401

    try:
        resp = requests.post(
            f"https://{shop}/admin/oauth/access_token",
            json={
                "client_id": _cfg("SHOPIFY_API_KEY", required=True),
                "client_secret": _cfg("SHOPIFY_API_SECRET", required=True),
                "code": code,
                # Shopify no longer accepts non-expiring offline tokens.
                "expiring": 1,
            },
            timeout=20,
        )
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as exc:
        current_app.logger.warning("Token exchange failed for %s: %s", shop, exc)
        return jsonify({"ok": False, "error": "Could not complete the install."}), 502

    access_token = payload.get("access_token")
    if not access_token:
        return jsonify({"ok": False, "error": "Shopify returned no access token."}), 502

    _save_token_payload(shop, payload)
    register_uninstall_webhook(shop, access_token)

    # Mark this browser session as belonging to the shop, so the UI knows which
    # store it's acting on without the token ever reaching the browser.
    session["shop"] = shop
    return redirect(f"/?shop={shop}")


# --------------------------------------------------------------------------
# Uninstall
# --------------------------------------------------------------------------

def _expiry(seconds):
    if not seconds:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=int(seconds))).isoformat(timespec="seconds")


def _save_token_payload(shop, payload):
    token_store.save_shop(
        shop, payload["access_token"], payload.get("scope"),
        refresh_token=payload.get("refresh_token"),
        expires_at=_expiry(payload.get("expires_in")),
        refresh_expires_at=_expiry(payload.get("refresh_token_expires_in")),
    )


def fresh_token(shop):
    """Returns a valid access token for the shop, refreshing it first if it
    expires within a minute. None if the shop isn't installed."""
    rec = token_store.get_shop(shop)
    if not rec:
        return None
    exp = rec.get("expires_at")
    if exp and rec.get("refresh_token"):
        soon = datetime.now(timezone.utc) + timedelta(seconds=60)
        if datetime.fromisoformat(exp) <= soon:
            resp = requests.post(
                f"https://{shop}/admin/oauth/access_token",
                json={
                    "client_id": _cfg("SHOPIFY_API_KEY", required=True),
                    "client_secret": _cfg("SHOPIFY_API_SECRET", required=True),
                    "grant_type": "refresh_token",
                    "refresh_token": rec["refresh_token"],
                },
                timeout=20,
            )
            resp.raise_for_status()
            payload = resp.json()
            _save_token_payload(shop, payload)
            return payload["access_token"]
    return rec["access_token"]


def register_uninstall_webhook(shop, access_token):
    """Ask Shopify to tell us when the merchant uninstalls. Best effort — a
    failure here shouldn't break an otherwise successful install."""
    try:
        requests.post(
            f"https://{shop}/admin/api/{api_version()}/webhooks.json",
            headers={"X-Shopify-Access-Token": access_token,
                     "Content-Type": "application/json"},
            json={"webhook": {
                "topic": "app/uninstalled",
                "address": _cfg("SHOPIFY_APP_URL", "").rstrip("/") + "/webhooks/uninstalled",
                "format": "json",
            }},
            timeout=15,
        )
    except requests.RequestException as exc:
        current_app.logger.warning("Could not register uninstall webhook for %s: %s", shop, exc)


@auth_bp.route("/webhooks/uninstalled", methods=["POST"])
def uninstalled():
    """The token is already dead by the time this arrives; drop our copy."""
    if not verify_webhook_hmac(request.get_data(), request.headers.get("X-Shopify-Hmac-Sha256")):
        return "", 401
    shop = request.headers.get("X-Shopify-Shop-Domain", "").strip().lower()
    if valid_shop(shop):
        token_store.delete_shop(shop)
        current_app.logger.info("Uninstalled: %s", shop)
    return "", 200


# --------------------------------------------------------------------------
# Helpers for the rest of the app
# --------------------------------------------------------------------------

def current_shop():
    """The shop this request is acting on: the browser session first, then a
    ?shop= parameter, then the single-store .env fallback for local use."""
    shop = session.get("shop")
    if valid_shop(shop):
        return shop
    shop = (request.args.get("shop") or "").strip().lower()
    if valid_shop(shop) and token_store.get_token(shop):
        session["shop"] = shop
        return shop
    fallback = (os.environ.get("SHOPIFY_STORE_DOMAIN") or "").strip().lower()
    return fallback or None


def current_credentials():
    """Returns (shop, access_token). Raises if this browser isn't installed on
    any shop, so callers can send the merchant to /auth."""
    shop = current_shop()
    if not shop:
        raise PermissionError("No shop in session. Visit /auth?shop=your-store.myshopify.com")
    token = fresh_token(shop) or os.environ.get("SHOPIFY_ACCESS_TOKEN")
    if not token:
        raise PermissionError(f"{shop} hasn't installed the app yet. Visit /auth?shop={shop}")
    return shop, token


@auth_bp.route("/auth/logout", methods=["POST"])
def logout():
    """Clears the browser session. Does not uninstall or revoke the token."""
    session.clear()
    return jsonify({"ok": True})
