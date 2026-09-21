"""Keyword suggestions and the keyword-stuffing checker.

While the AI reads each image it also proposes 3 keywords. They are used
sparingly in the ALT text (see check_stuffing) and saved here so the merchant
can reuse them later in blog posts and other content.
"""
import csv
import io
import re
from collections import Counter
from datetime import datetime, timezone

from flask import Blueprint, Response, jsonify

import billing_store
from shopify_auth import current_shop
from token_store import conn as _shared_conn

keywords_bp = Blueprint("keywords", __name__)

MAX_ALT = 125
_STOP = set("""a an the and or of in on at to for with by from as is are was were be this that these those
its it their his her our your over under into onto near next up down out off very""".split())
_GENERIC = {"image", "images", "photo", "photos", "picture", "pictures", "product", "products", "item", "items"}


# ---------------------------------------------------------------- keywords

def _norm(text):
    text = re.sub(r"[^a-z0-9'\- ]+", " ", str(text).lower().replace("_", " "))
    return re.sub(r"\s+", " ", text).strip(" -'")


def clean_keywords(raw, banned=(), limit=3):
    """Up to `limit` short, distinct, specific keyword phrases. Drops empty or
    generic ones ("photo"), anything over 4 words, and anything containing a
    collection name."""
    banned_n = [_norm(b) for b in banned if _norm(b)]
    out = []
    for item in raw or []:
        k = _norm(item)
        words = k.split()
        if not k or len(words) > 4 or all(w in _GENERIC or w in _STOP for w in words):
            continue
        if any(b and b in k for b in banned_n) or "collection" in words:
            continue
        if k not in out:
            out.append(k)
    return out[:limit]


def _count(text, phrase):
    t = " " + re.sub(r"[^a-z0-9' ]+", " ", text.lower()) + " "
    p = re.sub(r"[^a-z0-9' ]+", " ", phrase.lower()).strip()
    t = re.sub(r"\s+", " ", t)
    return len(re.findall(r"(?<![a-z0-9])" + re.escape(p) + r"(?![a-z0-9])", t)) if p else 0


# ---------------------------------------------------------- stuffing check

def check_stuffing(alt, keywords=()):
    """Flags ALT text that reads like keyword stuffing.

    Returns {"ok": bool, "issues": [str, ...]}. Deliberately conservative: it
    only flags clear patterns — a word said 3+ times, a keyword phrase
    repeated, several separate keywords crammed in, a comma-separated list of
    fragments, a filler opening, or text over 125 characters.
    """
    alt = (alt or "").strip()
    issues = []
    if len(alt) > MAX_ALT:
        issues.append(f"longer than {MAX_ALT} characters")
    if re.match(r"(?i)^(an? )?(image|picture|photo|photograph)s? of\b", alt):
        issues.append('starts with "image of" / "picture of"')

    words = [w for w in re.findall(r"[a-z0-9']+", alt.lower()) if w not in _STOP and len(w) > 2]
    for w, c in Counter(words).most_common(1):
        if c > 2:
            issues.append(f'the word "{w}" is repeated {c} times')

    used = [k for k in keywords if _count(alt, k)]
    for k in used:
        if _count(alt, k) > 1:
            issues.append(f'keyword "{k}" is repeated')
    # keywords contained in a longer used keyword count once ("t-shirt" inside "red cotton t-shirt")
    separate = [k for k in used if not any(k != o and _norm(k) in _norm(o) for o in used)]
    if len(separate) > 2:
        issues.append("crams in " + str(len(separate)) + " separate keywords")

    fragments = [f for f in alt.split(",") if f.strip()]
    if len(fragments) >= 4 and sum(len(f.split()) for f in fragments) / len(fragments) <= 2.5:
        issues.append("reads like a comma-separated keyword list")

    return {"ok": not issues, "issues": issues}


def trim_alt(alt):
    """Last-resort fix for ALT text that is still flagged: keep the first
    clause and cut at a word boundary within the limit."""
    first = re.split(r"[,;|]", alt)[0].strip() or alt.strip()
    if len(first) > MAX_ALT:
        first = first[:MAX_ALT].rsplit(" ", 1)[0]
    return re.sub(r"^(?i:(an? )?(image|picture|photo|photograph)s? of )", "", first).strip(" ,.-")[:1].upper() + \
        re.sub(r"^(?i:(an? )?(image|picture|photo|photograph)s? of )", "", first).strip(" ,.-")[1:]


# ----------------------------------------------------------------- storage

def _conn():
    c = _shared_conn()
    c.execute("""
        CREATE TABLE IF NOT EXISTS image_keywords (
            shop TEXT NOT NULL, product_id TEXT NOT NULL, product_title TEXT,
            image_id TEXT NOT NULL, keyword TEXT NOT NULL, created_at TEXT NOT NULL,
            PRIMARY KEY (shop, image_id, keyword)
        )
    """)
    c.commit()
    return c


def save_keywords(shop, product_id, title, image_id, keywords):
    """Replaces the keywords stored for one image. Never raises."""
    try:
        c = _conn()
        c.execute("DELETE FROM image_keywords WHERE shop=? AND image_id=?", (shop, str(image_id)))
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        c.executemany(
            "INSERT OR IGNORE INTO image_keywords VALUES (?,?,?,?,?,?)",
            [(shop, str(product_id), title, str(image_id), k, now) for k in keywords],
        )
        c.commit()
    except Exception:  # noqa: BLE001
        pass


def summary(shop):
    rows = _conn().execute(
        "SELECT product_id, product_title, image_id, keyword FROM image_keywords WHERE shop=? ORDER BY rowid",
        (shop,),
    ).fetchall()
    per_product, titles, images = {}, {}, set()
    overall = Counter()
    for r in rows:
        per_product.setdefault(r["product_id"], Counter())[r["keyword"]] += 1
        titles[r["product_id"]] = r["product_title"] or f"Product {r['product_id']}"
        images.add(r["image_id"])
        overall[r["keyword"]] += 1
    products = []
    for pid, counter in per_product.items():
        ranked = [k for k, _ in counter.most_common()]
        products.append({"product_id": pid, "title": titles[pid], "keywords": ranked[:3], "all": ranked,
                         "images": sum(counter.values())})
    products.sort(key=lambda p: p["title"].lower())
    return {
        "totals": {"products": len(products), "images": len(images), "keywords": len(overall)},
        "top": [{"keyword": k, "count": c} for k, c in overall.most_common(15)],
        "products": products,
    }


@keywords_bp.route("/api/keywords")
def keywords_api():
    shop = current_shop()
    if not shop:
        return jsonify({"ok": False, "error": "No shop in session."}), 401
    st = billing_store.get_stats(shop)
    return jsonify({"ok": True, **summary(shop),
                    "stuffing": {"checked": st["stuffing_checked"], "fixed": st["stuffing_fixed"]}})


@keywords_bp.route("/api/keywords.csv")
def keywords_csv():
    shop = current_shop()
    if not shop:
        return jsonify({"ok": False, "error": "No shop in session."}), 401
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["product", "keyword", "images_using_it"])
    for p in summary(shop)["products"]:
        counts = Counter()
        for r in _conn().execute(
            "SELECT keyword FROM image_keywords WHERE shop=? AND product_id=?", (shop, p["product_id"])
        ):
            counts[r["keyword"]] += 1
        for k in p["all"]:
            w.writerow([p["title"], k, counts[k]])
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=keyword-suggestions.csv"})
