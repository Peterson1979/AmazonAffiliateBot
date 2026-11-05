"""Focused regression tests for immutable affiliate URL provenance."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from amazon.scraper import Product
from content.package import ContentPackage
from publishing.adapters.facebook_adapter import FacebookAdapter
from publishing.adapters.pinterest_adapter import PinterestAdapter
from publishing.adapters.threads_adapter import ThreadsAdapter
from publishing.queue import QueueItem, resolve_product_for_url
from web import services as dashboard_services


class TestAffiliateUrlPreservation(unittest.TestCase):
    URL = "https://www.amazon.co.uk/dp/B0ABC12345?tag=partner-20&linkCode=ll1&th=1&psc=1"
    REDIRECTED = "https://www.amazon.com/dp/B0ABC12345"
    IMAGE = "https://m.media-amazon.com/images/I/test.jpg"

    def product(self):
        return Product(
            title="Example product",
            features=["A real feature"],
            affiliate_url=self.URL,
            source_url=self.REDIRECTED,
            asin="B0ABC12345",
            image_url=self.IMAGE,
        )

    def test_cache_resolution_does_not_replace_tracking_url_with_redirect(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent.parent) as root:
            product_dir = Path(root) / "products"
            product_dir.mkdir()
            (product_dir / "asin_B0ABC12345.json").write_text(
                json.dumps(self.product().to_dict()), encoding="utf-8"
            )
            ok, product, _, errors = resolve_product_for_url(self.URL, products_dir=product_dir)
            self.assertTrue(ok, errors)
            self.assertEqual(product.affiliate_url, self.URL)
            self.assertEqual(product.source_url, self.REDIRECTED)

    def test_queue_and_content_package_keep_exact_url(self):
        product = self.product()
        item = QueueItem(queue_item_id="item_001", original_url=self.URL, asin=product.asin)
        self.assertEqual(QueueItem.from_dict(item.to_dict()).original_url, self.URL)

        social = {
            "platforms": {
                "facebook": {"content": f"Facebook copy {self.URL}"},
                "threads": {"content": f"Threads copy {self.URL}"},
                "pinterest": {"title": "Example", "description": f"Pin copy {self.URL}"},
            }
        }
        package = ContentPackage.build_package(product, social, timestamp="2026-01-01T00:00:00+00:00")
        self.assertEqual(package["source"]["affiliate_url"], self.URL)
        self.assertIn(self.URL, package["platforms"]["facebook"]["content"])
        self.assertEqual(package["platforms"]["pinterest"]["link"], self.URL)
        self.assertIn(self.URL, package["platforms"]["threads"]["content"])

    @patch("web.services.ContentGenerator.generate_all_platforms")
    @patch("web.services.resolve_product_for_url")
    def test_dashboard_ingestion_stores_original_url_and_reports_repeat(self, resolve, generate):
        product = self.product()
        # Simulate a resolved page whose redirect URL differs from the input.
        product.affiliate_url = self.REDIRECTED
        generate.return_value = {
            "platforms": {
                "facebook": {"content": f"Facebook {self.URL}"},
                "threads": {"content": f"Threads {self.URL}"},
                "pinterest": {"title": "Example", "description": f"Pin {self.URL}"},
            }
        }
        resolve.return_value = (True, product, "asin_B0ABC12345", [])

        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent.parent) as root:
            root_path = Path(root)
            queue_file = root_path / "queue.json"
            products_dir = root_path / "products"
            generated_dir = root_path / "generated"
            with patch.object(dashboard_services, "QUEUE_FILE", queue_file), \
                 patch.object(dashboard_services, "BATCHES_FILE", root_path / "batches.json"), \
                 patch.object(dashboard_services, "PRODUCTS_DIR", products_dir), \
                 patch.object(dashboard_services, "GENERATED_DIR", generated_dir):
                first = dashboard_services.add_products_from_urls(self.URL, config={"affiliate": {"disclosure": "Ad"}})
                self.assertEqual(first["accepted"], 1)
                stored_queue = dashboard_services.PublishingQueue.load(queue_file)
                self.assertEqual(stored_queue.items[0].original_url, self.URL)
                package = json.loads((generated_dir / "asin_B0ABC12345_content_package.json").read_text(encoding="utf-8"))
                self.assertEqual(package["source"]["affiliate_url"], self.URL)
                self.assertEqual(package["platforms"]["pinterest"]["link"], self.URL)

                second = dashboard_services.add_products_from_urls(self.URL, config={"affiliate": {"disclosure": "Ad"}})
                self.assertEqual(second["accepted"], 0)
                self.assertEqual(second["results"][0]["status"], "DUPLICATE")

    @patch("publishing.adapters.facebook_adapter.requests.post")
    def test_facebook_payload_receives_original_url(self, post):
        response = MagicMock(status_code=200)
        response.json.return_value = {"id": "post-1"}
        post.return_value = response
        result = FacebookAdapter({"access_token": "token", "page_id": "page"})._execute_live_publish(
            {"content": self.URL}, {"account": "test"}
        )
        self.assertEqual(result["status"], "PUBLISHED")
        self.assertEqual(post.call_args.kwargs["data"]["message"], self.URL)

    @patch("publishing.adapters.pinterest_adapter.requests.post")
    def test_pinterest_payload_receives_original_url(self, post):
        response = MagicMock(status_code=201)
        response.json.return_value = {"id": "pin-1"}
        post.return_value = response
        result = PinterestAdapter({"access_token": "token"})._execute_live_publish(
            {"title": "Example", "description": "Copy", "link": self.URL, "image_url": self.IMAGE},
            {"account": "test", "board": "Board", "board_id": "board-1"},
        )
        self.assertEqual(result["status"], "PUBLISHED")
        self.assertEqual(post.call_args.kwargs["json"]["link"], self.URL)

    @patch("publishing.adapters.threads_adapter.time.sleep")
    @patch("publishing.adapters.threads_adapter.requests.get")
    @patch("publishing.adapters.threads_adapter.requests.post")
    def test_threads_payload_receives_original_url(self, post, get, _sleep):
        create_response = MagicMock(status_code=200)
        create_response.json.return_value = {"id": "container-1"}
        publish_response = MagicMock(status_code=200)
        publish_response.json.return_value = {"id": "thread-1"}
        post.side_effect = [create_response, publish_response]
        get.return_value = MagicMock(status_code=500)
        result = ThreadsAdapter({"access_token": "token", "user_id": "user"})._execute_live_publish(
            {"content": self.URL}, {"account": "test"}
        )
        self.assertEqual(result["status"], "PUBLISHED")
        self.assertEqual(post.call_args_list[0].kwargs["data"]["text"], self.URL)


if __name__ == "__main__":
    unittest.main()
