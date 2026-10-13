"""Focused offline coverage for verified product-image ingestion."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from amazon.scraper import Product
from content.package import ContentPackage, STATUS_READY_FOR_REVIEW
from publishing.queue import resolve_product_for_url


class TestImageIngestion(unittest.TestCase):
    def _social_data(self):
        return {
            "platforms": {
                "facebook": {"content": "Facebook copy"},
                "pinterest": {"title": "Pin title", "description": "Pin description"},
                "threads": {"content": "Threads copy"},
            }
        }

    def test_valid_scraped_image_is_persisted_and_packaged(self):
        product = Product(
            title="Test Lamp",
            features=["1000 lumens"],
            affiliate_url="https://link.amazon/test-image",
            source_url="https://www.amazon.com/dp/B0IMAGE001",
            asin="B0IMAGE001",
            image_url="https://m.media-amazon.com/images/I/test-image.jpg",
        )

        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / ".test-tmp") as temp_dir:
            products_dir = Path(temp_dir)
            with patch("amazon.scraper.scrape_amazon_product", return_value=product):
                ok, resolved, cache_key, errors = resolve_product_for_url(
                    product.affiliate_url,
                    products_dir=products_dir,
                    allow_scrape=True,
                )

            self.assertTrue(ok, errors)
            self.assertEqual(cache_key, "asin_B0IMAGE001")
            cache_path = products_dir / "asin_B0IMAGE001.json"
            self.assertEqual(json.loads(cache_path.read_text(encoding="utf-8"))["image_url"], product.image_url)

            package = ContentPackage.build_package(
                product=resolved,
                social_data=self._social_data(),
                cache_file_path=str(cache_path),
            )
            self.assertEqual(package["status"], STATUS_READY_FOR_REVIEW)
            self.assertEqual(package["platforms"]["pinterest"]["image_url"], product.image_url)

    def test_missing_or_invalid_scraped_image_fails_closed_without_cache(self):
        for image_url in (None, "http://example.com/not-verified.jpg"):
            with self.subTest(image_url=image_url), tempfile.TemporaryDirectory(dir=PROJECT_ROOT / ".test-tmp") as temp_dir:
                product = Product(
                    title="Test Lamp",
                    features=["1000 lumens"],
                    affiliate_url="https://link.amazon/test-image",
                    asin="B0IMAGE002",
                    image_url=image_url,
                )
                products_dir = Path(temp_dir)
                with patch("amazon.scraper.scrape_amazon_product", return_value=product):
                    ok, _, _, errors = resolve_product_for_url(
                        product.affiliate_url,
                        products_dir=products_dir,
                        allow_scrape=True,
                    )

                self.assertFalse(ok)
                self.assertIn("no verified HTTPS image URL", errors[0])
                self.assertFalse((products_dir / "asin_B0IMAGE002.json").exists())


if __name__ == "__main__":
    unittest.main()
