"""
Unit tests for cached product loading.
"""

import sys
import json
import unittest
from pathlib import Path

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

    def test_load_cached_espresso_machine_product(self):
        """Verify espresso machine B0GNGTN7TV is correctly cached and matched to its affiliate URL."""
        from amazon_affiliate import load_cached_product

        affiliate_url = "https://link.amazon/B0bXBNrkK"
        is_cached, product, key = load_cached_product(affiliate_url)
        self.assertTrue(is_cached, "Espresso machine product should be cached")
        self.assertIsNotNone(product)
        self.assertEqual(product.asin, "B0GNGTN7TV")
        self.assertIn("Espresso", product.title)
        self.assertEqual(product.affiliate_url, affiliate_url)
        self.assertEqual(key, "asin_B0GNGTN7TV")

    def test_cache_does_not_return_mismatched_product_for_different_url(self):
        """Verify that searching for an unknown URL does not return an arbitrary cached product."""
        from amazon_affiliate import load_cached_product

        is_cached, product, key = load_cached_product("https://link.amazon/B0UNKNOWN99")
        self.assertFalse(is_cached)
        self.assertIsNone(product)


if __name__ == "__main__":
    unittest.main()
