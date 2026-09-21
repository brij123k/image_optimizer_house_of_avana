"""Small wrapper around the Shopify Admin REST API — listing collections and
products, replacing image data, and persisting an "already optimized" ledger
in a shop-level metafield.
"""
import json
import re
import time

import requests

LEDGER_NAMESPACE = "image_compactor"
LEDGER_KEY_PREFIX = "ledger_"
# Kept well under Shopify's metafield value ceiling; the ledger is split
# across ledger_0, ledger_1, ... as it grows.
LEDGER_CHUNK_CHARS = 60000

# Separate namespace for the AI ALT-text/rename record, so it can be reset
# independently of the compression ledger.
AI_LEDGER_NAMESPACE = "image_ai_labels"
AI_LEDGER_KEY_PREFIX = "ledger_"


class ShopifyClient:
    def __init__(self, store_domain, access_token, api_version="2026-07"):
        self.store_domain = (
            store_domain.strip().replace("https://", "").replace("http://", "").rstrip("/")
        )
        self.access_token = access_token.strip()
        self.api_version = api_version.strip()
        self.base_url = f"https://{self.store_domain}/admin/api/{self.api_version}"
        self.session = requests.Session()
        self.session.headers.update({
            "X-Shopify-Access-Token": self.access_token,
            "Content-Type": "application/json",
        })

    # ---------- internals ----------

    def _request(self, method, url, **kwargs):
        """Wraps requests with a simple 429 retry (Shopify's leaky bucket)."""
        for _ in range(4):
            resp = self.session.request(method, url, **kwargs)
            if resp.status_code == 429:
                time.sleep(float(resp.headers.get("Retry-After", 2)))
                continue
            resp.raise_for_status()
            return resp
        resp.raise_for_status()
        return resp

    def _paginated(self, url, params):
        """Yields response objects, following Shopify's cursor pagination."""
        while url:
            resp = self._request("GET", url, params=params, timeout=30)
            yield resp
            url, params = None, None
            match = re.search(r'<([^>]+)>;\s*rel="next"', resp.headers.get("Link", ""))
            if match:
                url = match.group(1)

    # ---------- reads ----------

    def verify_connection(self):
        """Raises for bad credentials/domain; returns shop name on success."""
        return self._request("GET", f"{self.base_url}/shop.json", timeout=15).json()["shop"]["name"]

    def iter_products(self, limit=250, collection_id=None):
        """Yields product dicts (each including its 'images' list)."""
        params = {"limit": limit, "fields": "id,title,images"}
        if collection_id:
            params["collection_id"] = collection_id
        for resp in self._paginated(f"{self.base_url}/products.json", params):
            for product in resp.json().get("products", []):
                yield product

    def iter_collection_products(self, collection_id):
        return self.iter_products(collection_id=collection_id)

    def get_products_by_ids(self, product_ids):
        """Returns product dicts for an explicit list of IDs, in chunks of 250."""
        ids = [str(pid) for pid in product_ids if pid]
        products = []
        for i in range(0, len(ids), 250):
            resp = self._request(
                "GET", f"{self.base_url}/products.json",
                params={"ids": ",".join(ids[i:i + 250]), "limit": 250,
                        "fields": "id,title,images"},
                timeout=30,
            )
            products.extend(resp.json().get("products", []))
        return products

    def list_collections(self):
        """Returns [{id, title, products_count, kind}], sorted by title."""
        collections = []
        for kind, endpoint in (("custom", "custom_collections"), ("smart", "smart_collections")):
            params = {"limit": 250, "fields": "id,title,products_count"}
            for resp in self._paginated(f"{self.base_url}/{endpoint}.json", params):
                for coll in resp.json().get(endpoint, []):
                    collections.append({
                        "id": coll["id"],
                        "title": coll.get("title", f"Collection {coll['id']}"),
                        "products_count": coll.get("products_count"),
                        "kind": kind,
                    })
        collections.sort(key=lambda c: c["title"].lower())
        return collections

    def download_image(self, src_url):
        resp = requests.get(src_url, timeout=30)
        resp.raise_for_status()
        return resp.content

    def graphql(self, query, variables=None):
        """The Billing API (one-time purchases, subscriptions) is GraphQL-only
        — there's no REST equivalent."""
        resp = self._request(
            "POST", f"{self.base_url}/graphql.json",
            json={"query": query, "variables": variables or {}}, timeout=30,
        )
        data = resp.json()
        if data.get("errors"):
            raise RuntimeError(f"GraphQL error: {data['errors']}")
        return data["data"]

    # ---------- writes ----------

    def update_image(self, product_id, image_id, base64_data, filename=None, alt=None):
        """Replaces an image's binary data in place, keeping its ID (and so its
        position). NOTE: Shopify silently ignores `filename` on an in-place
        update — the call succeeds but the name never changes. To really rename
        an image use replace_image(). Returns the updated image object."""
        url = f"{self.base_url}/products/{product_id}/images/{image_id}.json"
        image = {"id": image_id, "attachment": base64_data}
        if filename:
            image["filename"] = filename
        if alt is not None:
            image["alt"] = alt
        return self._request("PUT", url, json={"image": image}, timeout=60).json()["image"]

    def replace_image(self, product_id, image_id, base64_data, filename, alt=None):
        """Really renames an image: uploads the data as a new image under
        `filename`, then deletes the old one. Shopify has no rename call.

        Keeps the old image's position and variant assignments (and its ALT
        text unless `alt` is given). The new image has a NEW id and CDN URL, so
        callers must record the returned image's id, not the old one.
        Order matters: the new image is created before the old one is removed,
        so a failure part-way never loses the picture."""
        base = f"{self.base_url}/products/{product_id}/images"
        old = self._request("GET", f"{base}/{image_id}.json", timeout=30).json()["image"]
        payload = {
            "attachment": base64_data,
            "filename": filename,
            "alt": alt if alt is not None else old.get("alt"),
            "position": old.get("position"),
        }
        if old.get("variant_ids"):
            payload["variant_ids"] = old["variant_ids"]
        new = self._request("POST", f"{base}.json", json={"image": payload}, timeout=60).json()["image"]
        try:
            self._request("DELETE", f"{base}/{image_id}.json", timeout=30)
        except Exception:
            # Roll back so the product isn't left with the picture twice.
            try:
                self._request("DELETE", f"{base}/{new['id']}.json", timeout=30)
            except Exception:  # noqa: BLE001
                pass
            raise
        return new

    def update_image_alt(self, product_id, image_id, alt):
        """Sets an image's ALT text only — no re-upload, no URL change."""
        url = f"{self.base_url}/products/{product_id}/images/{image_id}.json"
        image = {"id": image_id, "alt": alt}
        return self._request("PUT", url, json={"image": image}, timeout=30).json()["image"]

    # ---------- ledger ----------
    #
    # Maps image_id -> the image's updated_at *after* we rewrote it. An image
    # counts as done only if both match, so newly uploaded images (unseen ID)
    # and images edited since (changed timestamp) are picked up automatically.
    #
    # The same chunked-metafield mechanism backs two separate records: the
    # compression ledger (LEDGER_NAMESPACE) and the AI ALT-text/rename ledger
    # (AI_LEDGER_NAMESPACE) — namespace and key prefix are parameters so both
    # can share this code without stepping on each other.

    def _ledger_metafields(self, namespace=LEDGER_NAMESPACE, key_prefix=LEDGER_KEY_PREFIX):
        """Returns the raw shop-level metafields holding the given ledger."""
        params = {"namespace": namespace, "limit": 250}
        found = []
        for resp in self._paginated(f"{self.base_url}/metafields.json", params):
            for mf in resp.json().get("metafields", []):
                if mf.get("key", "").startswith(key_prefix):
                    found.append(mf)
        return sorted(found, key=lambda m: m["key"])

    def load_ledger(self, namespace=LEDGER_NAMESPACE, key_prefix=LEDGER_KEY_PREFIX):
        """Returns the merged dict stored across a ledger's chunks. Never
        raises — a missing or unreadable ledger just means nothing is known
        yet."""
        ledger = {}
        try:
            metafields = self._ledger_metafields(namespace, key_prefix)
        except requests.HTTPError:
            return {}
        for mf in metafields:
            try:
                chunk = json.loads(mf.get("value") or "{}")
            except (ValueError, TypeError):
                continue
            if isinstance(chunk, dict):
                ledger.update(chunk)
        return ledger

    def save_ledger(self, ledger, namespace=LEDGER_NAMESPACE, key_prefix=LEDGER_KEY_PREFIX):
        """Writes the ledger back, splitting it across as many metafields as it
        needs and removing chunks that are no longer used."""
        chunks, current = [], {}
        for key, value in ledger.items():
            current[key] = value
            if len(json.dumps(current)) >= LEDGER_CHUNK_CHARS:
                chunks.append(current)
                current = {}
        if current or not chunks:
            chunks.append(current)

        existing = {mf["key"]: mf for mf in self._ledger_metafields(namespace, key_prefix)}

        for i, chunk in enumerate(chunks):
            key = f"{key_prefix}{i}"
            payload = {"metafield": {
                "namespace": namespace,
                "key": key,
                "value": json.dumps(chunk, separators=(",", ":")),
                "type": "json",
            }}
            if key in existing:
                self._request(
                    "PUT", f"{self.base_url}/metafields/{existing[key]['id']}.json",
                    json=payload, timeout=30,
                )
            else:
                self._request("POST", f"{self.base_url}/metafields.json",
                              json=payload, timeout=30)

        # Drop any chunks left over from a previously larger ledger.
        for key, mf in existing.items():
            index = key[len(key_prefix):]
            if index.isdigit() and int(index) >= len(chunks):
                self._request("DELETE", f"{self.base_url}/metafields/{mf['id']}.json", timeout=30)

    def clear_ledger(self, namespace=LEDGER_NAMESPACE, key_prefix=LEDGER_KEY_PREFIX):
        """Forgets every record, so the next run treats all images as new."""
        for mf in self._ledger_metafields(namespace, key_prefix):
            self._request("DELETE", f"{self.base_url}/metafields/{mf['id']}.json", timeout=30)

    # ---------- AI label ledger ----------
    #
    # Maps image_id -> {"alt": ..., "renamed": bool, "at": iso timestamp} for
    # every image the AI feature has touched — lets the label report tell
    # "labeled by this app" apart from "alt text set some other way".

    def load_ai_ledger(self):
        return self.load_ledger(AI_LEDGER_NAMESPACE, AI_LEDGER_KEY_PREFIX)

    def save_ai_ledger(self, ledger):
        return self.save_ledger(ledger, AI_LEDGER_NAMESPACE, AI_LEDGER_KEY_PREFIX)

    def clear_ai_ledger(self):
        return self.clear_ledger(AI_LEDGER_NAMESPACE, AI_LEDGER_KEY_PREFIX)