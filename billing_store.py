"""Per-shop image-optimization quota and one-time purchase records.

Every shop gets FREE_IMAGES optimizations for free, once, for the life of the
install. Past that, images processed are billed against purchased credit
packs (see billing.py for the Shopify Billing API side of buying one).
"""
from datetime import datetime, timezone

from token_store import conn as _shared_conn

FREE_IMAGES = 20


def _conn():
    c = _shared_conn()
    c.execute("""
        CREATE TABLE IF NOT EXISTS usage (
            shop              TEXT PRIMARY KEY,
            images_used       INTEGER NOT NULL DEFAULT 0,
            credits_purchased INTEGER NOT NULL DEFAULT 0
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS purchases (
            charge_id  TEXT PRIMARY KEY,
            shop       TEXT NOT NULL,
            plan_id    TEXT NOT NULL,
            images     INTEGER NOT NULL,
            price      TEXT NOT NULL,
            status     TEXT NOT NULL DEFAULT 'pending',  -- pending | credited
            created_at TEXT NOT NULL
        )
    """)
    c.commit()
    return c


def get_usage(shop):
    """Returns {used, free, credits, remaining} for the given shop."""
    row = _conn().execute(
        "SELECT images_used, credits_purchased FROM usage WHERE shop = ?", (shop,)
    ).fetchone()
    used, credits = (row["images_used"], row["credits_purchased"]) if row else (0, 0)
    remaining = max(0, FREE_IMAGES + credits - used)
    return {"used": used, "free": FREE_IMAGES, "credits": credits, "remaining": remaining}


def remaining(shop):
    return get_usage(shop)["remaining"]


def record_usage(shop, n=1):
    """Call once per image actually optimized (re-uploaded) — never for
    skips or already-done images, since those cost nothing."""
    c = _conn()
    c.execute("""
        INSERT INTO usage (shop, images_used, credits_purchased) VALUES (?, ?, 0)
        ON CONFLICT(shop) DO UPDATE SET images_used = images_used + excluded.images_used
    """, (shop, n))
    c.commit()


def record_purchase(charge_id, shop, plan_id, images, price):
    """Logs a charge as soon as it's created (status 'pending'), before the
    merchant has approved it — credit_purchase() flips it to 'credited' once
    Shopify confirms the charge actually went through."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    c = _conn()
    c.execute("""
        INSERT INTO purchases (charge_id, shop, plan_id, images, price, status, created_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?)
        ON CONFLICT(charge_id) DO NOTHING
    """, (charge_id, shop, plan_id, images, str(price), now))
    c.commit()


def pending_charge_ids(shop):
    rows = _conn().execute(
        "SELECT charge_id FROM purchases WHERE shop = ? AND status = 'pending'", (shop,)
    ).fetchall()
    return {r["charge_id"] for r in rows}


def credit_purchase(charge_id):
    """Marks a pending purchase as paid and adds its images to the shop's
    balance. Idempotent — crediting an already-credited charge is a no-op, so
    it's safe to call this speculatively every time the merchant returns from
    checkout."""
    c = _conn()
    row = c.execute(
        "SELECT shop, images, status FROM purchases WHERE charge_id = ?", (charge_id,)
    ).fetchone()
    if not row or row["status"] == "credited":
        return False
    c.execute("UPDATE purchases SET status = 'credited' WHERE charge_id = ?", (charge_id,))
    c.execute("""
        INSERT INTO usage (shop, images_used, credits_purchased) VALUES (?, 0, ?)
        ON CONFLICT(shop) DO UPDATE SET credits_purchased = credits_purchased + excluded.credits_purchased
    """, (row["shop"], row["images"]))
    c.commit()
    return True


def list_purchases(shop):
    rows = _conn().execute(
        "SELECT charge_id, plan_id, images, price, status, created_at FROM purchases "
        "WHERE shop = ? ORDER BY created_at DESC", (shop,)
    ).fetchall()
    return [dict(r) for r in rows]
