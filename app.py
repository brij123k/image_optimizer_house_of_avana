import os
import threading
import time
from datetime import datetime

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

from shopify_client import ShopifyClient
from image_optimizer import compress_image

load_dotenv()

app = Flask(__name__)

# Minimum bytes an image must shrink by to count as "optimized" and get
# re-uploaded. Avoids pointless writes for images that are already tiny.
MIN_SAVINGS_BYTES = 5 * 1024
# Skip images already smaller than this — nothing meaningful to save.
SKIP_BELOW_BYTES = 20 * 1024

EXT_MAP = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}

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
    "failed_images": 0,
    "bytes_before": 0,
    "bytes_after": 0,
    "log": [],
    "shop_name": None,
    "stop_requested": False,
}


def log(msg):
    stamp = datetime.now().strftime("%H:%M:%S")
    with STATE_LOCK:
        STATE["log"].append(f"[{stamp}] {msg}")
        STATE["log"] = STATE["log"][-300:]  # cap log length


def get_client():
    domain = os.environ.get("SHOPIFY_STORE_DOMAIN", "")
    token = os.environ.get("SHOPIFY_ACCESS_TOKEN", "")
    version = os.environ.get("SHOPIFY_API_VERSION", "2026-07")
    if not domain or not token:
        raise RuntimeError(
            "Missing SHOPIFY_STORE_DOMAIN or SHOPIFY_ACCESS_TOKEN. "
            "Copy .env.example to .env and fill in your credentials."
        )
    return ShopifyClient(domain, token, version)


def _resolve_products(client, scope, collection_id, product_ids):
    """Returns the list of products to process for the chosen scope."""
    if scope == "products" and product_ids:
        return client.get_products_by_ids(product_ids)
    if scope == "collection" and collection_id:
        return list(client.iter_collection_products(collection_id))
    return list(client.iter_products())  # scope == "store"


def _new_filename(src_url, out_format):
    """Keeps the original base name, swaps the extension to match the new format."""
    orig = src_url.rsplit("/", 1)[-1].split("?")[0]
    base = orig.rsplit(".", 1)[0] if "." in orig else orig
    return f"{base}.{EXT_MAP.get(out_format, 'jpg')}"


def run_optimization(quality, max_width, force_webp=False,
                     scope="store", collection_id=None, product_ids=None):
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
            "failed_images": 0,
            "bytes_before": 0,
            "bytes_after": 0,
            "log": [],
            "stop_requested": False,
        })

    try:
        client = get_client()
        shop_name = client.verify_connection()
        with STATE_LOCK:
            STATE["shop_name"] = shop_name

        if scope == "products":
            log(f"Connected to {shop_name}. Loading {len(product_ids or [])} selected product(s)...")
        elif scope == "collection":
            log(f"Connected to {shop_name}. Loading collection...")
        else:
            log(f"Connected to {shop_name}. Loading entire store...")

        products = _resolve_products(client, scope, collection_id, product_ids)
        total_images = sum(len(p.get("images", [])) for p in products)
        with STATE_LOCK:
            STATE["total_products"] = len(products)
            STATE["total_images"] = total_images
            STATE["status"] = "running"
        log(f"Found {len(products)} products, {total_images} images. Starting.")

        for product in products:
            with STATE_LOCK:
                if STATE["stop_requested"]:
                    STATE["status"] = "stopped"
                    log("Stopped.")
                    return

            for image in product.get("images", []):
                image_id = image["id"]
                src = image["src"]
                title = product.get("title", f"Product {product['id']}")
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
                        raw, quality=quality, max_width=max_width, force_webp=force_webp
                    )
                    new_size = len(compressed)
                    saved = original_size - new_size

                    # When converting to WebP we write even for small savings,
                    # since the format change is the point.
                    worth_writing = saved >= MIN_SAVINGS_BYTES or (
                        force_webp and not already_webp and saved > 0
                    )

                    if not worth_writing:
                        with STATE_LOCK:
                            STATE["skipped_images"] += 1
                        log(f"Skip (no meaningful savings): {title}")
                        continue

                    client.update_image(
                        product["id"], image_id, b64,
                        filename=_new_filename(src, out_format),
                    )
                    with STATE_LOCK:
                        STATE["optimized_images"] += 1
                        STATE["bytes_before"] += original_size
                        STATE["bytes_after"] += new_size
                    log(
                        f"Optimized: {title} — {original_size // 1024}KB -> "
                        f"{new_size // 1024}KB ({100 * saved / original_size:.0f}% smaller, {out_format})"
                    )

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

        with STATE_LOCK:
            STATE["status"] = "done"
            STATE["finished_at"] = datetime.now().isoformat(timespec="seconds")
            saved_total = STATE["bytes_before"] - STATE["bytes_after"]
            optimized = STATE["optimized_images"]
        log(f"Done. Optimized {optimized} images, saved {saved_total // 1024}KB total.")

    except Exception as exc:  # noqa: BLE001 - top-level failure (bad creds, network)
        with STATE_LOCK:
            STATE["status"] = "error"
            STATE["finished_at"] = datetime.now().isoformat(timespec="seconds")
        log(f"Error: {exc}")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/test-connection", methods=["POST"])
def test_connection():
    try:
        client = get_client()
        return jsonify({"ok": True, "shop_name": client.verify_connection()})
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
    """Products for the picker. Pass ?collection_id=123 to filter."""
    collection_id = request.args.get("collection_id") or None
    try:
        client = get_client()
        items = list(client.iter_products(collection_id=collection_id))
        return jsonify({"ok": True, "products": [
            {
                "id": p["id"],
                "title": p.get("title", f"Product {p['id']}"),
                "image_count": len(p.get("images", [])),
                "thumbnail": p["images"][0]["src"] if p.get("images") else None,
            }
            for p in items
        ]})
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400


@app.route("/api/start", methods=["POST"])
def start():
    with STATE_LOCK:
        if STATE["status"] in ("scanning", "running"):
            return jsonify({"ok": False, "error": "A job is already running."}), 409

    body = request.get_json(silent=True) or {}
    quality = max(1, min(95, int(body.get("quality", 75))))
    max_width_raw = body.get("max_width")
    max_width = int(max_width_raw) if max_width_raw else None
    force_webp = bool(body.get("force_webp", False))

    scope = body.get("scope", "store")
    if scope not in ("store", "collection", "products"):
        scope = "store"
    collection_id = body.get("collection_id")
    product_ids = body.get("product_ids") or []

    if scope == "products" and not product_ids:
        return jsonify({"ok": False, "error": "Select at least one product."}), 400
    if scope == "collection" and not collection_id:
        return jsonify({"ok": False, "error": "Choose a collection."}), 400

    threading.Thread(
        target=run_optimization,
        kwargs=dict(
            quality=quality, max_width=max_width, force_webp=force_webp,
            scope=scope, collection_id=collection_id, product_ids=product_ids,
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
    port = int(os.environ.get("PORT", 5057))
    app.run(host="0.0.0.0", port=port, debug=True)