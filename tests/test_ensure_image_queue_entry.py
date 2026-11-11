"""Focused regression tests for ensure_image_queue_entry and ASIN B0D72N7642.

Proves:
1. A VIDEO entry in slot 1 does not prevent IMAGE entry creation.
2. The existing VIDEO entry remains unchanged as VIDEO (media_type, video_url, etc. preserved).
3. Repeated image upload/calls do not create duplicate IMAGE entries.
4. Correct representation and media resolution for ASIN B0D72N7642 in production queue.
"""

import json
from pathlib import Path
import tempfile
import unittest

from publishing.queue import PublishingQueue, QueueItem
from web.services import (
    ensure_image_queue_entry,
    _resolve_queue_item_media,
    QUEUE_FILE,
)


class TestEnsureImageQueueEntry(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_dir = Path(self.temp_dir.name)
        self.test_queue_file = self.test_dir / "publishing_queue.json"

        # Create a mock content package for a test ASIN
        self.test_asin = "B0TESTHUMID"
        self.generated_dir = Path("data/generated")
        self.generated_dir.mkdir(parents=True, exist_ok=True)
        self.pkg_file = self.generated_dir / f"asin_{self.test_asin}_content_package.json"
        self.pkg_data = {
            "asin": self.test_asin,
            "title": "Test Humidifier Product",
            "source": {
                "product_cache": f"data/products/asin_{self.test_asin}.json",
                "affiliate_url": "https://link.amazon/test-humidifier",
            },
            "media_type": "video",
            "video_url": "https://pub-r2.dev/videos/test.mp4",
        }
        with open(self.pkg_file, "w", encoding="utf-8") as f:
            json.dump(self.pkg_data, f)

    def tearDown(self):
        if self.pkg_file.exists():
            self.pkg_file.unlink()
        self.temp_dir.cleanup()

    def test_video_entry_in_slot_1_does_not_prevent_image_entry_creation(self):
        """A VIDEO entry in slot 1 does not prevent IMAGE entry creation."""
        # Setup queue with a VIDEO entry in daily_slot 1
        queue = PublishingQueue(
            queue_id="test_queue",
            created_at="2026-11-09T00:00:00Z",
            start_date="2026-11-09",
            items=[],
        )
        video_item = QueueItem(
            queue_item_id=f"item_001_{self.test_asin}",
            original_url="https://link.amazon/test-humidifier",
            asin=self.test_asin,
            product_name="Test Humidifier Product",
            scheduled_date="2026-11-09",
            daily_slot=1,
            scheduled_at="2026-11-09T15:00:00+01:00",
            timezone="Europe/Budapest",
            package_reference=str(self.pkg_file),
            status="READY",
            validation_status="READY_FOR_REVIEW",
            media_type="video",
            video_url="https://pub-r2.dev/videos/test.mp4",
        )
        queue.items.append(video_item)
        queue.save(self.test_queue_file)

        # Call ensure_image_queue_entry
        res = ensure_image_queue_entry(self.test_asin, queue_file=self.test_queue_file)
        self.assertTrue(res.get("success"), f"Expected success: {res}")
        self.assertTrue(res.get("created"), "Expected a new IMAGE queue entry to be created")

        # Reload queue and verify
        reloaded = PublishingQueue.load(self.test_queue_file)
        self.assertEqual(len(reloaded.items), 2)

        image_items = [
            it for it in reloaded.items
            if (it.asin or "").upper() == self.test_asin and it.media_type == "image"
        ]
        self.assertEqual(len(image_items), 1)
        created_img = image_items[0]
        self.assertEqual(created_img.media_type, "image")
        self.assertIsNone(created_img.video_url)
        self.assertEqual(created_img.daily_slot, 1)

    def test_existing_video_entry_remains_unchanged_as_video(self):
        """The existing VIDEO entry remains unchanged as VIDEO with all video data intact."""
        queue = PublishingQueue(
            queue_id="test_queue",
            created_at="2026-11-09T00:00:00Z",
            start_date="2026-11-09",
            items=[],
        )
        video_item = QueueItem(
            queue_item_id=f"item_001_{self.test_asin}",
            original_url="https://link.amazon/test-humidifier",
            asin=self.test_asin,
            product_name="Test Humidifier Product",
            scheduled_date="2026-11-09",
            daily_slot=1,
            scheduled_at="2026-11-09T15:00:00+01:00",
            timezone="Europe/Budapest",
            package_reference=str(self.pkg_file),
            status="READY",
            validation_status="READY_FOR_REVIEW",
            media_type="video",
            video_url="https://pub-r2.dev/videos/test.mp4",
        )
        queue.items.append(video_item)
        queue.save(self.test_queue_file)

        ensure_image_queue_entry(self.test_asin, queue_file=self.test_queue_file)

        reloaded = PublishingQueue.load(self.test_queue_file)
        v_item = next(
            (it for it in reloaded.items if it.queue_item_id == f"item_001_{self.test_asin}"),
            None,
        )
        self.assertIsNotNone(v_item)
        self.assertEqual(v_item.media_type, "video")
        self.assertEqual(v_item.video_url, "https://pub-r2.dev/videos/test.mp4")
        self.assertEqual(v_item.original_url, "https://link.amazon/test-humidifier")
        self.assertEqual(v_item.status, "READY")
        self.assertEqual(v_item.daily_slot, 1)

    def test_repeated_image_upload_does_not_create_duplicate_image_entries(self):
        """Repeated image upload / ensure calls do not create duplicate IMAGE entries."""
        queue = PublishingQueue(
            queue_id="test_queue",
            created_at="2026-11-09T00:00:00Z",
            start_date="2026-11-09",
            items=[],
        )
        queue.save(self.test_queue_file)

        # First call: creates the image entry
        res1 = ensure_image_queue_entry(self.test_asin, queue_file=self.test_queue_file)
        self.assertTrue(res1.get("success"))
        self.assertTrue(res1.get("created"))
        item_id_1 = res1.get("queue_item_id")

        q1 = PublishingQueue.load(self.test_queue_file)
        self.assertEqual(len(q1.items), 1)

        # Second call: must recognize existing image entry and NOT create duplicate
        res2 = ensure_image_queue_entry(self.test_asin, queue_file=self.test_queue_file)
        self.assertTrue(res2.get("success"))
        self.assertFalse(res2.get("created"))
        self.assertEqual(res2.get("queue_item_id"), item_id_1)

        # Third call: still exactly 1 item
        res3 = ensure_image_queue_entry(self.test_asin, queue_file=self.test_queue_file)
        self.assertFalse(res3.get("created"))

        q2 = PublishingQueue.load(self.test_queue_file)
        self.assertEqual(len(q2.items), 1)
        self.assertEqual(q2.items[0].queue_item_id, item_id_1)
        self.assertEqual(q2.items[0].media_type, "image")

    def test_production_queue_b0d72n7642_entries(self):
        """Verify production queue data for ASIN B0D72N7642 has separate IMAGE and VIDEO entries."""
        self.assertTrue(QUEUE_FILE.exists(), "Production queue file must exist")
        queue = PublishingQueue.load(QUEUE_FILE)

        # Find items for B0D72N7642
        items = [it for it in queue.items if (it.asin or "").upper() == "B0D72N7642"]
        self.assertGreaterEqual(len(items), 2, "Must have both IMAGE and VIDEO entries for B0D72N7642")

        video_items = [it for it in items if it.media_type == "video"]
        image_items = [it for it in items if it.media_type == "image"]

        self.assertEqual(len(video_items), 1, "Must have exactly 1 VIDEO entry for B0D72N7642")
        self.assertEqual(len(image_items), 1, "Must have exactly 1 IMAGE entry for B0D72N7642")

        v_item = video_items[0]
        i_item = image_items[0]

        # Check published VIDEO entry (item_036 was published on 2026-11-08)
        self.assertEqual(v_item.queue_item_id, "item_036_B0D72N7642")
        self.assertEqual(v_item.daily_slot, 1)
        self.assertEqual(v_item.scheduled_date, "2026-11-08")
        self.assertIn(v_item.status, ("PUBLISHED", "PARTIALLY_PUBLISHED"))
        self.assertTrue(v_item.video_url and v_item.video_url.startswith("https://"))
        self.assertEqual(v_item.original_url, "https://link.amazon/B0dLOKIZO")

        # Check pending IMAGE entry (item_038 is scheduled for 2026-11-09 Slot 1)
        self.assertEqual(i_item.queue_item_id, "item_038_B0D72N7642")
        self.assertEqual(i_item.daily_slot, 1)
        self.assertEqual(i_item.scheduled_date, "2026-11-09")
        self.assertEqual(i_item.scheduled_at, "2026-11-09T15:00:00+01:00")
        self.assertIn(i_item.status, ("READY", "PARTIALLY_PUBLISHED", "PUBLISHED"))
        self.assertIsNone(i_item.video_url)
        self.assertEqual(i_item.original_url, "https://link.amazon/B0dLOKIZO")

        # Verify media resolution
        v_media = _resolve_queue_item_media(v_item)
        self.assertEqual(v_media["media_type"], "video")
        self.assertEqual(v_media["video_url"], v_item.video_url)

        i_media = _resolve_queue_item_media(i_item)
        self.assertEqual(i_media["media_type"], "image")
        self.assertIsNone(i_media["video_url"])
        self.assertEqual(i_media["image_url"], "/creatives/B0D72N7642/gift_inspiration_flow.jpg")

        # Verify ensure_image_queue_entry idempotency on production queue
        res = ensure_image_queue_entry("B0D72N7642")
        self.assertTrue(res.get("success"))
        self.assertFalse(res.get("created"), "Must not create duplicate on production queue")
        self.assertEqual(res.get("queue_item_id"), i_item.queue_item_id)

    def test_production_queue_b0gr5wf8m4_entries(self):
        """Verify production queue data for ASIN B0GR5WF8M4 has separate IMAGE and VIDEO entries."""
        self.assertTrue(QUEUE_FILE.exists(), "Production queue file must exist")
        queue = PublishingQueue.load(QUEUE_FILE)

        # Find items for B0GR5WF8M4
        items = [it for it in queue.items if (it.asin or "").upper() == "B0GR5WF8M4"]
        self.assertGreaterEqual(len(items), 2, "Must have both IMAGE and VIDEO entries for B0GR5WF8M4")

        video_items = [it for it in items if it.media_type == "video"]
        image_items = [it for it in items if it.media_type == "image"]

        self.assertEqual(len(video_items), 1, "Must have exactly 1 VIDEO entry for B0GR5WF8M4")
        self.assertEqual(len(image_items), 1, "Must have exactly 1 IMAGE entry for B0GR5WF8M4")

        v_item = video_items[0]
        i_item = image_items[0]

        # Check existing VIDEO entry (item_039)
        self.assertEqual(v_item.queue_item_id, "item_039_B0GR5WF8M4")
        self.assertIn(v_item.status, ("READY", "FAILED", "PARTIALLY_PUBLISHED", "PUBLISHED"))
        self.assertTrue(v_item.video_url and v_item.video_url.startswith("https://"))
        self.assertIn("ad-_myusalife4", v_item.video_url)
        self.assertEqual(v_item.original_url, "https://link.amazon/B00TR8Qpy")

        # Check newly created IMAGE entry (item_043 is scheduled for Slot 1 at 15:00)
        self.assertEqual(i_item.queue_item_id, "item_043_B0GR5WF8M4")
        self.assertEqual(i_item.daily_slot, 1)
        self.assertEqual(i_item.scheduled_date, "2026-11-13")
        self.assertEqual(i_item.scheduled_at, "2026-11-13T15:00:00+01:00")
        self.assertIn(i_item.status, ("READY", "PARTIALLY_PUBLISHED", "PUBLISHED"))
        self.assertIsNone(i_item.video_url)
        self.assertEqual(i_item.original_url, "https://link.amazon/B00TR8Qpy")

        # Verify media resolution
        v_media = _resolve_queue_item_media(v_item)
        self.assertEqual(v_media["media_type"], "video")
        self.assertEqual(v_media["video_url"], v_item.video_url)
        self.assertEqual(v_media["image_url"], "/creatives/B0GR5WF8M4/everyday_pick_flow.jpg")

        i_media = _resolve_queue_item_media(i_item)
        self.assertEqual(i_media["media_type"], "image")
        self.assertIsNone(i_media["video_url"])
        self.assertEqual(i_media["image_url"], "/creatives/B0GR5WF8M4/everyday_pick_flow.jpg")

        # Verify ensure_image_queue_entry idempotency on production queue
        res = ensure_image_queue_entry("B0GR5WF8M4")
        self.assertTrue(res.get("success"))
        self.assertFalse(res.get("created"), "Must not create duplicate on production queue")
        self.assertEqual(res.get("queue_item_id"), i_item.queue_item_id)

    def test_product_with_both_image_and_assigned_video_has_separate_entries(self):
        """Test queue workflow for a product having both generated image and assigned video."""
        queue = PublishingQueue(
            queue_id="test_dual_media_queue",
            created_at="2026-11-09T00:00:00Z",
            start_date="2026-11-09",
            items=[],
        )
        # 1. Existing video entry in Slot 2
        video_item = QueueItem(
            queue_item_id=f"item_video_{self.test_asin}",
            original_url="https://link.amazon/test-humidifier",
            asin=self.test_asin,
            product_name="Test Humidifier Product",
            scheduled_date="2026-11-10",
            daily_slot=2,
            scheduled_at="2026-11-10T21:00:00+01:00",
            timezone="Europe/Budapest",
            package_reference=str(self.pkg_file),
            status="READY",
            validation_status="READY_FOR_REVIEW",
            media_type="video",
            video_url="https://pub-r2.dev/videos/test.mp4",
        )
        queue.items.append(video_item)
        queue.save(self.test_queue_file)

        # 2. Call ensure_image_queue_entry
        res = ensure_image_queue_entry(self.test_asin, queue_file=self.test_queue_file)
        self.assertTrue(res.get("success"))
        self.assertTrue(res.get("created"))

        # 3. Verify queue state
        reloaded = PublishingQueue.load(self.test_queue_file)
        items = [it for it in reloaded.items if (it.asin or "").upper() == self.test_asin]
        self.assertEqual(len(items), 2, "Must contain exactly 2 queue entries")

        v_item = next(it for it in items if it.media_type == "video")
        i_item = next(it for it in items if it.media_type == "image")

        self.assertEqual(v_item.daily_slot, 2)
        self.assertEqual(v_item.scheduled_at, "2026-11-10T21:00:00+01:00")
        self.assertEqual(v_item.video_url, "https://pub-r2.dev/videos/test.mp4")

        self.assertEqual(i_item.daily_slot, 1)
        self.assertTrue(i_item.scheduled_at.endswith("15:00:00+01:00"))
        self.assertIsNone(i_item.video_url)


if __name__ == "__main__":
    unittest.main()
