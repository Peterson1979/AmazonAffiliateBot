"""
Comprehensive Unit and Integration Tests for Publishing Queue Architecture.
Tests URL parsing, deduplication, product isolation, deterministic date/slot assignment,
30-day capacity, insufficient product handling, failed product handling, and persistence.
"""

import sys
import unittest
from pathlib import Path
import json
import tempfile
import shutil
from datetime import datetime, date, timedelta, timezone

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from zoneinfo import ZoneInfo

from amazon.scraper import Product
from content.package import ContentPackage, STATUS_READY_FOR_REVIEW, STATUS_FAILED_VALIDATION
from publishing.queue import (
    parse_affiliate_urls,
    QueueItem,
    PublishingQueue,
    QueueBuilder,
    QueuePublisher,
    get_budapest_timezone,
    SLOT_1_TIME,
    SLOT_2_TIME,
    STATUS_QUEUED,
    STATUS_READY,
    STATUS_FAILED,
    STATUS_PUBLISHED,
    STATUS_PARTIALLY_PUBLISHED,
)
from publishing.publisher import (
    SocialPublisher,
    PublishingConfig,
    SUPPORTED_PLATFORMS,
    STATUS_WOULD_PUBLISH,
    STATUS_FAILED as PLATFORM_STATUS_FAILED,
)
from publishing.config import DEFAULT_TARGET_ACCOUNTS


class TestPublishingQueue(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.queue_file = self.temp_dir / "test_queue.json"
        self.input_file = self.temp_dir / "test_urls.txt"

    def tearDown(self):
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)

    # ==================== 1. URL LIST PARSING & DEDUPLICATION ====================

    def test_url_parsing_ignores_blanks_and_comments(self):
        """Ignores blank lines and comment lines starting with '#'."""
        content = """
        # Top recommended tech products
        https://link.amazon/tech01

        # Camping gear
        https://link.amazon/camp02
        
        # Another section
        https://link.amazon/home03
        """
        with open(self.input_file, "w", encoding="utf-8") as f:
            f.write(content)

        urls = parse_affiliate_urls(self.input_file)
        self.assertEqual(len(urls), 3)
        self.assertEqual(urls[0], "https://link.amazon/tech01")
        self.assertEqual(urls[1], "https://link.amazon/camp02")
        self.assertEqual(urls[2], "https://link.amazon/home03")

    def test_url_parsing_exact_preservation(self):
        """Preserves every affiliate URL exactly as supplied without modification."""
        exact_url = "https://www.amazon.com/dp/B01N5IB20Q?tag=mytag-20&linkCode=ogi&th=1&psc=1"
        content = f"# Test\n{exact_url}\n"
        with open(self.input_file, "w", encoding="utf-8") as f:
            f.write(content)

        urls = parse_affiliate_urls(self.input_file)
        self.assertEqual(len(urls), 1)
        self.assertEqual(urls[0], exact_url)

    def test_deterministic_deduplication(self):
        """Deduplicates identical URLs while preserving first appearance order."""
        content = """
        https://link.amazon/prodA
        https://link.amazon/prodB
        https://link.amazon/prodA
        https://link.amazon/prodC
        https://link.amazon/prodB
        https://link.amazon/prodA
        """
        with open(self.input_file, "w", encoding="utf-8") as f:
            f.write(content)

        urls = parse_affiliate_urls(self.input_file)
        self.assertEqual(len(urls), 3)
        self.assertEqual(urls, [
            "https://link.amazon/prodA",
            "https://link.amazon/prodB",
            "https://link.amazon/prodC"
        ])

    # ==================== 2. QUEUE DATA MODEL & SERIALIZATION ====================

    def test_queue_item_serialization_roundtrip(self):
        """QueueItem serializes to and deserializes from dictionary cleanly."""
        item = QueueItem(
            queue_item_id="item_001_B001",
            original_url="https://link.amazon/item1",
            asin="B001",
            product_name="Sample Blender",
            scheduled_date="2026-10-07",
            daily_slot=1,
            package_reference="data/generated/asin_B001_content_package.json",
            status=STATUS_READY,
            validation_status=STATUS_READY_FOR_REVIEW,
            errors=[]
        )
        d = item.to_dict()
        item_reconstructed = QueueItem.from_dict(d)
        self.assertEqual(item.queue_item_id, item_reconstructed.queue_item_id)
        self.assertEqual(item.scheduled_date, item_reconstructed.scheduled_date)
        self.assertEqual(item.daily_slot, item_reconstructed.daily_slot)
        self.assertEqual(item.status, item_reconstructed.status)

    def test_publishing_queue_save_and_load(self):
        """PublishingQueue persists to JSON file and loads back faithfully."""
        queue = PublishingQueue(
            queue_id="queue_20261007_30d",
            created_at=datetime.now(timezone.utc).isoformat(),
            start_date="2026-10-07",
            total_days=30,
            products_per_day=2,
            target_capacity=60,
            total_submitted=2,
            total_processed=2,
            total_ready=2,
            total_failed=0,
            scheduled_posts=2,
            unfilled_slots=58,
            items=[
                QueueItem(
                    queue_item_id="item_001_B001",
                    original_url="https://link.amazon/item1",
                    asin="B001",
                    product_name="Sample Blender",
                    scheduled_date="2026-10-07",
                    daily_slot=1,
                    status=STATUS_READY,
                    validation_status=STATUS_READY_FOR_REVIEW
                ),
                QueueItem(
                    queue_item_id="item_002_B002",
                    original_url="https://link.amazon/item2",
                    asin="B002",
                    product_name="Sample Monitor",
                    scheduled_date="2026-10-07",
                    daily_slot=2,
                    status=STATUS_READY,
                    validation_status=STATUS_READY_FOR_REVIEW
                )
            ]
        )
        saved_path = queue.save(self.queue_file)
        self.assertTrue(saved_path.exists())

        loaded_queue = PublishingQueue.load(self.queue_file)
        self.assertEqual(loaded_queue.queue_id, queue.queue_id)
        self.assertEqual(loaded_queue.total_ready, 2)
        self.assertEqual(len(loaded_queue.items), 2)
        self.assertEqual(loaded_queue.items[0].product_name, "Sample Blender")
        self.assertEqual(loaded_queue.items[1].product_name, "Sample Monitor")

    # ==================== 3. 30-DAY DETERMINISTIC SCHEDULING ====================

    def test_deterministic_date_and_slot_assignment_two_per_day(self):
        """Tests that slots are strictly 2 per day across consecutive dates."""
        slots = QueueBuilder.compute_schedule_slots("2026-10-01", total_days=3, products_per_day=2)
        date_slots = [(s["date"], s["slot"]) for s in slots]
        expected_slots = [
            ("2026-10-01", 1),
            ("2026-10-01", 2),
            ("2026-10-02", 1),
            ("2026-10-02", 2),
            ("2026-10-03", 1),
            ("2026-10-03", 2),
        ]
        self.assertEqual(date_slots, expected_slots)
        self.assertEqual(len(slots), 6)
        self.assertTrue(all(s["timezone"] == "Europe/Budapest" for s in slots))

    def test_full_60_product_30_day_capacity(self):
        """Creates 60 synthetic valid products across 5 categories and builds a full 30-day queue."""
        urls = [f"https://link.amazon/prod_{i:03d}" for i in range(1, 61)]
        
        def mock_processor(url: str):
            idx = int(url.split("_")[1])
            asin = f"B{idx:09d}"
            prod = Product(
                title=f"Synthetic Product {idx} with High Quality Specifications",
                features=[f"Specification {idx} with confirmed measurement"],
                affiliate_url=url,
                asin=asin
            )
            package = {
                "asin": asin,
                "status": STATUS_READY_FOR_REVIEW,
                "platforms": {
                    "x": {"status": "VALID"},
                    "facebook": {"status": "VALID"},
                    "pinterest": {"status": "VALID"},
                    "threads": {"status": "VALID"}
                }
            }
            return True, prod, package, f"data/generated/asin_{asin}_content_package.json", []

        queue = QueueBuilder.build_queue(
            urls=urls,
            start_date="2026-11-01",
            total_days=30,
            products_per_day=2,
            product_processor=mock_processor,
            output_file=self.queue_file
        )

        self.assertEqual(queue.total_submitted, 60)
        self.assertEqual(queue.total_processed, 60)
        self.assertEqual(queue.total_ready, 60)
        self.assertEqual(queue.total_failed, 0)
        self.assertEqual(queue.scheduled_posts, 60)
        self.assertEqual(queue.unfilled_slots, 0)

        # Verify Day 1 slots
        self.assertEqual(queue.items[0].scheduled_date, "2026-11-01")
        self.assertEqual(queue.items[0].daily_slot, 1)
        self.assertEqual(queue.items[1].scheduled_date, "2026-11-01")
        self.assertEqual(queue.items[1].daily_slot, 2)

        # Verify Day 30 slots
        self.assertEqual(queue.items[58].scheduled_date, "2026-11-30")
        self.assertEqual(queue.items[58].daily_slot, 1)
        self.assertEqual(queue.items[59].scheduled_date, "2026-11-30")
        self.assertEqual(queue.items[59].daily_slot, 2)

    # ==================== 4. INSUFFICIENT PRODUCTS & CAPACITY ====================

    def test_insufficient_products_leaves_slots_unfilled_without_fabrication(self):
        """5 valid products submitted for 30-day / 60-slot queue -> 5 scheduled, 55 unfilled slots."""
        urls = [f"https://link.amazon/prod_{i:03d}" for i in range(1, 6)]

        def mock_processor(url: str):
            idx = int(url.split("_")[1])
            asin = f"B{idx:09d}"
            prod = Product(title=f"Product {idx}", features=["Feature 1"], affiliate_url=url, asin=asin)
            package = {"asin": asin, "status": STATUS_READY_FOR_REVIEW}
            return True, prod, package, f"data/generated/asin_{asin}_content_package.json", []

        queue = QueueBuilder.build_queue(
            urls=urls,
            start_date="2026-11-01",
            total_days=30,
            products_per_day=2,
            product_processor=mock_processor,
            output_file=self.queue_file
        )

        self.assertEqual(queue.total_submitted, 5)
        self.assertEqual(queue.total_ready, 5)
        self.assertEqual(queue.scheduled_posts, 5)
        self.assertEqual(queue.unfilled_slots, 55)

        # Day 1: 2 items, Day 2: 2 items, Day 3: 1 item
        self.assertEqual(queue.items[0].scheduled_date, "2026-11-01")
        self.assertEqual(queue.items[0].daily_slot, 1)
        self.assertEqual(queue.items[1].scheduled_date, "2026-11-01")
        self.assertEqual(queue.items[1].daily_slot, 2)
        self.assertEqual(queue.items[2].scheduled_date, "2026-11-02")
        self.assertEqual(queue.items[2].daily_slot, 1)
        self.assertEqual(queue.items[3].scheduled_date, "2026-11-02")
        self.assertEqual(queue.items[3].daily_slot, 2)
        self.assertEqual(queue.items[4].scheduled_date, "2026-11-03")
        self.assertEqual(queue.items[4].daily_slot, 1)

    # ==================== 5. FAILED PRODUCT ISOLATION & HANDLING ====================

    def test_failed_product_does_not_consume_slot_or_block_subsequent_products(self):
        """Product 2 fails validation: marked FAILED without slot; Product 3 takes slot (Day 1, Slot 2)."""
        urls = [
            "https://link.amazon/prod_001",
            "https://link.amazon/prod_002",
            "https://link.amazon/prod_003"
        ]

        def mock_processor(url: str):
            idx = int(url.split("_")[1])
            asin = f"B{idx:09d}"
            if idx == 2:
                # Product 2 fails validation
                return False, None, None, None, ["Validation error: unsupported claims."]
            prod = Product(title=f"Product {idx}", features=["Feature 1"], affiliate_url=url, asin=asin)
            package = {"asin": asin, "status": STATUS_READY_FOR_REVIEW}
            return True, prod, package, f"data/generated/asin_{asin}_content_package.json", []

        queue = QueueBuilder.build_queue(
            urls=urls,
            start_date="2026-11-01",
            total_days=30,
            products_per_day=2,
            product_processor=mock_processor,
            output_file=self.queue_file
        )

        self.assertEqual(queue.total_submitted, 3)
        self.assertEqual(queue.total_ready, 2)
        self.assertEqual(queue.total_failed, 1)
        self.assertEqual(queue.scheduled_posts, 2)

        # Product 1 -> Day 1, Slot 1
        self.assertEqual(queue.items[0].status, STATUS_READY)
        self.assertEqual(queue.items[0].scheduled_date, "2026-11-01")
        self.assertEqual(queue.items[0].daily_slot, 1)

        # Product 2 -> FAILED, no slot
        self.assertEqual(queue.items[1].status, STATUS_FAILED)
        self.assertIsNone(queue.items[1].scheduled_date)
        self.assertIsNone(queue.items[1].daily_slot)
        self.assertIn("Validation error", queue.items[1].errors[0])

        # Product 3 -> Day 1, Slot 2
        self.assertEqual(queue.items[2].status, STATUS_READY)
        self.assertEqual(queue.items[2].scheduled_date, "2026-11-01")
        self.assertEqual(queue.items[2].daily_slot, 2)

    # ==================== 6. PRODUCT ISOLATION & NO ASIN CONTAMINATION ====================

    def test_products_maintain_strict_isolation_across_multiple_categories(self):
        """Tests processing across Appliance, Electronics, and Camping products preserves isolated data."""
        products_data = [
            ("https://link.amazon/appliance1", "B0APPL0001", "Coffee Machine with 15 bar pressure"),
            ("https://link.amazon/elec2", "B0ELEC0002", "4K Monitor 27-inch Display"),
            ("https://link.amazon/tent3", "B0TENT0003", "4-Person Waterproof Camping Tent")
        ]

        def isolated_processor(url: str):
            for orig_url, asin, title in products_data:
                if url == orig_url:
                    prod = Product(title=title, features=[title], affiliate_url=orig_url, asin=asin)
                    pkg = {"asin": asin, "status": STATUS_READY_FOR_REVIEW}
                    return True, prod, pkg, f"data/generated/asin_{asin}_content_package.json", []
            return False, None, None, None, ["Not found"]

        urls = [p[0] for p in products_data]
        queue = QueueBuilder.build_queue(
            urls=urls,
            start_date="2026-12-01",
            total_days=5,
            products_per_day=2,
            product_processor=isolated_processor,
            output_file=self.queue_file
        )

        self.assertEqual(len(queue.items), 3)
        self.assertEqual(queue.items[0].asin, "B0APPL0001")
        self.assertEqual(queue.items[0].product_name, "Coffee Machine with 15 bar pressure")
        self.assertEqual(queue.items[1].asin, "B0ELEC0002")
        self.assertEqual(queue.items[1].product_name, "4K Monitor 27-inch Display")
        self.assertEqual(queue.items[2].asin, "B0TENT0003")
        self.assertEqual(queue.items[2].product_name, "4-Person Waterproof Camping Tent")

    # ==================== 7. 6-CATEGORY END-TO-END ISOLATION & SCHEDULING ====================

    def test_six_distinct_categories_isolation_and_slot_assignment(self):
        """
        Comprehensive Multi-Product Verification:
        Tests 6 distinct product categories (Appliance, Electronics, Furniture, Clothing, Camping, Tools).
        Verifies:
        1. Exact URL preservation.
        2. No fact/content cross-contamination across ASINs.
        3. Day 1 (slots 1, 2), Day 2 (slots 1, 2), Day 3 (slots 1, 2).
        4. 6 scheduled posts, 54 unfilled slots out of 60.
        5. Persistence fidelity upon reload.
        """
        categories_data = [
            ("https://link.amazon/appliance_espresso", "B0ESPRESS1", "Espresso Machine with 15 bar pressure", "15 bar extraction pressure"),
            ("https://link.amazon/elec_monitor", "B0MONITOR2", "Computer Monitor 27-Inch IPS Display", "27-inch IPS screen"),
            ("https://link.amazon/furn_desk", "B0DESK0003", "Writing Desk Solid Wood Workstation", "120 cm width solid wood"),
            ("https://link.amazon/cloth_jacket", "B0JACKET04", "Waterproof Rain Jacket with YKK Zipper", "Waterproof nylon shell with YKK zipper"),
            ("https://link.amazon/camp_tent", "B0TENT0005", "4-Person Camping Tent with Rainfly", "3000mm waterproof rainfly"),
            ("https://link.amazon/tool_drill", "B0DRILL006", "Cordless Drill 20V Max with 2 Batteries", "20V motor and 2 batteries"),
        ]

        def multi_category_processor(url: str):
            for orig_url, asin, title, feat in categories_data:
                if url == orig_url:
                    prod = Product(title=title, features=[feat], affiliate_url=orig_url, asin=asin)
                    pkg = {
                        "asin": asin,
                        "status": STATUS_READY_FOR_REVIEW,
                        "platforms": {
                            "x": {"content": f"{title}. {orig_url}"},
                            "facebook": {"content": f"{title}\n\n{feat}\n\n{orig_url}"},
                            "pinterest": {"title": title, "description": f"{feat} {orig_url}"},
                            "threads": {"content": f"{title}. {feat}. {orig_url}"}
                        }
                    }
                    pkg_ref = f"data/generated/asin_{asin}_content_package.json"
                    return True, prod, pkg, pkg_ref, []
            return False, None, None, None, ["Unknown URL"]

        urls = [item[0] for item in categories_data]
        queue = QueueBuilder.build_queue(
            urls=urls,
            start_date="2026-11-01",
            total_days=30,
            products_per_day=2,
            product_processor=multi_category_processor,
            output_file=self.queue_file
        )

        # 1. Metric verifications
        self.assertEqual(queue.total_submitted, 6)
        self.assertEqual(queue.total_processed, 6)
        self.assertEqual(queue.total_ready, 6)
        self.assertEqual(queue.total_failed, 0)
        self.assertEqual(queue.scheduled_posts, 6)
        self.assertEqual(queue.unfilled_slots, 54)
        self.assertEqual(queue.target_capacity, 60)

        # 2. Schedule slot verifications
        # Day 1: Products 0 and 1
        self.assertEqual(queue.items[0].scheduled_date, "2026-11-01")
        self.assertEqual(queue.items[0].daily_slot, 1)
        self.assertEqual(queue.items[0].asin, "B0ESPRESS1")
        self.assertEqual(queue.items[0].original_url, "https://link.amazon/appliance_espresso")

        self.assertEqual(queue.items[1].scheduled_date, "2026-11-01")
        self.assertEqual(queue.items[1].daily_slot, 2)
        self.assertEqual(queue.items[1].asin, "B0MONITOR2")
        self.assertEqual(queue.items[1].original_url, "https://link.amazon/elec_monitor")

        # Day 2: Products 2 and 3
        self.assertEqual(queue.items[2].scheduled_date, "2026-11-02")
        self.assertEqual(queue.items[2].daily_slot, 1)
        self.assertEqual(queue.items[2].asin, "B0DESK0003")
        self.assertEqual(queue.items[2].original_url, "https://link.amazon/furn_desk")

        self.assertEqual(queue.items[3].scheduled_date, "2026-11-02")
        self.assertEqual(queue.items[3].daily_slot, 2)
        self.assertEqual(queue.items[3].asin, "B0JACKET04")
        self.assertEqual(queue.items[3].original_url, "https://link.amazon/cloth_jacket")

        # Day 3: Products 4 and 5
        self.assertEqual(queue.items[4].scheduled_date, "2026-11-03")
        self.assertEqual(queue.items[4].daily_slot, 1)
        self.assertEqual(queue.items[4].asin, "B0TENT0005")
        self.assertEqual(queue.items[4].original_url, "https://link.amazon/camp_tent")

        self.assertEqual(queue.items[5].scheduled_date, "2026-11-03")
        self.assertEqual(queue.items[5].daily_slot, 2)
        self.assertEqual(queue.items[5].asin, "B0DRILL006")
        self.assertEqual(queue.items[5].original_url, "https://link.amazon/tool_drill")

        # 3. Isolation & No cross-contamination checks
        asins = [item.asin for item in queue.items]
        self.assertEqual(len(set(asins)), 6, "All 6 ASINs must be strictly unique!")
        
        urls_in_queue = [item.original_url for item in queue.items]
        self.assertEqual(urls_in_queue, urls, "Original URLs must be preserved in exact order!")

        package_refs = [item.package_reference for item in queue.items]
        for item, pkg_ref in zip(queue.items, package_refs):
            self.assertIn(item.asin, pkg_ref, "Package reference must strictly match item ASIN!")

        # 4. Persistence round-trip verification
        reloaded_queue = PublishingQueue.load(self.queue_file)
        self.assertEqual(reloaded_queue.to_dict(), queue.to_dict())


class TestQueuePublisher(unittest.TestCase):
    """
    Comprehensive tests for the QueuePublisher and Daily Scheduled Publishing pipeline:
    1. Slot 1 = 12:00 Europe/Budapest
    2. Slot 2 = 19:00 Europe/Budapest
    3. Exactly 7-hour separation
    4. Correct IANA Europe/Budapest timezone identifier
    5. DST-safe scheduling across Spring and Autumn transitions
    6. Before 12:00 -> 0 items eligible/published
    7. Between 12:00 and 19:00 -> Only Slot 1 eligible/published
    8. After 19:00 -> Both slots eligible if Slot 1 was unprocessed
    9. Repeated execution -> strictly idempotent (no duplicate publishing)
    10. Queue persistence after publishing
    11. Partial / Total failure handling (never falsely marked successful)
    12. Instagram strictly excluded
    13. DRY_RUN safety (no live network requests)
    """

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.queue_file = self.temp_dir / "test_publishing_queue.json"
        self.package_file_1 = self.temp_dir / "asin_B0ESPRESS1_content_package.json"
        self.package_file_2 = self.temp_dir / "asin_B0MONITOR2_content_package.json"

        # Create valid mock ContentPackage files
        self.valid_pkg_1 = {
            "asin": "B0ESPRESS1",
            "status": STATUS_READY_FOR_REVIEW,
            "platforms": {
                "facebook": {"status": "VALID", "content": "Espresso machine.\n\n15 bar pump.\n\nhttps://link.amazon/1"},
                "pinterest": {"status": "VALID", "title": "Espresso Machine", "description": "15 bar pump https://link.amazon/1", "image_url": "https://m.media-amazon.com/images/I/test1.jpg"},
                "threads": {"status": "VALID", "content": "Espresso machine. 15 bar pump. https://link.amazon/1"},
            }
        }
        self.valid_pkg_2 = {
            "asin": "B0MONITOR2",
            "status": STATUS_READY_FOR_REVIEW,
            "platforms": {
                "facebook": {"status": "VALID", "content": "4K Monitor.\n\n27-inch IPS.\n\nhttps://link.amazon/2"},
                "pinterest": {"status": "VALID", "title": "4K Monitor", "description": "27-inch IPS https://link.amazon/2", "image_url": "https://m.media-amazon.com/images/I/test2.jpg"},
                "threads": {"status": "VALID", "content": "4K Monitor. 27-inch IPS. https://link.amazon/2"},
            }
        }

        with open(self.package_file_1, "w", encoding="utf-8") as f:
            json.dump(self.valid_pkg_1, f)
        with open(self.package_file_2, "w", encoding="utf-8") as f:
            json.dump(self.valid_pkg_2, f)

    def tearDown(self):
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)

    def _create_sample_queue(self, start_date: str = "2026-10-07") -> PublishingQueue:
        """Helper to create a standard 2-product 1-day queue for testing."""
        slots = QueueBuilder.compute_schedule_slots(start_date, total_days=1, products_per_day=2)
        item1 = QueueItem(
            queue_item_id="item_001_B0ESPRESS1",
            original_url="https://link.amazon/espresso",
            asin="B0ESPRESS1",
            product_name="Espresso Machine",
            scheduled_date=slots[0]["date"],
            daily_slot=slots[0]["slot"],
            scheduled_at=slots[0]["scheduled_at"],
            timezone=slots[0]["timezone"],
            package_reference=str(self.package_file_1),
            status=STATUS_READY,
            validation_status=STATUS_READY_FOR_REVIEW
        )
        item2 = QueueItem(
            queue_item_id="item_002_B0MONITOR2",
            original_url="https://link.amazon/monitor",
            asin="B0MONITOR2",
            product_name="Computer Monitor",
            scheduled_date=slots[1]["date"],
            daily_slot=slots[1]["slot"],
            scheduled_at=slots[1]["scheduled_at"],
            timezone=slots[1]["timezone"],
            package_reference=str(self.package_file_2),
            status=STATUS_READY,
            validation_status=STATUS_READY_FOR_REVIEW
        )
        queue = PublishingQueue(
            queue_id="test_queue_001",
            created_at=datetime.now(timezone.utc).isoformat(),
            start_date=start_date,
            total_days=1,
            products_per_day=2,
            target_capacity=2,
            total_submitted=2,
            total_processed=2,
            total_ready=2,
            total_failed=0,
            scheduled_posts=2,
            unfilled_slots=0,
            items=[item1, item2]
        )
        queue.save(self.queue_file)
        return queue

    # ==================== 1. SLOT 1 = 12:00 EUROPE/BUDAPEST ====================

    def test_01_slot_1_is_1200_europe_budapest(self):
        """Slot 1 is scheduled at exactly 12:00 in Europe/Budapest."""
        slots = QueueBuilder.compute_schedule_slots("2026-10-07", total_days=1, products_per_day=2)
        slot1 = slots[0]
        self.assertEqual(slot1["slot"], 1)
        self.assertEqual(slot1["timezone"], "Europe/Budapest")
        self.assertIn("T12:00:00", slot1["scheduled_at"])

        dt1 = QueuePublisher.parse_datetime(slot1["scheduled_at"])
        self.assertEqual(dt1.hour, 12)
        self.assertEqual(dt1.minute, 0)
        self.assertEqual(dt1.second, 0)

    # ==================== 2. SLOT 2 = 19:00 EUROPE/BUDAPEST ====================

    def test_02_slot_2_is_1900_europe_budapest(self):
        """Slot 2 is scheduled at exactly 19:00 in Europe/Budapest."""
        slots = QueueBuilder.compute_schedule_slots("2026-10-07", total_days=1, products_per_day=2)
        slot2 = slots[1]
        self.assertEqual(slot2["slot"], 2)
        self.assertEqual(slot2["timezone"], "Europe/Budapest")
        self.assertIn("T19:00:00", slot2["scheduled_at"])

        dt2 = QueuePublisher.parse_datetime(slot2["scheduled_at"])
        self.assertEqual(dt2.hour, 19)
        self.assertEqual(dt2.minute, 0)
        self.assertEqual(dt2.second, 0)

    # ==================== 3. SEVEN-HOUR SEPARATION ====================

    def test_03_exact_seven_hour_separation(self):
        """Difference between daily Slot 1 (12:00) and Slot 2 (19:00) is strictly 7 hours (25200 seconds)."""
        slots = QueueBuilder.compute_schedule_slots("2026-10-07", total_days=5, products_per_day=2)
        for day_idx in range(5):
            slot1_dt = QueuePublisher.parse_datetime(slots[day_idx * 2]["scheduled_at"])
            slot2_dt = QueuePublisher.parse_datetime(slots[day_idx * 2 + 1]["scheduled_at"])
            separation = slot2_dt - slot1_dt
            self.assertEqual(separation.total_seconds(), 7 * 3600, f"Day {day_idx + 1} separation must be 7 hours!")

    # ==================== 4. CORRECT TIMEZONE IDENTIFIER ====================

    def test_04_correct_timezone_identifier_europe_budapest(self):
        """Scheduled slots and queue items consistently use IANA timezone 'Europe/Budapest'."""
        slots = QueueBuilder.compute_schedule_slots("2026-10-07", total_days=3, products_per_day=2)
        for s in slots:
            self.assertEqual(s["timezone"], "Europe/Budapest")

        queue = self._create_sample_queue()
        for item in queue.items:
            self.assertEqual(item.timezone, "Europe/Budapest")

    # ==================== 5. DST-SAFE SCHEDULING ====================

    def test_05_dst_safe_scheduling_spring_and_autumn(self):
        """
        Tests DST transitions in Europe/Budapest without hardcoded fixed offsets:
        - Spring 2026 transition: Last Sunday of March (2026-03-29).
          2026-03-28 -> CET (UTC+1)
          2026-03-30 -> CEST (UTC+2)
        - Autumn 2026 transition: Last Sunday of October (2026-10-25).
          2026-10-24 -> CEST (UTC+2)
          2026-10-26 -> CET (UTC+1)
        """
        # 1. Spring Transition
        spring_slots = QueueBuilder.compute_schedule_slots("2026-03-28", total_days=3, products_per_day=2)
        # 2026-03-28 Slot 1 (CET, UTC+1)
        dt_pre_spring = QueuePublisher.parse_datetime(spring_slots[0]["scheduled_at"])
        self.assertEqual(dt_pre_spring.utcoffset(), timedelta(hours=1))

        # 2026-03-30 Slot 1 (CEST, UTC+2)
        dt_post_spring = QueuePublisher.parse_datetime(spring_slots[4]["scheduled_at"])
        self.assertEqual(dt_post_spring.utcoffset(), timedelta(hours=2))

        # 2. Autumn Transition
        autumn_slots = QueueBuilder.compute_schedule_slots("2026-10-24", total_days=3, products_per_day=2)
        # 2026-10-24 Slot 1 (CEST, UTC+2)
        dt_pre_autumn = QueuePublisher.parse_datetime(autumn_slots[0]["scheduled_at"])
        self.assertEqual(dt_pre_autumn.utcoffset(), timedelta(hours=2))

        # 2026-10-26 Slot 1 (CET, UTC+1)
        dt_post_autumn = QueuePublisher.parse_datetime(autumn_slots[4]["scheduled_at"])
        self.assertEqual(dt_post_autumn.utcoffset(), timedelta(hours=1))

    # ==================== 6. BEFORE 12:00 -> NEITHER ITEM PUBLISHED ====================

    def test_06_before_1200_neither_item_eligible_nor_published(self):
        """When evaluated before 12:00 (e.g. 11:59), 0 items are due, 0 are published."""
        self._create_sample_queue("2026-10-07")
        report = QueuePublisher.publish_due_items(
            queue_file=self.queue_file,
            now_str="2026-10-07T11:59:00+02:00",
            dry_run=True
        )
        self.assertEqual(report["due_items_count"], 0)
        self.assertEqual(report["published_count"], 0)

        # Verify items remain STATUS_READY
        reloaded = PublishingQueue.load(self.queue_file)
        self.assertEqual(reloaded.items[0].status, STATUS_READY)
        self.assertEqual(reloaded.items[1].status, STATUS_READY)

    # ==================== 7. BETWEEN 12:00 AND 19:00 -> ONLY SLOT 1 ELIGIBLE ====================

    def test_07_between_1200_and_1900_only_slot_1_eligible_and_published(self):
        """
        When evaluated between 12:00 and 19:00 (e.g. 14:30), ONLY Slot 1 is eligible and published.
        Slot 2 remains STATUS_READY and un-published.
        """
        self._create_sample_queue("2026-10-07")
        report = QueuePublisher.publish_due_items(
            queue_file=self.queue_file,
            now_str="2026-10-07T14:30:00+02:00",
            dry_run=True
        )
        self.assertEqual(report["due_items_count"], 1)
        self.assertEqual(report["published_count"], 1)

        reloaded = PublishingQueue.load(self.queue_file)
        self.assertEqual(reloaded.items[0].status, STATUS_PUBLISHED)
        self.assertEqual(reloaded.items[0].daily_slot, 1)
        self.assertIsNotNone(reloaded.items[0].published_at)

        # Slot 2 must NOT be published yet
        self.assertEqual(reloaded.items[1].status, STATUS_READY)
        self.assertEqual(reloaded.items[1].daily_slot, 2)
        self.assertIsNone(reloaded.items[1].published_at)

    # ==================== 8. AFTER 19:00 -> BOTH SLOTS ELIGIBLE IF OVERDUE ====================

    def test_08_after_1900_both_slots_eligible_if_slot_1_unprocessed(self):
        """
        If system was offline until after 19:00 (e.g. 20:15), both Slot 1 and Slot 2 are processed in order.
        """
        self._create_sample_queue("2026-10-07")
        report = QueuePublisher.publish_due_items(
            queue_file=self.queue_file,
            now_str="2026-10-07T20:15:00+02:00",
            dry_run=True
        )
        self.assertEqual(report["due_items_count"], 2)
        self.assertEqual(report["published_count"], 2)

        reloaded = PublishingQueue.load(self.queue_file)
        self.assertEqual(reloaded.items[0].status, STATUS_PUBLISHED)
        self.assertEqual(reloaded.items[1].status, STATUS_PUBLISHED)

    # ==================== 9. REPEATED EXECUTION -> STRICT IDEMPOTENCY ====================

    def test_09_repeated_execution_is_strictly_idempotent(self):
        """
        Running publish_due_items repeatedly never duplicates publications.
        Run 1 at 14:00 -> Slot 1 published
        Run 2 at 14:00 -> 0 items published
        Run 3 at 14:05 -> 0 items published
        Run 4 at 19:30 -> Slot 2 published (Slot 1 not re-published)
        Run 5 at 19:35 -> 0 items published
        """
        self._create_sample_queue("2026-10-07")

        # Run 1: 14:00 -> Publishes Slot 1
        r1 = QueuePublisher.publish_due_items(self.queue_file, now_str="2026-10-07T14:00:00+02:00", dry_run=True)
        self.assertEqual(r1["published_count"], 1)

        # Run 2: 14:00 immediate repeat -> 0 published
        r2 = QueuePublisher.publish_due_items(self.queue_file, now_str="2026-10-07T14:00:00+02:00", dry_run=True)
        self.assertEqual(r2["published_count"], 0)
        self.assertEqual(r2["due_items_count"], 0)

        # Run 3: 14:05 later same window -> 0 published
        r3 = QueuePublisher.publish_due_items(self.queue_file, now_str="2026-10-07T14:05:00+02:00", dry_run=True)
        self.assertEqual(r3["published_count"], 0)

        # Run 4: 19:30 -> Publishes Slot 2 ONLY
        r4 = QueuePublisher.publish_due_items(self.queue_file, now_str="2026-10-07T19:30:00+02:00", dry_run=True)
        self.assertEqual(r4["published_count"], 1)
        self.assertEqual(r4["items_processed"][0]["slot"], 2)

        # Run 5: 19:35 -> 0 published
        r5 = QueuePublisher.publish_due_items(self.queue_file, now_str="2026-10-07T19:35:00+02:00", dry_run=True)
        self.assertEqual(r5["published_count"], 0)
        self.assertEqual(r5["due_items_count"], 0)

    # ==================== 10. QUEUE PERSISTENCE AFTER PUBLISHING ====================

    def test_10_queue_persistence_after_publishing(self):
        """Queue file on disk accurately reflects published state, published_at timestamp, and platform results."""
        self._create_sample_queue("2026-10-07")
        QueuePublisher.publish_due_items(self.queue_file, now_str="2026-10-07T12:30:00+02:00", dry_run=True)

        reloaded = PublishingQueue.load(self.queue_file)
        item = reloaded.items[0]
        self.assertEqual(item.status, STATUS_PUBLISHED)
        self.assertEqual(item.published_at, "2026-10-07T12:30:00+02:00")
        self.assertIsNotNone(item.publishing_results)
        self.assertIn("facebook", item.publishing_results)
        self.assertIn("pinterest", item.publishing_results)
        self.assertIn("threads", item.publishing_results)

    # ==================== 11. FAILED PUBLICATION NOT FALSELY MARKED SUCCESSFUL ====================

    def test_11_failed_publication_does_not_become_falsely_marked_successful(self):
        """
        If a platform adapter fails during publishing:
        - All platforms fail -> status is FAILED (never PUBLISHED)
        - Partial failure (e.g. 1 platform fails) -> status is PARTIALLY_PUBLISHED (never PUBLISHED)
        """
        self._create_sample_queue("2026-10-07")

        class MockFailingPublisher:
            def __init__(self, mode="all_fail"):
                self.mode = mode
                self.config = PublishingConfig(dry_run=True)

            def publish_package(self, package, dry_run=True, prior_results=None):
                if self.mode == "all_fail":
                    return {
                        "mode": "DRY_RUN",
                        "status": "FAILED",
                        "results": {
                            "facebook": {"status": PLATFORM_STATUS_FAILED, "errors": ["Network error"]},
                            "pinterest": {"status": PLATFORM_STATUS_FAILED, "errors": ["Network error"]},
                            "threads": {"status": PLATFORM_STATUS_FAILED, "errors": ["Network error"]},
                        }
                    }
                else:  # partial failure
                    return {
                        "mode": "DRY_RUN",
                        "status": "FAILED",
                        "results": {
                            "facebook": {"status": STATUS_WOULD_PUBLISH},
                            "pinterest": {"status": PLATFORM_STATUS_FAILED, "errors": ["Pinterest API down"]},
                            "threads": {"status": STATUS_WOULD_PUBLISH},
                        }
                    }

        # Test Total Failure
        fail_pub = MockFailingPublisher("all_fail")
        report_fail = QueuePublisher.publish_due_items(
            self.queue_file,
            now_str="2026-10-07T12:15:00+02:00",
            publisher=fail_pub
        )
        self.assertEqual(report_fail["failed_count"], 1)
        self.assertEqual(report_fail["published_count"], 0)
        reloaded_fail = PublishingQueue.load(self.queue_file)
        self.assertEqual(reloaded_fail.items[0].status, STATUS_FAILED)

        # Reset item status to READY for partial failure test
        reloaded_fail.items[0].status = STATUS_READY
        reloaded_fail.items[0].errors = []
        reloaded_fail.items[0].publishing_results = None
        reloaded_fail.save(self.queue_file)

        # Test Partial Failure
        partial_pub = MockFailingPublisher("partial_fail")
        report_partial = QueuePublisher.publish_due_items(
            self.queue_file,
            now_str="2026-10-07T12:15:00+02:00",
            publisher=partial_pub
        )
        self.assertEqual(report_partial["partially_published_count"], 1)
        self.assertEqual(report_partial["published_count"], 0)
        reloaded_partial = PublishingQueue.load(self.queue_file)
        self.assertEqual(reloaded_partial.items[0].status, STATUS_PARTIALLY_PUBLISHED)
        self.assertIn("Pinterest API down", reloaded_partial.items[0].errors)

    # ==================== 12. INSTAGRAM EXCLUSION ====================

    def test_12_instagram_remains_strictly_excluded(self):
        """Instagram is completely excluded across platforms, routing accounts, and adapters."""
        self.assertNotIn("instagram", SUPPORTED_PLATFORMS)
        self.assertNotIn("instagram", DEFAULT_TARGET_ACCOUNTS)
        pub = SocialPublisher()
        self.assertNotIn("instagram", pub.adapters)

    # ==================== 13. DRY_RUN SAFETY ====================

    def test_13_dry_run_prevents_live_platform_requests(self):
        """In DRY_RUN mode, adapters return STATUS_WOULD_PUBLISH and zero live network calls are made."""
        self._create_sample_queue("2026-10-07")
        report = QueuePublisher.publish_due_items(
            self.queue_file,
            now_str="2026-10-07T12:30:00+02:00",
            dry_run=True
        )
        self.assertEqual(report["mode"], "DRY_RUN")
        reloaded = PublishingQueue.load(self.queue_file)
        results = reloaded.items[0].publishing_results
        for p, r in results.items():
            self.assertEqual(r["status"], STATUS_WOULD_PUBLISH)
            self.assertEqual(r["mode"], "DRY_RUN")

    # ==================== 14. STANDARD ZONEINFO USAGE ====================

    def test_14_standard_zoneinfo_type_and_behavior(self):
        """Verify that get_budapest_timezone() uses Python standard library ZoneInfo('Europe/Budapest')."""
        tz = get_budapest_timezone()
        self.assertIsInstance(tz, ZoneInfo)
        self.assertEqual(tz.key, "Europe/Budapest")

        # Verify summer date (CEST, UTC+2)
        summer_dt = datetime(2026, 7, 15, 12, 0, 0, tzinfo=tz)
        self.assertEqual(summer_dt.utcoffset(), timedelta(hours=2))
        self.assertEqual(summer_dt.tzname(), "CEST")

        # Verify winter date (CET, UTC+1)
        winter_dt = datetime(2026, 1, 15, 12, 0, 0, tzinfo=tz)
        self.assertEqual(winter_dt.utcoffset(), timedelta(hours=1))
        self.assertEqual(winter_dt.tzname(), "CET")

    # ==================== 15. STRUCTURED LOGGING ====================

    def test_15_daily_publisher_logging_creates_file_and_records_events(self):
        """Daily publisher creates and appends structured entries to data/logs/daily_publisher.log."""
        self._create_sample_queue("2026-10-07")
        custom_log_file = self.temp_dir / "test_publisher.log"
        report = QueuePublisher.publish_due_items(
            self.queue_file,
            now_str="2026-10-07T12:30:00+02:00",
            dry_run=True,
            log_file=custom_log_file
        )
        self.assertTrue(custom_log_file.exists())
        log_content = custom_log_file.read_text(encoding="utf-8")
        self.assertIn("DAILY PUBLISHER RUN START", log_content)
        self.assertIn("Europe/Budapest", log_content)
        self.assertIn("item_001_B0ESPRESS1", log_content)
        self.assertIn("Slot 1", log_content)
        self.assertIn("DAILY PUBLISHER RUN END (SUCCESS)", log_content)

    # ==================== 16. OVERDUE RECOVERY ====================

    def test_16_overdue_recovery_when_machine_was_offline(self):
        """
        When machine was offline during Slot 1 (12:00) and starts at 21:00:
        Both Slot 1 and Slot 2 are recognized as due, processed chronologically, and published.
        """
        self._create_sample_queue("2026-10-07")
        report = QueuePublisher.publish_due_items(
            self.queue_file,
            now_str="2026-10-07T21:00:00+02:00",
            dry_run=True
        )
        self.assertEqual(report["due_items_count"], 2)
        self.assertEqual(report["published_count"], 2)

        # Chronological execution order: Slot 1 first, then Slot 2
        self.assertEqual(report["items_processed"][0]["slot"], 1)
        self.assertEqual(report["items_processed"][1]["slot"], 2)

        reloaded = PublishingQueue.load(self.queue_file)
        self.assertEqual(reloaded.items[0].status, STATUS_PUBLISHED)
        self.assertEqual(reloaded.items[1].status, STATUS_PUBLISHED)

    # ==================== 17. MISSING PACKAGE RESILIENCE ====================

    def test_17_missing_content_package_does_not_corrupt_queue(self):
        """
        If Item 1's package file is missing:
        - Item 1 is marked FAILED with clear error
        - Item 2 is still processed and marked PUBLISHED
        - Queue is persisted cleanly
        """
        queue = self._create_sample_queue("2026-10-07")
        # Point Item 1 to nonexistent file
        queue.items[0].package_reference = str(self.temp_dir / "nonexistent_package.json")
        queue.save(self.queue_file)

        report = QueuePublisher.publish_due_items(
            self.queue_file,
            now_str="2026-10-07T20:00:00+02:00",
            dry_run=True
        )
        self.assertEqual(report["due_items_count"], 2)
        self.assertEqual(report["failed_count"], 1)
        self.assertEqual(report["published_count"], 1)

        reloaded = PublishingQueue.load(self.queue_file)
        self.assertEqual(reloaded.items[0].status, STATUS_FAILED)
        self.assertIn("not found", reloaded.items[0].errors[0].lower())
        self.assertEqual(reloaded.items[1].status, STATUS_PUBLISHED)


    # ==================== 18. EXACT 16-URL SOURCE LIST REGRESSION TESTS ====================

    def test_18_all_16_authoritative_urls_accounted_for_exactly_once(self):
        """Authoritative source file contains 16 unique URLs, all accounted for exactly once."""
        from publishing.queue import get_authoritative_urls_file
        auth_file = get_authoritative_urls_file()
        self.assertTrue(auth_file.exists())
        urls = parse_affiliate_urls(auth_file)
        self.assertEqual(len(urls), 16)
        self.assertEqual(len(set(urls)), 16)

    def test_18_resolve_product_for_url_shortlink_isolation(self):
        """Shortlink URLs without inline ASIN resolve reliably to matching cached product without cross-contamination."""
        from publishing.queue import resolve_product_for_url
        test_pdir = self.temp_dir / "products"
        test_pdir.mkdir(parents=True, exist_ok=True)

        # Create two distinct product caches with shortlinks
        prod_a = Product(
            title="Product A Espresso",
            asin="B0AAA11111",
            price="$99.00",
            features=["Feature A1", "Feature A2"],
            affiliate_url="https://link.amazon/shortA"
        )
        prod_b = Product(
            title="Product B Headphones",
            asin="B0BBB22222",
            price="$49.00",
            features=["Feature B1", "Feature B2"],
            affiliate_url="https://link.amazon/shortB"
        )
        with open(test_pdir / "asin_B0AAA11111.json", "w", encoding="utf-8") as f:
            json.dump(prod_a.to_dict(), f)
        with open(test_pdir / "asin_B0BBB22222.json", "w", encoding="utf-8") as f:
            json.dump(prod_b.to_dict(), f)

        # Resolve shortlink A
        ok_a, resolved_a, key_a, errs_a = resolve_product_for_url("https://link.amazon/shortA", products_dir=test_pdir)
        self.assertTrue(ok_a)
        self.assertEqual(resolved_a.asin, "B0AAA11111")
        self.assertEqual(resolved_a.title, "Product A Espresso")
        self.assertEqual(resolved_a.affiliate_url, "https://link.amazon/shortA")

        # Resolve shortlink B
        ok_b, resolved_b, key_b, errs_b = resolve_product_for_url("https://link.amazon/shortB", products_dir=test_pdir)
        self.assertTrue(ok_b)
        self.assertEqual(resolved_b.asin, "B0BBB22222")
        self.assertEqual(resolved_b.title, "Product B Headphones")
        self.assertEqual(resolved_b.affiliate_url, "https://link.amazon/shortB")

        # Ensure no cross-contamination
        self.assertNotEqual(resolved_a.asin, resolved_b.asin)
        self.assertNotEqual(resolved_a.title, resolved_b.title)

    def test_18_unresolved_or_insufficient_product_cannot_become_ready(self):
        """Unresolved ASIN or empty features fail closed and cannot become valid."""
        from publishing.queue import resolve_product_for_url
        test_pdir = self.temp_dir / "products"
        test_pdir.mkdir(parents=True, exist_ok=True)

        # Product with unknown ASIN and empty features (like Alpine fountain)
        prod_unknown = Product(
            title="Alpine Fountain",
            asin="UNKNOWN",
            features=[],
            affiliate_url="https://link.amazon/shortUnknown"
        )
        with open(test_pdir / "asin_UNKNOWN.json", "w", encoding="utf-8") as f:
            json.dump(prod_unknown.to_dict(), f)

        ok, resolved, key, errs = resolve_product_for_url("https://link.amazon/shortUnknown", products_dir=test_pdir)
        self.assertFalse(ok)
        self.assertTrue(len(errs) > 0)
        self.assertIn("unresolvable ASIN", errs[0])

    def test_18_dynamic_queue_counters_derived_from_actual_items(self):
        """Queue summary counters are dynamically recalculated from actual items, not hardcoded."""
        queue = PublishingQueue(
            queue_id="test_counts_queue",
            created_at=datetime.now(timezone.utc).isoformat(),
            start_date="2026-10-11",
            target_capacity=60
        )
        # Add 1 READY, 1 FAILED, 1 PUBLISHED, 1 SKIPPED
        queue.items.append(QueueItem(
            queue_item_id="item_001",
            original_url="https://link.amazon/url1",
            asin="B000000001",
            product_name="Product 1",
            scheduled_at="2026-10-11T12:00:00+02:00",
            status=STATUS_READY,
            validation_status=STATUS_READY_FOR_REVIEW
        ))
        queue.items.append(QueueItem(
            queue_item_id="item_002",
            original_url="https://link.amazon/url2",
            asin="B000000002",
            product_name="Product 2",
            status=STATUS_FAILED,
            validation_status=STATUS_FAILED_VALIDATION
        ))
        queue.items.append(QueueItem(
            queue_item_id="item_003",
            original_url="https://link.amazon/url3",
            asin="B000000003",
            product_name="Product 3",
            status=STATUS_PUBLISHED,
            validation_status=STATUS_READY_FOR_REVIEW
        ))
        queue.items.append(QueueItem(
            queue_item_id="item_004",
            original_url="https://link.amazon/url4",
            asin="B000000004",
            product_name="Product 4",
            status="SKIPPED",
            validation_status=STATUS_FAILED_VALIDATION
        ))

        queue.recalculate_counts()
        summary = queue.summary()
        self.assertEqual(summary["total_processed"], 4)
        self.assertEqual(summary["total_ready"], 1)
        self.assertEqual(summary["total_failed"], 1)
        self.assertEqual(summary["scheduled_posts"], 1)
        self.assertEqual(summary["published_posts"], 1)
        self.assertEqual(summary["unfilled_slots"], 59)


if __name__ == "__main__":
    unittest.main()


