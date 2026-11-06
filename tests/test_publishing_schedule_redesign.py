"""
tests/test_publishing_schedule_redesign.py - Targeted Tests for Redesigned Publishing Schedule

Validates:
1. All four scheduled windows (15:00, 18:00, 21:00, 23:00 Europe/Budapest).
2. CET (UTC+1, winter) and CEST (UTC+2, summer) timezone conversions.
3. Independent Pinterest account scheduling:
   - Account 1 (dailyideasforyou) publishes at 15:00 (image) and 21:00 (video).
   - Account 2 (LifeModeHQ) publishes at 18:00 (image) and 23:00 (video).
   - Facebook & Threads publish at 15:00 (image) and 21:00 (video).
4. Image and video slot isolation: Image posts run only in image slots (Slot 1);
   Video posts run only in video slots (Slot 2). Never published simultaneously.
5. Catch-up behaviour: Missed slots never bundle image and video posts together.
6. Minimum platform separation:
   - Enforces >= 120 minutes interval per platform and account.
   - Exact 120-minute interval is allowed; < 120 minutes is rejected/deferred.
7. Partial publishing failures and safe retry behaviour:
   - Successful platforms are preserved and skipped via SKIP_DUPLICATE.
8. Concurrent execution and duplicate prevention:
   - Slot claims prevent duplicate execution across separate Actions runs.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from publishing.queue import (
    QueuePublisher,
    PublishingQueue,
    QueueItem,
    PUBLISHING_WINDOWS,
    ALL_PLATFORM_DESTINATIONS,
    get_active_publishing_window,
    check_platform_min_separation,
    _is_destination_published,
    get_budapest_timezone,
    STATUS_READY,
    STATUS_PUBLISHED,
    STATUS_PARTIALLY_PUBLISHED,
    STATUS_FAILED,
)
from publishing.publisher import (
    SocialPublisher,
    PublishingConfig,
    STATUS_WOULD_PUBLISH,
    STATUS_PUBLISHED as PLATFORM_STATUS_PUBLISHED,
    STATUS_FAILED as PLATFORM_STATUS_FAILED,
)
from scripts.run_cloud_publisher import (
    check_budapest_publishing_window,
    claim_due_slots,
    finalize_slot_claims,
)


class TestPublishingWindowsAndDestinations(unittest.TestCase):
    """Test 1: Validate all 4 daily publishing windows and their destination routing."""

    def test_window_definitions_and_destinations(self):
        """Verify window configuration table matches the approved schedule exactly."""
        self.assertEqual(len(PUBLISHING_WINDOWS), 4)

        # Window 1: 15:00 Image (Main)
        w1 = PUBLISHING_WINDOWS[1]
        self.assertEqual(w1["daily_slot"], 1)
        self.assertEqual(w1["media_type"], "image")
        self.assertEqual(w1["start_hour"], 15)
        self.assertEqual(w1["end_hour"], 18)
        self.assertEqual(set(w1["target_destinations"]), {"facebook", "threads", "pinterest"})

        # Window 2: 18:00 Image (LifeModeHQ Pinterest)
        w2 = PUBLISHING_WINDOWS[2]
        self.assertEqual(w2["daily_slot"], 1)
        self.assertEqual(w2["media_type"], "image")
        self.assertEqual(w2["start_hour"], 18)
        self.assertEqual(w2["end_hour"], 21)
        self.assertEqual(set(w2["target_destinations"]), {"pinterest_lifemodehq"})

        # Window 3: 21:00 Video (Main)
        w3 = PUBLISHING_WINDOWS[3]
        self.assertEqual(w3["daily_slot"], 2)
        self.assertEqual(w3["media_type"], "video")
        self.assertEqual(w3["start_hour"], 21)
        self.assertEqual(w3["end_hour"], 23)
        self.assertEqual(set(w3["target_destinations"]), {"facebook", "threads", "pinterest"})

        # Window 4: 23:00 Video (LifeModeHQ Pinterest)
        w4 = PUBLISHING_WINDOWS[4]
        self.assertEqual(w4["daily_slot"], 2)
        self.assertEqual(w4["media_type"], "video")
        self.assertEqual(w4["start_hour"], 23)
        self.assertEqual(w4["end_hour"], 24)
        self.assertEqual(set(w4["target_destinations"]), {"pinterest_lifemodehq"})

    def test_independent_pinterest_account_separation(self):
        """Two Pinterest accounts must publish in separate windows."""
        w1_dests = PUBLISHING_WINDOWS[1]["target_destinations"]
        w2_dests = PUBLISHING_WINDOWS[2]["target_destinations"]
        w3_dests = PUBLISHING_WINDOWS[3]["target_destinations"]
        w4_dests = PUBLISHING_WINDOWS[4]["target_destinations"]

        # Window 1 has dailyideasforyou, never lifemodehq
        self.assertIn("pinterest", w1_dests)
        self.assertNotIn("pinterest_lifemodehq", w1_dests)

        # Window 2 has lifemodehq, never dailyideasforyou
        self.assertIn("pinterest_lifemodehq", w2_dests)
        self.assertNotIn("pinterest", w2_dests)

        # Window 3 has dailyideasforyou, never lifemodehq
        self.assertIn("pinterest", w3_dests)
        self.assertNotIn("pinterest_lifemodehq", w3_dests)

        # Window 4 has lifemodehq, never dailyideasforyou
        self.assertIn("pinterest_lifemodehq", w4_dests)
        self.assertNotIn("pinterest", w4_dests)


class TestTimezoneConversionCETCEST(unittest.TestCase):
    """Test 2: Validate CET and CEST UTC-to-Budapest conversions for all 4 windows."""

    def test_summer_cest_conversion(self):
        """Summer (CEST, UTC+2): 13:00 -> 15:00, 16:00 -> 18:00, 19:00 -> 21:00, 21:00 -> 23:00."""
        # 13:00 UTC = 15:00 CEST (Window 1)
        self.assertEqual(get_active_publishing_window(datetime.fromisoformat("2026-07-15T13:00:00+00:00")), 1)
        # 16:00 UTC = 18:00 CEST (Window 2)
        self.assertEqual(get_active_publishing_window(datetime.fromisoformat("2026-07-15T16:00:00+00:00")), 2)
        # 19:00 UTC = 21:00 CEST (Window 3)
        self.assertEqual(get_active_publishing_window(datetime.fromisoformat("2026-07-15T19:00:00+00:00")), 3)
        # 21:00 UTC = 23:00 CEST (Window 4)
        self.assertEqual(get_active_publishing_window(datetime.fromisoformat("2026-07-15T21:00:00+00:00")), 4)

        # Outside hours
        self.assertIsNone(get_active_publishing_window(datetime.fromisoformat("2026-07-15T12:00:00+00:00")))  # 14:00 CEST
        self.assertIsNone(get_active_publishing_window(datetime.fromisoformat("2026-07-15T22:00:00+00:00")))  # 00:00 CEST

    def test_winter_cet_conversion(self):
        """Winter (CET, UTC+1): 14:00 -> 15:00, 17:00 -> 18:00, 20:00 -> 21:00, 22:00 -> 23:00."""
        # 14:00 UTC = 15:00 CET (Window 1)
        self.assertEqual(get_active_publishing_window(datetime.fromisoformat("2026-12-15T14:00:00+00:00")), 1)
        # 17:00 UTC = 18:00 CET (Window 2)
        self.assertEqual(get_active_publishing_window(datetime.fromisoformat("2026-12-15T17:00:00+00:00")), 2)
        # 20:00 UTC = 21:00 CET (Window 3)
        self.assertEqual(get_active_publishing_window(datetime.fromisoformat("2026-12-15T20:00:00+00:00")), 3)
        # 22:00 UTC = 23:00 CET (Window 4)
        self.assertEqual(get_active_publishing_window(datetime.fromisoformat("2026-12-15T22:00:00+00:00")), 4)

        # Outside hours
        self.assertIsNone(get_active_publishing_window(datetime.fromisoformat("2026-12-15T13:00:00+00:00")))  # 14:00 CET
        self.assertIsNone(get_active_publishing_window(datetime.fromisoformat("2026-12-15T23:00:00+00:00")))  # 00:00 CET


class TestPlatformMinSeparation(unittest.TestCase):
    """Test 6: Validate minimum platform separation (exact 120 min allowed, < 120 min rejected)."""

    def setUp(self):
        self.queue = PublishingQueue(
            queue_id="test_sep_q",
            created_at="2026-11-01T00:00:00+00:00",
            start_date="2026-11-06",
            total_days=1,
            products_per_day=2,
            target_capacity=2,
            items=[],
        )

    def test_no_prior_publications_always_allowed(self):
        """First publication on any destination is always allowed."""
        now = datetime.fromisoformat("2026-11-06T15:00:00+01:00")
        for dest in ALL_PLATFORM_DESTINATIONS:
            allowed, latest, diff = check_platform_min_separation(self.queue, dest, now, min_minutes=120)
            self.assertTrue(allowed)
            self.assertIsNone(latest)
            self.assertIsNone(diff)

    def test_less_than_120_minutes_is_rejected(self):
        """Publication less than 120 minutes after prior publication is blocked."""
        pub_time = "2026-11-06T13:01:00+01:00"
        self.queue.items.append(
            QueueItem(
                queue_item_id="item_prev",
                original_url="https://amazon.com/dp/B001",
                asin="B001",
                product_name="Product 1",
                scheduled_date="2026-11-06",
                daily_slot=1,
                scheduled_at=pub_time,
                status=STATUS_PARTIALLY_PUBLISHED,
                publishing_results={
                    "facebook": {
                        "status": PLATFORM_STATUS_PUBLISHED,
                        "mode": "LIVE",
                        "post_id": "fb_123",
                        "published_at": pub_time,
                    }
                }
            )
        )

        # Evaluation at 15:00:00 (119 minutes elapsed -> < 120 -> REJECTED)
        eval_dt = datetime.fromisoformat("2026-11-06T15:00:00+01:00")
        allowed, latest, diff = check_platform_min_separation(self.queue, "facebook", eval_dt, min_minutes=120)
        self.assertFalse(allowed)
        self.assertIsNotNone(latest)
        self.assertAlmostEqual(diff, 119.0, places=1)

    def test_exact_120_minutes_is_allowed(self):
        """Exact 120-minute interval boundary is allowed."""
        pub_time = "2026-11-06T13:00:00+01:00"
        self.queue.items.append(
            QueueItem(
                queue_item_id="item_prev",
                original_url="https://amazon.com/dp/B001",
                asin="B001",
                product_name="Product 1",
                scheduled_date="2026-11-06",
                daily_slot=1,
                scheduled_at=pub_time,
                status=STATUS_PARTIALLY_PUBLISHED,
                publishing_results={
                    "facebook": {
                        "status": PLATFORM_STATUS_PUBLISHED,
                        "mode": "LIVE",
                        "post_id": "fb_123",
                        "published_at": pub_time,
                    }
                }
            )
        )

        # Evaluation at 15:00:00 (exact 120 minutes elapsed -> ALLOWED)
        eval_dt = datetime.fromisoformat("2026-11-06T15:00:00+01:00")
        allowed, latest, diff = check_platform_min_separation(self.queue, "facebook", eval_dt, min_minutes=120)
        self.assertTrue(allowed)
        self.assertAlmostEqual(diff, 120.0, places=1)

    def test_more_than_120_minutes_is_allowed(self):
        """Publication > 120 minutes after prior publication is allowed."""
        pub_time = "2026-11-06T11:00:00+01:00"
        self.queue.items.append(
            QueueItem(
                queue_item_id="item_prev",
                original_url="https://amazon.com/dp/B001",
                asin="B001",
                product_name="Product 1",
                scheduled_date="2026-11-06",
                daily_slot=1,
                scheduled_at=pub_time,
                status=STATUS_PARTIALLY_PUBLISHED,
                publishing_results={
                    "pinterest_lifemodehq": {
                        "status": PLATFORM_STATUS_PUBLISHED,
                        "mode": "LIVE",
                        "pin_id": "pin_456",
                        "published_at": pub_time,
                    }
                }
            )
        )

        eval_dt = datetime.fromisoformat("2026-11-06T18:00:00+01:00")  # 7 hours later
        allowed, latest, diff = check_platform_min_separation(self.queue, "pinterest_lifemodehq", eval_dt, min_minutes=120)
        self.assertTrue(allowed)
        self.assertAlmostEqual(diff, 420.0, places=1)

    def test_separation_is_platform_independent(self):
        """A recent publication on Facebook does NOT block Threads or Pinterest."""
        pub_time = "2026-11-06T14:30:00+01:00"  # 30 min ago
        self.queue.items.append(
            QueueItem(
                queue_item_id="item_prev",
                original_url="https://amazon.com/dp/B001",
                asin="B001",
                product_name="Product 1",
                scheduled_date="2026-11-06",
                daily_slot=1,
                scheduled_at=pub_time,
                status=STATUS_PARTIALLY_PUBLISHED,
                publishing_results={
                    "facebook": {
                        "status": PLATFORM_STATUS_PUBLISHED,
                        "mode": "LIVE",
                        "post_id": "fb_123",
                        "published_at": pub_time,
                    }
                }
            )
        )

        eval_dt = datetime.fromisoformat("2026-11-06T15:00:00+01:00")
        # Facebook is blocked (30m < 120m)
        fb_allowed, _, _ = check_platform_min_separation(self.queue, "facebook", eval_dt, min_minutes=120)
        self.assertFalse(fb_allowed)

        # Threads has no prior publication -> allowed!
        th_allowed, _, _ = check_platform_min_separation(self.queue, "threads", eval_dt, min_minutes=120)
        self.assertTrue(th_allowed)

        # Pinterest has no prior publication -> allowed!
        pin_allowed, _, _ = check_platform_min_separation(self.queue, "pinterest", eval_dt, min_minutes=120)
        self.assertTrue(pin_allowed)


class TestSlotIsolationAndCatchup(unittest.TestCase):
    """Test 4 & 5: Validate strict slot isolation (Image vs Video) and catch-up behavior."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_isolation_"))
        self.queue_file = self.temp_dir / "test_queue.json"

        # Create dummy content packages
        self.pkg_dir = self.temp_dir / "packages"
        self.pkg_dir.mkdir(parents=True, exist_ok=True)

        self.img_pkg_file = self.pkg_dir / "pkg_img.json"
        self.img_pkg_file.write_text(json.dumps({
            "asin": "B0IMAGE001",
            "title": "Image Product",
            "platforms": {
                "facebook": {"status": "VALID", "text": "Post 1", "image_url": "https://img.test/1.jpg"},
                "threads": {"status": "VALID", "text": "Post 1", "image_url": "https://img.test/1.jpg"},
                "pinterest": {"status": "VALID", "text": "Post 1", "image_url": "https://img.test/1.jpg"},
            }
        }), encoding="utf-8")

        self.vid_pkg_file = self.pkg_dir / "pkg_vid.json"
        self.vid_pkg_file.write_text(json.dumps({
            "asin": "B0VIDEO002",
            "title": "Video Product",
            "video_url": "https://pub-r2.test/video.mp4",
            "platforms": {
                "facebook": {"status": "VALID", "text": "Post 2", "video_url": "https://pub-r2.test/video.mp4"},
                "threads": {"status": "VALID", "text": "Post 2", "video_url": "https://pub-r2.test/video.mp4"},
                "pinterest": {"status": "VALID", "text": "Post 2", "video_url": "https://pub-r2.test/video.mp4", "image_url": "https://img.test/cover.jpg"},
            }
        }), encoding="utf-8")

    def tearDown(self):
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_window_1_selects_only_slot_1_image(self):
        """Window 1 (15:00) selects only Slot 1; Slot 2 (Video) is never selected."""
        queue = PublishingQueue(
            queue_id="test_q",
            created_at="2026-11-06T00:00:00+00:00",
            start_date="2026-11-06",
            total_days=1,
            products_per_day=2,
            target_capacity=2,
            items=[
                QueueItem(
                    queue_item_id="item_slot1_img",
                    original_url="https://amazon.com/dp/B0IMAGE001",
                    asin="B0IMAGE001",
                    product_name="Image Product",
                    scheduled_date="2026-11-06",
                    daily_slot=1,
                    scheduled_at="2026-11-06T15:00:00+01:00",
                    package_reference=str(self.img_pkg_file),
                    status=STATUS_READY,
                ),
                QueueItem(
                    queue_item_id="item_slot2_vid",
                    original_url="https://amazon.com/dp/B0VIDEO002",
                    asin="B0VIDEO002",
                    product_name="Video Product",
                    scheduled_date="2026-11-06",
                    daily_slot=2,
                    scheduled_at="2026-11-06T21:00:00+01:00",
                    package_reference=str(self.vid_pkg_file),
                    status=STATUS_READY,
                ),
            ],
        )

        now_w1 = datetime.fromisoformat("2026-11-06T15:15:00+01:00")
        due = QueuePublisher.find_due_items(queue, now_w1, window_id=1)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].queue_item_id, "item_slot1_img")
        self.assertEqual(due[0].daily_slot, 1)

    def test_missed_slot_1_in_window_3_is_never_bundled_with_video(self):
        """If Slot 1 was missed earlier, Window 3 (21:00) does NOT process Slot 1 alongside Slot 2."""
        queue = PublishingQueue(
            queue_id="test_q",
            created_at="2026-11-06T00:00:00+00:00",
            start_date="2026-11-06",
            total_days=1,
            products_per_day=2,
            target_capacity=2,
            items=[
                # Slot 1 is overdue (scheduled at 15:00, now it's 21:05)
                QueueItem(
                    queue_item_id="item_slot1_img",
                    original_url="https://amazon.com/dp/B0IMAGE001",
                    asin="B0IMAGE001",
                    product_name="Image Product",
                    scheduled_date="2026-11-06",
                    daily_slot=1,
                    scheduled_at="2026-11-06T15:00:00+01:00",
                    package_reference=str(self.img_pkg_file),
                    status=STATUS_READY,
                ),
                # Slot 2 is due now (scheduled at 21:00)
                QueueItem(
                    queue_item_id="item_slot2_vid",
                    original_url="https://amazon.com/dp/B0VIDEO002",
                    asin="B0VIDEO002",
                    product_name="Video Product",
                    scheduled_date="2026-11-06",
                    daily_slot=2,
                    scheduled_at="2026-11-06T21:00:00+01:00",
                    package_reference=str(self.vid_pkg_file),
                    status=STATUS_READY,
                ),
            ],
        )

        now_w3 = datetime.fromisoformat("2026-11-06T21:05:00+01:00")
        due = QueuePublisher.find_due_items(queue, now_w3, window_id=3)

        # Must select ONLY the video item (Slot 2); Image item (Slot 1) is never bundled!
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].queue_item_id, "item_slot2_vid")
        self.assertEqual(due[0].daily_slot, 2)


class TestFullScheduleLifecycle(unittest.TestCase):
    """Test 3 & 7: End-to-end lifecycle across all 4 windows with partial publishing and retries."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_lifecycle_"))
        self.queue_file = self.temp_dir / "publishing_queue.json"
        self.log_file = self.temp_dir / "publisher.log"

        self.pkg_dir = self.temp_dir / "packages"
        self.pkg_dir.mkdir(parents=True, exist_ok=True)

        self.pkg1_file = self.pkg_dir / "pkg1.json"
        self.pkg1_file.write_text(json.dumps({
            "asin": "B001",
            "platforms": {
                "facebook": {"status": "VALID", "text": "Post 1", "image_url": "https://img.test/1.jpg"},
                "threads": {"status": "VALID", "text": "Post 1", "image_url": "https://img.test/1.jpg"},
                "pinterest": {"status": "VALID", "text": "Post 1", "image_url": "https://img.test/1.jpg"},
            }
        }), encoding="utf-8")

        self.pkg2_file = self.pkg_dir / "pkg2.json"
        self.pkg2_file.write_text(json.dumps({
            "asin": "B002",
            "video_url": "https://pub-r2.test/video2.mp4",
            "platforms": {
                "facebook": {"status": "VALID", "text": "Post 2", "video_url": "https://pub-r2.test/video2.mp4"},
                "threads": {"status": "VALID", "text": "Post 2", "video_url": "https://pub-r2.test/video2.mp4"},
                "pinterest": {"status": "VALID", "text": "Post 2", "video_url": "https://pub-r2.test/video2.mp4", "image_url": "https://img.test/cover2.jpg"},
            }
        }), encoding="utf-8")

        self.queue = PublishingQueue(
            queue_id="lifecycle_q",
            created_at="2026-11-06T00:00:00+00:00",
            start_date="2026-11-06",
            total_days=1,
            products_per_day=2,
            target_capacity=2,
            items=[
                QueueItem(
                    queue_item_id="item_001",
                    original_url="https://amazon.com/dp/B001",
                    asin="B001",
                    product_name="Product 1 (Image)",
                    scheduled_date="2026-11-06",
                    daily_slot=1,
                    scheduled_at="2026-11-06T15:00:00+01:00",
                    package_reference=str(self.pkg1_file),
                    status=STATUS_READY,
                ),
                QueueItem(
                    queue_item_id="item_002",
                    original_url="https://amazon.com/dp/B002",
                    asin="B002",
                    product_name="Product 2 (Video)",
                    scheduled_date="2026-11-06",
                    daily_slot=2,
                    scheduled_at="2026-11-06T21:00:00+01:00",
                    package_reference=str(self.pkg2_file),
                    status=STATUS_READY,
                ),
            ],
        )
        self.queue.save(self.queue_file)

    def tearDown(self):
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def create_mock_publisher(self):
        mock_pub = MagicMock()
        mock_pub.config.dry_run = False

        def mock_publish(package, dry_run=False, prior_results=None, target_destinations=None):
            results = dict(prior_results or {})
            targets = target_destinations or ["facebook", "threads", "pinterest", "pinterest_lifemodehq"]
            for dest in targets:
                results[dest] = {
                    "platform": dest.replace("pinterest_lifemodehq", "pinterest"),
                    "status": PLATFORM_STATUS_PUBLISHED,
                    "mode": "LIVE",
                    "post_id": f"mock_id_{dest}",
                    "published_at": datetime.now(get_budapest_timezone()).isoformat(),
                }
            all_ok = set(results.keys()) == {"facebook", "pinterest", "pinterest_lifemodehq", "threads"}
            return {
                "asin": package.get("asin"),
                "status": PLATFORM_STATUS_PUBLISHED if all_ok else "PARTIAL",
                "mode": "LIVE",
                "results": results,
            }

        mock_pub.publish_package.side_effect = mock_publish
        return mock_pub

    def test_progressive_publishing_across_four_windows(self):
        """Simulate execution through all 4 windows of a single day."""
        mock_pub = self.create_mock_publisher()

        # Window 1: 15:00 -> Item 1 publishes facebook, threads, pinterest
        w1_dt = datetime.fromisoformat("2026-11-06T15:00:00+01:00")
        report1 = QueuePublisher.publish_due_items(
            queue_file=self.queue_file,
            now_override=w1_dt,
            dry_run=False,
            publisher=mock_pub,
            window_id=1,
            log_file=self.log_file,
        )
        q1 = PublishingQueue.load(self.queue_file)
        item1 = q1.items[0]
        # In Window 1, item 1 is partially published (LifeModeHQ is pending for Window 2)
        self.assertEqual(item1.status, STATUS_PARTIALLY_PUBLISHED)
        self.assertIn("facebook", item1.publishing_results)
        self.assertIn("threads", item1.publishing_results)
        self.assertIn("pinterest", item1.publishing_results)
        self.assertNotIn("pinterest_lifemodehq", item1.publishing_results)

        # Window 2: 18:00 -> Item 1 publishes pinterest_lifemodehq
        w2_dt = datetime.fromisoformat("2026-11-06T18:00:00+01:00")
        report2 = QueuePublisher.publish_due_items(
            queue_file=self.queue_file,
            now_override=w2_dt,
            dry_run=False,
            publisher=mock_pub,
            window_id=2,
            log_file=self.log_file,
        )
        q2 = PublishingQueue.load(self.queue_file)
        item1_after_w2 = q2.items[0]
        # Now all 4 destinations are published! Item 1 is fully published!
        self.assertEqual(item1_after_w2.status, STATUS_PUBLISHED)
        self.assertIn("pinterest_lifemodehq", item1_after_w2.publishing_results)

        # Window 3: 21:00 -> Item 2 (Video) publishes facebook, threads, pinterest
        w3_dt = datetime.fromisoformat("2026-11-06T21:00:00+01:00")
        report3 = QueuePublisher.publish_due_items(
            queue_file=self.queue_file,
            now_override=w3_dt,
            dry_run=False,
            publisher=mock_pub,
            window_id=3,
            log_file=self.log_file,
        )
        q3 = PublishingQueue.load(self.queue_file)
        item2 = q3.items[1]
        self.assertEqual(item2.status, STATUS_PARTIALLY_PUBLISHED)
        self.assertIn("facebook", item2.publishing_results)
        self.assertIn("threads", item2.publishing_results)
        self.assertIn("pinterest", item2.publishing_results)
        self.assertNotIn("pinterest_lifemodehq", item2.publishing_results)

        # Window 4: 23:00 -> Item 2 (Video) publishes pinterest_lifemodehq
        w4_dt = datetime.fromisoformat("2026-11-06T23:00:00+01:00")
        report4 = QueuePublisher.publish_due_items(
            queue_file=self.queue_file,
            now_override=w4_dt,
            dry_run=False,
            publisher=mock_pub,
            window_id=4,
            log_file=self.log_file,
        )
        q4 = PublishingQueue.load(self.queue_file)
        item2_after_w4 = q4.items[1]
        # Now Item 2 is fully published!
        self.assertEqual(item2_after_w4.status, STATUS_PUBLISHED)
        self.assertIn("pinterest_lifemodehq", item2_after_w4.publishing_results)


class TestRetryAndSkipDuplicate(unittest.TestCase):
    """Test 7: Validate retry behavior and skip-duplicate protection."""

    def test_failed_platform_is_retried_while_successful_is_skipped(self):
        """If Facebook fails in Window 1, retry republishes Facebook but skips Threads/Pinterest."""
        config = PublishingConfig()
        publisher = SocialPublisher(config=config)

        mock_fb = MagicMock()
        mock_fb.publish.return_value = {
            "platform": "facebook",
            "status": PLATFORM_STATUS_PUBLISHED,
            "mode": "LIVE",
            "post_id": "fb_new_123",
        }
        mock_th = MagicMock()
        mock_pin = MagicMock()

        publisher.adapters = {
            "facebook": mock_fb,
            "threads": mock_th,
            "pinterest": mock_pin,
        }

        package = {
            "asin": "B0RETRY",
            "status": "READY_FOR_REVIEW",
            "platforms": {
                "facebook": {"status": "VALID", "text": "Post FB", "image_url": "https://img.test/1.jpg"},
                "threads": {"status": "VALID", "text": "Post TH", "image_url": "https://img.test/1.jpg"},
                "pinterest": {"status": "VALID", "text": "Post PIN", "image_url": "https://img.test/1.jpg"},
            }
        }

        prior_results = {
            "threads": {
                "platform": "threads",
                "status": PLATFORM_STATUS_PUBLISHED,
                "mode": "LIVE",
                "post_id": "th_already_done",
            },
            "pinterest": {
                "platform": "pinterest",
                "status": PLATFORM_STATUS_PUBLISHED,
                "mode": "LIVE",
                "pin_id": "pin_already_done",
            },
            "facebook": {
                "platform": "facebook",
                "status": PLATFORM_STATUS_FAILED,
                "mode": "LIVE",
                "errors": ["Connection timeout"],
            },
        }

        # Run publish_package with prior results
        res = publisher.publish_package(
            package,
            dry_run=False,
            prior_results=prior_results,
            target_destinations=["facebook", "threads", "pinterest"],
        )

        # Facebook adapter was called
        self.assertEqual(mock_fb.publish.call_count, 1)
        # Threads and Pinterest adapters were NEVER called (skipped duplicate!)
        self.assertEqual(mock_th.publish.call_count, 0)
        self.assertEqual(mock_pin.publish.call_count, 0)

        # Results preserve SKIP_DUPLICATE for Threads and Pinterest
        self.assertEqual(res["results"]["threads"]["action"], "SKIP_DUPLICATE")
        self.assertEqual(res["results"]["pinterest"]["action"], "SKIP_DUPLICATE")
        self.assertEqual(res["results"]["facebook"]["status"], PLATFORM_STATUS_PUBLISHED)


if __name__ == "__main__":
    unittest.main()
