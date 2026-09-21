"""AI-generated ALT text and filenames for product images, via Gemini's vision
model.

Every image is sent to Gemini on its own, so each one gets ALT text, a file-name
slug and 3 suggested keywords that describe what that specific image shows.
Each ALT text is checked for keyword stuffing (see keywords.py) and rewritten if
it looks over-optimised.

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
import keywords as kw
import product_cache
from shopify_auth import current_credentials
from shopify_client import ShopifyClient

ai_bp = Blueprint("ai_metadata", __name__)

GEMINI_MODEL = "gemini-3.6-flash"  # override with GEMINI_MODEL in .env
GEMINI_FALLBACK_MODELS = ["gemini-3.5-flash", "gemini-3.1-flash-lite"]
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


_PROMPT = """You are writing SEO metadata for ONE photo of a product on an e-commerce store.
Product name (only a hint for what kind of product this is): {title}
This is image {position} of {total} for the product.
{taken}
Base everything on what you can actually SEE in THIS image — the real item, its
colour, material, pattern, angle, and any visible detail. If the product name
disagrees with the picture, trust the picture. Never invent details you cannot see.

Never mention any of these: collection names, category/section names, the store
or brand name, or the word "collection".{banned}

Respond with ONLY a JSON object, no markdown, no commentary:
{{"alt_text": "...", "file_slug": "...", "keywords": ["...", "...", "..."]}}

keywords rules:
- Exactly 3 keywords: the most important things a shopper would search for that are clearly visible in THIS image.
- Each is a specific 1-3 word phrase (e.g. "red cotton t-shirt", "crew neck"), all different from each other.
- Not generic words like "image", "photo" or "product". Never a collection, store or brand name.{used_kw}
alt_text rules:
- What the item is + what THIS image shows (angle, close-up detail, inside, or lifestyle use) + visible colour or material.
- Natural phrase, plain words, 80-125 characters. Never start with "image of" or "picture of".
- Must differ from the alt text of the product's other images; name the specific angle or detail.
- Use AT MOST ONE of the keywords, once, only if it fits naturally. NEVER stuff keywords: no word repeated
  more than twice, no keyword repeated, no comma-separated lists of keywords. Write it for a person, not a search engine.{fix}
file_slug rules:
- Lowercase words separated by hyphens, 3-6 words, no extension, no special characters.
- Describe the item and a short view/detail word when it helps (e.g. "-side-view", "-buckle"). No numbers-only names.
"""


# Google returns these when a model is briefly overloaded or rate-limited; the
# same request usually succeeds seconds later, so retry rather than fail the image.
_RETRY_STATUS = {429, 500, 502, 503, 504}
_RETRY_DELAYS = (2, 5, 10)


def _post_with_retry(body):
    """Tries the primary model with backoff, then a fallback model, before giving up."""
    primary = os.environ.get("GEMINI_MODEL") or GEMINI_MODEL
    models = [primary] + [m for m in GEMINI_FALLBACK_MODELS if m != primary]
    last = None
    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        for delay in (*_RETRY_DELAYS, None):
            try:
                resp = requests.post(url, headers={"x-goog-api-key": _gemini_key()}, json=body, timeout=45)
            except requests.RequestException as exc:
                last = exc
            else:
                if resp.status_code not in _RETRY_STATUS:
                    resp.raise_for_status()
                    return resp
                last = requests.HTTPError(f"{resp.status_code} from {model}", response=resp)
            if delay is None:
                break
            time.sleep(delay)
    raise last


def _strip_terms(text, terms, slug=False):
    """Removes collection names (and the bare word "collection") from generated
    text, in case the model repeats them despite the prompt. For slugs, terms
    are compared in hyphenated form."""
    out = text
    for term in sorted({t for t in terms if t and t.strip()}, key=len, reverse=True):
        needle = _slugify(term) if slug else term.strip()
        if not needle:
            continue
        pattern = (r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])") if slug \
            else (r"(?<!\w)" + re.escape(needle) + r"(?!\w)")
        out = re.sub(pattern, " ", out, flags=re.IGNORECASE)
    word = r"(?<![a-z0-9])collections?(?![a-z0-9])" if slug else r"\bcollections?\b"
    out = re.sub(word, " ", out, flags=re.IGNORECASE)
    if slug:
        return re.sub(r"-{2,}", "-", out.replace(" ", "-")).strip("-")
    out = re.sub(r"\s+([,.;:])", r"\1", out)
    out = re.sub(r"\s{2,}", " ", out).strip(" ,-–—")
    # Drop connector words stranded by the removal ("...from the, front view").
    tail = r"\b(?:from|in|of|the|our|by|at|with|for|and|a|an)\b"
    prev = None
    while prev != out:
        prev = out
        out = re.sub(rf"(?:\s+{tail})+(?=\s*(?:[,.;:]|$))", "", out, flags=re.IGNORECASE)
        out = re.sub(rf"^(?:{tail}\s+)+", "", out, flags=re.IGNORECASE)
        out = re.sub(r"\s+([,.;:])", r"\1", out).strip(" ,-–—")
    return out[:1].upper() + out[1:]


def generate_metadata(raw_bytes, mime_type, product_title, position=1, total=1, taken_alts=(), banned_terms=(),
                      used_keywords=(), fix_issues=()):
    """Calls Gemini's vision model once and returns {"alt_text", "file_slug", "keywords"}."""
    body = {
        "contents": [{
            "parts": [
                {"text": _PROMPT.format(
                    title=product_title or "", position=position, total=total,
                    taken=("Alt texts already used on this product (do not repeat): "
                           + " | ".join(taken_alts)) if taken_alts else "",
                    banned=(" Specifically avoid: " + "; ".join(banned_terms) + ".") if banned_terms else "",
                    used_kw=("\n- Already used in this product's other ALT texts (prefer different keywords, so no keyword "
                             "appears on every image): " + ", ".join(used_keywords) + ".") if used_keywords else "",
                    fix=("\n- YOUR PREVIOUS ATTEMPT WAS FLAGGED AS KEYWORD STUFFING (" + "; ".join(fix_issues)
                         + "). Rewrite it shorter and more natural, using no keyword at all if needed.") if fix_issues else "",
                )},
                {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(raw_bytes).decode()}},
            ]
        }],
        "generationConfig": {"response_mime_type": "application/json"},
    }
    resp = _post_with_retry(body)
    data = resp.json()
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"]
        parsed = json.loads(text)
        alt_text = _strip_terms(str(parsed["alt_text"]), banned_terms)[:250]
        file_slug = _slugify(_strip_terms(str(parsed.get("file_slug") or ""), banned_terms, slug=True))
        keywords = kw.clean_keywords(parsed.get("keywords") or [], banned_terms)
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        raise RuntimeError(f"Unexpected Gemini response: {exc}") from exc
    if not alt_text:
        raise RuntimeError("Gemini returned no ALT text.")
    return {"alt_text": alt_text, "file_slug": file_slug, "keywords": keywords}


def generate_clean_metadata(raw_bytes, mime_type, product_title, position=1, total=1, taken_alts=(),
                            banned_terms=(), used_keywords=()):
    """generate_metadata() plus the keyword-stuffing check. If the ALT text is
    flagged, the model is asked once to rewrite it; if it is still flagged the
    text is trimmed. Adds meta["stuffing"] = {"status": clean|rewritten|trimmed, "issues": [...]}."""
    meta = generate_metadata(raw_bytes, mime_type, product_title, position, total, taken_alts,
                             banned_terms, used_keywords)
    check = kw.check_stuffing(meta["alt_text"], meta["keywords"])
    if check["ok"]:
        meta["stuffing"] = {"status": "clean", "issues": []}
        return meta
    issues = check["issues"]
    retry = generate_metadata(raw_bytes, mime_type, product_title, position, total, taken_alts,
                              banned_terms, used_keywords, fix_issues=issues)
    if kw.check_stuffing(retry["alt_text"], retry["keywords"] or meta["keywords"])["ok"]:
        retry["keywords"] = retry["keywords"] or meta["keywords"]
        retry["stuffing"] = {"status": "rewritten", "issues": issues}
        return retry
    meta["alt_text"] = kw.trim_alt(retry["alt_text"] or meta["alt_text"])
    meta["stuffing"] = {"status": "trimmed", "issues": issues}
    return meta


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
    product_cache.invalidate(domain)

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
        try:  # names that must never appear in ALT text or file names
            banned_terms = [c["title"] for c in client.list_collections()]
        except Exception:  # noqa: BLE001 - the prompt rule still applies without it
            banned_terms = []
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
                    product_cache.invalidate(domain)
                    return

            title = product.get("title", f"Product {product['id']}")
            images = product.get("images", [])

            try:
                now = datetime.now().isoformat(timespec="seconds")
                taken_alts, used_slugs, kw_used, product_kw = [], set(), {}, {}
                meta = None

                for idx, image in enumerate(images, start=1):
                    if quota_left <= 0:
                        with STATE_LOCK:
                            STATE["quota_exceeded"] = True
                        continue

                    # Each photo shows something different, so each gets its own
                    # ALT text and file name rather than one copied across all.
                    raw = client.download_image(image["src"])
                    mime_type, ext = _image_info(raw)
                    hint = [f"{k} ({n}x)" for k, n in sorted(kw_used.items(), key=lambda kv: -kv[1])][:6]
                    meta = generate_clean_metadata(raw, mime_type, title, idx, len(images), taken_alts, banned_terms, hint)
                    for k in meta["keywords"]:
                        product_kw[k] = product_kw.get(k, 0) + 1
                        if kw._count(meta["alt_text"], k):
                            kw_used[k] = kw_used.get(k, 0) + 1
                    billing_store.add_stat(domain, "stuffing_checked", 1)
                    if meta["stuffing"]["status"] != "clean":
                        billing_store.add_stat(domain, "stuffing_fixed", 1)
                        _log(f"Stuffing guard: {title} image {idx} — {meta['stuffing']['status']} "
                             f"({'; '.join(meta['stuffing']['issues'])})")
                    taken_alts.append(meta["alt_text"])
                    slug, n = meta["file_slug"], 2
                    while slug in used_slugs:
                        slug = f"{meta['file_slug']}-{n}"
                        n += 1
                    used_slugs.add(slug)

                    touched = False
                    if rename_files:
                        filename = f"{slug}.{ext}"
                        # Shopify can't rename in place, so this uploads a copy
                        # under the new name and removes the old one. The image
                        # gets a new id — the ledger below must use that one.
                        new_image = client.replace_image(
                            product["id"], image["id"],
                            base64.b64encode(raw).decode(),
                            filename=filename,
                            alt=meta["alt_text"] if update_alt else None,
                        )
                        image = {**image, "id": new_image["id"]}
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
                        if update_alt:
                            billing_store.add_stat(domain, "alt_updated", 1)
                        if rename_files:
                            billing_store.add_stat(domain, "files_renamed", 1)
                        ledger[str(image["id"])] = {
                            "alt": meta["alt_text"] if update_alt else None,
                            "renamed": bool(rename_files),
                            "at": now,
                            "keywords": meta["keywords"],
                        }
                        kw.save_keywords(domain, product["id"], title, image["id"], meta["keywords"])
                        billing_store.add_stat(domain, "keywords_found", len(meta["keywords"]))
                        unsaved += 1
                    time.sleep(0.3)

                if unsaved >= LEDGER_FLUSH_EVERY:
                    flush_ledger()

                top = [k for k, _ in sorted(product_kw.items(), key=lambda kv: -kv[1])][:3]
                _log(f"Labeled: {title} — {len(taken_alts)} of {len(images)} image(s), e.g. \"{taken_alts[0] if taken_alts else ''}\""
                     + (f" · Keywords: {', '.join(top)}" if top else ""))

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
        product_cache.invalidate(domain)

    except Exception as exc:  # noqa: BLE001 - top-level failure (bad creds, network)
        try:
            flush_ledger()
        except Exception:  # noqa: BLE001
            pass
        with STATE_LOCK:
            STATE["status"] = "error"
            STATE["finished_at"] = datetime.now().isoformat(timespec="seconds")
        _log(f"Error: {exc}")
        product_cache.invalidate(domain)


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
        refresh = bool(request.args.get("refresh"))
        items = product_cache.products(domain, client, collection_id, refresh)
        ledger = product_cache.ledger(domain, client, "ai", refresh)

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
        product_cache.invalidate(client.store_domain)
        return jsonify({"ok": True})
    except PermissionError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 401
    except Exception as exc:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(exc)}), 400
