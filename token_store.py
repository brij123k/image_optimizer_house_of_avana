"""Per-shop access token storage.

One row per installed shop. Tokens are as sensitive as passwords — anyone
holding one can read and write that merchant's store — so the database file is
created with owner-only permissions and should never be committed.
"""
import os
import sqlite3
import threading
from datetime import datetime, timezone

DB_PATH = os.environ.get("SHOP_DB_PATH", "shops.db")

_LOCAL = threading.local()


def _conn():
    """One connection per thread; sqlite3 objects aren't shareable across them."""
    if getattr(_LOCAL, "conn", None) is None:
        new_file = not os.path.exists(DB_PATH)
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
        # WAL lets one thread write while others read without "database is
        # locked" errors — matters once multiple merchants hit the app
        # (and multiple gunicorn workers) at the same time.
        conn.execute("PRAGMA journal_mode=WAL")
        if new_file:
            try:
                os.chmod(DB_PATH, 0o600)
            except OSError:
                pass
        conn.execute("""
            CREATE TABLE IF NOT EXISTS shops (
                shop          TEXT PRIMARY KEY,
                access_token  TEXT NOT NULL,
                scope         TEXT,
                installed_at  TEXT NOT NULL,
                updated_at    TEXT NOT NULL
            )
        """)
        conn.commit()
        _LOCAL.conn = conn
    return _LOCAL.conn


def conn():
    """Exposes the shared connection so other modules (billing_store.py) can
    keep their tables in this same database file."""
    return _conn()


def save_shop(shop, access_token, scope=None):
    """Insert or update a shop's token. Re-installing overwrites the old one."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = _conn()
    conn.execute("""
        INSERT INTO shops (shop, access_token, scope, installed_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(shop) DO UPDATE SET
            access_token = excluded.access_token,
            scope        = excluded.scope,
            updated_at   = excluded.updated_at
    """, (shop, access_token, scope, now, now))
    conn.commit()


def get_token(shop):
    """Returns the stored access token, or None if the shop hasn't installed."""
    row = _conn().execute(
        "SELECT access_token FROM shops WHERE shop = ?", (shop,)
    ).fetchone()
    return row["access_token"] if row else None


def get_shop(shop):
    row = _conn().execute("SELECT * FROM shops WHERE shop = ?", (shop,)).fetchone()
    return dict(row) if row else None


def delete_shop(shop):
    """Called when the merchant uninstalls — the token is dead at that point."""
    conn = _conn()
    conn.execute("DELETE FROM shops WHERE shop = ?", (shop,))
    conn.commit()


def list_shops():
    """Every installed shop, without exposing the tokens."""
    rows = _conn().execute(
        "SELECT shop, scope, installed_at, updated_at FROM shops ORDER BY shop"
    ).fetchall()
    return [dict(r) for r in rows]