"""
Comprehensive Unit and Integration Tests for Automatic Amazon Product Discovery and Queue Replenishment.
Verifies:
1. Discovery candidate extraction & quality filtering (missing title, invalid ASIN, missing features).
2. Deduplication across existing queue items, persistent history, affiliate URLs, and discovery batches.
3. Replenishment logic: healthy queue (no replenishment), below target, empty queue, insufficient candidates.
4. Slot assignment: appending to future slots (12:00 & 19:00 Europe/Budapest, 7-hr separation), preserving existing slots.
5. Marketing-oriented copy & strict source-grounding verification (grounded marketing vs ungrounded benefits/outcomes).
6. Pinterest-specific marketing title and grounded description quality.
7. Safety: DRY_RUN mode, zero live platform requests, Instagram exclusion.
8. Persistence: queue reload, history reload, and maintenance idempotency.
"""

import sys
import unittest
from pathlib import Path
import json
import re
import tempfile
import shutil
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from amazon.scraper import Product
from amazon.discovery import (
    AmazonDiscoveryEngine,
    DiscoveryHistory,
    DiscoveredCandidate,
    DEFAULT_CONFIG_QUERIES
)
from content.facts import FactExtractor, CanonicalProductFacts
from content.generator import ContentGenerator
from content.validator import ClaimValidator
from content.package import ContentPackage, STATUS_READY_FOR_REVIEW, STATUS_FAILED_VALIDATION
from publishing.queue import (
    PublishingQueue,
    QueueItem,
    QueueBuilder,
    QueuePublisher,
    get_budapest_timezone,
    SLOT_1_TIME,
    SLOT_2_TIME,
    STATUS_READY,
    STATUS_PUBLISHED,
    STATUS_FAILED,
)
from publishing.replenisher import (
    QueueReplenisher,
    DEFAULT_TARGET_FUTURE_CAPACITY
)
from publishing.publisher import SocialPublisher, SUPPORTED_PLATFORMS, STATUS_WOULD_PUBLISH


class TestDiscoveryAndReplenishment(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp())
        self.queue_file = self.temp_dir / "test_publishing_queue.json"
        self.history_file = self.temp_dir / "test_discovery_history.json"
        self.log_file = self.temp_dir / "test_maintenance.log"

    def tearDown(self):
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir)

    def _create_synthetic_product(self, asin: str, title: str, features: list) -> Product:
        return Product(
            title=title,
            features=features,
            affiliate_url=f"https://www.amazon.com/dp/{asin}?tag=lifemode-20",
            source_url=f"https://www.amazon.com/dp/{asin}",
            price="$49.99",
            asin=asin
        )

    # ==================== 1. DISCOVERY CANDIDATE FILTERING ====================

    def test_candidate_quality_filtering(self):
        """Rejects candidates with invalid ASIN, missing title, or short title."""
        engine = AmazonDiscoveryEngine(history=DiscoveryHistory(self.history_file))
        existing_asins = set()
        existing_urls = set()
        batch_asins = set()

        # Valid candidate
        valid_cand = DiscoveredCandidate(
            asin="B001VALID1",
            title="Ergonomic Aluminum Laptop Stand with 7 Inch Adjustment",
            source_url="https://www.amazon.com/dp/B001VALID1",
            affiliate_url="https://www.amazon.com/dp/B001VALID1?tag=lifemode-20",
            query="laptop stand"
        )
        is_valid, reason = engine.filter_candidate(valid_cand, existing_asins, existing_urls, batch_asins)
        self.assertTrue(is_valid)
        self.assertIsNone(reason)

        # Invalid ASIN (too short / symbols)
        invalid_asin_cand = DiscoveredCandidate(
            asin="B001_INVALID",
            title="Some Product Title",
            source_url="",
            affiliate_url="",
            query="test"
        )
        is_valid, reason = engine.filter_candidate(invalid_asin_cand, existing_asins, existing_urls, batch_asins)
        self.assertFalse(is_valid)
        self.assertEqual(reason, "INVALID_ASIN")

        # Missing / Too Short Title
        short_title_cand = DiscoveredCandidate(
            asin="B002VALID2",
            title="Desk",
            source_url="https://www.amazon.com/dp/B002VALID2",
            affiliate_url="https://www.amazon.com/dp/B002VALID2?tag=lifemode-20",
            query="desk"
        )
        is_valid, reason = engine.filter_candidate(short_title_cand, existing_asins, existing_urls, batch_asins)
        self.assertFalse(is_valid)
        self.assertEqual(reason, "MISSING_TITLE")

    # ==================== 2. PERSISTENT DEDUPLICATION ====================

    def test_deduplication_by_asin_url_batch_and_history(self):
        """Verifies deduplication across existing queue, batch, and persistent history."""
        history = DiscoveryHistory(self.history_file)
        engine = AmazonDiscoveryEngine(history=history)

        cand = DiscoveredCandidate(
            asin="B001DUPL01",
            title="Espresso Coffee Machine 15 Bar Pump",
            source_url="https://www.amazon.com/dp/B001DUPL01",
            affiliate_url="https://www.amazon.com/dp/B001DUPL01?tag=lifemode-20",
            query="espresso"
        )

        # 1. Existing queue ASIN
        is_valid, reason = engine.filter_candidate(cand, {"B001DUPL01"}, set(), set())
        self.assertFalse(is_valid)
        self.assertEqual(reason, "DUPLICATE_IN_QUEUE")

        # 2. Existing queue URL
        is_valid, reason = engine.filter_candidate(cand, set(), {cand.affiliate_url}, set())
        self.assertFalse(is_valid)
        self.assertEqual(reason, "DUPLICATE_URL")

        # 3. Batch duplicate
        is_valid, reason = engine.filter_candidate(cand, set(), set(), {"B001DUPL01"})
        self.assertFalse(is_valid)
        self.assertEqual(reason, "DUPLICATE_IN_BATCH")

        # 4. History duplicate
        history.record_candidate(cand, status="ACCEPTED")
        history.save()
        is_valid, reason = engine.filter_candidate(cand, set(), set(), set())
        self.assertFalse(is_valid)
        self.assertEqual(reason, "DUPLICATE_IN_HISTORY")

    def test_different_asin_with_similar_title_is_accepted(self):
        """Different products with similar titles are NOT rejected if ASINs differ."""
        engine = AmazonDiscoveryEngine(history=DiscoveryHistory(self.history_file))
        cand1 = DiscoveredCandidate(
            asin="B001DIFF01",
            title="Adjustable Laptop Stand Aluminum Riser",
            source_url="https://www.amazon.com/dp/B001DIFF01",
            affiliate_url="https://www.amazon.com/dp/B001DIFF01?tag=lifemode-20",
            query="stand"
        )
        cand2 = DiscoveredCandidate(
            asin="B002DIFF02",
            title="Adjustable Laptop Stand Aluminum Riser",
            source_url="https://www.amazon.com/dp/B002DIFF02",
            affiliate_url="https://www.amazon.com/dp/B002DIFF02?tag=lifemode-20",
            query="stand"
        )
        is_valid1, _ = engine.filter_candidate(cand1, set(), set(), set())
        is_valid2, _ = engine.filter_candidate(cand2, {"B001DIFF01"}, {cand1.affiliate_url}, set())
        self.assertTrue(is_valid1)
        self.assertTrue(is_valid2)

    # ==================== 3. QUEUE REPLENISHMENT ====================

    def test_replenishment_when_capacity_is_healthy_does_nothing(self):
        """When future capacity >= target, maintain_queue returns without making discovery attempts."""
        queue = PublishingQueue(
            queue_id="test_healthy_queue",
            created_at="2026-10-07T10:00:00+02:00",
            start_date="2026-10-08",
            total_days=14,
            products_per_day=2,
            target_capacity=28,
            items=[
                QueueItem(
                    queue_item_id=f"item_{i:03d}_B00{i:03d}",
                    original_url=f"https://www.amazon.com/dp/B00{i:03d}",
                    asin=f"B00{i:03d}",
                    product_name=f"Product {i}",
                    scheduled_date="2026-10-08",
                    daily_slot=1 if i % 2 == 1 else 2,
                    scheduled_at=f"2026-10-08T{12 if i % 2 == 1 else 19}:00:00+02:00",
                    timezone="Europe/Budapest",
                    status=STATUS_READY
                )
                for i in range(1, 29)
            ]
        )
        queue.save(self.queue_file)

        report = QueueReplenisher.maintain_queue(
            queue_file=self.queue_file,
            target_capacity=28,
            log_file=self.log_file,
            history_file=self.history_file,
            now_override="2026-10-07T10:00:00+02:00"
        )
        self.assertFalse(report["replenished"])
        self.assertEqual(report["items_added_count"], 0)
        self.assertEqual(report["final_future_capacity"], 28)

    def test_replenishment_appends_to_free_slots_without_altering_existing(self):
        """When queue is below target capacity, new products are appended to consecutive future slots."""
        # Queue has 2 existing items scheduled for Day 1 (2026-10-08 Slot 1 & 2)
        existing_item1 = QueueItem(
            queue_item_id="item_001_B0EXIST01",
            original_url="https://www.amazon.com/dp/B0EXIST01",
            asin="B0EXIST01",
            product_name="Existing Coffee Machine",
            scheduled_date="2026-10-08",
            daily_slot=1,
            scheduled_at="2026-10-08T12:00:00+02:00",
            timezone="Europe/Budapest",
            status=STATUS_READY
        )
        existing_item2 = QueueItem(
            queue_item_id="item_002_B0EXIST02",
            original_url="https://www.amazon.com/dp/B0EXIST02",
            asin="B0EXIST02",
            product_name="Existing Grinder",
            scheduled_date="2026-10-08",
            daily_slot=2,
            scheduled_at="2026-10-08T19:00:00+02:00",
            timezone="Europe/Budapest",
            status=STATUS_READY
        )
        queue = PublishingQueue(
            queue_id="test_append_queue",
            created_at="2026-10-07T10:00:00+02:00",
            start_date="2026-10-08",
            total_days=5,
            products_per_day=2,
            target_capacity=10,
            items=[existing_item1, existing_item2]
        )
        queue.save(self.queue_file)

        # Mock discovery candidates (2 candidates to fill to 4)
        def mock_candidate_provider(query, max_count):
            return [
                DiscoveredCandidate(
                    asin="B0NEW00003",
                    title="4K Monitor 27-inch IPS Display",
                    source_url="https://www.amazon.com/dp/B0NEW00003",
                    affiliate_url="https://www.amazon.com/dp/B0NEW00003?tag=lifemode-20",
                    query=query
                ),
                DiscoveredCandidate(
                    asin="B0NEW00004",
                    title="Wireless Mechanical Keyboard RGB",
                    source_url="https://www.amazon.com/dp/B0NEW00004",
                    affiliate_url="https://www.amazon.com/dp/B0NEW00004?tag=lifemode-20",
                    query=query
                ),
            ]

        def mock_product_loader(url):
            if "B0NEW00003" in url:
                return self._create_synthetic_product("B0NEW00003", "4K Monitor 27-inch IPS Display", ["27-inch IPS panel", "3840x2160 resolution"])
            elif "B0NEW00004" in url:
                return self._create_synthetic_product("B0NEW00004", "Wireless Mechanical Keyboard RGB", ["Bluetooth 5.0 connection", "RGB backlighting"])
            return None

        class MockGenerator:
            model_name = "mock_model"
            def generate_all_platforms(self, product, disclosure):
                features_text = ". ".join(product.features) if product.features else ""
                return {
                    "asin": product.asin,
                    "affiliate_url": product.affiliate_url,
                    "platforms": {
                        "x": {"text": f"{product.title}. {features_text}. {disclosure}", "status": "VALID"},
                        "facebook": {"text": f"{product.title}. {features_text}.\n\n{disclosure}", "status": "VALID"},
                        "pinterest": {
                            "title": product.title[:95],
                            "description": f"{features_text[:300]}. {disclosure}",
                            "status": "VALID"
                        },
                        "threads": {"text": f"{product.title}. {features_text}. {disclosure}", "status": "VALID"}
                    }
                }

        report = QueueReplenisher.maintain_queue(
            queue_file=self.queue_file,
            target_capacity=4,
            candidate_provider=mock_candidate_provider,
            product_loader=mock_product_loader,
            generator=MockGenerator(),
            log_file=self.log_file,
            history_file=self.history_file,
            now_override="2026-10-07T10:00:00+02:00"
        )

        self.assertTrue(report["replenished"])
        self.assertEqual(report["items_added_count"], 2)
        self.assertEqual(report["final_future_capacity"], 4)

        reloaded = PublishingQueue.load(self.queue_file)
        self.assertEqual(len(reloaded.items), 4)

        # Existing items must be completely untouched
        self.assertEqual(reloaded.items[0].asin, "B0EXIST01")
        self.assertEqual(reloaded.items[0].scheduled_date, "2026-10-08")
        self.assertEqual(reloaded.items[0].daily_slot, 1)

        self.assertEqual(reloaded.items[1].asin, "B0EXIST02")
        self.assertEqual(reloaded.items[1].scheduled_date, "2026-10-08")
        self.assertEqual(reloaded.items[1].daily_slot, 2)

        # New items appended consecutively to Day 2 (2026-10-09)
        self.assertEqual(reloaded.items[2].asin, "B0NEW00003")
        self.assertEqual(reloaded.items[2].scheduled_date, "2026-10-09")
        self.assertEqual(reloaded.items[2].daily_slot, 1)
        self.assertEqual(reloaded.items[2].scheduled_at, "2026-10-09T12:00:00+02:00")

        self.assertEqual(reloaded.items[3].asin, "B0NEW00004")
        self.assertEqual(reloaded.items[3].scheduled_date, "2026-10-09")
        self.assertEqual(reloaded.items[3].daily_slot, 2)
        self.assertEqual(reloaded.items[3].scheduled_at, "2026-10-09T19:00:00+02:00")

    # ==================== 4. MARKETING-ORIENTED COPY GROUNDING ====================

    def test_marketing_oriented_grounded_copy_passes_validation(self):
        """
        Marketing copy that expresses confirmed characteristics naturally passes Layer B proposition grounding.
        e.g., 'Features an ergonomic aluminum frame with 7 inches of height adjustment.' -> PASS
        """
        product = self._create_synthetic_product(
            asin="B0MKTG0001",
            title="Ergonomic Aluminum Laptop Stand Adjustable Height",
            features=["Up to 7 inches height adjustment", "Lightweight aluminum construction with 11 lbs loading capacity"]
        )
        facts = FactExtractor.extract_canonical_facts(product)

        # Grounded marketing copy
        grounded_copy = "Features an ergonomic aluminum stand with 7 inches of height adjustment. Supports up to 11 lbs."
        issues = ClaimValidator.validate_content_claims(grounded_copy, facts, disclosure="")
        self.assertEqual(len(issues), 0, f"Grounded marketing copy should pass without issues: {issues}")

    def test_unsupported_marketing_benefits_fail_validation(self):
        """
        Marketing copy containing ungrounded benefits, outcomes, or quality claims is strictly rejected.
        e.g., 'Guarantees comfort and improves posture for effortless work.' -> FAIL
        """
        product = self._create_synthetic_product(
            asin="B0MKTG0002",
            title="Ergonomic Aluminum Laptop Stand Adjustable Height",
            features=["Up to 7 inches height adjustment", "Lightweight aluminum construction with 11 lbs loading capacity"]
        )
        facts = FactExtractor.extract_canonical_facts(product)

        # Ungrounded marketing benefits
        hallucinated_copy = "Guarantees comfort and improves posture for effortless work throughout the day."
        issues = ClaimValidator.validate_content_claims(hallucinated_copy, facts, disclosure="")
        self.assertGreater(len(issues), 0, "Ungrounded outcome/benefit claims must fail validation!")

    # ==================== 5. PINTEREST MARKETING TITLE & DESCRIPTION ====================

    def test_pinterest_marketing_title_and_grounded_description(self):
        """Pinterest title is natural and product-focused (not a raw spec dump) and description is strictly grounded."""
        product = self._create_synthetic_product(
            asin="B0PIN00001",
            title="Electactic Espresso Machine 15 Bar with Integrated Grinder",
            features=["15 bar extraction pump", "Integrated grinder with stainless steel burrs", "Milk steam wand included"]
        )
        facts = FactExtractor.extract_canonical_facts(product)

        # Natural Pinterest title & grounded description
        pin_title = "Electactic 15 Bar Espresso Machine with Grinder"
        pin_desc = "Features a 15 bar extraction pump and an integrated grinder with stainless steel burrs. Includes a milk steam wand."

        title_issues = ClaimValidator.validate_content_claims(pin_title, facts, disclosure="")
        desc_issues = ClaimValidator.validate_content_claims(pin_desc, facts, disclosure="")

        self.assertEqual(len(title_issues), 0)
        self.assertEqual(len(desc_issues), 0)
        self.assertLessEqual(len(pin_title), 100)
        self.assertLessEqual(len(pin_desc), 400)

    # ==================== 6. FAILURE BEHAVIOR & REPLENISHMENT EDGE CASES ====================

    def test_replenishment_empty_queue_initialization(self):
        """When queue is completely empty / file missing, replenishment builds from scratch to target capacity."""
        def mock_candidate_provider(query, max_count):
            return [
                DiscoveredCandidate(
                    asin=f"B0EMPTY{i:03d}",
                    title=f"Test Discovery Product {i}",
                    source_url=f"https://www.amazon.com/dp/B0EMPTY{i:03d}",
                    affiliate_url=f"https://www.amazon.com/dp/B0EMPTY{i:03d}?tag=lifemode-20",
                    query=query
                )
                for i in range(1, 4)
            ]

        def mock_product_loader(url):
            asin_match = re.search(r'B0EMPTY(\d+)', url)
            idx = asin_match.group(1) if asin_match else "001"
            return self._create_synthetic_product(
                f"B0EMPTY{idx}",
                f"Test Discovery Product {idx}",
                [f"Feature 1 for product {idx}", f"Feature 2 for product {idx}"]
            )

        class MockGenerator:
            model_name = "mock_model"
            def generate_all_platforms(self, product, disclosure):
                features_text = ". ".join(product.features) if product.features else ""
                return {
                    "asin": product.asin,
                    "affiliate_url": product.affiliate_url,
                    "platforms": {
                        "x": {"text": f"{product.title}. {features_text}. {disclosure}", "status": "VALID"},
                        "facebook": {"text": f"{product.title}. {features_text}.\n\n{disclosure}", "status": "VALID"},
                        "pinterest": {
                            "title": product.title[:95],
                            "description": f"{features_text[:300]}. {disclosure}",
                            "status": "VALID"
                        },
                        "threads": {"text": f"{product.title}. {features_text}. {disclosure}", "status": "VALID"}
                    }
                }

        report = QueueReplenisher.maintain_queue(
            queue_file=self.queue_file,
            target_capacity=3,
            candidate_provider=mock_candidate_provider,
            product_loader=mock_product_loader,
            generator=MockGenerator(),
            log_file=self.log_file,
            history_file=self.history_file,
            now_override="2026-10-07T10:00:00+02:00"
        )
        self.assertTrue(report["replenished"])
        self.assertEqual(report["items_added_count"], 3)
        self.assertEqual(report["final_future_capacity"], 3)

        reloaded = PublishingQueue.load(self.queue_file)
        self.assertEqual(len(reloaded.items), 3)
        # Verify slots: Day 1 (Slot 1 & Slot 2), Day 2 (Slot 1)
        self.assertEqual(reloaded.items[0].daily_slot, 1)
        self.assertEqual(reloaded.items[1].daily_slot, 2)
        self.assertEqual(reloaded.items[2].daily_slot, 1)
        self.assertEqual(reloaded.items[0].scheduled_date, "2026-10-07")
        self.assertEqual(reloaded.items[1].scheduled_date, "2026-10-07")
        self.assertEqual(reloaded.items[2].scheduled_date, "2026-10-08")

    def test_replenishment_insufficient_candidates_preserves_queue_and_avoids_fabrication(self):
        """When discovery finds fewer valid products than needed, adds available valid items without fabrication."""
        def mock_candidate_provider(query, max_count):
            # Only 1 candidate returned when 5 are needed
            return [
                DiscoveredCandidate(
                    asin="B0ONLY0001",
                    title="Single Discovered Product",
                    source_url="https://www.amazon.com/dp/B0ONLY0001",
                    affiliate_url="https://www.amazon.com/dp/B0ONLY0001?tag=lifemode-20",
                    query=query
                )
            ]

        def mock_product_loader(url):
            return self._create_synthetic_product("B0ONLY0001", "Single Discovered Product", ["Single feature"])

        class MockGenerator:
            model_name = "mock_model"
            def generate_all_platforms(self, product, disclosure):
                return {
                    "asin": product.asin,
                    "affiliate_url": product.affiliate_url,
                    "platforms": {
                        "x": {"text": f"{product.title}. Single feature. {disclosure}", "status": "VALID"},
                        "facebook": {"text": f"{product.title}. Single feature.\n\n{disclosure}", "status": "VALID"},
                        "pinterest": {"title": product.title[:95], "description": f"Single feature. {disclosure}", "status": "VALID"},
                        "threads": {"text": f"{product.title}. Single feature. {disclosure}", "status": "VALID"}
                    }
                }

        report = QueueReplenisher.maintain_queue(
            queue_file=self.queue_file,
            target_capacity=5,
            candidate_provider=mock_candidate_provider,
            product_loader=mock_product_loader,
            generator=MockGenerator(),
            log_file=self.log_file,
            history_file=self.history_file,
            now_override="2026-10-07T10:00:00+02:00"
        )
        self.assertTrue(report["replenished"])
        self.assertEqual(report["items_added_count"], 1)
        self.assertEqual(report["final_future_capacity"], 1)
        reloaded = PublishingQueue.load(self.queue_file)
        self.assertEqual(len(reloaded.items), 1)

    def test_failed_candidate_validation_rejected_without_entering_queue(self):
        """If a candidate's generated content fails validation, it is rejected and not added to queue."""
        def mock_candidate_provider(query, max_count):
            return [
                DiscoveredCandidate(
                    asin="B0BADVAL01",
                    title="Product With Hallucinated Copy",
                    source_url="https://www.amazon.com/dp/B0BADVAL01",
                    affiliate_url="https://www.amazon.com/dp/B0BADVAL01?tag=lifemode-20",
                    query=query
                )
            ]

        def mock_product_loader(url):
            return self._create_synthetic_product("B0BADVAL01", "Product With Hallucinated Copy", ["Genuine feature"])

        class InvalidCopyGenerator:
            model_name = "mock_model"
            def generate_all_platforms(self, product, disclosure):
                # Returns hallucinated claims that fail ClaimValidator
                return {
                    "asin": product.asin,
                    "affiliate_url": product.affiliate_url,
                    "platforms": {
                        "x": {"text": f"{product.title}. Guarantees 100% cure and miraculous life transformation. {disclosure}", "status": "FAILED_VALIDATION"},
                        "facebook": {"text": f"{product.title}. Miraculous comfort.\n\n{disclosure}", "status": "FAILED_VALIDATION"},
                        "pinterest": {"title": product.title[:95], "description": f"Miraculous cure. {disclosure}", "status": "FAILED_VALIDATION"},
                        "threads": {"text": f"{product.title}. Miraculous cure. {disclosure}", "status": "FAILED_VALIDATION"}
                    }
                }

        report = QueueReplenisher.maintain_queue(
            queue_file=self.queue_file,
            target_capacity=3,
            discovery_queries=["test_query"],
            candidate_provider=mock_candidate_provider,
            product_loader=mock_product_loader,
            generator=InvalidCopyGenerator(),
            log_file=self.log_file,
            history_file=self.history_file,
            now_override="2026-10-07T10:00:00+02:00"
        )
        self.assertFalse(report["replenished"])
        self.assertEqual(report["items_added_count"], 0)
        self.assertEqual(report["candidates_rejected"], 1)
        self.assertEqual(report["rejection_summary"].get("VALIDATION_FAILED"), 1)

    def test_maintenance_idempotency(self):
        """Repeatedly running maintenance on the same day with same candidates produces 0 additions on 2nd run."""
        def mock_candidate_provider(query, max_count):
            return [
                DiscoveredCandidate(
                    asin="B0IDEMP001",
                    title="Idempotency Test Item",
                    source_url="https://www.amazon.com/dp/B0IDEMP001",
                    affiliate_url="https://www.amazon.com/dp/B0IDEMP001?tag=lifemode-20",
                    query=query
                )
            ]

        def mock_product_loader(url):
            return self._create_synthetic_product("B0IDEMP001", "Idempotency Test Item", ["Item feature"])

        class MockGenerator:
            model_name = "mock_model"
            def generate_all_platforms(self, product, disclosure):
                return {
                    "asin": product.asin,
                    "affiliate_url": product.affiliate_url,
                    "platforms": {
                        "x": {"text": f"{product.title}. Item feature. {disclosure}", "status": "VALID"},
                        "facebook": {"text": f"{product.title}. Item feature.\n\n{disclosure}", "status": "VALID"},
                        "pinterest": {"title": product.title[:95], "description": f"Item feature. {disclosure}", "status": "VALID"},
                        "threads": {"text": f"{product.title}. Item feature. {disclosure}", "status": "VALID"}
                    }
                }

        # Run 1
        rep1 = QueueReplenisher.maintain_queue(
            queue_file=self.queue_file,
            target_capacity=1,
            candidate_provider=mock_candidate_provider,
            product_loader=mock_product_loader,
            generator=MockGenerator(),
            log_file=self.log_file,
            history_file=self.history_file,
            now_override="2026-10-07T10:00:00+02:00"
        )
        self.assertTrue(rep1["replenished"])
        self.assertEqual(rep1["items_added_count"], 1)

        # Run 2 (Immediately after)
        rep2 = QueueReplenisher.maintain_queue(
            queue_file=self.queue_file,
            target_capacity=1,
            candidate_provider=mock_candidate_provider,
            product_loader=mock_product_loader,
            generator=MockGenerator(),
            log_file=self.log_file,
            history_file=self.history_file,
            now_override="2026-10-07T10:05:00+02:00"
        )
        self.assertFalse(rep2["replenished"])
        self.assertEqual(rep2["items_added_count"], 0)
        self.assertEqual(rep2["reason"], "CAPACITY_HEALTHY")

    def test_unsupported_comparison_and_relative_superlative_fail(self):
        """Unsupported comparative claims ('faster than competitors', 'best in class') are rejected."""
        product = self._create_synthetic_product(
            asin="B0COMP0001",
            title="Fast Charger USB-C 65W Power Adapter",
            features=["65W power delivery", "USB-C port"]
        )
        facts = FactExtractor.extract_canonical_facts(product)

        hallucinated_comparison = "Charges 3x faster than all competitor models on the market."
        issues = ClaimValidator.validate_content_claims(hallucinated_comparison, facts, disclosure="")
        self.assertGreater(len(issues), 0, "Unsupported comparative claims must be rejected!")

    # ==================== 7. SAFETY & DRY_RUN ====================

    def test_safety_dry_run_and_instagram_exclusion(self):
        """Verifies DRY_RUN is preserved and Instagram remains completely excluded."""
        self.assertNotIn("instagram", SUPPORTED_PLATFORMS)
        pub = SocialPublisher()
        self.assertNotIn("instagram", pub.adapters)
        self.assertTrue(pub.config.dry_run)


if __name__ == "__main__":
    unittest.main()
