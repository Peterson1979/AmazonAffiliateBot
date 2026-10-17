"""Focused regressions for evidence-backed dashboard and queue behavior."""

from unittest import TestCase
from unittest.mock import patch
from pathlib import Path

from publishing.queue import QueueItem, PublishingQueue
from web.services import get_platform_publication_result, get_queue_item_presentation, add_products_from_urls


class TestTruthfulDashboardStates(TestCase):
    def test_live_success_requires_platform_evidence(self):
        item = QueueItem("item-1", "https://amazon.test/dp/B000000001", asin="B000000001")
        item.publishing_results = {
            "facebook": {"status": "PUBLISHED", "mode": "LIVE"},
            "threads": {"status": "PUBLISHED", "mode": "LIVE", "post_id": "threads-1"},
        }
        self.assertEqual(get_platform_publication_result(item, "facebook")["state"], "UNKNOWN")
        self.assertEqual(get_platform_publication_result(item, "threads")["state"], "PUBLISHED")

    def test_pinterest_aggregates_accounts_without_hiding_failure(self):
        item = QueueItem("item-1", "url", asin="B000000001")
        item.publishing_results = {
            "pinterest": {"status": "PUBLISHED", "mode": "LIVE", "pin_id": "pin-1"},
            "pinterest_lifemodehq": {"status": "FAILED", "mode": "LIVE", "errors": ["board denied"]},
        }
        result = get_platform_publication_result(item, "pinterest")
        self.assertEqual(result["state"], "FAILED")
        self.assertIn("board denied", result["errors"])

    def test_ready_item_with_persisted_results_uses_results_as_truth(self):
        item = QueueItem("item-1", "url", asin="B000000001", status="READY")
        item.publishing_results = {
            "facebook": {"status": "PUBLISHED", "mode": "LIVE", "post_id": "fb-1"},
            "pinterest": {"status": "FAILED", "mode": "LIVE", "errors": ["HTTP 400"]},
        }
        queue = PublishingQueue("q", "2026-01-01T00:00:00+00:00", "2026-01-01", items=[item])
        self.assertEqual(get_queue_item_presentation(queue, item)["state"], "PARTIAL")

    def test_duplicate_asin_submission_is_rejected_without_processing(self):
        queue = PublishingQueue(
            "q", "2026-01-01T00:00:00+00:00", "2026-01-01",
            items=[QueueItem("item-1", "old-url", asin="B08CRPWQHB")],
        )
        with patch("web.services.QUEUE_FILE", Path("data/queue/publishing_queue.json")), \
             patch("web.services.PublishingQueue.load", return_value=queue), \
             patch.object(queue, "save"), \
             patch("web.services.resolve_product_for_url") as resolver:
            result = add_products_from_urls("https://www.amazon.com/dp/B08CRPWQHB?tag=test-20")
            self.assertEqual(result["accepted"], 0)
            self.assertEqual(result["results"][0]["status"], "DUPLICATE")
            resolver.assert_not_called()
