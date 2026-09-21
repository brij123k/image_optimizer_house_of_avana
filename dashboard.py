"""Dashboard data: one call that gathers everything the landing page shows.

Counts only — no IP addresses here (those stay on the session-only History page).
"""
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify

import billing_store
import keywords as kw
import token_store
from history import _conn as _history_conn
from shopify_auth import current_shop

dashboard_bp = Blueprint("dashboard", __name__)


@dashboard_bp.route("/api/dashboard")
def dashboard():
    shop = current_shop()
    if not shop:
        return jsonify({"ok": False, "error": "No shop in session."}), 401

    rec = token_store.get_shop(shop) or {}
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat(timespec="seconds")
    c = _history_conn()
    visits_7d = c.execute(
        "SELECT COUNT(*) n FROM access_log WHERE shop=? AND ts>=?", (shop, week_ago)
    ).fetchone()["n"]
    last = c.execute(
        "SELECT ts, city, country FROM access_log WHERE shop=? ORDER BY ts DESC LIMIT 1", (shop,)
    ).fetchone()

    return jsonify({
        "ok": True,
        "shop": {
            "domain": shop,
            "name": rec.get("shop_name"),
            "owner": rec.get("owner_name"),
            "plan": rec.get("shopify_plan"),
            "installed_at": rec.get("installed_at"),
        },
        "usage": billing_store.get_usage(shop),
        "stats": billing_store.get_stats(shop),
        "purchases": [p for p in billing_store.list_purchases(shop) if p["status"] == "credited"][:3],
        "top_keywords": kw.summary(shop)["top"][:8],
        "visits_7d": visits_7d,
        "last_visit": dict(last) if last else None,
    })
