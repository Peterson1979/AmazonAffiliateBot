"""
Unit tests for cached product loading.
"""

import sys
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from amazon.scraper import Product


class TestProductCache(unittest.TestCase):
    def test_load_existing_cached_product(self):
        cache_path = Path(__file__).resolve().parent.parent / "data" / "products" / "asin_B0GKGBWRTR.json"
        self.assertTrue(cache_path.exists(), f"Cached product file not found at {cache_path}")

        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        product = Product.from_dict(data)
        self.assertEqual(product.asin, "B0GKGBWRTR")
        self.assertIn("Laptop Stand", product.title)
        self.assertGreaterEqual(len(product.features), 3)
        self.assertEqual(product.affiliate_url, "https://link.amazon/B0hwa4Nkm")

    def test_load_cached_product_rejects_stale_image_cache(self):
        """A same-ASIN cache without a verified image must trigger ingestion recovery."""
        import amazon_affiliate
        from amazon_affiliate import get_cache_key, load_cached_product

        affiliate_url = "https://link.amazon/B0bXBNrkK"
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / ".test-tmp") as temp_dir:
            cache_dir = Path(temp_dir)
            key = get_cache_key(affiliate_url)
            (cache_dir / f"{key}.json").write_text(json.dumps({
                "title": "Stale product",
                "features": ["feature"],
                "affiliate_url": affiliate_url,
                "asin": "B0STALE001",
                "image_url": None,
            }), encoding="utf-8")
            with patch.object(amazon_affiliate, "PRODUCTS_CACHE_DIR", cache_dir):
                is_cached, product, found_key = load_cached_product(affiliate_url)
            self.assertFalse(is_cached)
            self.assertIsNone(product)
            self.assertEqual(found_key, key)

    def test_cache_does_not_return_mismatched_product_for_different_url(self):
        """Verify that searching for an unknown URL does not return an arbitrary cached product."""
        from amazon_affiliate import load_cached_product

        is_cached, product, key = load_cached_product("https://link.amazon/B0UNKNOWN99")
        self.assertFalse(is_cached)
        self.assertIsNone(product)


if __name__ == "__main__":
    unittest.main()
