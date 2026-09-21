"""Store speed page: shows the merchant what the storefront features do.

The storefront features (LazyLoad, responsive images, preloading, deferred
CSS/JS ...) live in a Shopify theme extension and are switched on in the theme
editor, where this app can't see. So instead the app reads the store's public
home page and reports (a) whether the extension is running and which options are
on, and (b) how much the images on that page weigh — which drops after images
are compressed, so a re-check shows the effect.
"""
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from flask import Blueprint, jsonify

from shopify_auth import api_version, current_credentials
from shopify_client import ShopifyClient
from token_store import conn as _shared_conn

speed_bp = Blueprint("speed", __name__)

UA = "Mozilla/5.0 (compatible; ImageOptimizerCheck/1.0)"
MAX_IMAGES = 24
IMG_TAG = re.compile(r"<img\b[^>]*>", re.I)
ATTR = lambda name: re.compile(r'\b' + name + r'\s*=\s*(?:"([^"]*)"|\'([^\']*)\')', re.I)
SRC, DATA_SRC, SRCSET, LOADING = ATTR("src"), ATTR("data-src"), ATTR("srcset"), ATTR("loading")
FLAG = lambda k: re.compile(r'\b' + k + r'\s*:\s*(true|false)')


def _conn():
    c = _shared_conn()
    c.execute("""
        CREATE TABLE IF NOT EXISTS speed_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, shop TEXT NOT NULL, ts TEXT NOT NULL,
            image_count INTEGER, image_bytes INTEGER
        )
    """)
    c.commit()
    return c


def extension_live():
    """Set SPEED_EXTENSION_LIVE=true once the theme extension has been deployed,
    so merchants are only told to switch on something that exists."""
    return os.environ.get("SPEED_EXTENSION_LIVE", "").strip().lower() in ("1", "true", "yes")


def _first(rx, tag):
    m = rx.search(tag)
    return (m.group(1) if m and m.group(1) is not None else (m.group(2) if m else None))


def _sizes(urls):
    def head(u):
        try:
            r = requests.head(u, headers={"User-Agent": UA}, timeout=8, allow_redirects=True)
            n = r.headers.get("Content-Length")
            return u, int(n) if n and n.isdigit() else None
        except requests.RequestException:
            return u, None
    with ThreadPoolExecutor(max_workers=8) as ex:
        return list(ex.map(head, urls))


@speed_bp.route("/api/speed/info")
def info():
    try:
        shop, _ = current_credentials()
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401
    handle = shop.replace(".myshopify.com", "")
    return jsonify({
        "ok": True, "extension_live": extension_live(),
        "editor_url": f"https://admin.shopify.com/store/{handle}/themes/current/editor?context=apps",
    })


@speed_bp.route("/api/speed/check", methods=["POST"])
def check():
    try:
        shop, token = current_credentials()
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401
    try:
        client = ShopifyClient(shop, token, api_version())
        s = client._request("GET", f"{client.base_url}/shop.json", timeout=15).json()["shop"]
        if s.get("password_enabled"):
            return jsonify({"ok": False, "error": "Your storefront is password-protected, so it can't be checked from outside. Remove the password (Online Store > Preferences) and try again."}), 400
        domain = s.get("domain") or shop
        page = requests.get(f"https://{domain}/", headers={"User-Agent": UA}, timeout=25)
        page.raise_for_status()
        html = page.text
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"Could not read your storefront: {exc}"}), 502

    # is the theme extension running, and which options are on?
    detected = "window.IHS" in html
    flags = {}
    if detected:
        for key, label in (("lazy", "LazyLoad"), ("responsive", "Responsive images"), ("deferCss", "Deferred CSS"),
                           ("deferJs", "Smart JS Defer"), ("delayApps", "App optimization")):
            m = FLAG(key).search(html)
            flags[label] = bool(m and m.group(1) == "true")
        flags["Preloading"] = 'rel="preload"' in html and 'as="image"' in html
        flags["Critical CSS"] = 'id="ihs-critical-css"' in html

    # images on the page
    seen, urls, with_srcset, native_lazy = set(), [], 0, 0
    tags = IMG_TAG.findall(html)
    for tag in tags:
        src = _first(SRC, tag) or _first(DATA_SRC, tag)
        if _first(SRCSET, tag): with_srcset += 1
        if (_first(LOADING, tag) or "").lower() == "lazy": native_lazy += 1
        if not src or src.startswith("data:"):
            continue
        u = urljoin(page.url, src)
        if u.startswith("http") and u not in seen and len(urls) < MAX_IMAGES:
            seen.add(u); urls.append(u)
    sized = [(u, n) for u, n in _sizes(urls) if n]
    total = sum(n for _, n in sized)

    c = _conn()
    prev = c.execute("SELECT ts, image_count, image_bytes FROM speed_checks WHERE shop=? ORDER BY id DESC LIMIT 1", (shop,)).fetchone()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    c.execute("INSERT INTO speed_checks (shop, ts, image_count, image_bytes) VALUES (?,?,?,?)", (shop, now, len(sized), total))
    c.commit()

    return jsonify({
        "ok": True, "url": page.url, "at": now,
        "extension": {"detected": detected, "features": flags},
        "images": {
            "on_page": len(tags), "measured": len(sized), "bytes": total,
            "with_srcset": with_srcset, "lazy": native_lazy,
            "heaviest": [{"url": u, "bytes": n} for u, n in sorted(sized, key=lambda x: -x[1])[:5]],
        },
        "previous": dict(prev) if prev else None,
    })
