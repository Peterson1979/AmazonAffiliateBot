"""Regression tests for the explicitly confirmed dashboard live action."""

from unittest import TestCase
from unittest.mock import Mock, patch

from publishing.queue import QueueItem, PublishingQueue
from web.services import execute_live_publish_item


class TestLiveDashboardAction(TestCase):
    def _queue(self, prior_results=None):
        item = QueueItem(
            "item-live-test",
            "https://www.amazon.com/dp/B0GKGBWRTR",
            asin="B0GKGBWRTR",
            package_reference="data/generated/asin_B0GKGBWRTR_content_package.json",
            status="READY",
            validation_status="READY_FOR_REVIEW",
            publishing_results=prior_results,
        )
        return PublishingQueue(
            "queue-test",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01",
            items=[item],
        )

    def test_confirmation_is_required_before_loading_or_publishing(self):
        with patch("web.services.QUEUE_FILE") as queue_file:
            result = execute_live_publish_item("item-live-test", confirmed=False)
        self.assertFalse(result["success"])
        self.assertTrue(result["requires_confirmation"])
        queue_file.exists.assert_not_called()

    def test_explicit_live_action_persists_independent_platform_results(self):
        queue = self._queue()
        publisher = Mock()
        publisher.publish_package.return_value = {
            "mode": "LIVE",
            "results": {
                "facebook": {"status": "PUBLISHED", "mode": "LIVE", "post_id": "fb-1"},
                "pinterest": {"status": "FAILED", "mode": "LIVE", "errors": ["board denied"]},
                "threads": {"status": "PUBLISHED", "mode": "LIVE", "post_id": "threads-1"},
            },
        }
        with patch("web.services.QUEUE_FILE") as queue_file, \
             patch("web.services.PublishingQueue.load", return_value=queue), \
             patch.object(queue, "save"), \
             patch("web.services.SocialPublisher", return_value=publisher):
            queue_file.exists.return_value = True
            result = execute_live_publish_item("item-live-test", confirmed=True)

        self.assertTrue(result["success"])
        self.assertEqual(result["status"], "PARTIALLY_PUBLISHED")
        self.assertEqual(result["confirmed_platforms"], 2)
        self.assertEqual(queue.items[0].publishing_results["facebook"]["post_id"], "fb-1")
        self.assertIn("board denied", queue.items[0].errors)
        publisher.publish_package.assert_called_once()

    def test_prior_confirmed_platform_is_passed_to_existing_retry_safe_publisher(self):
        prior = {
            "facebook": {"status": "PUBLISHED", "mode": "LIVE", "post_id": "fb-existing"}
        }
        queue = self._queue(prior_results=prior)
        publisher = Mock()
        publisher.publish_package.return_value = {
            "mode": "LIVE",
            "results": {
                "facebook": {"status": "FAILED", "mode": "LIVE", "errors": ["should not replace prior success"]},
                "pinterest": {"status": "FAILED", "mode": "LIVE", "errors": ["board denied"]},
                "threads": {"status": "PUBLISHED", "mode": "LIVE", "post_id": "threads-1"},
            },
        }
        with patch("web.services.QUEUE_FILE") as queue_file, \
             patch("web.services.PublishingQueue.load", return_value=queue), \
             patch.object(queue, "save"), \
             patch("web.services.SocialPublisher", return_value=publisher):
            queue_file.exists.return_value = True
            result = execute_live_publish_item("item-live-test", confirmed=True)

        self.assertEqual(result["status"], "PARTIALLY_PUBLISHED")
        call = publisher.publish_package.call_args.kwargs
        self.assertEqual(call["prior_results"]["facebook"]["post_id"], "fb-existing")
        self.assertEqual(queue.items[0].publishing_results["facebook"]["action"], "SKIP_DUPLICATE")
        self.assertEqual(queue.items[0].publishing_results["facebook"]["post_id"], "fb-existing")
