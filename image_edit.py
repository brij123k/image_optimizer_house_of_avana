"""Backend for the six per-image editing tools (Enhance, Crop & Transform,
Resize, Draw, Color Background, Generate).

Crop & Transform and Resize keep using the existing /api/start job (see
app.py) — they're just settings that ride along with a normal optimize run.
Everything here is for the other four, which each write directly to one or
more images and charge billing_store the same way run_optimization() does:
one credit per image actually re-uploaded. Generate is the exception: every
preview costs GENERATE_CREDITS, since each one is a paid image-generation call.

These run synchronously in the request — a handful of interactive edits, not
a bulk job, so there's no need for the background-thread/polling machinery
app.py uses for Compress Images.
"""
import base64
import io
import os
import time

import requests

import product_cache
from flask import Blueprint, jsonify, request
from PIL import Image

import billing_store
from image_optimizer import apply_enhancements, composite_over_color
from shopify_auth import current_credentials
from shopify_client import ShopifyClient

edit_bp = Blueprint("edit", __name__)

# Primary model first; the Lite one is the fallback when the primary stays busy.
GEMINI_IMAGE_MODELS = ["gemini-3.1-flash-image", "gemini-3.1-flash-lite-image"]
GENERATE_CREDITS = 3  # one generated image costs ~3x an ALT-text label in Gemini fees

# Google answers these when the model is briefly overloaded or rate-limited
# ("server is busy"); the same request usually works seconds later. 404 means
# a model isn't available to this key, so move on to the next one.
_IMAGE_RETRY_STATUS = {429, 500, 502, 503, 504}
_IMAGE_RETRY_DELAYS = (2, 5)
_IMAGE_DEADLINE = 120  # seconds for all tries together, well under gunicorn's 300s timeout


class GeminiBusy(RuntimeError):
    """Every image model stayed busy through all retries."""


def _generate_image(prompt):
    """Calls Gemini image generation, retrying busy answers and falling back to
    the next model. Returns the parsed JSON response."""
    deadline = time.time() + _IMAGE_DEADLINE
    for model in GEMINI_IMAGE_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for delay in (*_IMAGE_RETRY_DELAYS, None):
            left = deadline - time.time()
            if left < 10:
                raise GeminiBusy("Ran out of time waiting for Gemini.")
            try:
                resp = requests.post(url, headers={"x-goog-api-key": _gemini_key()},
                                     json={"contents": [{"parts": [{"text": prompt}]}]},
                                     timeout=min(60, left))
            except requests.RequestException:
                pass                                # network hiccup or timeout: retry
            else:
                if resp.status_code == 404:
                    break                           # model not available: next model
                if resp.status_code not in _IMAGE_RETRY_STATUS:
                    resp.raise_for_status()
                    return resp.json()
            if delay is None:
                break
            time.sleep(delay)
    raise GeminiBusy("Every Gemini image model is busy.")


def _get_client():
    domain, token = current_credentials()
    version = os.environ.get("SHOPIFY_API_VERSION", "2026-07")
    return domain, ShopifyClient(domain, token, version)


def _gemini_key():
    key = os.environ.get("GEMINI_API_KEY", "")
    if not key:
        raise RuntimeError("Missing GEMINI_API_KEY. Set it in .env.")
    return key


def _find_image(client, product_id, image_id):
    """Fetches a single product's images and returns the matching one, or
    raises if the product/image can't be found."""
    products = client.get_products_by_ids([product_id])
    if not products:
        raise RuntimeError(f"Product {product_id} not found.")
    for im in products[0].get("images", []):
        if im["id"] == image_id:
            return im
    raise RuntimeError(f"Image {image_id} not found on product {product_id}.")


def _image_pairs(body):
    """Reads {product_ids, image_ids} (parallel arrays, same order) from the
    request body and returns [(product_id, image_id), ...]."""
    product_ids = body.get("product_ids") or []
    image_ids = body.get("image_ids") or []
    if len(product_ids) != len(image_ids):
        raise ValueError("product_ids and image_ids must be the same length.")
    return list(zip(product_ids, image_ids))


@edit_bp.route("/api/edit/enhance", methods=["POST"])
def edit_enhance():
    try:
        shop, client = _get_client()
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401

    body = request.get_json(silent=True) or {}
    try:
        pairs = _image_pairs(body)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not pairs:
        return jsonify({"ok": False, "error": "Select at least one image."}), 400

    values = body.get("values") or {}

    quota_left = billing_store.remaining(shop)
    if quota_left <= 0:
        return jsonify({"ok": False, "error": "Your plan limit is used up.", "quota_exceeded": True}), 402

    updated, failed = [], []
    for product_id, image_id in pairs:
        if quota_left <= 0:
            break
        try:
            image = _find_image(client, product_id, image_id)
            raw = client.download_image(image["src"])
            img = Image.open(io.BytesIO(raw))
            edited = apply_enhancements(img, values)
            out = io.BytesIO()
            edited.save(out, format="PNG" if edited.mode == "RGBA" else "JPEG", quality=90)
            b64 = base64.b64encode(out.getvalue()).decode()
            client.update_image(product_id, image_id, b64)
            product_cache.invalidate(client.store_domain)
            billing_store.record_usage(shop, 1)
            quota_left -= 1
            updated.append(image_id)
        except Exception as exc:  # noqa: BLE001 - keep going on per-image failure
            failed.append({"image_id": image_id, "error": str(exc)})

    return jsonify({"ok": True, "updated": updated, "failed": failed})


@edit_bp.route("/api/edit/draw", methods=["POST"])
def edit_draw():
    try:
        shop, client = _get_client()
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401

    if billing_store.remaining(shop) <= 0:
        return jsonify({"ok": False, "error": "Your plan limit is used up.", "quota_exceeded": True}), 402

    body = request.get_json(silent=True) or {}
    product_id = body.get("product_id")
    image_id = body.get("image_id")
    data_url = body.get("image_data_url") or ""
    if not product_id or not image_id or not data_url:
        return jsonify({"ok": False, "error": "Missing product_id, image_id, or image_data_url."}), 400

    # The canvas already composited the original image + strokes client-side
    # — just strip the "data:image/png;base64," prefix and upload as-is.
    if "," in data_url:
        data_url = data_url.split(",", 1)[1]

    try:
        client.update_image(product_id, image_id, data_url, filename="drawn.png")
        product_cache.invalidate(client.store_domain)
        billing_store.record_usage(shop, 1)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400

    return jsonify({"ok": True})


@edit_bp.route("/api/edit/color-bg/cutout", methods=["POST"])
def edit_color_bg_cutout():
    """Runs background removal on ONE image and returns the RGBA cutout as a
    base64 PNG — no quota charge, nothing written. The frontend caches this
    per image and composites it over whatever color/feather the merchant
    picks entirely client-side, so changing the color swatch is instant
    instead of re-running background removal on every tweak."""
    try:
        _shop, client = _get_client()
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401

    body = request.get_json(silent=True) or {}
    product_id = body.get("product_id")
    image_id = body.get("image_id")
    if not product_id or not image_id:
        return jsonify({"ok": False, "error": "Missing product_id or image_id."}), 400

    try:
        from rembg import remove
    except ImportError:
        return jsonify({"ok": False, "error": "Background removal isn't installed on this server (missing rembg)."}), 500

    try:
        image = _find_image(client, product_id, image_id)
        raw = client.download_image(image["src"])
        cutout = remove(Image.open(io.BytesIO(raw)))
        out = io.BytesIO()
        cutout.save(out, format="PNG")
        b64 = base64.b64encode(out.getvalue()).decode()
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400

    return jsonify({"ok": True, "cutout_base64": b64})


@edit_bp.route("/api/edit/color-bg", methods=["POST"])
def edit_color_bg():
    try:
        shop, client = _get_client()
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401

    body = request.get_json(silent=True) or {}
    try:
        pairs = _image_pairs(body)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not pairs:
        return jsonify({"ok": False, "error": "Select at least one image."}), 400

    mode = body.get("mode", "solid")
    if mode not in ("solid", "transparent"):
        return jsonify({"ok": False, "error": "Only 'solid' and 'transparent' are supported right now."}), 400
    color = body.get("color", "#0a2029")
    feather = max(0, min(12, int(body.get("feather", 0) or 0)))

    quota_left = billing_store.remaining(shop)
    if quota_left <= 0:
        return jsonify({"ok": False, "error": "Your plan limit is used up.", "quota_exceeded": True}), 402

    try:
        from rembg import remove  # heavy import — only pay for it on this route
    except ImportError:
        return jsonify({"ok": False, "error": "Background removal isn't installed on this server (missing rembg)."}), 500

    updated, failed = [], []
    for product_id, image_id in pairs:
        if quota_left <= 0:
            break
        try:
            image = _find_image(client, product_id, image_id)
            raw = client.download_image(image["src"])
            cutout = remove(Image.open(io.BytesIO(raw)))  # RGBA, subject only

            out = io.BytesIO()
            if mode == "transparent":
                cutout.save(out, format="PNG")
                filename = "transparent.png"
            else:
                composited = composite_over_color(cutout, color, feather)
                composited.save(out, format="JPEG", quality=90)
                filename = "recolored.jpg"

            b64 = base64.b64encode(out.getvalue()).decode()
            client.update_image(product_id, image_id, b64, filename=filename)
            product_cache.invalidate(client.store_domain)
            billing_store.record_usage(shop, 1)
            quota_left -= 1
            updated.append(image_id)
        except Exception as exc:  # noqa: BLE001
            failed.append({"image_id": image_id, "error": str(exc)})

    return jsonify({"ok": True, "updated": updated, "failed": failed})


@edit_bp.route("/api/edit/generate/preview", methods=["POST"])
def edit_generate_preview():
    """Generates an image but writes nothing to the store. Charges
    GENERATE_CREDITS per preview: each call is a paid Gemini image generation,
    whether or not the merchant then uses the result."""
    try:
        shop, _unused_client = _get_client()
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401

    if billing_store.remaining(shop) < GENERATE_CREDITS:
        return jsonify({"ok": False, "quota_exceeded": True,
                        "error": f"Generating an image uses {GENERATE_CREDITS} credits. Buy a pack to continue."}), 402

    body = request.get_json(silent=True) or {}
    prompt = (body.get("prompt") or "").strip()
    if not prompt:
        return jsonify({"ok": False, "error": "Enter a prompt."}), 400
    style = body.get("style", "studio")
    ratio = body.get("ratio", "1:1")
    product_title = body.get("product_title", "")

    full_prompt = (
        f"Product photo for an e-commerce listing. Product: {product_title or 'unspecified'}. "
        f"Style: {style}. Aspect ratio: {ratio}. {prompt}"
    )

    try:
        data = _generate_image(full_prompt)
        parts = data["candidates"][0]["content"]["parts"]
        image_part = next(p for p in parts if "inline_data" in p or "inlineData" in p)
        inline = image_part.get("inline_data") or image_part.get("inlineData")
        image_b64 = inline["data"]
        mime_type = inline.get("mime_type") or inline.get("mimeType") or "image/png"
    except GeminiBusy:
        return jsonify({"ok": False, "error": "Google's image service is busy right now. Please try "
                                              "again in a minute. No credits were used."}), 503
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"Generation failed: {exc}"}), 502

    billing_store.record_usage(shop, GENERATE_CREDITS)
    return jsonify({"ok": True, "image_base64": image_b64, "mime_type": mime_type})


@edit_bp.route("/api/edit/generate/use", methods=["POST"])
def edit_generate_use():
    try:
        _shop, client = _get_client()
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401

    # No charge here: the credits were already taken when the preview was generated.
    body = request.get_json(silent=True) or {}
    product_id = body.get("product_id")
    image_id = body.get("image_id")
    image_b64 = body.get("image_base64")
    if not product_id or not image_id or not image_b64:
        return jsonify({"ok": False, "error": "Missing product_id, image_id, or image_base64."}), 400

    try:
        client.update_image(product_id, image_id, image_b64, filename="generated.png")
        product_cache.invalidate(client.store_domain)
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400

    return jsonify({"ok": True})
