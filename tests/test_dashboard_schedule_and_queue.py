"""
Tests for AmazonAffiliateBot Dashboard Schedule and Queue Visibility.

Covers:
1. Complete queue visibility (all IMAGE and VIDEO queue items displayed).
2. Media type detection (individual queue item metadata/slot, no package override).
3. Europe/Budapest production publishing windows (15:00, 18:00, 21:00, 23:00).
4. Platform routing and window attribution (Slot 1 -> Windows 1 & 2; Slot 2 -> Windows 3 & 4).
5. Accurate platform publication status reporting.
"""

import unittest
from datetime import datetime
from unittest.mock import patch

from publishing.queue import (
    PublishingQueue,
    QueueItem,
    PUBLISHING_WINDOWS,
    ALL_PLATFORM_DESTINATIONS,
)
from web.app import create_app
from web.services import (
    get_budapest_timezone,
    get_platform_scheduled_dt,
    get_windows_for_item,
    get_queue_item_presentation,
    _resolve_queue_item_media,
    get_all_products,
    get_product_detail,
)


def _make_item(
    queue_item_id: str,
    asin: str = "B0TEST1234",
    scheduled_date: str = "2026-11-07",
    daily_slot: int = 1,
    scheduled_at: str = "2026-11-07T15:00:00+01:00",
    media_type: str = "image",
    original_url: str = "https://amazon.com/dp/B0TEST1234",
    video_url: str = None,
    status: str = "READY",
    publishing_results: dict = None,
) -> QueueItem:
    return QueueItem(
        queue_item_id=queue_item_id,
        asin=asin,
        scheduled_date=scheduled_date,
        daily_slot=daily_slot,
        scheduled_at=scheduled_at,
        original_url=original_url,
        timezone="Europe/Budapest",
        media_type=media_type,
        video_url=video_url,
        status=status,
        publishing_results=publishing_results or {},
    )


def _make_queue(items=None) -> PublishingQueue:
    return PublishingQueue(
        queue_id="test_queue",
        created_at="2026-11-07T00:00:00+01:00",
        start_date="2026-11-07",
        items=items or [],
    )


class TestDashboardScheduleAndQueue(unittest.TestCase):
    def setUp(self):
        self.tz = get_budapest_timezone()
        self.app = create_app({"TESTING": True})
        self.client = self.app.test_client()

    def test_publishing_windows_definition(self):
        """Verify the 4 authoritative production publishing windows."""
        self.assertEqual(len(PUBLISHING_WINDOWS), 4)
        self.assertEqual(list(PUBLISHING_WINDOWS.keys()), [1, 2, 3, 4])

        # Window 1: Slot 1 Main (15:00)
        w1 = PUBLISHING_WINDOWS[1]
        self.assertEqual(w1["start_hour"], 15)
        self.assertEqual(w1["daily_slot"], 1)
        self.assertEqual(w1["media_type"], "image")
        self.assertIn("facebook", w1["target_destinations"])
        self.assertIn("threads", w1["target_destinations"])
        self.assertIn("pinterest", w1["target_destinations"])
        self.assertNotIn("pinterest_lifemodehq", w1["target_destinations"])

        # Window 2: Slot 1 LifeModeHQ (18:00)
        w2 = PUBLISHING_WINDOWS[2]
        self.assertEqual(w2["start_hour"], 18)
        self.assertEqual(w2["daily_slot"], 1)
        self.assertEqual(w2["media_type"], "image")
        self.assertEqual(w2["target_destinations"], ["pinterest_lifemodehq"])

        # Window 3: Slot 2 Main (21:00)
        w3 = PUBLISHING_WINDOWS[3]
        self.assertEqual(w3["start_hour"], 21)
        self.assertEqual(w3["daily_slot"], 2)
        self.assertEqual(w3["media_type"], "video")
        self.assertIn("facebook", w3["target_destinations"])
        self.assertIn("threads", w3["target_destinations"])
        self.assertIn("pinterest", w3["target_destinations"])
        self.assertNotIn("pinterest_lifemodehq", w3["target_destinations"])

        # Window 4: Slot 2 LifeModeHQ (23:00)
        w4 = PUBLISHING_WINDOWS[4]
        self.assertEqual(w4["start_hour"], 23)
        self.assertEqual(w4["daily_slot"], 2)
        self.assertEqual(w4["media_type"], "video")
        self.assertEqual(w4["target_destinations"], ["pinterest_lifemodehq"])

    def test_get_platform_scheduled_dt_slot1(self):
        """Slot 1 platforms must resolve to 15:00 for Main and 18:00 for LifeModeHQ."""
        item = _make_item("item_test_slot1", daily_slot=1, scheduled_at="2026-11-07T15:00:00+01:00")
        fb_dt = get_platform_scheduled_dt(item, "facebook", tz=self.tz)
        threads_dt = get_platform_scheduled_dt(item, "threads", tz=self.tz)
        pin_dt = get_platform_scheduled_dt(item, "pinterest", tz=self.tz)
        pin_lm_dt = get_platform_scheduled_dt(item, "pinterest_lifemodehq", tz=self.tz)

        self.assertEqual(fb_dt.hour, 15)
        self.assertEqual(fb_dt.minute, 0)
        self.assertEqual(threads_dt.hour, 15)
        self.assertEqual(pin_dt.hour, 15)
        # Pinterest LifeModeHQ runs in Window 2 at 18:00 Budapest
        self.assertEqual(pin_lm_dt.hour, 18)
        self.assertEqual(pin_lm_dt.minute, 0)

    def test_get_platform_scheduled_dt_slot2(self):
        """Slot 2 platforms must resolve to 21:00 for Main and 23:00 for LifeModeHQ."""
        item = _make_item("item_test_slot2", daily_slot=2, scheduled_at="2026-11-07T21:00:00+01:00", media_type="video")
        fb_dt = get_platform_scheduled_dt(item, "facebook", tz=self.tz)
        threads_dt = get_platform_scheduled_dt(item, "threads", tz=self.tz)
        pin_dt = get_platform_scheduled_dt(item, "pinterest", tz=self.tz)
        pin_lm_dt = get_platform_scheduled_dt(item, "pinterest_lifemodehq", tz=self.tz)

        self.assertEqual(fb_dt.hour, 21)
        self.assertEqual(fb_dt.minute, 0)
        self.assertEqual(threads_dt.hour, 21)
        self.assertEqual(pin_dt.hour, 21)
        # Pinterest LifeModeHQ runs in Window 4 at 23:00 Budapest
        self.assertEqual(pin_lm_dt.hour, 23)
        self.assertEqual(pin_lm_dt.minute, 0)

    def test_get_windows_for_item_structure(self):
        """get_windows_for_item must return applicable windows with platform statuses."""
        item = _make_item("item_slot1", daily_slot=1, scheduled_at="2026-11-07T15:00:00+01:00")
        # At 16:00 Budapest, Window 1 (15:00) has passed, Window 2 (18:00) is in future
        eval_dt = datetime(2026, 11, 7, 16, 0, tzinfo=self.tz)
        windows = get_windows_for_item(item, current_dt=eval_dt)

        self.assertEqual(len(windows), 2)
        win1, win2 = windows[0], windows[1]

        self.assertEqual(win1["window_id"], 1)
        self.assertEqual(win1["time"], "15:00")
        self.assertEqual(win1["media_type"], "image")
        self.assertIn("facebook", win1["platforms"])
        self.assertIn("threads", win1["platforms"])
        self.assertIn("pinterest", win1["platforms"])
        # Window 1 has passed and wasn't published
        self.assertTrue(win1["time_has_passed"])
        self.assertIn(win1["state"], ("NOT_PUBLISHED", "SIMULATED"))
        self.assertEqual(win1["ui_label"], "Not published")

        self.assertEqual(win2["window_id"], 2)
        self.assertEqual(win2["time"], "18:00")
        self.assertEqual(win2["media_type"], "image")
        self.assertIn("pinterest_lifemodehq", win2["platforms"])
        # Window 2 is still in the future!
        self.assertTrue(win2["is_future"])
        self.assertEqual(win2["state"], "SCHEDULED")

    def test_media_type_detection_no_package_override(self):
        """An image queue item must NOT be overridden by a video in a shared package."""
        image_item = _make_item("item_img", asin="B0CLGNYKFR", daily_slot=1, media_type="image")
        media_info = _resolve_queue_item_media(image_item)
        self.assertEqual(media_info["media_type"], "image")
        # Video URL must never be attached to an image queue item
        self.assertIsNone(media_info["video_url"])

    def test_media_type_detection_video_item(self):
        """A video queue item must resolve to video and preserve video_url."""
        video_item = _make_item(
            "item_vid",
            asin="B0CLGNYKFR",
            daily_slot=2,
            media_type="video",
            video_url="https://r2.example.com/video.mp4",
        )
        media_info = _resolve_queue_item_media(video_item)
        self.assertEqual(media_info["media_type"], "video")
        self.assertEqual(media_info["video_url"], "https://r2.example.com/video.mp4")

    def test_queue_item_presentation_execution_times(self):
        """Presentation must format execution_times and attach windows."""
        queue = _make_queue()
        item1 = _make_item("item_slot1", daily_slot=1, scheduled_at="2026-11-07T15:00:00+01:00")
        pres1 = get_queue_item_presentation(queue, item1)
        self.assertEqual(pres1["execution_times"], "15:00 & 18:00 Budapest")
        self.assertEqual(pres1["media_type"], "image")
        self.assertEqual(len(pres1["windows"]), 2)

        item2 = _make_item("item_slot2", daily_slot=2, scheduled_at="2026-11-07T21:00:00+01:00", media_type="video")
        pres2 = get_queue_item_presentation(queue, item2)
        self.assertEqual(pres2["execution_times"], "21:00 & 23:00 Budapest")
        self.assertEqual(pres2["media_type"], "video")
        self.assertEqual(len(pres2["windows"]), 2)

    def test_accurate_platform_status_no_false_published(self):
        """Platform without live post ID/confirmation must not be reported as PUBLISHED."""
        queue = _make_queue()
        item = _make_item(
            "item_not_pub",
            status="READY",
            publishing_results={"facebook": {"status": "SUCCESS"}},
        )
        # Evaluate after slot time
        eval_dt = datetime(2026, 11, 7, 16, 0, tzinfo=self.tz)
        pres = get_queue_item_presentation(queue, item, current_dt=eval_dt)
        fb_result = pres["platforms"]["facebook"]
        self.assertNotEqual(fb_result["state"], "PUBLISHED")
        self.assertIn(fb_result["state"], ("NOT_PUBLISHED", "SIMULATED", "UNKNOWN"))

    def test_accurate_platform_status_with_verified_post(self):
        """Platform with verified post_id and URL is reported as PUBLISHED."""
        queue = _make_queue()
        item = _make_item(
            "item_pub",
            status="PUBLISHED",
            publishing_results={
                "facebook": {
                    "status": "PUBLISHED",
                    "mode": "LIVE",
                    "post_id": "fb_12345",
                    "permalink_url": "https://facebook.com/posts/12345",
                },
            },
        )
        pres = get_queue_item_presentation(queue, item)
        fb_result = pres["platforms"]["facebook"]
        self.assertEqual(fb_result["state"], "PUBLISHED")
        self.assertEqual(fb_result["post_id"], "fb_12345")
        self.assertEqual(fb_result["post_url"], "https://facebook.com/posts/12345")

    def test_dashboard_routes_render_real_queue(self):
        """Verify /queue, /products, and /products/<asin> render with production windows."""
        # /queue
        resp = self.client.get("/queue")
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode("utf-8")
        self.assertIn("15:00 &amp; 18:00 Budapest", html)
        self.assertIn("21:00 &amp; 23:00 Budapest", html)
        self.assertIn("15:00 Budapest", html)
        self.assertIn("18:00 Budapest", html)
        self.assertIn("21:00 Budapest", html)
        self.assertIn("23:00 Budapest", html)

        # /products
        resp = self.client.get("/products?q=B0CLGNYKFR")
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode("utf-8")
        self.assertIn("15:00 &amp; 18:00 Budapest", html)
        self.assertIn("21:00 &amp; 23:00 Budapest", html)
        self.assertIn("Slot 1 &bull; 🖼️ Image", html)
        self.assertIn("Slot 2 &bull; 🎬 Video", html)

        # /products/B0CLGNYKFR
        resp = self.client.get("/products/B0CLGNYKFR")
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode("utf-8")
        self.assertIn("15:00 Budapest", html)
        self.assertIn("18:00 Budapest", html)
        self.assertIn("21:00 Budapest", html)
        self.assertIn("23:00 Budapest", html)
        self.assertIn("item_034", html)
        self.assertIn("item_035", html)


if __name__ == "__main__":
    unittest.main()
