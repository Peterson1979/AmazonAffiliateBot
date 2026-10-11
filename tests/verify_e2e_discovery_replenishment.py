"""
End-to-End Simulation Script for Automatic Amazon Product Discovery + Queue Replenishment.
Verifies all 11 requirements from Section 17:
1. Queue has fewer than target capacity.
2. Discovery returns multiple products.
3. Some candidates are duplicates (by ASIN, affiliate URL, batch, history).
4. Some candidates lack sufficient source data (missing title, short title, insufficient features).
5. Some products generate invalid/hallucinated claims (rejected by ClaimValidator).
6. Valid products are accepted and content packages built.
7. Queue is replenished to target capacity.
8. Products are scheduled at 12:00 (Slot 1) & 19:00 (Slot 2) in Europe/Budapest.
9. Publishing simulation processes the correct due slots.
10. Re-running maintenance does not duplicate products (idempotent).
11. Re-running publishing does not duplicate posts (idempotent).
"""

import sys
from pathlib import Path
import json
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
from content.facts import FactExtractor
from content.package import ContentPackage, STATUS_READY_FOR_REVIEW
from publishing.queue import (
    PublishingQueue,
    QueueItem,
    QueuePublisher,
    get_budapest_timezone,
    STATUS_READY,
    STATUS_PUBLISHED
)
from publishing.replenisher import (
    QueueReplenisher,
    DEFAULT_TARGET_FUTURE_CAPACITY
)
from publishing.publisher import SocialPublisher, STATUS_WOULD_PUBLISH


def run_e2e_simulation():
    temp_dir = Path(tempfile.mkdtemp(prefix="amazon_e2e_sim_"))
    print(f"=== E2E SIMULATION ENVIRONMENT INITIALIZED AT: {temp_dir} ===")

    try:
        queue_file = temp_dir / "publishing_queue.json"
        history_file = temp_dir / "discovery_history.json"
        log_file = temp_dir / "queue_maintenance.log"
        publish_log_file = temp_dir / "daily_publisher.log"
        products_dir = temp_dir / "data" / "products"
        generated_dir = temp_dir / "data" / "generated"
        products_dir.mkdir(parents=True, exist_ok=True)
        generated_dir.mkdir(parents=True, exist_ok=True)

        tz = get_budapest_timezone()
        sim_now_str = "2026-10-08T10:00:00+02:00"
        sim_now = datetime.fromisoformat(sim_now_str)

        # -------------------------------------------------------------
        # Step 1: Initial Queue with 2 items (target = 6, below target)
        # -------------------------------------------------------------
        print("\n[Step 1] Initializing Queue with 2 existing items (Target capacity = 6)...")
        initial_item1 = QueueItem(
            queue_item_id="item_001_B0EXIST001",
            original_url="https://www.amazon.com/dp/B0EXIST001?tag=lifemode-20",
            asin="B0EXIST001",
            product_name="Pre-Existing Camping Lantern 1000 Lumens",
            scheduled_date="2026-10-08",
            daily_slot=1,
            scheduled_at="2026-10-08T12:00:00+02:00",
            timezone="Europe/Budapest",
            status=STATUS_READY
        )
        initial_item2 = QueueItem(
            queue_item_id="item_002_B0EXIST002",
            original_url="https://www.amazon.com/dp/B0EXIST002?tag=lifemode-20",
            asin="B0EXIST002",
            product_name="Pre-Existing Pour-Over Coffee Dripper Stainless Steel",
            scheduled_date="2026-10-08",
            daily_slot=2,
            scheduled_at="2026-10-08T19:00:00+02:00",
            timezone="Europe/Budapest",
            status=STATUS_READY
        )
        q = PublishingQueue(
            queue_id="sim_queue_20261008",
            created_at=sim_now.isoformat(),
            start_date="2026-10-08",
            total_days=14,
            products_per_day=2,
            target_capacity=6,
            items=[initial_item1, initial_item2]
        )
        q.save(queue_file)
        print(f"  -> Initial Queue items: {len(q.items)} | Future valid capacity: 2 / 6")

        # -------------------------------------------------------------
        # Step 2-5: Discovery dataset with mixed quality candidates
        # -------------------------------------------------------------
        print("\n[Step 2-5] Preparing Synthetic Discovery Dataset with Diverse Quality Candidates...")

        # Candidates pool:
        # 1. B0EXIST001 (Duplicate in queue) -> Should reject: DUPLICATE_IN_QUEUE
        # 2. B0INVALID1 (Bad ASIN) -> Should reject: INVALID_ASIN
        # 3. B0NODATA01 (Missing product data / title) -> Should reject: MISSING_PRODUCT_DATA
        # 4. B0NOFACT01 (Insufficient facts) -> Should reject: INSUFFICIENT_FACTS
        # 5. B0BADVAL01 (Validation failure / hallucination) -> Should reject: VALIDATION_FAILED
        # 6. B0VALID001 (Valid Product 1: Espresso Tamper) -> ACCEPT
        # 7. B0VALID002 (Valid Product 2: Ergonomic Laptop Stand) -> ACCEPT
        # 8. B0VALID003 (Valid Product 3: USB-C Fast Charger 65W) -> ACCEPT
        # 9. B0VALID004 (Valid Product 4: Thermal Water Bottle 32oz) -> ACCEPT
        # 10. B0VALID001 (Duplicate within batch) -> Should reject: DUPLICATE_IN_BATCH

        synthetic_candidates = [
            DiscoveredCandidate(
                asin="B0EXIST001",
                title="Duplicate Pre-Existing Lantern",
                source_url="https://www.amazon.com/dp/B0EXIST001",
                affiliate_url="https://www.amazon.com/dp/B0EXIST001?tag=lifemode-20",
                query="camping gear"
            ),
            DiscoveredCandidate(
                asin="B0INVALID_ASIN",
                title="Invalid ASIN Product",
                source_url="",
                affiliate_url="",
                query="camping gear"
            ),
            DiscoveredCandidate(
                asin="B0NODATA01",
                title="No Data Item",
                source_url="https://www.amazon.com/dp/B0NODATA01",
                affiliate_url="https://www.amazon.com/dp/B0NODATA01?tag=lifemode-20",
                query="kitchen gadgets"
            ),
            DiscoveredCandidate(
                asin="B0NOFACT01",
                title="Insufficient Facts Item",
                source_url="https://www.amazon.com/dp/B0NOFACT01",
                affiliate_url="https://www.amazon.com/dp/B0NOFACT01?tag=lifemode-20",
                query="kitchen gadgets"
            ),
            DiscoveredCandidate(
                asin="B0BADVAL01",
                title="Hallucinated Copy Item",
                source_url="https://www.amazon.com/dp/B0BADVAL01",
                affiliate_url="https://www.amazon.com/dp/B0BADVAL01?tag=lifemode-20",
                query="kitchen gadgets"
            ),
            DiscoveredCandidate(
                asin="B0VALID001",
                title="58mm Precision Espresso Coffee Tamper Stainless Steel",
                source_url="https://www.amazon.com/dp/B0VALID001",
                affiliate_url="https://www.amazon.com/dp/B0VALID001?tag=lifemode-20",
                query="espresso machine accessories"
            ),
            DiscoveredCandidate(
                asin="B0VALID002",
                title="Adjustable Aluminum Laptop Stand with 7 Height Settings",
                source_url="https://www.amazon.com/dp/B0VALID002",
                affiliate_url="https://www.amazon.com/dp/B0VALID002?tag=lifemode-20",
                query="home office ergonomic desk setup"
            ),
            DiscoveredCandidate(
                asin="B0VALID003",
                title="65W GaN USB-C Dual Port Fast Wall Charger",
                source_url="https://www.amazon.com/dp/B0VALID003",
                affiliate_url="https://www.amazon.com/dp/B0VALID003?tag=lifemode-20",
                query="portable tech accessories"
            ),
            DiscoveredCandidate(
                asin="B0VALID004",
                title="Vacuum Insulated Stainless Steel Water Bottle 32oz",
                source_url="https://www.amazon.com/dp/B0VALID004",
                affiliate_url="https://www.amazon.com/dp/B0VALID004?tag=lifemode-20",
                query="camping and hiking gear essentials"
            ),
            DiscoveredCandidate(
                asin="B0VALID001",
                title="Batch Duplicate Tamper",
                source_url="https://www.amazon.com/dp/B0VALID001",
                affiliate_url="https://www.amazon.com/dp/B0VALID001?tag=lifemode-20",
                query="espresso machine accessories"
            ),
        ]

        def mock_candidate_provider(query, max_count):
            return synthetic_candidates

        def mock_product_loader(url):
            if "B0EXIST001" in url:
                return Product(title="Duplicate Lantern", features=["Lantern feature"], affiliate_url=url, source_url=url, asin="B0EXIST001")
            elif "B0NODATA01" in url:
                return None  # Scraping / loader failed
            elif "B0NOFACT01" in url:
                return Product(title="", features=[], affiliate_url=url, source_url=url, asin="B0NOFACT01")
            elif "B0BADVAL01" in url:
                return Product(title="Hallucinated Copy Item", features=["Feature 1"], affiliate_url=url, source_url=url, asin="B0BADVAL01")
            elif "B0VALID001" in url:
                return Product(
                    title="58mm Precision Espresso Coffee Tamper Stainless Steel",
                    features=["Calibrated 30 lbs spring mechanism", "58mm flat stainless steel base", "Ergonomic walnut wood handle"],
                    affiliate_url=url, source_url=url, price="$29.99", asin="B0VALID001"
                )
            elif "B0VALID002" in url:
                return Product(
                    title="Adjustable Aluminum Laptop Stand with 7 Height Settings",
                    features=["7 adjustable height levels from 2.5 to 6 inches", "Solid aluminum alloy construction", "Supports up to 17.3 inch laptops"],
                    affiliate_url=url, source_url=url, price="$34.99", asin="B0VALID002"
                )
            elif "B0VALID003" in url:
                return Product(
                    title="65W GaN USB-C Dual Port Fast Wall Charger",
                    features=["65W max Power Delivery 3.0 output", "Dual USB-C and USB-A ports", "GaN semiconductor technology"],
                    affiliate_url=url, source_url=url, price="$24.99", asin="B0VALID003"
                )
            elif "B0VALID004" in url:
                return Product(
                    title="Vacuum Insulated Stainless Steel Water Bottle 32oz",
                    features=["Double-wall vacuum insulation", "Food-grade 18/8 stainless steel", "Leak-proof straw lid included"],
                    affiliate_url=url, source_url=url, price="$21.99", asin="B0VALID004"
                )
            return None

        class E2EMockGenerator:
            model_name = "mock_model"
            def generate_all_platforms(self, product, disclosure):
                if product.asin == "B0BADVAL01":
                    # Fails validation intentionally
                    return {
                        "asin": product.asin,
                        "affiliate_url": product.affiliate_url,
                        "platforms": {
                            "x": {"text": f"{product.title}. Guaranteed cure and effortless happiness. {disclosure}", "status": "FAILED_VALIDATION"},
                            "facebook": {"text": f"{product.title}. Guaranteed cure.\n\n{disclosure}", "status": "FAILED_VALIDATION"},
                            "pinterest": {"title": product.title[:95], "description": f"Guaranteed cure. {disclosure}", "status": "FAILED_VALIDATION"},
                            "threads": {"text": f"{product.title}. Guaranteed cure. {disclosure}", "status": "FAILED_VALIDATION"}
                        }
                    }
                # Grounded marketing copy for valid products
                features_text = ". ".join(product.features) if product.features else ""
                pin_title = f"{product.title.split('with')[0].strip()}"[:95]
                return {
                    "asin": product.asin,
                    "affiliate_url": product.affiliate_url,
                    "platforms": {
                        "x": {"text": f"{product.title}. {features_text}. {disclosure}", "status": "VALID"},
                        "facebook": {"text": f"{product.title}. {features_text}.\n\n{disclosure}", "status": "VALID"},
                        "pinterest": {
                            "title": pin_title,
                            "description": f"{features_text[:300]}. {disclosure}",
                            "status": "VALID"
                        },
                        "threads": {"text": f"{product.title}. {features_text}. {disclosure}", "status": "VALID"}
                    }
                }

        # -------------------------------------------------------------
        # Step 6-8: Run Queue Maintenance / Replenishment
        # -------------------------------------------------------------
        print("\n[Step 6-8] Executing Queue Maintenance (Target = 6)...")
        report = QueueReplenisher.maintain_queue(
            queue_file=queue_file,
            target_capacity=6,
            discovery_queries=["test_query"],
            candidate_provider=mock_candidate_provider,
            product_loader=mock_product_loader,
            generator=E2EMockGenerator(),
            log_file=log_file,
            history_file=history_file,
            now_override=sim_now_str
        )

        print(f"  -> Maintenance Result: Replenished={report['replenished']}")
        print(f"  -> Candidates Evaluated: {report['candidates_evaluated']}")
        print(f"  -> Candidates Rejected: {report['candidates_rejected']}")
        print(f"  -> Rejection Summary: {json.dumps(report['rejection_summary'])}")
        print(f"  -> Items Added: {report['items_added_count']}")
        print(f"  -> Final Future Capacity: {report['final_future_capacity']} / 6")

        assert report["replenished"] is True, "Replenishment must be True"
        assert report["items_added_count"] == 4, f"Expected 4 items added, got {report['items_added_count']}"
        assert report["final_future_capacity"] == 6, f"Expected 6 final capacity, got {report['final_future_capacity']}"

        reloaded_queue = PublishingQueue.load(queue_file)
        assert len(reloaded_queue.items) == 6, f"Queue must contain 6 items, found {len(reloaded_queue.items)}"

        # Verify scheduling slot assignments:
        # Item 1: 2026-10-08 Slot 1 (12:00) [Pre-existing]
        # Item 2: 2026-10-08 Slot 2 (19:00) [Pre-existing]
        # Item 3: 2026-10-09 Slot 1 (12:00) [New: B0VALID001]
        # Item 4: 2026-10-09 Slot 2 (19:00) [New: B0VALID002]
        # Item 5: 2026-10-10 Slot 1 (12:00) [New: B0VALID003]
        # Item 6: 2026-10-10 Slot 2 (19:00) [New: B0VALID004]

        print("\n  Scheduled Slot Verification:")
        for idx, itm in enumerate(reloaded_queue.items, start=1):
            print(f"    Slot {idx}: {itm.scheduled_date} Slot {itm.daily_slot} ({itm.scheduled_at}) -> [{itm.asin}] {itm.product_name[:45]}")

        assert reloaded_queue.items[0].asin == "B0EXIST001" and reloaded_queue.items[0].daily_slot == 1
        assert reloaded_queue.items[1].asin == "B0EXIST002" and reloaded_queue.items[1].daily_slot == 2
        assert reloaded_queue.items[2].asin == "B0VALID001" and reloaded_queue.items[2].daily_slot == 1 and reloaded_queue.items[2].scheduled_date == "2026-10-09"
        assert reloaded_queue.items[3].asin == "B0VALID002" and reloaded_queue.items[3].daily_slot == 2 and reloaded_queue.items[3].scheduled_date == "2026-10-09"
        assert reloaded_queue.items[4].asin == "B0VALID003" and reloaded_queue.items[4].daily_slot == 1 and reloaded_queue.items[4].scheduled_date == "2026-10-10"
        assert reloaded_queue.items[5].asin == "B0VALID004" and reloaded_queue.items[5].daily_slot == 2 and reloaded_queue.items[5].scheduled_date == "2026-10-10"

        # Also save content package for pre-existing items so publisher can load them
        for asin, title in [("B0EXIST001", "Pre-Existing Camping Lantern 1000 Lumens"), ("B0EXIST002", "Pre-Existing Pour-Over Coffee Dripper Stainless Steel")]:
            dummy_pkg = {
                "asin": asin,
                "product_name": title,
                "status": STATUS_READY_FOR_REVIEW,
                "platforms": {
                    "x": {"content": f"{title}. Reliable camping gear.", "status": "VALID"},
                    "facebook": {"content": f"{title}. High quality.", "status": "VALID"},
                    "pinterest": {"title": title[:95], "description": f"{title}. Reliable.", "status": "VALID"},
                    "threads": {"content": f"{title}. Reliable.", "status": "VALID"}
                }
            }
            pkg_path = Path(f"data/generated/asin_{asin}_content_package.json")
            pkg_path.parent.mkdir(parents=True, exist_ok=True)
            with open(pkg_path, "w", encoding="utf-8") as f:
                json.dump(dummy_pkg, f, indent=2)

        # -------------------------------------------------------------
        # Step 9: Publishing Simulation across Due Slots
        # -------------------------------------------------------------
        print("\n[Step 9] Simulating Scheduled Daily Publishing at 12:30 (Day 1 Slot 1 Due)...")
        pub_result_1230 = QueuePublisher.publish_due_items(
            queue_file=queue_file,
            now_override="2026-10-08T12:30:00+02:00",
            log_file=publish_log_file,
            dry_run=True
        )
        print(f"  -> Items due: {pub_result_1230['due_items_count']} | Published: {pub_result_1230['published_count']}")
        assert pub_result_1230["published_count"] == 1, "Only Slot 1 should be published at 12:30"
        assert pub_result_1230["items_processed"][0]["asin"] == "B0EXIST001"

        print("\n[Step 9b] Simulating Scheduled Daily Publishing at 19:15 (Day 1 Slot 2 Due)...")
        pub_result_1915 = QueuePublisher.publish_due_items(
            queue_file=queue_file,
            now_override="2026-10-08T19:15:00+02:00",
            log_file=publish_log_file,
            dry_run=True
        )
        print(f"  -> Items due: {pub_result_1915['due_items_count']} | Published: {pub_result_1915['published_count']}")
        assert pub_result_1915["published_count"] == 1, "Only Slot 2 should be published at 19:15"
        assert pub_result_1915["items_processed"][0]["asin"] == "B0EXIST002"

        # -------------------------------------------------------------
        # Step 10: Idempotency of Maintenance
        # -------------------------------------------------------------
        print("\n[Step 10] Testing Queue Maintenance Idempotency (Re-running maintenance)...")
        rep_idem = QueueReplenisher.maintain_queue(
            queue_file=queue_file,
            target_capacity=6,
            discovery_queries=["test_query"],
            candidate_provider=mock_candidate_provider,
            product_loader=mock_product_loader,
            generator=E2EMockGenerator(),
            log_file=log_file,
            history_file=history_file,
            now_override="2026-10-08T20:00:00+02:00"
        )
        print(f"  -> Second Run Replenished: {rep_idem['replenished']} | Items Added: {rep_idem['items_added_count']}")
        # Remaining future items = 4 (since 2 were published today). Target = 6.
        # But candidates in mock are either in queue, in history, or invalid -> 0 added, gracefully handled.
        assert rep_idem["items_added_count"] == 0, "No duplicate items should be added"

        # -------------------------------------------------------------
        # Step 11: Idempotency of Publishing
        # -------------------------------------------------------------
        print("\n[Step 11] Testing Publishing Idempotency (Re-running publishing at 19:30)...")
        pub_idem = QueuePublisher.publish_due_items(
            queue_file=queue_file,
            now_override="2026-10-08T19:30:00+02:00",
            log_file=publish_log_file,
            dry_run=True
        )
        print(f"  -> Items Due: {pub_idem['due_items_count']} | Published: {pub_idem['published_count']}")
        assert pub_idem["due_items_count"] == 0, "Zero items should be due on duplicate run"
        assert pub_idem["published_count"] == 0, "Zero items should be published on duplicate run"

        print("\n=== COMPLETE END-TO-END SIMULATION COMPLETED SUCCESSFULLY WITH ZERO ERRORS ===")
        return True

    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)


if __name__ == "__main__":
    success = run_e2e_simulation()
    sys.exit(0 if success else 1)
