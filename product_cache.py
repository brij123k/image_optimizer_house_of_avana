"""Short-lived cache of a store's product list and its "already optimized" records.

Reading every product from Shopify is slow on a big store: Shopify hands out 250 at a
time, one page after another (House Of Avana: 6,326 products, about 30 seconds). The
picker, the image report and the AI report all read the same list, so it is read once
and reused for a few minutes. Anything that changes images clears it (invalidate), and
a job never uses it: jobs always read fresh data.
"""
import threading
import time

TTL = 900  # seconds (15 min). Anything this app changes clears the cache at once; the Refresh buttons force a re-read.
_lock = threading.Lock()
_entries = {}       # key -> (loaded_at, value)
_loading = {}       # key -> Lock, so two requests for the same list don't both hit Shopify


def _get(key, loader, refresh=False):
    with _lock:
        hit = _entries.get(key)
        if hit and not refresh and time.time() - hit[0] < TTL:
            return hit[1]
        gate = _loading.setdefault(key, threading.Lock())
    with gate:                       # the second caller waits here, then finds the first one's result
        with _lock:
            hit = _entries.get(key)
            if hit and not refresh and time.time() - hit[0] < TTL:
                return hit[1]
        value = loader()
        with _lock:
            _entries[key] = (time.time(), value)
        return value


def products(shop, client, collection_id=None, refresh=False):
    return _get((shop, "products", collection_id or ""),
                lambda: list(client.iter_products(collection_id=collection_id)), refresh)


def ledger(shop, client, kind="compress", refresh=False):
    load = client.load_ai_ledger if kind == "ai" else client.load_ledger
    return _get((shop, "ledger", kind), load, refresh)


def invalidate(shop):
    """Call after anything that changes a store's images or its records."""
    with _lock:
        for key in [k for k in _entries if k[0] == shop]:
            _entries.pop(key, None)


_warming = set()


def prewarm(shop, client):
    """Start reading the whole store in the background (once), so the product picker and the
    reports are already loaded by the time the merchant opens them. If the merchant opens one
    while this is still running, it waits for this load instead of starting a second one."""
    with _lock:
        hit = _entries.get((shop, "products", ""))
        fresh = hit and time.time() - hit[0] < TTL
        if fresh or shop in _warming:
            return False
        _warming.add(shop)

    def work():
        try:
            products(shop, client)
            ledger(shop, client, "compress")
            ledger(shop, client, "ai")
        except Exception:  # noqa: BLE001 - a failed warm-up just means the first real request loads it
            pass
        finally:
            with _lock:
                _warming.discard(shop)

    threading.Thread(target=work, daemon=True).start()
    return True
