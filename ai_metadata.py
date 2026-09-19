"""AI-generated ALT text and filenames for product images, via Gemini's vision
model.

Only the first image of each product is sent to Gemini — the resulting ALT
text and filename slug are then applied to every image on that product. This
keeps API calls (and cost) to one per product rather than one per image,
under the assumption that a product's images are all the same item.

Renaming a file re-uploads it under a new name, which changes its Shopify CDN
URL — anything linking to the old URL (external sites, ads, cached pages)
breaks. It's therefore opt-in per run (rename_files=False by default) and
never happens as a side effect of an ALT-text-only run.
"""
import base64
import io
import json
import os
import re
import threading
import time
from datetime import datetime

import requests
from flask import Blueprint, jsonify, request
from PIL import Image

import billing_store
from shopify_auth import current_credentials
from shopify_client import ShopifyClient

ai_bp = Blueprint("ai_metadata", __name__)

GEMINI_MODEL = "gemini-2.0-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

MIME_BY_FORMAT = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}
EXT_BY_FORMAT = {"JPEG": "jpg", "PNG": "png", "WEBP": "webp", "GIF": "gif"}

STATE_LOCK = threading.Lock()
STATE = {
    "status": "idle",  # idle | running | done | error | stopped
    "started_at": None,
    "finished_at": None,
    "total_products": 0,
    "processed_products": 0,
    "updated_alt": 0,
    "renamed_images": 0,
    "failed_products": 0,
    "log": [],
    "stop_requested": False,
    "quota_exceeded": False,
}


def _log(msg):
    stamp = datetime.now().strftime("%H:%M:%S")
    with STATE_LOCK:
        STATE["log"].append(f"[{stamp}] {msg}")
        STATE["log"] = STATE["log"][-300:]


def _gemini_key():
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("Missing GEMINI_API_KEY. Set it in .env.")
    return key


def _image_info(raw_bytes):
    """Returns (mime_type, extension) for a downloaded image's bytes."""
    fmt = (Image.open(io.BytesIO(raw_bytes)).format or "JPEG").upper()
    return MIME_BY_FORMAT.get(fmt, "image/jpeg"), EXT_BY_FORMAT.get(fmt, "jpg")


def _slugify(text, fallback="product-image"):
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    slug = re.sub(r"-{2,}", "-", slug)
    return (slug[:60].rstrip("-")) or fallback


_PROMPT = """You are writing metadata for a product photo on an e-commerce store.
Product title: {title}

Look at the image and respond with ONLY a JSON object, no markdown, no
commentary:
{{"alt_text": "a concise, factual description of what's visibly in the image, under 125 characters, no marketing language", "file_slug": "a url-safe filename slug: lowercase words separated by hyphens, 3-6 words, no extension, no special characters"}}
"""


def generate_metadata(raw_bytes, mime_type, product_title):
    """Calls Gemini's vision model once and returns {"alt_text", "file_slug"}."""
    body = {
        "contents": [{
            "parts": [
                {"text": _PROMPT.format(title=product_title or "")},
                {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(raw_bytes).decode()}},
            ]
        }],
        "generationConfig": {"response_mime_type": "application/json"},
    }
    resp = requests.post(
        GEMINI_URL, params={"key": _gemini_key()}, json=body, timeout=45,
    )
    resp.raise_for_status()
    data = resp.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
        alt_text = str(parsed["alt_text"]).strip()[:250]
        file_slug = _slugify(parsed.get("file_slug"))
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise RuntimeError(f"Unexpected Gemini response: {exc}") from exc
    if not alt_text:
        raise RuntimeError("Gemini returned no ALT text.")
    return {"alt_text": alt_text, "file_slug": file_slug}


def _resolve_products(client, scope, collection_id, product_ids):
    if scope == "products" and product_ids:
        return client.get_products_by_ids(product_ids)
    if scope == "collection" and collection_id:
        return list(client.iter_collection_products(collection_id))
    return list(client.iter_products())


LEDGER_FLUSH_EVERY = 25


def run_ai_job(domain, token, scope="store", collection_id=None, product_ids=None,
                update_alt=True, rename_files=False):
    with STATE_LOCK:
        STATE.update({
            "status": "running",
            "started_at": datetime.now().isoformat(timespec="seconds"),
            "finished_at": None,
            "total_products": 0,
            "processed_products": 0,
            "updated_alt": 0,
            "renamed_images": 0,
            "failed_products": 0,
            "log": [],
            "stop_requested": False,
            "quota_exceeded": False,
        })

    ledger = {}
    unsaved = 0

    def flush_ledger():
        """Best effort — a failed ledger write must not fail the job."""
        nonlocal unsaved
        if not unsaved:
            return
        try:
            client.save_ai_ledger(ledger)
            unsaved = 0
        except Exception as exc:  # noqa: BLE001
            _log(f"Warning: could not save the label record — {exc}")

    try:
        version = os.environ.get("SHOPIFY_API_VERSION", "2026-07")
        client = ShopifyClient(domain, token, version)
        ledger = client.load_ai_ledger()
        products = _resolve_products(client, scope, collection_id, product_ids)
        products = [p for p in products if p.get("images")]

        with STATE_LOCK:
            STATE["total_products"] = len(products)
        total_images = sum(len(p["images"]) for p in products)
        _log(f"Found {len(products)} products with images to label.")

        quota_left = billing_store.remaining(domain)
        if quota_left < total_images:
            _log(
                f"Plan limit: {quota_left} image credit(s) left, {total_images} images to touch. "
                f"Labeling what fits, then stopping — buy a pack to do the rest."
            )

        for product in products:
            with STATE_LOCK:
                if STATE["stop_requested"]:
                    STATE["status"] = "stopped"
                    flush_ledger()
                    _log("Stopped.")
                    return

            title = product.get("title", f"Product {product['id']}")
            images = product.get("images", [])

            try:
                first_raw = client.download_image(images[0]["src"])
                mime_type, _ext = _image_info(first_raw)
                meta = generate_metadata(first_raw, mime_type, title)
                now = datetime.now().isoformat(timespec="seconds")

                for idx, image in enumerate(images, start=1):
                    if quota_left <= 0:
                        with STATE_LOCK:
                            STATE["quota_exceeded"] = True
                        continue

                    touched = False
                    if rename_files:
                        raw = first_raw if idx == 1 else client.download_image(image["src"])
                        _mime, ext = _image_info(raw)
                        filename = f"{meta['file_slug']}-{idx}.{ext}"
                        client.update_image(
                            product["id"], image["id"],
                            base64.b64encode(raw).decode(),
                            filename=filename,
                            alt=meta["alt_text"] if update_alt else None,
                        )
                        touched = True
                        with STATE_LOCK:
                            STATE["renamed_images"] += 1
                            if update_alt:
                                STATE["updated_alt"] += 1
                    elif update_alt:
                        client.update_image_alt(product["id"], image["id"], meta["alt_text"])
                        touched = True
                        with STATE_LOCK:
                            STATE["updated_alt"] += 1

                    if touched:
                        # One credit per image touched this run, no matter how many
                        # operations (resize, ALT, rename) applied to it.
                        quota_left -= 1
                        billing_store.record_usage(domain, 1)
                        ledger[str(image["id"])] = {
                            "alt": meta["alt_text"] if update_alt else None,
                            "renamed": bool(rename_files),
                            "at": now,
                        }
                        unsaved += 1
                    time.sleep(0.3)

                if unsaved >= LEDGER_FLUSH_EVERY:
                    flush_ledger()

                _log(f"Labeled: {title} — \"{meta['alt_text']}\" ({len(images)} image(s))")

            except Exception as exc:  # noqa: BLE001 - keep going on per-product failure
                with STATE_LOCK:
                    STATE["failed_products"] += 1
                _log(f"Failed: {title} — {exc}")

            finally:
                with STATE_LOCK:
                    STATE["processed_products"] += 1

        flush_ledger()
        with STATE_LOCK:
            STATE["status"] = "done"
            STATE["finished_at"] = datetime.now().isoformat(timespec="seconds")
            quota_hit = STATE["quota_exceeded"]
        summary = f"Done. Labeled {STATE['processed_products']} products."
        if quota_hit:
            summary += " Your plan limit was reached — buy an image pack to label the rest."
        _log(summary)

    except Exception as exc:  # noqa: BLE001 - top-level failure (bad creds, network)
        try:
            flush_ledger()
        except Exception:  # noqa: BLE001
            pass
        with STATE_LOCK:
            STATE["status"] = "error"
            STATE["finished_at"] = datetime.now().isoformat(timespec="seconds")
        _log(f"Error: {exc}")


@ai_bp.route("/api/ai/status")
def ai_status():
    with STATE_LOCK:
        return jsonify(dict(STATE))


@ai_bp.route("/api/ai/start", methods=["POST"])
def ai_start():
    with STATE_LOCK:
        if STATE["status"] == "running":
            return jsonify({"ok": False, "error": "An AI labeling job is already running."}), 409

    try:
        domain, token = current_credentials()
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401

    if not os.environ.get("GEMINI_API_KEY"):
        return jsonify({"ok": False, "error": "Missing GEMINI_API_KEY. Set it in .env."}), 400

    if billing_store.remaining(domain) <= 0:
        return jsonify({
            "ok": False,
            "error": "Your plan limit is used up. Buy an image pack to keep going.",
            "quota_exceeded": True,
        }), 402

    body = request.get_json(silent=True) or {}
    scope = body.get("scope", "store")
    if scope not in ("store", "collection", "products"):
        scope = "store"
    collection_id = body.get("collection_id")
    product_ids = body.get("product_ids") or []
    update_alt = bool(body.get("update_alt", True))
    rename_files = bool(body.get("rename_files", False))

    if not update_alt and not rename_files:
        return jsonify({"ok": False, "error": "Pick at least one: update ALT text or rename files."}), 400
    if scope == "products" and not product_ids:
        return jsonify({"ok": False, "error": "Select at least one product."}), 400
    if scope == "collection" and not collection_id:
        return jsonify({"ok": False, "error": "Choose a collection."}), 400

    threading.Thread(
        target=run_ai_job,
        kwargs=dict(
            domain=domain, token=token, scope=scope, collection_id=collection_id,
            product_ids=product_ids, update_alt=update_alt, rename_files=rename_files,
        ),
        daemon=True,
    ).start()
    return jsonify({"ok": True})


@ai_bp.route("/api/ai/stop", methods=["POST"])
def ai_stop():
    with STATE_LOCK:
        STATE["stop_requested"] = True
    return jsonify({"ok": True})


@ai_bp.route("/api/ai/report")
def ai_report():
    """Every image with its current ALT text and whether this app labeled it,
    so the merchant can see what actually changed. Pass ?collection_id= to
    narrow it down."""
    collection_id = request.args.get("collection_id") or None
    try:
        domain, token = current_credentials()
        version = os.environ.get("SHOPIFY_API_VERSION", "2026-07")
        client = ShopifyClient(domain, token, version)
        items = list(client.iter_products(collection_id=collection_id))
        ledger = client.load_ai_ledger()

        products_out = []
        total = labeled_total = 0
        for p in items:
            images = []
            for im in p.get("images", []):
                record = ledger.get(str(im["id"]))
                labeled = record is not None
                images.append({
                    "id": im["id"],
                    "src": im["src"],
                    "current_alt": im.get("alt"),
                    "labeled": labeled,
                    "labeled_at": record.get("at") if record else None,
                    "renamed": bool(record and record.get("renamed")),
                })
                total += 1
                labeled_total += 1 if labeled else 0
            products_out.append({
                "id": p["id"],
                "title": p.get("title", f"Product {p['id']}"),
                "images": images,
                "labeled_count": sum(1 for i in images if i["labeled"]),
            })

        products_out.sort(key=lambda x: (
            len(x["images"]) and x["labeled_count"] == len(x["images"]),
            x["title"].lower(),
        ))
        return jsonify({
            "ok": True,
            "totals": {
                "products": len(products_out),
                "images": total,
                "labeled": labeled_total,
                "pending": total - labeled_total,
            },
            "products": products_out,
        })
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400


@ai_bp.route("/api/ai/ledger", methods=["DELETE"])
def ai_reset_ledger():
    """Forgets every label record. Doesn't touch the ALT text/filenames
    already applied — just the "labeled by this app" bookkeeping."""
    try:
        domain, token = current_credentials()
        version = os.environ.get("SHOPIFY_API_VERSION", "2026-07")
        client = ShopifyClient(domain, token, version)
        client.clear_ai_ledger()
        return jsonify({"ok": True})
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400
