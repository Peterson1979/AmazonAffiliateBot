"""Focused tests for persistent dashboard import batches."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from amazon.scraper import Product
from publishing.queue import STATUS_PARTIALLY_PUBLISHED, STATUS_PUBLISHED, PublishingQueue
from web import services


class TestBatchManagement(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=Path(__file__).resolve().parent.parent))
        self.queue_file = self.root / "queue.json"
        self.batches_file = self.root / "batches.json"
        self.products_dir = self.root / "products"
        self.generated_dir = self.root / "generated"
        self.urls = []

    def tearDown(self):
        for path in sorted(self.root.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        self.root.rmdir()

    def _product_for(self, url):
        asin = url.rsplit("/", 1)[-1].split("?", 1)[0]
        return Product(
            title=f"Product {asin}",
            features=["Verified feature"],
            affiliate_url=url,
            original_affiliate_url=url,
            asin=asin,
            image_url="https://m.media-amazon.com/images/I/test.jpg",
        )

    def _generate(self, product, disclosure):
        url = product.affiliate_url
        return {"platforms": {
            "facebook": {"content": f"Facebook {url}"},
            "threads": {"content": f"Threads {url}"},
            "pinterest": {"title": product.title, "description": f"Pinterest {url}"},
        }}

    def _import(self, urls):
        with patch.object(services, "QUEUE_FILE", self.queue_file), \
             patch.object(services, "BATCHES_FILE", self.batches_file), \
             patch.object(services, "PRODUCTS_DIR", self.products_dir), \
             patch.object(services, "GENERATED_DIR", self.generated_dir), \
             patch("web.services.resolve_product_for_url") as resolve, \
             patch("web.services.ContentGenerator.generate_all_platforms", side_effect=self._generate):
            def resolve_url(url, **kwargs):
                if "not-a-product" in url:
                    return False, None, None, ["Invalid product URL"]
                return True, self._product_for(url), f"asin_{url.rsplit('/', 1)[-1].split('?', 1)[0]}", []
            resolve.side_effect = resolve_url
            return services.add_products_from_urls("\n".join(urls), config={"affiliate": {"disclosure": "Ad"}})

    def test_latest_batch_switches_without_deleting_previous_batch(self):
        first_urls = [f"https://amazon.test/dp/B0A{i:07d}" for i in range(16)]
        second_urls = [f"https://amazon.test/dp/B0B{i:07d}" for i in range(32)]
        first = self._import(first_urls)
        second = self._import(second_urls)

        batches = services.load_import_batches(self.batches_file)
        self.assertEqual(len(batches), 2)
        self.assertNotEqual(batches[0]["batch_id"], batches[1]["batch_id"])
        self.assertEqual(batches[0]["submitted_count"], 16)
        self.assertEqual(batches[1]["submitted_count"], 32)
        self.assertEqual(services.get_import_batch(path=self.batches_file)["batch_id"], second["batch_id"])
        queue = PublishingQueue.load(self.queue_file)
        self.assertEqual(len(queue.items), 48)
        self.assertEqual(sum(item.batch_id == first["batch_id"] for item in queue.items), 16)
        self.assertEqual(sum(item.batch_id == second["batch_id"] for item in queue.items), 32)

    def test_same_date_batches_and_outcomes_are_retained(self):
        valid = "https://amazon.test/dp/B0C0000001?tag=exact-20&x=1"
        result = self._import([valid, valid, "https://amazon.test/not-a-product"])
        batches = services.load_import_batches(self.batches_file)
        batch = batches[-1]
        self.assertEqual(batch["submitted_count"], 3)
        self.assertEqual(batch["accepted_count"], 1)
        self.assertEqual(batch["duplicate_count"], 1)
        self.assertEqual(batch["failed_count"], 1)
        self.assertEqual(batch["entries"][0]["url"], valid)
        self.assertEqual(batch["entries"][1]["status"], "DUPLICATE")
        self.assertEqual(batch["entries"][2]["status"], "REJECTED")
        self.assertEqual(batch["entries"][0]["queue_item_id"], result["results"][0]["queue_item_id"])

    def test_status_changes_do_not_change_batch_membership(self):
        urls = [f"https://amazon.test/dp/B0D{i:07d}" for i in range(3)]
        result = self._import(urls)
        queue = PublishingQueue.load(self.queue_file)
        queue.items[0].status = STATUS_PUBLISHED
        queue.items[1].status = STATUS_PARTIALLY_PUBLISHED
        queue.save(self.queue_file)
        view = services.build_import_batch_view(services.get_import_batch(result["batch_id"], self.batches_file), PublishingQueue.load(self.queue_file))
        self.assertEqual(len(view["items"]), 3)
        self.assertEqual({item.batch_id for item in view["items"]}, {result["batch_id"]})
        self.assertEqual({item.status for item in view["items"]}, {"READY", STATUS_PUBLISHED, STATUS_PARTIALLY_PUBLISHED})


if __name__ == "__main__":
    unittest.main()
