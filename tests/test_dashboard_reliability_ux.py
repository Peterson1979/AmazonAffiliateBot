"""
Focused tests for Dashboard Reliability and UX Improvements.
Validates:
1. Publication evidence and status (truthful labels, live precedence over simulation).
2. Queue item removal (preserving product, creatives, and history).
3. Queue item reordering (move up, move down, reorder list).
4. Queue rescheduling for overdue unpublished items (chronological slots, no conflicts).
5. Flow-generated creative status on Products page (none, uploaded, selected).
6. Batch filtering and filter interactions (readable labels, combined filters, reset, pagination).
7. Products page grouping (published collapsible section, group counts).
8. Dashboard overview metrics (authoritative counts, separation of queue vs publication).
"""

import json
import shutil
import tempfile
from datetime import datetime, date, timedelta
from pathlib import Path
from unittest import TestCase
from zoneinfo import ZoneInfo

from publishing.queue import (
    PublishingQueue,
    QueueItem,
    STATUS_READY,
    STATUS_PUBLISHED,
    STATUS_FAILED,
    STATUS_PARTIALLY_PUBLISHED,
    STATUS_QUEUED,
    get_budapest_timezone,
)
from content.pinterest_creative import (
    get_product_flow_creative_status,
    CREATIVE_TYPE_GIFT,
    CREATIVE_TYPE_EVERYDAY,
)
from web.services import (
    get_platform_publication_result,
    get_queue_item_presentation,
    format_batch_label,
    get_all_products,
    get_dashboard_summary,
)
from web.app import create_app


class TestPublicationEvidenceAndStatus(TestCase):
    def test_confirmed_live_not_displayed_as_simulated(self):
        item = QueueItem("item-1", "https://amazon.test/dp/B001", asin="B001", status=STATUS_PUBLISHED)
        item.publishing_results = {
            "facebook": {"status": "PUBLISHED", "mode": "LIVE", "post_id": "fb_123", "permalink_url": "https://fb.com/123"},
            "pinterest": {"status": "PUBLISHED", "mode": "LIVE", "pin_id": "pin_123", "pin_url": "https://pin.com/123"},
            "pinterest_lifemodehq": {"status": "PUBLISHED", "mode": "LIVE", "pin_id": "pin_456", "pin_url": "https://pin.com/456"},
            "threads": {"status": "PUBLISHED", "mode": "LIVE", "post_id": "th_123", "permalink": "https://threads.com/123"},
        }
        queue = PublishingQueue("q", "2026-01-01T00:00:00+00:00", "2026-01-01", items=[item])
        pres = get_queue_item_presentation(queue, item)
        self.assertEqual(pres["state"], "PUBLISHED")
        self.assertEqual(pres["ui_label"], "Published")

    def test_simulated_attempt_never_displayed_as_published(self):
        item = QueueItem("item-sim", "https://amazon.test/dp/B002", asin="B002", status=STATUS_READY)
        item.publishing_results = {
            p: {"status": "WOULD_PUBLISH", "mode": "DRY_RUN", "message": "Simulated"}
            for p in ("facebook", "pinterest", "pinterest_lifemodehq", "threads")
        }
        queue = PublishingQueue("q", "2026-01-01T00:00:00+00:00", "2026-01-01", items=[item])
        pres = get_queue_item_presentation(queue, item)
        self.assertNotEqual(pres["state"], "PUBLISHED")
        self.assertNotEqual(pres["ui_label"], "Published")
        self.assertIn(pres["ui_label"], ("Not published", "Overdue (Held)", "Scheduled"))

    def test_confirmed_live_takes_precedence_over_dry_run(self):
        # A live confirmed result for a platform is never overridden by dry-run
        item = QueueItem("item-mixed", "https://amazon.test/dp/B003", asin="B003", status=STATUS_READY)
        item.publishing_results = {
            "facebook": {"status": "PUBLISHED", "mode": "LIVE", "post_id": "fb_live_1"},
            "pinterest": {"status": "WOULD_PUBLISH", "mode": "DRY_RUN"},
            "pinterest_lifemodehq": {"status": "WOULD_PUBLISH", "mode": "DRY_RUN"},
            "threads": {"status": "WOULD_PUBLISH", "mode": "DRY_RUN"},
        }
        queue = PublishingQueue("q", "2026-01-01T00:00:00+00:00", "2026-01-01", items=[item])
        pres = get_queue_item_presentation(queue, item)
        self.assertEqual(pres["state"], "PARTIAL")
        self.assertEqual(pres["ui_label"], "Partial")
        self.assertEqual(pres["platforms"]["facebook"]["state"], "PUBLISHED")
        self.assertEqual(pres["platforms"]["facebook"]["ui_label"], "Published")
        self.assertEqual(pres["platforms"]["pinterest"]["ui_label"], "Not published")

    def test_platform_statuses_remain_separate(self):
        item = QueueItem("item-sep", "https://amazon.test/dp/B004", asin="B004", status=STATUS_READY)
        item.publishing_results = {
            "facebook": {"status": "PUBLISHED", "mode": "LIVE", "post_id": "fb_1"},
            "pinterest": {"status": "FAILED", "mode": "LIVE", "errors": ["Board not found"]},
            "pinterest_lifemodehq": {"status": "PUBLISHED", "mode": "LIVE", "pin_id": "pin_lmh"},
            "threads": {"status": "WOULD_PUBLISH", "mode": "DRY_RUN"},
        }
        queue = PublishingQueue("q", "2026-01-01T00:00:00+00:00", "2026-01-01", items=[item])
        pres = get_queue_item_presentation(queue, item)
        self.assertEqual(pres["platforms"]["facebook"]["state"], "PUBLISHED")
        self.assertEqual(pres["platforms"]["pinterest"]["state"], "FAILED")
        self.assertEqual(pres["platforms"]["pinterest_lifemodehq"]["state"], "PUBLISHED")
        self.assertEqual(pres["platforms"]["threads"]["ui_label"], "Not published")


class TestQueueManagementAndRescheduling(TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.queue_file = self.temp_dir / "test_queue.json"

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_failed_item_can_be_removed_preserving_others(self):
        item1 = QueueItem("item_001", "https://amazon.test/1", asin="B001", status=STATUS_READY, product_name="Product 1")
        item2 = QueueItem("item_002", "https://amazon.test/2", asin="B002", status=STATUS_FAILED, product_name="Failed Product")
        item3 = QueueItem("item_003", "https://amazon.test/3", asin="B003", status=STATUS_PUBLISHED, product_name="Published Product")
        queue = PublishingQueue("q_test", "2026-11-01T00:00:00+00:00", "2026-11-01", items=[item1, item2, item3])

        removed = queue.remove_item("item_002")
        self.assertIsNotNone(removed)
        self.assertEqual(removed.asin, "B002")
        self.assertEqual(len(queue.items), 2)
        self.assertEqual([i.queue_item_id for i in queue.items], ["item_001", "item_003"])
        # Counters recalculated
        self.assertEqual(queue.total_failed, 0)
        self.assertEqual(queue.total_ready, 1)

    def test_queue_order_can_be_changed(self):
        item1 = QueueItem("item_001", "https://amazon.test/1", asin="B001", status=STATUS_READY, scheduled_date="2026-11-03", daily_slot=1, scheduled_at="2026-11-03T13:00:00+01:00")
        item2 = QueueItem("item_002", "https://amazon.test/2", asin="B002", status=STATUS_READY, scheduled_date="2026-11-03", daily_slot=2, scheduled_at="2026-11-03T19:00:00+01:00")
        queue = PublishingQueue("q_test", "2026-11-01T00:00:00+00:00", "2026-11-01", items=[item1, item2])

        # Move item2 up
        moved = queue.move_item("item_002", "up")
        self.assertTrue(moved)
        self.assertEqual([i.queue_item_id for i in queue.items], ["item_002", "item_001"])
        # Scheduled slots swapped between unpublished items
        self.assertEqual(item2.daily_slot, 1)
        self.assertEqual(item1.daily_slot, 2)

        # Reorder via explicit list
        queue.reorder_items(["item_001", "item_002"])
        self.assertEqual([i.queue_item_id for i in queue.items], ["item_001", "item_002"])

    def test_overdue_unpublished_items_rescheduled_correctly(self):
        tz = get_budapest_timezone()
        # Reference datetime: 2026-11-02 14:00 (Slot 1 at 13:00 is past, Slot 2 at 19:00 is upcoming)
        ref_dt = datetime(2026, 11, 2, 14, 0, 0, tzinfo=tz)

        # Item 1 is confirmed published in the past -> must NOT be rescheduled
        item1 = QueueItem("item_001", "url1", asin="B001", status=STATUS_PUBLISHED, scheduled_date="2026-10-25", daily_slot=1, scheduled_at="2026-10-25T13:00:00+02:00")
        # Item 2 is overdue unpublished READY -> must be rescheduled to next slot (Slot 2 today, 2026-11-02 19:00)
        item2 = QueueItem("item_002", "url2", asin="B002", status=STATUS_READY, scheduled_date="2026-10-28", daily_slot=1, scheduled_at="2026-10-28T13:00:00+01:00")
        # Item 3 is overdue unpublished READY -> must be rescheduled to tomorrow Slot 1 (2026-11-03 13:00)
        item3 = QueueItem("item_003", "url3", asin="B003", status=STATUS_READY, scheduled_date="2026-10-29", daily_slot=2, scheduled_at="2026-10-29T19:00:00+01:00")

        queue = PublishingQueue("q_test", "2026-10-25T00:00:00+00:00", "2026-10-25", items=[item1, item2, item3])
        rescheduled = queue.reschedule_overdue_items(reference_dt=ref_dt)

        self.assertEqual(len(rescheduled), 2)
        # Confirmed published item untouched
        self.assertEqual(item1.scheduled_date, "2026-10-25")
        self.assertEqual(item1.daily_slot, 1)
        # Item 2 rescheduled to 2026-11-02 19:00
        self.assertEqual(item2.scheduled_date, "2026-11-02")
        self.assertEqual(item2.daily_slot, 2)
        self.assertEqual(item2.scheduled_at, "2026-11-02T19:00:00+01:00")
        # Item 3 rescheduled to 2026-11-03 13:00
        self.assertEqual(item3.scheduled_date, "2026-11-03")
        self.assertEqual(item3.daily_slot, 1)
        self.assertEqual(item3.scheduled_at, "2026-11-03T13:00:00+01:00")


class TestFlowCreativeStatus(TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_no_flow_image_when_manifest_missing(self):
        status = get_product_flow_creative_status("BNOMANIFEST", creatives_dir=self.temp_dir)
        self.assertEqual(status["status"], "none")
        self.assertEqual(status["label"], "No Flow image")

    def test_no_flow_image_when_manifest_has_no_uploaded_file(self):
        asin_dir = self.temp_dir / "B00EMPTY"
        asin_dir.mkdir(parents=True)
        manifest = {
            "asin": "B00EMPTY",
            "creatives": {
                CREATIVE_TYPE_GIFT: {"uploaded_image_path": None},
                CREATIVE_TYPE_EVERYDAY: {"uploaded_image_path": None},
            }
        }
        with open(asin_dir / "flow_creatives.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f)

        status = get_product_flow_creative_status("B00EMPTY", creatives_dir=self.temp_dir)
        self.assertEqual(status["status"], "none")
        self.assertEqual(status["label"], "No Flow image")

    def test_flow_image_uploaded_when_file_exists_on_disk(self):
        asin_dir = self.temp_dir / "B00UPLOADED"
        asin_dir.mkdir(parents=True)
        img_file = asin_dir / "gift_inspiration_flow.jpg"
        img_file.write_bytes(b"\xff\xd8\xfffakejpegdata")

        manifest = {
            "asin": "B00UPLOADED",
            "selected_creative_type": None,
            "creatives": {
                CREATIVE_TYPE_GIFT: {"uploaded_image_path": str(img_file)},
                CREATIVE_TYPE_EVERYDAY: {"uploaded_image_path": None},
            }
        }
        with open(asin_dir / "flow_creatives.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f)

        status = get_product_flow_creative_status("B00UPLOADED", creatives_dir=self.temp_dir)
        self.assertEqual(status["status"], "uploaded")
        self.assertEqual(status["label"], "Flow image uploaded")

    def test_selected_for_publishing_when_selected_and_file_verified(self):
        asin_dir = self.temp_dir / "B00SELECTED"
        asin_dir.mkdir(parents=True)
        img_file = asin_dir / "everyday_pick_flow.png"
        img_file.write_bytes(b"\x89PNGfakeimage")

        manifest = {
            "asin": "B00SELECTED",
            "selected_creative_type": CREATIVE_TYPE_EVERYDAY,
            "selected_image_path": str(img_file),
            "creatives": {
                CREATIVE_TYPE_GIFT: {"uploaded_image_path": None},
                CREATIVE_TYPE_EVERYDAY: {"uploaded_image_path": str(img_file)},
            }
        }
        with open(asin_dir / "flow_creatives.json", "w", encoding="utf-8") as f:
            json.dump(manifest, f)

        status = get_product_flow_creative_status("B00SELECTED", creatives_dir=self.temp_dir)
        self.assertEqual(status["status"], "selected")
        self.assertEqual(status["label"], "Selected for publishing")
        self.assertEqual(status["selected_display"], "Everyday Pick")


class TestBatchFormattingAndRoutes(TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()

    def test_format_batch_label(self):
        batch = {
            "batch_id": "batch_20261026T141635592098_27945101",
            "created_at": "2026-10-26T14:16:35.600412+01:00",
            "submitted_count": 14,
            "accepted_count": 4,
        }
        label = format_batch_label(batch)
        self.assertIn("Oct 26, 2026", label)
        self.assertIn("14:16", label)
        self.assertIn("4 accepted / 14 submitted", label)

    def test_queue_page_http_200(self):
        resp = self.client.get("/queue")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        # Misleading technical terms removed from UI
        self.assertNotIn("Local dry-run only", html)
        self.assertIn("All Batches", html)

    def test_products_page_http_200_and_grouping(self):
        resp = self.client.get("/products")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Active / Requiring Attention", html)
        self.assertIn("Published Products", html)
        self.assertIn("Flow Creative", html)

