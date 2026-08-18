"""Small wrapper around the Shopify Admin REST API — just the bits this
tool needs: listing collections, listing products (whole store, by
collection, or by explicit IDs), and replacing an image's data.
"""
import re
import time

import requests


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
        """Wraps session requests with a simple 429 retry (Shopify's leaky
        bucket). Retries up to 4 times, honouring Retry-After."""
        for attempt in range(4):
            resp = self.session.request(method, url, **kwargs)
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", 2))
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        resp.raise_for_status()
        return resp

    def _paginated(self, url, params):
        """Yields response objects, following Shopify's cursor pagination.
        Subsequent requests must use the full 'next' URL with no extra params."""
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
        resp = self._request("GET", f"{self.base_url}/shop.json", timeout=15)
        return resp.json()["shop"]["name"]

    def iter_products(self, limit=250, collection_id=None):
        """Yields product dicts (each including its 'images' list). Pass
        collection_id to restrict to a single collection."""
        params = {"limit": limit, "fields": "id,title,images"}
        if collection_id:
            params["collection_id"] = collection_id
        for resp in self._paginated(f"{self.base_url}/products.json", params):
            for product in resp.json().get("products", []):
                yield product

    def iter_collection_products(self, collection_id):
        """Yields every product in one collection (custom or smart)."""
        return self.iter_products(collection_id=collection_id)

    def get_products_by_ids(self, product_ids):
        """Returns product dicts for an explicit list of IDs, in chunks of 250."""
        ids = [str(pid) for pid in product_ids if pid]
        products = []
        for i in range(0, len(ids), 250):
            chunk = ",".join(ids[i:i + 250])
            resp = self._request(
                "GET",
                f"{self.base_url}/products.json",
                params={"ids": chunk, "limit": 250, "fields": "id,title,images"},
                timeout=30,
            )
            products.extend(resp.json().get("products", []))
        return products

    def list_collections(self):
        """Returns [{id, title, products_count, kind}] for custom + smart
        collections, sorted by title."""
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

    # ---------- writes ----------

    def update_image(self, product_id, image_id, base64_data, filename=None):
        """Replaces an existing product image's binary data in place, keeping
        its image ID (and therefore its position/alt text) unchanged. Pass
        filename when the format changes (e.g. .jpg -> .webp)."""
        url = f"{self.base_url}/products/{product_id}/images/{image_id}.json"
        image = {"id": image_id, "attachment": base64_data}
        if filename:
            image["filename"] = filename
        resp = self._request("PUT", url, json={"image": image}, timeout=60)
        return resp.json()["image"]