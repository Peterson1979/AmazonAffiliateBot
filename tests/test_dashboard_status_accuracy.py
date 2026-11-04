"""Focused tests for dashboard status accuracy and distinction of SKIP_DUPLICATE."""

import unittest
from unittest.mock import Mock, patch

from publishing.queue import PublishingQueue, QueueItem
from web.services import (
    execute_live_publish_item,
    get_platform_publication_result,
    get_queue_item_presentation,
)


class TestDashboardStatusAccuracy(unittest.TestCase):
    def test_distinguishes_published_skipped_failed_unverified(self):
        item = QueueItem(
            queue_item_id="item-status-test",
            original_url="https://example.com",
            asin="B09X5BQ969",
            scheduled_date="2026-11-03",
            daily_slot=1,
            scheduled_at="2026-11-03T13:00:00+01:00",
            status="PARTIALLY_PUBLISHED",
            publishing_results={
                "facebook": {
                    "platform": "facebook",
                    "status": "PUBLISHED",
                    "mode": "LIVE",
                    "action": "SKIP_DUPLICATE",
                    "post_id": "fb-old-123",
                    "permalink_url": "https://facebook.com/post/old",
                },
                "pinterest_lifemodehq": {
                    "platform": "pinterest",
                    "status": "PUBLISHED",
                    "mode": "LIVE",
                    "action": "PUBLISH",
                    "pin_id": "pin-new-456",
                    "pin_url": "https://pinterest.com/pin/new",
                },
                "pinterest": {
                    "platform": "pinterest",
                    "status": "FAILED",
                    "mode": "LIVE",
                    "errors": ["Token missing boards:write"],
                },
                # threads is missing / unverified
            },
        )

        fb_res = get_platform_publication_result(item, "facebook")
        self.assertEqual(fb_res["state"], "SKIPPED")
        self.assertEqual(fb_res["ui_label"], "Previously published / skipped")
        self.assertEqual(fb_res["post_id"], "fb-old-123")

        pin_life_res = get_platform_publication_result(item, "pinterest_lifemodehq")
        self.assertEqual(pin_life_res["state"], "PUBLISHED")
        self.assertEqual(pin_life_res["ui_label"], "Published")
        self.assertEqual(pin_life_res["post_id"], "pin-new-456")

        pin_daily_res = get_platform_publication_result(item, "pinterest")
        self.assertEqual(pin_daily_res["state"], "FAILED")
        self.assertEqual(pin_daily_res["ui_label"], "Failed")

        # In historical report or unverified state
        with patch("web.services.get_historical_destination_report", return_value={"reported_outcome": "UNKNOWN"}):
            threads_res = get_platform_publication_result(item, "threads")
            self.assertEqual(threads_res["state"], "UNKNOWN")
            self.assertEqual(threads_res["ui_label"], "Unverified")

    def test_live_execution_does_not_count_skipped_as_newly_confirmed(self):
        item = QueueItem(
            queue_item_id="item-test",
            original_url="https://example.com",
            asin="B09X5BQ969",
            package_reference="data/generated/asin_B09X5BQ969_content_package.json",
            status="READY",
            publishing_results={
                "facebook": {
                    "status": "PUBLISHED",
                    "mode": "LIVE",
                    "action": "SKIP_DUPLICATE",
                    "post_id": "fb-old",
                }
            },
        )
        queue = PublishingQueue(
            queue_id="queue-test",
            created_at="2026-11-04T00:00:00+00:00",
            start_date="2026-11-04",
            items=[item],
        )

        publisher = Mock()
        publisher.publish_package.return_value = {
            "mode": "LIVE",
            "results": {
                "facebook": {"status": "PUBLISHED", "mode": "LIVE", "action": "SKIP_DUPLICATE", "post_id": "fb-old"},
                "pinterest_lifemodehq": {"status": "PUBLISHED", "mode": "LIVE", "action": "PUBLISH", "pin_id": "pin-new"},
                "pinterest": {"status": "FAILED", "mode": "LIVE", "errors": ["Missing scope"]},
            },
        }

        with patch("web.services.QUEUE_FILE") as queue_file, \
             patch("web.services.PublishingQueue.load", return_value=queue), \
             patch.object(queue, "save"), \
             patch("web.services.SocialPublisher", return_value=publisher):
            queue_file.exists.return_value = True
            result = execute_live_publish_item("item-test", confirmed=True)

        self.assertTrue(result["success"])
        # Only pinterest_lifemodehq was newly confirmed
        self.assertEqual(result["confirmed_platforms"], 1)
        # facebook was skipped
        self.assertEqual(result["skipped_platforms"], 1)
        # pinterest failed
        self.assertEqual(result["failed_platforms"], 1)


if __name__ == "__main__":
    unittest.main()

