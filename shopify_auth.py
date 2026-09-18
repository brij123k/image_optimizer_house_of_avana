"""Shopify OAuth (authorization code grant) for a non-embedded Flask app.

Flow:
    1. Merchant hits /auth?shop=their-store.myshopify.com
    2. We validate the shop domain, mint a nonce, and redirect to Shopify
    3. Shopify sends them back to /auth/callback with a code
    4. We verify the HMAC and the nonce, swap the code for a token, store it

Every parameter arriving from Shopify is attacker-controllable until proven
otherwise, so each one is validated before it's used.
"""
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from urllib.parse import urlencode

import requests
from flask import Blueprint, current_app, jsonify, redirect, request, session

import billing_store
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
    secret = _cfg("SHOPIFY_API_SECRET", required=True)
    digest = base64.b64encode(
        hmac.new(secret.encode(), raw_body, hashlib.sha256).digest()
    ).decode()
    return hmac.compare_digest(digest, header_hmac or "")


# --------------------------------------------------------------------------
# Embedded app: session tokens + token exchange
#
# App Bridge (loaded in templates/index.html) attaches a short-lived session
# token (a JWT) to every request it makes on the embedded app's behalf. This
# is the embedded-app equivalent of the classic OAuth flow above: instead of
# a session cookie proving who's asking, the signed token itself does —
# which matters because third-party cookies are increasingly unavailable
# inside the Shopify Admin iframe this app runs in when embedded.
# --------------------------------------------------------------------------

def _b64url_decode(segment):
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def verify_session_token(token):
    """Verifies a Shopify session token and returns the shop domain it was
    issued for, or None if it's missing, mis-signed, expired, or meant for a
    different app. Hand-rolled rather than pulling in a JWT library — the
    check is the same three-part-signature shape as the HMAC checks above."""
    if not token or token.count(".") != 2:
        return None
    header_b64, payload_b64, signature_b64 = token.split(".")

    secret = _cfg("SHOPIFY_API_SECRET", required=True)
    expected_sig = hmac.new(
        secret.encode(), f"{header_b64}.{payload_b64}".encode(), hashlib.sha256
    ).digest()
    try:
        provided_sig = _b64url_decode(signature_b64)
    except (ValueError, TypeError):
        return None
    if not hmac.compare_digest(expected_sig, provided_sig):
        return None

    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, TypeError):
        return None

    if payload.get("aud") != _cfg("SHOPIFY_API_KEY", required=True):
        return None
    if payload.get("exp", 0) < time.time():
        return None

    dest = (payload.get("dest") or "").replace("https://", "").replace("http://", "").rstrip("/")
    return dest if valid_shop(dest) else None


def exchange_token_for_shop(shop, session_token):
    """Token exchange: swaps a verified session token for a real offline
    access token — the embedded-app equivalent of the classic authorization
    code swap in callback() below. Stores the result the same way."""
    try:
        resp = requests.post(
            f"https://{shop}/admin/oauth/access_token",
            json={
                "client_id": _cfg("SHOPIFY_API_KEY", required=True),
                "client_secret": _cfg("SHOPIFY_API_SECRET", required=True),
                "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
                "subject_token": session_token,
                "subject_token_type": "urn:ietf:params:oauth:token-type:id_token",
                # Must be Shopify's own offline-token URN, not the generic IETF
                # "access_token" value — that generic value is what silently
                # produced an ONLINE token with truncated scope last time.
                # Offline matches what the rest of the app needs: a token that
                # keeps working in background threads with no browser present.
                "requested_token_type": "urn:shopify:params:oauth:token-type:offline-access-token",
            },
            timeout=20,
        )
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as exc:
        current_app.logger.warning("Token exchange failed for %s: %s", shop, exc)
        return None

    access_token = payload.get("access_token")
    if not access_token:
        return None
    token_store.save_shop(shop, access_token, payload.get("scope"))
    return access_token


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

    token_store.save_shop(shop, access_token, payload.get("scope"))
    register_uninstall_webhook(shop, access_token)

    # Mark this browser session as belonging to the shop, so the UI knows which
    # store it's acting on without the token ever reaching the browser.
    session["shop"] = shop
    return redirect(f"/?shop={shop}")


# --------------------------------------------------------------------------
# Uninstall
# --------------------------------------------------------------------------

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
# GDPR compliance webhooks
#
# Shopify requires every app to expose these three endpoints — configured in
# the Partner Dashboard under App setup > Compliance webhooks, not registered
# via the Webhook API like app/uninstalled above. All three must verify the
# same HMAC scheme as the uninstall webhook.
# --------------------------------------------------------------------------

@auth_bp.route("/webhooks/customers/data_request", methods=["POST"])
def customers_data_request():
    """A customer (via the merchant) asked what data we hold on them. This
    app never stores customer-level personal data — only shop-level image
    optimization records and Shopify access tokens — so there is nothing
    customer-specific to return. Acknowledging is all that's required."""
    if not verify_webhook_hmac(request.get_data(), request.headers.get("X-Shopify-Hmac-Sha256")):
        return "", 401
    current_app.logger.info(
        "GDPR data_request for %s", request.headers.get("X-Shopify-Shop-Domain")
    )
    return "", 200


@auth_bp.route("/webhooks/customers/redact", methods=["POST"])
def customers_redact():
    """Asks us to erase a specific customer's data. Same as above — we hold
    no customer-level data, so there is nothing to erase."""
    if not verify_webhook_hmac(request.get_data(), request.headers.get("X-Shopify-Hmac-Sha256")):
        return "", 401
    current_app.logger.info(
        "GDPR customer redact for %s", request.headers.get("X-Shopify-Shop-Domain")
    )
    return "", 200


@auth_bp.route("/webhooks/shop/redact", methods=["POST"])
def shop_redact():
    """Sent ~48 hours after uninstall — erase everything we still hold for
    this shop. The access token is already gone (from the uninstall webhook),
    but this also purges billing/usage records, in case that webhook never
    arrived or this fires independently."""
    if not verify_webhook_hmac(request.get_data(), request.headers.get("X-Shopify-Hmac-Sha256")):
        return "", 401
    shop = request.headers.get("X-Shopify-Shop-Domain", "").strip().lower()
    if valid_shop(shop):
        token_store.delete_shop(shop)
        billing_store.delete_shop_data(shop)
        current_app.logger.info("GDPR shop redact completed for %s", shop)
    return "", 200


# --------------------------------------------------------------------------
# Helpers for the rest of the app
# --------------------------------------------------------------------------

def current_shop():
    """The shop this request is acting on. Checked in order:
    1. A verified Shopify session token (the embedded-app path — App Bridge
       attaches this to every request; stateless, no cookie needed, and
       works even where third-party cookies don't).
    2. The browser session (the classic-OAuth path — session["shop"] is only
       ever set by /auth/callback, after the OAuth HMAC and nonce checks
       confirm the request really came from Shopify for that shop).
    3. The single-store .env fallback, for local use.

    A bare ?shop= query parameter is NEVER trusted here — shop domains aren't
    secret, so treating "this shop exists in our token DB" as proof of
    identity would let anyone act as any installed shop just by guessing its
    domain."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        shop = verify_session_token(auth_header[7:])
        if shop:
            return shop

    shop = session.get("shop")
    if valid_shop(shop):
        return shop
    fallback = (os.environ.get("SHOPIFY_STORE_DOMAIN") or "").strip().lower()
    return fallback or None


def current_credentials():
    """Returns (shop, access_token). Raises if this browser isn't installed on
    any shop, so callers can send the merchant to /auth."""
    shop = current_shop()
    if not shop:
        raise PermissionError("No shop in session. Visit /auth?shop=your-store.myshopify.com")
    token = token_store.get_token(shop) or os.environ.get("SHOPIFY_ACCESS_TOKEN")
    if not token:
        # First load after an embedded install: we have a verified shop from
        # the session token above but no stored access token yet. Token
        # exchange gets one without a redirect-based consent screen.
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            token = exchange_token_for_shop(shop, auth_header[7:])
    if not token:
        raise PermissionError(f"{shop} hasn't installed the app yet. Visit /auth?shop={shop}")
    return shop, token


@auth_bp.route("/auth/logout", methods=["POST"])
def logout():
    """Clears the browser session. Does not uninstall or revoke the token."""
    session.clear()
    return jsonify({"ok": True})