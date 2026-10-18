"""Focused regressions for evidence-backed dashboard and queue behavior."""

from unittest import TestCase
from unittest.mock import patch
from pathlib import Path

from publishing.queue import QueueItem, PublishingQueue
from web.services import get_platform_publication_result, get_queue_item_presentation, add_products_from_urls
from publishing.reconciliation import get_historical_destination_report


class TestTruthfulDashboardStates(TestCase):
    def test_live_success_requires_platform_evidence(self):
        item = QueueItem("item-1", "https://amazon.test/dp/B000000001", asin="B000000001")
        item.publishing_results = {
            "facebook": {"status": "PUBLISHED", "mode": "LIVE"},
            "threads": {"status": "PUBLISHED", "mode": "LIVE", "post_id": "threads-1"},
        }
        self.assertEqual(get_platform_publication_result(item, "facebook")["state"], "UNKNOWN")
        self.assertEqual(get_platform_publication_result(item, "threads")["state"], "PUBLISHED")

    def test_pinterest_accounts_are_independent(self):
        item = QueueItem("item-1", "url", asin="B000000001")
        item.publishing_results = {
            "pinterest": {"status": "PUBLISHED", "mode": "LIVE", "pin_id": "pin-1"},
            "pinterest_lifemodehq": {"status": "FAILED", "mode": "LIVE", "errors": ["board denied"]},
        }
        self.assertEqual(get_platform_publication_result(item, "pinterest")["state"], "PUBLISHED")
        result = get_platform_publication_result(item, "pinterest_lifemodehq")
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

    def test_legacy_published_dry_run_record_is_unknown_not_simulated(self):
        item = QueueItem(
            "item-legacy",
            "url",
            asin="B0DHKSF81K",
            status="PUBLISHED",
            published_at="2026-10-17T12:00:05+02:00",
        )
        item.publishing_results = {
            "facebook": {"status": "WOULD_PUBLISH", "mode": "DRY_RUN"},
            "pinterest": {"status": "WOULD_PUBLISH", "mode": "DRY_RUN"},
            "threads": {"status": "WOULD_PUBLISH", "mode": "DRY_RUN"},
        }
        result = get_platform_publication_result(item, "facebook")
        self.assertEqual(result["state"], "UNKNOWN")
        self.assertIn("no live platform ID", result["evidence_note"])

    def test_verified_live_record_remains_published(self):
        item = QueueItem("item-live", "url", asin="B000000001", status="PUBLISHED")
        item.publishing_results = {
            "facebook": {"status": "PUBLISHED", "mode": "LIVE", "post_id": "fb-1"},
        }
        self.assertEqual(get_platform_publication_result(item, "facebook")["state"], "PUBLISHED")

    def test_simulated_result_is_not_published_and_all_destinations_are_visible(self):
        item = QueueItem("item-sim", "url", asin="B000000001", status="READY")
        item.publishing_results = {
            key: {"status": "WOULD_PUBLISH", "mode": "DRY_RUN"}
            for key in ("facebook", "pinterest", "pinterest_lifemodehq", "threads")
        }
        queue = PublishingQueue("q", "2026-01-01T00:00:00+00:00", "2026-01-01", items=[item])
        presentation = get_queue_item_presentation(queue, item)
        self.assertEqual(presentation["state"], "SIMULATED")
        self.assertEqual(set(presentation["platforms"]), {"facebook", "pinterest", "pinterest_lifemodehq", "threads"})
        self.assertNotEqual(presentation["state"], "PUBLISHED")

    def test_b0d_historical_report_distinguishes_user_report_from_dry_run(self):
        facebook = get_historical_destination_report("B0DTHHK2S1", "facebook")
        pinterest = get_historical_destination_report("B0DTHHK2S1", "pinterest")
        self.assertEqual(facebook["state"], "UNKNOWN")
        self.assertEqual(facebook["reported_outcome"], "not_visible")
        self.assertEqual(pinterest["state"], "UNKNOWN")
        self.assertEqual(pinterest["reported_outcome"], "published")
        self.assertEqual(pinterest["evidence_status"], "user_reported")

    def test_missing_required_destination_downgrades_overall_state(self):
        item = QueueItem("item-x", "url", asin="B000000001", status="PARTIALLY_PUBLISHED")
        item.publishing_results = {
            "x": {"status": "FAILED", "mode": "LIVE", "errors": ["legacy excluded target"]},
            "facebook": {"status": "PUBLISHED", "mode": "LIVE", "post_id": "fb-1"},
            "pinterest": {"status": "PUBLISHED", "mode": "LIVE", "pin_id": "pin-1"},
            "threads": {"status": "PUBLISHED", "mode": "LIVE", "post_id": "threads-1"},
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
