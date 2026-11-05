"""Focused tests for explicit recovery of failed product imports."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from publishing.queue import QueueItem, PublishingQueue, STATUS_PUBLISHED
from web import services


class TestFailedProductRecovery(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=Path(__file__).resolve().parent.parent))
        self.queue_file = self.root / "queue.json"
        self.batches_file = self.root / "batches.json"
        self.products_dir = self.root / "products"
        self.generated_dir = self.root / "generated"
        self.products_dir.mkdir()
        self.generated_dir.mkdir()
        self.asin = "B0H4Z5W52B"
        self.url = "https://link.amazon/B01fd1Jv5"
        self.batch_id = "batch_20261020T181235361960_996e7287"
        (self.products_dir / f"asin_{self.asin}.json").write_text(json.dumps({
            "asin": self.asin,
            "title": "Existing product",
            "features": ["Verified feature"],
            "affiliate_url": self.url,
            "original_affiliate_url": self.url,
            "image_url": "https://example.test/image.jpg",
        }), encoding="utf-8")
        self.batches_file.write_text(json.dumps([{
            "batch_id": self.batch_id,
            "entries": [{
                "url": self.url,
                "asin": self.asin,
                "status": "CONTENT_GENERATION_FAILED",
            }],
        }]), encoding="utf-8")
        PublishingQueue(
            queue_id="queue_test",
            created_at="2026-10-20T00:00:00+02:00",
            start_date="2026-10-20",
            items=[QueueItem(
                queue_item_id="item_existing",
                original_url="https://link.amazon/EXISTING1",
                asin="B0EXISTING1",
                product_name="Existing published product",
                status=STATUS_PUBLISHED,
                validation_status="READY_FOR_REVIEW",
            )],
        ).save(self.queue_file)

    def tearDown(self):
        for path in sorted(self.root.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            elif path.is_dir():
                path.rmdir()
        self.root.rmdir()

    def _ready_package(self):
        self.generated_dir.joinpath(f"asin_{self.asin}_content_package.json").write_text(json.dumps({
            "asin": self.asin,
            "status": "READY_FOR_REVIEW",
            "source": {"affiliate_url": self.url},
        }), encoding="utf-8")
        return {"success": True, "is_ready": True, "package_status": "READY_FOR_REVIEW"}

    def test_explicit_recovery_preserves_url_batch_and_unrelated_queue(self):
        with patch.object(services, "QUEUE_FILE", self.queue_file), \
             patch.object(services, "BATCHES_FILE", self.batches_file), \
             patch.object(services, "PRODUCTS_DIR", self.products_dir), \
             patch.object(services, "GENERATED_DIR", self.generated_dir), \
             patch.object(services, "regenerate_content", side_effect=lambda asin, config=None: self._ready_package()) as regenerate:
            result = services.recover_failed_product(self.asin, batch_id=self.batch_id)

        self.assertTrue(result["success"])
        regenerate.assert_called_once()
        queue = PublishingQueue.load(self.queue_file)
        self.assertEqual(len(queue.items), 2)
        existing = next(item for item in queue.items if item.queue_item_id == "item_existing")
        self.assertEqual(existing.status, STATUS_PUBLISHED)
        item = next(item for item in queue.items if item.asin == self.asin)
        self.assertEqual(item.original_url, self.url)
        self.assertEqual(item.batch_id, self.batch_id)
        self.assertEqual(item.asin, self.asin)
        self.assertEqual(item.status, "READY")
        self.assertEqual(json.loads(self.batches_file.read_text(encoding="utf-8"))[0]["batch_id"], self.batch_id)

    def test_recovery_is_idempotent_and_does_not_create_duplicate_queue_item(self):
        with patch.object(services, "QUEUE_FILE", self.queue_file), \
             patch.object(services, "BATCHES_FILE", self.batches_file), \
             patch.object(services, "PRODUCTS_DIR", self.products_dir), \
             patch.object(services, "GENERATED_DIR", self.generated_dir), \
             patch.object(services, "regenerate_content", side_effect=lambda asin, config=None: self._ready_package()):
            first = services.recover_failed_product(self.asin, batch_id=self.batch_id)
            second = services.recover_failed_product(self.asin, batch_id=self.batch_id)

        self.assertTrue(first["success"])
        self.assertFalse(second["success"])
        queue = PublishingQueue.load(self.queue_file)
        self.assertEqual(len(queue.items), 2)


if __name__ == "__main__":
    unittest.main()
