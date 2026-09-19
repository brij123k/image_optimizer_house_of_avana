"""Per-shop access token storage.

One row per installed shop. Tokens are as sensitive as passwords — anyone
holding one can read and write that merchant's store — so the database file is
created with owner-only permissions and should never be committed.
"""
import os
import sqlite3
import threading
from datetime import datetime, timezone

# Anchored to this file's own directory, not the current working directory —
# a relative path here would silently try to open/create shops.db wherever
# the process happened to be launched *from*, which breaks the moment the
# server is started from a different terminal/cwd than usual.
_DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shops.db")
DB_PATH = os.environ.get("SHOP_DB_PATH") or _DEFAULT_DB_PATH

_LOCAL = threading.local()


def _conn():
    """One connection per thread; sqlite3 objects aren't shareable across them."""
    if getattr(_LOCAL, "conn", None) is None:
        new_file = not os.path.exists(DB_PATH)
        conn = sqlite3.connect(DB_PATH, timeout=10)
        conn.row_factory = sqlite3.Row
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
        # Expiring offline tokens: added later, so migrate older DBs in place.
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(shops)")}
        for col in ("refresh_token", "expires_at", "refresh_expires_at"):
            if col not in cols:
                conn.execute(f"ALTER TABLE shops ADD COLUMN {col} TEXT")
        conn.commit()
        _LOCAL.conn = conn
    return _LOCAL.conn


def conn():
    """Exposes the shared connection so other modules (billing_store.py) can
    keep their tables in this same database file."""
    return _conn()


def save_shop(shop, access_token, scope=None, refresh_token=None,
              expires_at=None, refresh_expires_at=None):
    """Insert or update a shop's token. Re-installing overwrites the old one.
    expires_at / refresh_expires_at are ISO timestamps (expiring offline tokens)."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    conn = _conn()
    conn.execute("""
        INSERT INTO shops (shop, access_token, scope, installed_at, updated_at,
                           refresh_token, expires_at, refresh_expires_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(shop) DO UPDATE SET
            access_token       = excluded.access_token,
            scope              = COALESCE(excluded.scope, shops.scope),
            updated_at         = excluded.updated_at,
            refresh_token      = excluded.refresh_token,
            expires_at         = excluded.expires_at,
            refresh_expires_at = excluded.refresh_expires_at
    """, (shop, access_token, scope, now, now, refresh_token, expires_at, refresh_expires_at))
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