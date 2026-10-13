"""Offline regression tests for queue image recovery and publication safety."""

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
from content.package import STATUS_READY_FOR_REVIEW
from publishing.queue import (
    PublishingQueue,
    QueueItem,
    STATUS_FAILED,
    STATUS_PARTIALLY_PUBLISHED,
    STATUS_READY,
    recover_pending_queue_images,
)


class TestQueueImageRecovery(unittest.TestCase):
    def _write_product(self, root, asin, image_url=None):
        path = root / "products" / f"asin_{asin}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(Product(
            title="Recovered product",
            features=["Grounded feature"],
            affiliate_url=f"https://link.amazon/{asin}",
            asin=asin,
            image_url=image_url,
        ).to_dict()), encoding="utf-8")
        return path

    def _write_package(self, root, asin, image_url=None):
        path = root / "generated" / f"asin_{asin}_content_package.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        platforms = {
            "facebook": {"status": "VALID", "content": "Facebook"},
            "pinterest": {"status": "VALID", "title": "Pin", "description": "Description"},
            "threads": {"status": "VALID", "content": "Threads"},
        }
        if image_url:
            for entry in platforms.values():
                entry["image_url"] = image_url
        path.write_text(json.dumps({
            "asin": asin, "status": STATUS_READY_FOR_REVIEW, "platforms": platforms,
        }), encoding="utf-8")
        return path

    def test_recovery_persists_image_and_requeues_unpublished_failure(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / ".test-tmp") as temp_dir:
            root = Path(temp_dir)
            asin = "B0RECOVER01"
            self._write_product(root, asin)
            self._write_package(root, asin, "https://m.media-amazon.com/images/I/recovered.jpg")
            item = QueueItem(
                queue_item_id=f"item_001_{asin}", asin=asin,
                original_url=f"https://link.amazon/{asin}", product_name="Recovered product",
                status=STATUS_FAILED, validation_status="READY_FOR_REVIEW",
                errors=["Skipped because item image preflight was blocked."],
            )
            queue = PublishingQueue(queue_id="test", created_at="2026-01-01T00:00:00Z", start_date="2026-01-01", items=[item])

            report = recover_pending_queue_images(queue, root / "products", root / "generated")

            self.assertEqual(report["recovered"], [asin])
            self.assertEqual(item.status, STATUS_READY)
            self.assertEqual(json.loads((root / "products" / f"asin_{asin}.json").read_text())["image_url"], "https://m.media-amazon.com/images/I/recovered.jpg")

    def test_recovery_does_not_requeue_partially_published_item(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / ".test-tmp") as temp_dir:
            root = Path(temp_dir)
            asin = "B0PUBLISHED1"
            image_url = "https://m.media-amazon.com/images/I/already.jpg"
            self._write_product(root, asin)
            self._write_package(root, asin, image_url)
            item = QueueItem(
                queue_item_id=f"item_001_{asin}", asin=asin,
                original_url=f"https://link.amazon/{asin}", product_name="Published product",
                status=STATUS_PARTIALLY_PUBLISHED, validation_status="READY_FOR_REVIEW",
                publishing_results={"facebook": {"status": "PUBLISHED"}},
            )
            queue = PublishingQueue(queue_id="test", created_at="2026-01-01T00:00:00Z", start_date="2026-01-01", items=[item])

            report = recover_pending_queue_images(queue, root / "products", root / "generated")

            self.assertEqual(report["already_published"], [asin])
            self.assertEqual(item.status, STATUS_PARTIALLY_PUBLISHED)
            self.assertNotIn(asin, report["recovered"])

    def test_missing_image_remains_unresolved_and_fail_closed(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / ".test-tmp") as temp_dir:
            root = Path(temp_dir)
            asin = "B0NOIMAGE01"
            self._write_product(root, asin)
            self._write_package(root, asin)
            item = QueueItem(
                queue_item_id=f"item_001_{asin}", asin=asin,
                original_url=f"https://link.amazon/{asin}", product_name="No image product",
                status=STATUS_READY, validation_status="READY_FOR_REVIEW",
            )
            queue = PublishingQueue(queue_id="test", created_at="2026-01-01T00:00:00Z", start_date="2026-01-01", items=[item])

            with patch("publishing.queue.resolve_product_for_url", return_value=(False, None, None, ["No verified image"])):
                report = recover_pending_queue_images(queue, root / "products", root / "generated", allow_scrape=True)

            self.assertEqual(report["recovered"], [])
            self.assertEqual(item.status, STATUS_READY)
            self.assertEqual(report["unresolved"][0]["asin"], asin)

    def test_intentional_skip_is_not_requeued(self):
        with tempfile.TemporaryDirectory(dir=PROJECT_ROOT / ".test-tmp") as temp_dir:
            root = Path(temp_dir)
            asin = "B0INTENT01"
            image_url = "https://m.media-amazon.com/images/I/intentional.jpg"
            self._write_product(root, asin, image_url)
            self._write_package(root, asin, image_url)
            item = QueueItem(
                queue_item_id=f"item_001_{asin}", asin=asin,
                original_url=f"https://link.amazon/{asin}", product_name="Intentional skip",
                status="SKIPPED", validation_status="READY_FOR_REVIEW",
            )
            queue = PublishingQueue(queue_id="test", created_at="2026-01-01T00:00:00Z", start_date="2026-01-01", items=[item])

            report = recover_pending_queue_images(queue, root / "products", root / "generated")

            self.assertEqual(report["intentionally_skipped"], [asin])
            self.assertEqual(item.status, "SKIPPED")
            self.assertNotIn(asin, report["recovered"])


if __name__ == "__main__":
    unittest.main()
