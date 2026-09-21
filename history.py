"""Access history: when the app was opened, from which city and IP.

Every page load (not the background API polling) is logged per shop. The
city comes from an IP geolocation lookup that runs off the request thread and
is cached per IP, so a slow or failing lookup never slows a page down.

IP addresses are personal data under GDPR/CCPA — mention them in your privacy
policy, and keep RETENTION_DAYS as short as you can justify.
"""
import ipaddress
import threading
from datetime import datetime, timedelta, timezone

import requests
from flask import Blueprint, current_app, jsonify, request, session

from shopify_auth import valid_shop
from token_store import conn as _shared_conn

history_bp = Blueprint("history", __name__)

RETENTION_DAYS = 180
PAGE_SIZE = 50
LOGGED_PATHS = {"/", "/plans"}
GEO_URL = "https://ipwho.is/{ip}"   # HTTPS, free tier, no key


def _conn():
    c = _shared_conn()
    c.execute("""
        CREATE TABLE IF NOT EXISTS access_log (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            shop    TEXT NOT NULL,
            ts      TEXT NOT NULL,          -- UTC ISO-8601
            ip      TEXT,
            city    TEXT, region TEXT, country TEXT,
            path    TEXT,
            agent   TEXT
        )
    """)
    c.execute("CREATE INDEX IF NOT EXISTS idx_access_shop_ts ON access_log(shop, ts)")
    c.execute("""
        CREATE TABLE IF NOT EXISTS ip_geo (
            ip TEXT PRIMARY KEY, city TEXT, region TEXT, country TEXT, looked_up_at TEXT
        )
    """)
    c.commit()
    return c


def client_ip():
    """The visitor's IP. Behind a proxy (nginx, a host's load balancer) the
    socket address is the proxy's, so trust X-Forwarded-For only in that case —
    a direct connection can't be allowed to claim any IP it likes."""
    remote = request.remote_addr or ""
    try:
        behind_proxy = ipaddress.ip_address(remote).is_private or ipaddress.ip_address(remote).is_loopback
    except ValueError:
        behind_proxy = False
    fwd = request.headers.get("X-Forwarded-For", "")
    if behind_proxy and fwd:
        return fwd.split(",")[0].strip()
    return remote


def _is_public(ip):
    try:
        a = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (a.is_private or a.is_loopback or a.is_link_local or a.is_reserved)


def _lookup_and_store(row_id, ip):
    """Runs in a thread: fills city/region/country on the log row."""
    try:
        c = _conn()
        cached = c.execute("SELECT city, region, country FROM ip_geo WHERE ip=?", (ip,)).fetchone()
        if cached:
            city, region, country = cached["city"], cached["region"], cached["country"]
        else:
            r = requests.get(GEO_URL.format(ip=ip), timeout=6)
            r.raise_for_status()
            d = r.json()
            if not d.get("success"):
                return
            city, region, country = d.get("city"), d.get("region"), d.get("country")
            c.execute("INSERT OR REPLACE INTO ip_geo VALUES (?,?,?,?,?)",
                      (ip, city, region, country, datetime.now(timezone.utc).isoformat(timespec="seconds")))
        c.execute("UPDATE access_log SET city=?, region=?, country=? WHERE id=?",
                  (city, region, country, row_id))
        c.commit()
    except Exception:  # noqa: BLE001 - geolocation is best-effort
        pass


@history_bp.before_app_request
def log_access():
    if request.method != "GET" or request.path not in LOGGED_PATHS:
        return
    shop = session.get("shop")
    if not valid_shop(shop):
        shop = (request.args.get("shop") or "").strip().lower()
        if not valid_shop(shop):
            return
    try:
        ip = client_ip()
        c = _conn()
        cur = c.execute(
            "INSERT INTO access_log (shop, ts, ip, path, agent) VALUES (?,?,?,?,?)",
            (shop, datetime.now(timezone.utc).isoformat(timespec="seconds"), ip,
             request.path, (request.headers.get("User-Agent") or "")[:200]),
        )
        c.execute("DELETE FROM access_log WHERE ts < ?",
                  ((datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat(timespec="seconds"),))
        c.commit()
        if _is_public(ip):
            threading.Thread(target=_lookup_and_store, args=(cur.lastrowid, ip), daemon=True).start()
    except Exception as exc:  # noqa: BLE001 - logging must never break a page
        current_app.logger.warning("access log failed: %s", exc)


def _iso(value):
    """Accepts an ISO timestamp from the browser and returns it as UTC ISO."""
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


@history_bp.route("/api/history")
def history():
    # Session only — deliberately not the ?shop= fallback: this lists IPs.
    shop = session.get("shop")
    if not valid_shop(shop):
        return jsonify({"ok": False, "error": "Connect your store first."}), 401
    try:
        start = _iso(request.args["from"])
        end = _iso(request.args["to"])
        page = max(1, int(request.args.get("page", 1)))
    except (KeyError, ValueError):
        return jsonify({"ok": False, "error": "Invalid date range."}), 400

    c = _conn()
    where, args = "shop=? AND ts>=? AND ts<?", (shop, start, end)
    total = c.execute(f"SELECT COUNT(*) n FROM access_log WHERE {where}", args).fetchone()["n"]
    uniq = c.execute(f"SELECT COUNT(DISTINCT ip) n FROM access_log WHERE {where}", args).fetchone()["n"]
    rows = c.execute(
        f"SELECT ts, ip, city, region, country, path, agent FROM access_log WHERE {where} "
        "ORDER BY ts DESC LIMIT ? OFFSET ?", (*args, PAGE_SIZE, (page - 1) * PAGE_SIZE),
    ).fetchall()
    return jsonify({
        "ok": True, "total": total, "unique_ips": uniq, "page": page, "page_size": PAGE_SIZE,
        "rows": [dict(r) for r in rows],
    })
