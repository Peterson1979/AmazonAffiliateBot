"""
Targeted tests for AmazonAffiliateBot dashboard fixes:
1. Pinterest video copy generation (separate copy for dailyideasforyou and LifeModeHQ, FB/Threads preserved).
2. Image creative upload and publishing queue integration (Slot 1 image item created, Slot 2 video item preserved, duplicate prevention).
3. Product deletion service (clean removal, blocking published items).
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from amazon.scraper import Product
from content.facts import FactExtractor
from content.validator import ClaimValidator
from publishing.queue import (
    PublishingQueue,
    QueueItem,
    STATUS_READY,
    STATUS_PUBLISHED,
    STATUS_READY_FOR_REVIEW,
)
from web.services import (
    ensure_pinterest_video_variants,
    ensure_image_queue_entry,
    delete_product_service,
    enqueue_existing_product_service,
    get_product_detail,
    PRODUCTS_DIR,
    GENERATED_DIR,
    QUEUE_FILE,
)


class TestDashboardFixes(unittest.TestCase):

    def setUp(self):
        self.test_asin = "B0TESTFIX01"
        self.prod_file = PRODUCTS_DIR / f"asin_{self.test_asin}.json"
        self.pkg_file = GENERATED_DIR / f"asin_{self.test_asin}_content_package.json"

        # Create sample valid product
        self.prod_data = {
            "asin": self.test_asin,
            "title": "Comfort Test Slippers with Memory Foam Footbed",
            "features": [
                "Comfortable Materials: Premium faux fur uppers and soft linings.",
                "High-density Memory Foam: Cloud-like memory foam and EVA footbed for arch support.",
                "Non-slip Sole: Anti-skid grips for good traction around the house.",
            ],
            "specs": {"Brand": "ComfortTest"},
            "image_url": "https://m.media-amazon.com/images/I/81testimage._AC_SL1500_.jpg",
            "affiliate_url": "https://link.amazon/testfix01",
        }
        with open(self.prod_file, "w", encoding="utf-8") as f:
            json.dump(self.prod_data, f)

        # Create sample package with existing Facebook & Threads copy
        self.fb_content = "Comfort Test Slippers feature faux fur uppers and soft linings. Link: https://link.amazon/testfix01\n\nThis post contains affiliate links. As an Amazon Associate I earn from qualifying purchases."
        self.th_content = "Comfort Test Slippers with cloud-like memory foam footbed. Link: https://link.amazon/testfix01\n\nThis post contains affiliate links. As an Amazon Associate I earn from qualifying purchases."
        self.pin_img_desc = "Comfort Test Slippers feature faux fur uppers and memory foam. https://link.amazon/testfix01\n\nThis post contains affiliate links. As an Amazon Associate I earn from qualifying purchases."

        self.pkg_data = {
            "asin": self.test_asin,
            "status": "READY_FOR_REVIEW",
            "source": {
                "product_cache": f"data/products/asin_{self.test_asin}.json",
                "affiliate_url": "https://link.amazon/testfix01",
            },
            "platforms": {
                "facebook": {
                    "status": "VALID",
                    "content": self.fb_content,
                    "variants": {
                        "image": {"content": self.fb_content},
                        "video": {"content": self.fb_content},
                    },
                },
                "threads": {
                    "status": "VALID",
                    "content": self.th_content,
                    "variants": {
                        "image": {"content": self.th_content},
                        "video": {"content": self.th_content},
                    },
                },
                "pinterest": {
                    "status": "VALID",
                    "title": "Comfort Test Slippers",
                    "description": self.pin_img_desc,
                    "variants": {
                        "image": {
                            "title": "Comfort Test Slippers",
                            "description": self.pin_img_desc,
                        },
                        "video": {
                            "title": "Comfort Test Slippers",
                            "description": self.pin_img_desc,
                        },
                    },
                },
            },
        }
        with open(self.pkg_file, "w", encoding="utf-8") as f:
            json.dump(self.pkg_data, f)

    def tearDown(self):
        if self.prod_file.exists():
            self.prod_file.unlink()
        if self.pkg_file.exists():
            self.pkg_file.unlink()
        # Clean up any queue items created for test ASIN
        if QUEUE_FILE.exists():
            try:
                q = PublishingQueue.load(QUEUE_FILE)
                orig_len = len(q.items)
                q.items = [it for it in q.items if it.asin != self.test_asin]
                if len(q.items) != orig_len:
                    q.recalculate_counts()
                    q.save(QUEUE_FILE)
            except Exception:
                pass

    def test_pinterest_video_copy_separation(self):
        """Issue 1: Pinterest video copy generates separate text for dailyideasforyou and LifeModeHQ, preserving FB/Threads."""
        with open(self.pkg_file, "r", encoding="utf-8") as f:
            pkg = json.load(f)

        ensure_pinterest_video_variants(pkg)

        # 1. FB and Threads are preserved
        self.assertEqual(pkg["platforms"]["facebook"]["content"], self.fb_content)
        self.assertEqual(pkg["platforms"]["threads"]["content"], self.th_content)

        # 2. Both Pinterest accounts exist
        self.assertIn("pinterest", pkg["platforms"])
        self.assertIn("pinterest_lifemodehq", pkg["platforms"])

        pin_d = pkg["platforms"]["pinterest"]
        pin_lm = pkg["platforms"]["pinterest_lifemodehq"]

        # 3. Both have video variants
        vid_d = pin_d["variants"]["video"]["description"]
        vid_lm = pin_lm["variants"]["video"]["description"]

        self.assertTrue(bool(vid_d))
        self.assertTrue(bool(vid_lm))

        # 4. The two Pinterest video descriptions are genuinely different
        self.assertNotEqual(vid_d, vid_lm)

        # 5. Both preserve affiliate URL
        self.assertIn("https://link.amazon/testfix01", vid_d)
        self.assertIn("https://link.amazon/testfix01", vid_lm)

    def test_image_queue_entry_creation_and_video_preservation(self):
        """Issue 2: Image queue entry is created in Slot 1 and existing video item in Slot 2 is preserved."""
        # Setup: Add a video item to the queue for this ASIN in Slot 2
        queue = PublishingQueue.load(QUEUE_FILE)
        video_item = QueueItem(
            queue_item_id=f"item_test_vid_{self.test_asin}",
            original_url="https://link.amazon/testfix01",
            asin=self.test_asin,
            product_name="Comfort Test Slippers",
            scheduled_date="2026-11-08",
            daily_slot=2,
            scheduled_at="2026-11-08T21:00:00+01:00",
            status=STATUS_READY,
            validation_status=STATUS_READY_FOR_REVIEW,
            media_type="video",
            video_url="https://example.com/video.mp4",
        )
        queue.items.append(video_item)
        queue.save(QUEUE_FILE)

        # Now call ensure_image_queue_entry
        res = ensure_image_queue_entry(self.test_asin)
        self.assertTrue(res["success"])
        self.assertTrue(res["created"])
        self.assertEqual(res["daily_slot"], 1)

        # Reload queue and verify both items exist
        q_reloaded = PublishingQueue.load(QUEUE_FILE)
        items_for_asin = [it for it in q_reloaded.items if it.asin == self.test_asin]
        self.assertEqual(len(items_for_asin), 2)

        slot1_item = next(it for it in items_for_asin if it.daily_slot == 1)
        slot2_item = next(it for it in items_for_asin if it.daily_slot == 2)

        self.assertEqual(slot1_item.media_type, "image")
        self.assertEqual(slot2_item.media_type, "video")
        self.assertEqual(slot2_item.queue_item_id, f"item_test_vid_{self.test_asin}")

        # Idempotency: Calling again does NOT create a duplicate
        res2 = ensure_image_queue_entry(self.test_asin)
        self.assertTrue(res2["success"])
        self.assertFalse(res2["created"])

        q_reloaded2 = PublishingQueue.load(QUEUE_FILE)
        items_for_asin2 = [it for it in q_reloaded2.items if it.asin == self.test_asin]
        self.assertEqual(len(items_for_asin2), 2)

    def test_delete_product_service(self):
        """Issue 3: Delete product cleanly removes product files and pending queue items."""
        # Add a pending queue item
        queue = PublishingQueue.load(QUEUE_FILE)
        queue.items.append(QueueItem(
            queue_item_id=f"item_del_test_{self.test_asin}",
            original_url="https://link.amazon/testfix01",
            asin=self.test_asin,
            product_name="Comfort Test Slippers",
            scheduled_date="2026-11-09",
            daily_slot=1,
            scheduled_at="2026-11-09T15:00:00+01:00",
            status=STATUS_READY,
        ))
        queue.save(QUEUE_FILE)

        res = delete_product_service(self.test_asin)
        self.assertTrue(res["success"])
        self.assertFalse(self.prod_file.exists())
        self.assertFalse(self.pkg_file.exists())

        # Queue item should be removed
        q_after = PublishingQueue.load(QUEUE_FILE)
        matching = [it for it in q_after.items if it.asin == self.test_asin]
        self.assertEqual(len(matching), 0)

    def test_get_all_products_displays_separate_image_and_video_rows(self):
        """Verify get_all_products() shows separate rows for image and video queue entries of the same ASIN."""
        queue = PublishingQueue.load(QUEUE_FILE)
        queue.items.append(QueueItem(
            queue_item_id=f"item_img_{self.test_asin}",
            original_url="https://link.amazon/testfix01",
            asin=self.test_asin,
            product_name="Comfort Test Slippers",
            scheduled_date="2026-11-09",
            daily_slot=1,
            scheduled_at="2026-11-09T15:00:00+01:00",
            status=STATUS_READY,
            media_type="image",
        ))
        queue.items.append(QueueItem(
            queue_item_id=f"item_vid_{self.test_asin}",
            original_url="https://link.amazon/testfix01",
            asin=self.test_asin,
            product_name="Comfort Test Slippers",
            scheduled_date="2026-11-09",
            daily_slot=2,
            scheduled_at="2026-11-09T21:00:00+01:00",
            status=STATUS_READY,
            media_type="video",
            video_url="https://pub-test.r2.dev/video.mp4",
        ))
        queue.save(QUEUE_FILE)

        from web.services import get_all_products
        prods = get_all_products()
        test_rows = [p for p in prods if p["asin"] == self.test_asin]
        self.assertEqual(len(test_rows), 2)

        img_row = next(p for p in test_rows if p["media_type"] == "image")
        vid_row = next(p for p in test_rows if p["media_type"] == "video")

        self.assertEqual(img_row["daily_slot"], 1)
        self.assertEqual(img_row["scheduled_at"], "2026-11-09T15:00:00+01:00")
        self.assertEqual(img_row["video_url"], "")

        self.assertEqual(vid_row["daily_slot"], 2)
        self.assertEqual(vid_row["scheduled_at"], "2026-11-09T21:00:00+01:00")
        self.assertEqual(vid_row["video_url"], "https://pub-test.r2.dev/video.mp4")


if __name__ == "__main__":
    unittest.main()

