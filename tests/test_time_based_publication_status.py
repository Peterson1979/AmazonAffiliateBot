"""
tests/test_time_based_publication_status.py - Focused tests for time-based publication status rules.

Validates:
1. Future scheduled publication -> 'Scheduled — <date>, <time>'
2. Scheduled time that has passed without confirmed publication -> 'Not published'
3. Confirmed successful publication -> 'Published'
4. Boundary condition around exact scheduled time
5. Consistency across Products page, dashboard counters, filters, and detail view
6. Live inspection of actual November 4 and November 5 queue items
"""

import unittest
from datetime import datetime
from unittest.mock import patch

from publishing.queue import (
    PublishingQueue,
    QueueItem,
    STATUS_READY,
    STATUS_PUBLISHED,
    get_budapest_timezone,
)
from web.services import (
    format_scheduled_datetime,
    parse_product_scheduled_dt,
    resolve_product_publication_status,
    get_platform_publication_result,
    get_queue_item_presentation,
    get_all_products,
    get_dashboard_summary,
    get_product_detail,
)
from web.app import create_app


class TestTimeBasedPublicationStatus(unittest.TestCase):
    def setUp(self):
        self.tz = get_budapest_timezone()
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-status-rules"})
        self.client = self.app.test_client()

    def test_future_scheduled_publication_displays_exact_date_and_time(self):
        """A future scheduled publication -> 'Scheduled', with exact date and time displayed."""
        item = QueueItem(
            "item-fut",
            "https://amazon.com/dp/B0BZYCJK89",
            asin="B0BZYCJK89",
            status=STATUS_READY,
            scheduled_date="2026-11-04",
            daily_slot=1,
            scheduled_at="2026-11-04T15:00:00+01:00",
        )
        # Even with dry-run/simulated results present from past testing:
        item.publishing_results = {
            "facebook": {"status": "WOULD_PUBLISH", "mode": "DRY_RUN"},
            "pinterest": {"status": "WOULD_PUBLISH", "mode": "DRY_RUN"},
            "pinterest_lifemodehq": {"status": "WOULD_PUBLISH", "mode": "DRY_RUN"},
            "threads": {"status": "WOULD_PUBLISH", "mode": "DRY_RUN"},
        }
        queue = PublishingQueue("q", "2026-11-01T00:00:00+01:00", "2026-11-01", items=[item])

        current_dt = datetime(2026, 11, 2, 15, 30, 0, tzinfo=self.tz)
        pres = get_queue_item_presentation(queue, item, current_dt=current_dt)

        self.assertEqual(pres["state"], "SCHEDULED")
        self.assertEqual(pres["ui_label"], "Scheduled \u2014 Nov 4, 2026, 15:00")
        for plat, res in pres["platforms"].items():
            self.assertEqual(res["state"], "SCHEDULED")
            self.assertEqual(res["ui_label"], "Scheduled")

    def test_scheduled_time_passed_without_confirmed_publication_is_not_published(self):
        """A scheduled time that has passed without confirmed publication -> 'Not published'."""
        item = QueueItem(
            "item-passed",
            "https://amazon.com/dp/B00PAST001",
            asin="B00PAST001",
            status=STATUS_READY,
            scheduled_date="2026-10-28",
            daily_slot=1,
            scheduled_at="2026-10-28T13:00:00+01:00",
        )
        queue = PublishingQueue("q", "2026-10-26T00:00:00+01:00", "2026-10-26", items=[item])

        current_dt = datetime(2026, 11, 2, 15, 30, 0, tzinfo=self.tz)
        pres = get_queue_item_presentation(queue, item, current_dt=current_dt)

        self.assertIn(pres["state"], ("SIMULATED", "NOT_PUBLISHED"))
        self.assertEqual(pres["ui_label"], "Not published")

    def test_confirmed_successful_publication_is_published(self):
        """A confirmed successful publication -> 'Published'. Scheduled time alone never implies publication."""
        item = QueueItem(
            "item-live",
            "https://amazon.com/dp/B00LIVE001",
            asin="B00LIVE001",
            status=STATUS_PUBLISHED,
            scheduled_date="2026-11-01",
            daily_slot=1,
            scheduled_at="2026-11-01T13:00:00+01:00",
        )
        item.publishing_results = {
            "facebook": {"status": "PUBLISHED", "mode": "LIVE", "post_id": "fb_999"},
            "pinterest": {"status": "PUBLISHED", "mode": "LIVE", "pin_id": "pin_999"},
            "pinterest_lifemodehq": {"status": "PUBLISHED", "mode": "LIVE", "pin_id": "pin_888"},
            "threads": {"status": "PUBLISHED", "mode": "LIVE", "post_id": "th_999"},
        }
        queue = PublishingQueue("q", "2026-11-01T00:00:00+01:00", "2026-11-01", items=[item])

        current_dt = datetime(2026, 11, 2, 15, 30, 0, tzinfo=self.tz)
        pres = get_queue_item_presentation(queue, item, current_dt=current_dt)

        self.assertEqual(pres["state"], "PUBLISHED")
        self.assertEqual(pres["ui_label"], "Published")

    def test_scheduled_time_boundary_behavior(self):
        """Correct behavior around the scheduled-time boundary."""
        item = QueueItem(
            "item-boundary",
            "https://amazon.com/dp/B0BOUNDARY1",
            asin="B0BOUNDARY1",
            status=STATUS_READY,
            scheduled_date="2026-11-04",
            daily_slot=1,
            scheduled_at="2026-11-04T15:00:00+01:00",
        )
        queue = PublishingQueue("q", "2026-11-01T00:00:00+01:00", "2026-11-01", items=[item])

        # 1 second before scheduled time -> still future -> Scheduled
        before_dt = datetime(2026, 11, 4, 14, 59, 59, tzinfo=self.tz)
        pres_before = get_queue_item_presentation(queue, item, current_dt=before_dt)
        self.assertEqual(pres_before["state"], "SCHEDULED")
        self.assertEqual(pres_before["ui_label"], "Scheduled \u2014 Nov 4, 2026, 15:00")

        # Exact scheduled time -> time has arrived/passed -> Not published
        exact_dt = datetime(2026, 11, 4, 15, 0, 0, tzinfo=self.tz)
        pres_exact = get_queue_item_presentation(queue, item, current_dt=exact_dt)
        self.assertIn(pres_exact["state"], ("SIMULATED", "NOT_PUBLISHED"))
        self.assertEqual(pres_exact["ui_label"], "Not published")

        # 1 second after scheduled time -> time has passed -> Not published
        after_dt = datetime(2026, 11, 4, 15, 0, 1, tzinfo=self.tz)
        pres_after = get_queue_item_presentation(queue, item, current_dt=after_dt)
        self.assertIn(pres_after["state"], ("SIMULATED", "NOT_PUBLISHED"))
        self.assertEqual(pres_after["ui_label"], "Not published")

    @patch("web.services.datetime")
    def test_consistency_across_counters_filters_and_products_listing(self, mock_dt):
        """Verify consistency across dashboard counters, filters, and products page."""
        fixed_now = datetime(2026, 11, 2, 12, 0, 0, tzinfo=self.tz)
        mock_dt.now.return_value = fixed_now
        mock_dt.combine = datetime.combine
        mock_dt.strptime = datetime.strptime
        mock_dt.fromisoformat = datetime.fromisoformat
        mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        products = get_all_products()
        asins = {p["asin"]: p for p in products}

        # Check November 4 items:
        # B0BZYCJK89 (Nov 4, 13:00)
        self.assertIn("B0BZYCJK89", asins)
        self.assertEqual(asins["B0BZYCJK89"]["publishing_status"], "SCHEDULED")
        self.assertEqual(asins["B0BZYCJK89"]["publishing_status_display"], "Scheduled \u2014 Nov 4, 2026, 15:00")

        # B07H9PZDQW (Nov 4, 19:00)
        self.assertIn("B07H9PZDQW", asins)
        self.assertEqual(asins["B07H9PZDQW"]["publishing_status"], "SCHEDULED")
        self.assertEqual(asins["B07H9PZDQW"]["publishing_status_display"], "Scheduled \u2014 Nov 4, 2026, 21:00")

        # Check November 5 items:
        # B0FL6Q8NFQ (Nov 5, 13:00)
        self.assertIn("B0FL6Q8NFQ", asins)
        self.assertEqual(asins["B0FL6Q8NFQ"]["publishing_status"], "SCHEDULED")
        self.assertEqual(asins["B0FL6Q8NFQ"]["publishing_status_display"], "Scheduled \u2014 Nov 5, 2026, 15:00")

        # B07PBXXNCY (Nov 5, 19:00)
        self.assertIn("B07PBXXNCY", asins)
        self.assertEqual(asins["B07PBXXNCY"]["publishing_status"], "SCHEDULED")
        self.assertEqual(asins["B07PBXXNCY"]["publishing_status_display"], "Scheduled \u2014 Nov 5, 2026, 21:00")

        # Check filtering on get_all_products:
        scheduled_filtered = get_all_products(status_filter="scheduled")
        scheduled_asins = [p["asin"] for p in scheduled_filtered]
        self.assertIn("B0BZYCJK89", scheduled_asins)
        self.assertIn("B07H9PZDQW", scheduled_asins)
        self.assertIn("B0FL6Q8NFQ", scheduled_asins)
        self.assertIn("B07PBXXNCY", scheduled_asins)

        # Check HTTP response on /products:
        resp = self.client.get("/products")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Scheduled \u2014 Nov 4, 2026, 15:00", html)
        self.assertIn("Scheduled \u2014 Nov 4, 2026, 21:00", html)
        self.assertIn("Scheduled \u2014 Nov 5, 2026, 15:00", html)
        self.assertIn("Scheduled \u2014 Nov 5, 2026, 21:00", html)

        # Check Dashboard summary counters:
        summary = get_dashboard_summary()
        self.assertEqual(summary["queue"]["future_scheduled"], 6)
        self.assertEqual(summary["queue"]["scheduled"], 6)
        self.assertEqual(summary["queue"]["published"], 0)
        self.assertEqual(summary["queue"]["simulated"], 0)


if __name__ == "__main__":
    unittest.main()

