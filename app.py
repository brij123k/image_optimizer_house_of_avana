import os
import threading
import time
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, jsonify, redirect, render_template, request, session

from shopify_client import ShopifyClient
from image_optimizer import compress_image
from shopify_auth import auth_bp, current_credentials, is_production, login_from_signed_request, valid_shop
from billing import billing_bp
import billing_store
import product_cache
from ai_metadata import ai_bp
from image_edit import edit_bp
from history import history_bp
from dashboard import dashboard_bp
from keywords import keywords_bp
from speed import speed_bp
from compliance import compliance_bp

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY")
if not app.secret_key:
    raise RuntimeError(
        "Missing FLASK_SECRET_KEY. Set it in .env — generate one with: "
        "python -c \"import secrets; print(secrets.token_hex(32))\""
    )

# ---- session cookie: only sent by this site's own pages, never readable by scripts ----
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=is_production(),     # HTTPS only on the live server
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 7,
)
if is_production():
    # Behind nginx: trust one proxy hop so the real client IP and https are seen.
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

app.register_blueprint(auth_bp)
app.register_blueprint(compliance_bp)
app.register_blueprint(billing_bp)
app.register_blueprint(ai_bp)
app.register_blueprint(edit_bp)
app.register_blueprint(history_bp)
app.register_blueprint(dashboard_bp)
app.register_blueprint(keywords_bp)
app.register_blueprint(speed_bp)

# Minimum bytes an image must shrink by to count as "optimized" and get
# re-uploaded. Avoids pointless writes for images that are already tiny.
MIN_SAVINGS_BYTES = 5 * 1024
# Skip images already smaller than this — nothing meaningful to save.
SKIP_BELOW_BYTES = 20 * 1024
# Write the ledger back to Shopify every N successful images, so a crash or a
# sleeping host doesn't lose the whole run's record.
LEDGER_FLUSH_EVERY = 25

EXT_MAP = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}


def _parse_crop_ratio(raw):
    """Turns a "W:H" string like "16:9" into a (16.0, 9.0) tuple, or None.
    Raises ValueError on anything malformed so the caller can 400 clearly."""
    if not raw:
        return None
    parts = str(raw).split(":")
    if len(parts) != 2:
        raise ValueError("crop_ratio must look like \"W:H\", e.g. \"16:9\".")
    w, h = (float(p) for p in parts)
    if w <= 0 or h <= 0:
        raise ValueError("crop_ratio values must be positive.")
    return (w, h)

STATE_LOCK = threading.Lock()
STATE = {
    "status": "idle",  # idle | scanning | running | done | error | stopped
    "started_at": None,
    "finished_at": None,
    "total_products": 0,
    "processed_products": 0,
    "total_images": 0,
    "processed_images": 0,
    "optimized_images": 0,
    "skipped_images": 0,
    "already_done_images": 0,
    "failed_images": 0,
    "bytes_before": 0,
    "bytes_after": 0,
    "log": [],
    "shop_name": None,
    "stop_requested": False,
    "quota_exceeded": False,
}


def log(msg):
    stamp = datetime.now().strftime("%H:%M:%S")
    with STATE_LOCK:
        STATE["log"].append(f"[{stamp}] {msg}")
        STATE["log"] = STATE["log"][-300:]  # cap log length


def get_client():
    domain, token = current_credentials()
    version = os.environ.get("SHOPIFY_API_VERSION", "2026-07")
    return ShopifyClient(domain, token, version)


def _resolve_products(client, scope, collection_id, product_ids):
    """Returns the list of products to process for the chosen scope."""
    if scope == "products" and product_ids:
        return client.get_products_by_ids(product_ids)
    if scope == "collection" and collection_id:
        return list(client.iter_collection_products(collection_id))
    return list(client.iter_products())  # scope == "store"


def _new_filename(src_url, out_format):
    """Keeps the original base name, swaps the extension to match the format."""
    orig = src_url.rsplit("/", 1)[-1].split("?")[0]
    base = orig.rsplit(".", 1)[0] if "." in orig else orig
    return f"{base}.{EXT_MAP.get(out_format, 'jpg')}"


def is_done(image, ledger):
    """An image counts as already optimized only if we've recorded its ID *and*
    its updated_at still matches what we left it at. A re-uploaded or edited
    image moves its timestamp and so comes back into scope."""
    recorded = ledger.get(str(image["id"]))
    return recorded is not None and recorded == image.get("updated_at")


def run_optimization(domain, token, quality, max_width, max_height=None, force_webp=False, redo=False,
                     scope="store", collection_id=None, product_ids=None, image_ids=None,
                     crop_ratio=None):
    with STATE_LOCK:
        STATE.update({
            "status": "scanning",
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "finished_at": None,
            "total_products": 0,
            "processed_products": 0,
            "total_images": 0,
            "processed_images": 0,
            "optimized_images": 0,
            "skipped_images": 0,
            "already_done_images": 0,
            "failed_images": 0,
            "bytes_before": 0,
            "bytes_after": 0,
            "log": [],
            "stop_requested": False,
            "quota_exceeded": False,
        })

    ledger = {}
    unsaved = 0
    product_cache.invalidate(domain)

    def flush_ledger():
        """Best effort — a failed ledger write must not fail the job."""
        nonlocal unsaved
        if not unsaved:
            return
        try:
            client.save_ledger(ledger)
            unsaved = 0
        except Exception as exc:  # noqa: BLE001
            log(f"Warning: could not save the optimized-image record — {exc}")

    try:
        version = os.environ.get("SHOPIFY_API_VERSION", "2026-07")
        client = ShopifyClient(domain, token, version)
        shop_name = client.verify_connection()
        with STATE_LOCK:
            STATE["shop_name"] = shop_name

        ledger = {} if redo else client.load_ledger()
        if redo:
            log(f"Connected to {shop_name}. Re-optimizing everything, ignoring past runs.")
        else:
            log(f"Connected to {shop_name}. {len(ledger)} images optimized in past runs.")

        products = _resolve_products(client, scope, collection_id, product_ids)

        only_ids = set(image_ids) if image_ids else None
        if only_ids is not None:
            filtered = []
            for p in products:
                imgs = [im for im in p.get("images", []) if im["id"] in only_ids]
                if imgs:
                    filtered.append({**p, "images": imgs})
            products = filtered
            log(f"Re-optimizing {sum(len(p['images']) for p in products)} selected image(s), ignoring their past-run record.")

        total_images = sum(len(p.get("images", [])) for p in products)
        pending = sum(
            1 for p in products for im in p.get("images", [])
            if redo or only_ids is not None or not is_done(im, ledger)
        )
        with STATE_LOCK:
            STATE["total_products"] = len(products)
            STATE["total_images"] = total_images
        log(f"Found {len(products)} products, {total_images} images — {pending} need work.")

        quota_left = billing_store.remaining(domain)
        if quota_left < pending:
            log(
                f"Plan limit: {quota_left} image credit(s) left, {pending} need work. "
                f"Optimizing what fits, then stopping — buy a pack to do the rest."
            )

        with STATE_LOCK:
            STATE["status"] = "running"

        for product in products:
            with STATE_LOCK:
                if STATE["stop_requested"]:
                    STATE["status"] = "stopped"
                    flush_ledger()
                    log("Stopped. Progress so far has been saved.")
                    return

            for image in product.get("images", []):
                image_id = image["id"]
                src = image["src"]
                title = product.get("title", f"Product {product['id']}")

                if not redo and only_ids is None and is_done(image, ledger):
                    with STATE_LOCK:
                        STATE["already_done_images"] += 1
                        STATE["processed_images"] += 1
                    continue  # no download, no API call — nothing to do

                if quota_left <= 0:
                    with STATE_LOCK:
                        STATE["skipped_images"] += 1
                        STATE["processed_images"] += 1
                        STATE["quota_exceeded"] = True
                    log(f"Skip (plan limit reached): {title} — buy an image pack to continue.")
                    continue

                try:
                    raw = client.download_image(src)
                    original_size = len(raw)
                    already_webp = src.split("?")[0].lower().endswith(".webp")

                    if original_size < SKIP_BELOW_BYTES and not (force_webp and not already_webp):
                        with STATE_LOCK:
                            STATE["skipped_images"] += 1
                        log(f"Skip (already small, {original_size // 1024}KB): {title}")
                        continue

                    compressed, b64, out_format = compress_image(
                        raw, quality=quality, max_width=max_width, max_height=max_height,
                        force_webp=force_webp, crop_ratio=crop_ratio,
                    )
                    new_size = len(compressed)
                    saved = original_size - new_size

                    worth_writing = saved >= MIN_SAVINGS_BYTES or (
                        force_webp and not already_webp and saved > 0
                    )
                    if not worth_writing:
                        with STATE_LOCK:
                            STATE["skipped_images"] += 1
                        log(f"Skip (no meaningful savings): {title}")
                        continue

                    updated = client.update_image(
                        product["id"], image_id, b64,
                        filename=_new_filename(src, out_format),
                    )
                    # Record the timestamp Shopify assigned *after* our write.
                    ledger[str(image_id)] = updated.get("updated_at")
                    unsaved += 1
                    quota_left -= 1
                    billing_store.record_usage(domain, 1)
                    billing_store.add_stat(domain, "images_compressed", 1)
                    billing_store.add_stat(domain, "bytes_saved", max(0, saved))

                    with STATE_LOCK:
                        STATE["optimized_images"] += 1
                        STATE["bytes_before"] += original_size
                        STATE["bytes_after"] += new_size
                    log(
                        f"Optimized: {title} — {original_size // 1024}KB -> "
                        f"{new_size // 1024}KB ({100 * saved / original_size:.0f}% smaller, {out_format})"
                    )

                    if unsaved >= LEDGER_FLUSH_EVERY:
                        flush_ledger()

                except Exception as exc:  # noqa: BLE001 - keep going on per-image failure
                    with STATE_LOCK:
                        STATE["failed_images"] += 1
                    log(f"Failed: {title} ({image_id}) — {exc}")

                finally:
                    with STATE_LOCK:
                        STATE["processed_images"] += 1
                    time.sleep(0.4)  # gentle pacing for Shopify's rate limits

            with STATE_LOCK:
                STATE["processed_products"] += 1

        flush_ledger()
        with STATE_LOCK:
            STATE["status"] = "done"
            STATE["finished_at"] = datetime.now().isoformat(timespec="seconds")
            saved_total = STATE["bytes_before"] - STATE["bytes_after"]
            optimized = STATE["optimized_images"]
            already = STATE["already_done_images"]
            quota_hit = STATE["quota_exceeded"]
        summary = f"Done. Optimized {optimized} images, saved {saved_total // 1024}KB total."
        if already:
            summary += f" Skipped {already} already optimized in earlier runs."
        if quota_hit:
            summary += " Your plan limit was reached — buy an image pack to optimize the rest."
        log(summary)
        product_cache.invalidate(domain)

    except Exception as exc:  # noqa: BLE001 - top-level failure (bad creds, network)
        try:
            flush_ledger()
        except Exception:  # noqa: BLE001
            pass
        with STATE_LOCK:
            STATE["status"] = "error"
            STATE["finished_at"] = datetime.now().isoformat(timespec="seconds")
        log(f"Error: {exc}")
        product_cache.invalidate(domain)


@app.before_request
def block_forged_requests():
    """CSRF guard. Requests that change data must carry a header that a page on
    another site cannot add to a cross-site request (static/loading.js adds it
    to every fetch this app's own pages make). Shopify's signed webhooks are exempt."""
    if request.method in ("POST", "PUT", "PATCH", "DELETE") and not request.path.startswith("/webhooks/"):
        if request.headers.get("X-Requested-With") != "ihs":
            return jsonify({"ok": False, "error": "Blocked: request did not come from this app."}), 403


@app.after_request
def compress_big_json(resp):
    """The product list can be ~15 MB of JSON on a big store; compressed it is ~10x smaller."""
    import gzip
    if (resp.status_code == 200 and resp.mimetype == "application/json" and not resp.direct_passthrough
            and "gzip" in request.headers.get("Accept-Encoding", "") and "Content-Encoding" not in resp.headers):
        data = resp.get_data()
        if len(data) > 20_000:
            resp.set_data(gzip.compress(data, compresslevel=5))
            resp.headers["Content-Encoding"] = "gzip"
            resp.headers["Vary"] = "Accept-Encoding"
    return resp


@app.after_request
def security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    if is_production():
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resp


@app.route("/")
def index():
    """The app's address. Shopify opens it with ?shop=<store> (usually signed) after an install.

    Rule: if the request names a store and this browser is not already logged in to THAT store,
    start the Shopify approval flow straight away (Shopify's automated check requires this).
    Starting the flow is harmless: only that store's own staff can approve it, so a made-up or
    forged ?shop= gets nobody in. A valid signed request from an installed store logs in directly.
    """
    shop = (request.args.get("shop") or "").strip().lower()
    if shop:
        if not valid_shop(shop):
            return ("That store address is not valid.", 400)
        if login_from_signed_request(request.args) != "logged_in" and session.get("shop") != shop:
            return redirect("/auth?shop=" + shop)
    return render_template("index.html", manual_shop=not is_production())


@app.route("/privacy")
def privacy_page():
    return render_template("legal.html", page="privacy", title="Privacy policy",
                           company=os.environ.get("COMPANY_NAME", "One Globe FZE"),
                           email=os.environ.get("SUPPORT_EMAIL", "support@example.com"))


@app.route("/support")
def support_page():
    return render_template("legal.html", page="support", title="Support",
                           company=os.environ.get("COMPANY_NAME", "One Globe FZE"),
                           email=os.environ.get("SUPPORT_EMAIL", "support@example.com"))


@app.route("/plans")
def plans_page():
    return render_template("plans.html")


@app.route("/api/test-connection", methods=["POST"])
def test_connection():
    try:
        client = get_client()
        name = client.verify_connection()
        product_cache.prewarm(client.store_domain, client)   # read the store in the background now
        return jsonify({"ok": True, "shop_name": name})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/collections")
def collections():
    try:
        client = get_client()
        return jsonify({"ok": True, "collections": client.list_collections()})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/products")
def products():
    """Products for the picker, annotated with how many images are already done."""
    collection_id = request.args.get("collection_id") or None
    try:
        client = get_client()
        refresh = bool(request.args.get("refresh"))
        items = product_cache.products(client.store_domain, client, collection_id, refresh)
        ledger = product_cache.ledger(client.store_domain, client, "compress", refresh)
        out = []
        for p in items:
            images = p.get("images", [])
            done = sum(1 for im in images if is_done(im, ledger))
            out.append({
                "id": p["id"],
                "title": p.get("title", f"Product {p['id']}"),
                "image_count": len(images),
                "done_count": done,
                "fully_done": bool(images) and done == len(images),
                "thumbnail": images[0]["src"] if images else None,
                # Full per-image list (id/src/optimized), so the picker can
                # offer selecting individual images within a product, not
                # just the whole product.
                "images": [
                    {"id": im["id"], "src": im["src"], "optimized": is_done(im, ledger)}
                    for im in images
                ],
            })
        return jsonify({"ok": True, "products": out})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/report")
def report():
    """Every image with its optimized status, so the record is visible rather
    than buried in a metafield. Pass ?collection_id= to narrow it down."""
    collection_id = request.args.get("collection_id") or None
    try:
        client = get_client()
        refresh = bool(request.args.get("refresh"))
        items = product_cache.products(client.store_domain, client, collection_id, refresh)
        ledger = product_cache.ledger(client.store_domain, client, "compress", refresh)

        products_out = []
        total = done_total = 0
        for p in items:
            images = []
            for im in p.get("images", []):
                done = is_done(im, ledger)
                images.append({
                    "id": im["id"],
                    "src": im["src"],
                    "optimized": done,
                    "optimized_at": ledger.get(str(im["id"])) if done else None,
                    "known_but_changed": (
                        str(im["id"]) in ledger and not done
                    ),
                    "width": im.get("width"),
                    "height": im.get("height"),
                })
                total += 1
                done_total += 1 if done else 0
            products_out.append({
                "id": p["id"],
                "title": p.get("title", f"Product {p['id']}"),
                "images": images,
                "done_count": sum(1 for i in images if i["optimized"]),
            })

        products_out.sort(key=lambda x: (
            len(x["images"]) and x["done_count"] == len(x["images"]),
            x["title"].lower(),
        ))
        return jsonify({
            "ok": True,
            "totals": {
                "products": len(products_out),
                "images": total,
                "optimized": done_total,
                "pending": total - done_total,
                "ledger_entries": len(ledger),
            },
            "products": products_out,
        })
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/ledger", methods=["DELETE"])
def reset_ledger():
    """Forgets every record. The next run treats all images as new."""
    try:
        client = get_client()
        client.clear_ledger()
        product_cache.invalidate(client.store_domain)
        return jsonify({"ok": True})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/start", methods=["POST"])
def start():
    with STATE_LOCK:
        if STATE["status"] in ("scanning", "running"):
            return jsonify({"ok": False, "error": "A job is already running."}), 409

    try:
        domain, token = current_credentials()
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401

    if billing_store.remaining(domain) <= 0:
        return jsonify({
            "ok": False,
            "error": "Your plan limit is used up. Buy an image pack to keep optimizing.",
            "quota_exceeded": True,
        }), 402

    body = request.get_json(silent=True) or {}
    quality = max(1, min(95, int(body.get("quality", 75))))
    max_width_raw = body.get("max_width")
    max_width = int(max_width_raw) if max_width_raw else None
    max_height_raw = body.get("max_height")
    max_height = int(max_height_raw) if max_height_raw else None
    force_webp = bool(body.get("force_webp", False))
    redo = bool(body.get("redo", False))
    try:
        crop_ratio = _parse_crop_ratio(body.get("crop_ratio"))
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    scope = body.get("scope", "store")
    if scope not in ("store", "collection", "products"):
        scope = "store"
    collection_id = body.get("collection_id")
    product_ids = body.get("product_ids") or []
    image_ids_raw = body.get("image_ids") or []
    image_ids = [int(i) for i in image_ids_raw] if image_ids_raw else None

    if scope == "products" and not product_ids:
        return jsonify({"ok": False, "error": "Select at least one product."}), 400
    if scope == "collection" and not collection_id:
        return jsonify({"ok": False, "error": "Choose a collection."}), 400

    threading.Thread(
        target=run_optimization,
        kwargs=dict(
            domain=domain, token=token,
            quality=quality, max_width=max_width, max_height=max_height,
            force_webp=force_webp, redo=redo,
            scope=scope, collection_id=collection_id, product_ids=product_ids,
            image_ids=image_ids, crop_ratio=crop_ratio,
        ),
        daemon=True,
    ).start()
    return jsonify({"ok": True})


@app.route("/api/stop", methods=["POST"])
def stop():
    with STATE_LOCK:
        STATE["stop_requested"] = True
    return jsonify({"ok": True})


@app.route("/api/status")
def status():
    with STATE_LOCK:
        return jsonify(dict(STATE))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5088))
    # The debugger allows remote code execution, so it is off unless FLASK_DEBUG=1 (local .env only).
    app.run(host="0.0.0.0", port=port, debug=os.environ.get("FLASK_DEBUG") == "1")
