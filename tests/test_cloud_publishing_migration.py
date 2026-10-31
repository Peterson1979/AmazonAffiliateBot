"""
Focused unit tests for the GitHub Actions Cloud Publishing Migration.
Tests:
1. Timezone window evaluation across CET and CEST daylight-saving time shifts.
2. Secret validation logic (Facebook, Threads, Pinterest required; X safely optional).
3. Dry-run protection (ensures real queue file on disk is not mutated in simulation).
4. Idempotency and duplicate prevention (published/failed items are never re-published).
5. GitHub Actions workflow structure and syntax verification.
"""

import os
import sys
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from datetime import datetime
from unittest.mock import patch, MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_cloud_publisher import (
    get_current_budapest_time,
    check_budapest_publishing_window,
    validate_required_live_secrets,
)
from publishing.queue import (
    QueuePublisher,
    PublishingQueue,
    QueueItem,
    STATUS_READY,
    STATUS_PUBLISHED,
    STATUS_PARTIALLY_PUBLISHED,
    STATUS_FAILED,
    STATUS_SKIPPED,
)


class TestCloudPublishingMigration(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_cloud_pub_"))
        self.queue_file = self.temp_dir / "test_queue.json"

    def tearDown(self):
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    # -------------------------------------------------------------------------
    # 1. Daylight-Saving Time & Window Tests
    # -------------------------------------------------------------------------

    def test_cest_summer_window_detection(self):
        """Verify window detection during CEST (Summer, UTC+2)."""
        # In CEST: 12:00 UTC = 14:00 CEST (Before Slot 1 -> None)
        dt_12_utc = datetime.fromisoformat("2026-07-15T12:00:00+00:00")
        self.assertIsNone(check_budapest_publishing_window(dt_12_utc))

        # In CEST: 13:00 UTC = 15:00 CEST (Slot 1 opens)
        dt_13_utc = datetime.fromisoformat("2026-07-15T13:00:00+00:00")
        self.assertEqual(check_budapest_publishing_window(dt_13_utc), 1)

        # In CEST: 18:59 UTC = 20:59 CEST (Slot 1 last eligible minute)
        dt_1859_utc = datetime.fromisoformat("2026-07-15T18:59:00+00:00")
        self.assertEqual(check_budapest_publishing_window(dt_1859_utc), 1)

        # In CEST: 19:00 UTC = 21:00 CEST (Slot 2 opens)
        dt_19_utc = datetime.fromisoformat("2026-07-15T19:00:00+00:00")
        self.assertEqual(check_budapest_publishing_window(dt_19_utc), 2)

        # In CEST: 20:00 UTC = 22:00 CEST (Slot 2 late-start recovery)
        dt_20_utc = datetime.fromisoformat("2026-07-15T20:00:00+00:00")
        self.assertEqual(check_budapest_publishing_window(dt_20_utc), 2)

    def test_cet_winter_window_detection(self):
        """Verify window detection during CET (Winter, UTC+1)."""
        # In CET: 12:00 UTC = 13:00 CET (Before Slot 1 -> None)
        dt_12_utc = datetime.fromisoformat("2026-12-15T12:00:00+00:00")
        self.assertIsNone(check_budapest_publishing_window(dt_12_utc))

        # In CET: 13:00 UTC = 14:00 CET (Before Slot 1 -> None)
        dt_13_utc = datetime.fromisoformat("2026-12-15T13:00:00+00:00")
        self.assertIsNone(check_budapest_publishing_window(dt_13_utc))

        # In CET: 14:00 UTC = 15:00 CET (Slot 1 opens)
        dt_14_utc = datetime.fromisoformat("2026-12-15T14:00:00+00:00")
        self.assertEqual(check_budapest_publishing_window(dt_14_utc), 1)

        # In CET: 19:00 UTC = 20:00 CET (Slot 1 late-start recovery)
        dt_19_utc = datetime.fromisoformat("2026-12-15T19:00:00+00:00")
        self.assertEqual(check_budapest_publishing_window(dt_19_utc), 1)

        # In CET: 20:00 UTC = 21:00 CET (Slot 2)
        dt_20_utc = datetime.fromisoformat("2026-12-15T20:00:00+00:00")
        self.assertEqual(check_budapest_publishing_window(dt_20_utc), 2)

    def test_publishing_window_boundaries(self):
        """Exact boundaries: Slot 1 15:00-20:59, Slot 2 21:00-23:59, else None."""
        cases = [
            ("2026-07-15T14:59:00", None),   # one minute before Slot 1
            ("2026-07-15T15:00:00", 1),      # Slot 1 opens
            ("2026-07-15T20:59:00", 1),      # Slot 1 last eligible minute
            ("2026-07-15T21:00:00", 2),      # Slot 2 opens
            ("2026-07-15T23:59:00", 2),      # Slot 2 last eligible minute
            ("2026-07-16T00:00:00", None),   # next day, outside both windows
        ]
        for iso_value, expected in cases:
            self.assertEqual(
                check_budapest_publishing_window(datetime.fromisoformat(iso_value)),
                expected,
                f"local time {iso_value}",
            )

    def test_budapest_time_parser(self):
        """Verify get_current_budapest_time converts UTC and naive timestamps properly."""
        dt_utc = get_current_budapest_time("2026-10-11T10:00:00Z")
        self.assertEqual(dt_utc.tzinfo.key, "Europe/Budapest")
        self.assertEqual(dt_utc.hour, 12)  # 10:00 UTC is 12:00 CEST

    # -------------------------------------------------------------------------
    # 2. Secret Validation Tests
    # -------------------------------------------------------------------------

    def test_validate_required_live_secrets_missing(self):
        """Verify validation catches missing Facebook, Threads, or Pinterest credentials."""
        with patch.dict(os.environ, {}, clear=True):
            ok, missing = validate_required_live_secrets()
            self.assertFalse(ok)
            self.assertIn("FACEBOOK_PAGE_ACCESS_TOKEN (or META_ACCESS_TOKEN)", missing)
            self.assertIn("FACEBOOK_PAGE_ID", missing)
            self.assertIn("THREADS_ACCESS_TOKEN", missing)
            self.assertIn("THREADS_USER_ID", missing)
            self.assertIn("PINTEREST_ACCESS_TOKEN", missing)
            self.assertIn("PINTEREST_LIFEMODEHQ_ACCESS_TOKEN", missing)

    def test_validate_required_live_secrets_present_without_x(self):
        """Verify validation succeeds when FB, Threads, and Pinterest are present (X optional)."""
        mock_env = {
            "FACEBOOK_PAGE_ACCESS_TOKEN": "mock_fb_token",
            "FACEBOOK_PAGE_ID": "123456",
            "THREADS_ACCESS_TOKEN": "mock_threads_token",
            "THREADS_USER_ID": "mock_threads_user",
            "PINTEREST_ACCESS_TOKEN": "mock_pin_token",
            "PINTEREST_LIFEMODEHQ_ACCESS_TOKEN": "mock_life_pin_token",
        }
        with patch.dict(os.environ, mock_env, clear=True):
            ok, missing = validate_required_live_secrets()
            self.assertTrue(ok)
            self.assertEqual(len(missing), 0)

    # -------------------------------------------------------------------------
    # 3. Queue Idempotency & Duplicate Prevention Tests
    # -------------------------------------------------------------------------

    def test_idempotency_prevents_duplicate_publishing(self):
        """Verify already published or partially published items are never returned as due."""
        queue = PublishingQueue(
            queue_id="test_idem_q",
            created_at="2026-10-01T00:00:00Z",
            start_date="2026-10-11",
            total_days=1,
            products_per_day=2,
            target_capacity=2,
            total_submitted=2,
            total_processed=2,
            total_ready=0,
            total_failed=0,
            scheduled_posts=2,
            unfilled_slots=0,
            items=[
                QueueItem(
                    queue_item_id="item_001_PUBLISHED",
                    original_url="https://link.amazon/1",
                    asin="B001",
                    product_name="Product 1",
                    scheduled_date="2026-10-11",
                    daily_slot=1,
                    scheduled_at="2026-10-11T12:00:00+02:00",
                    package_reference="",
                    status=STATUS_PUBLISHED,
                ),
                QueueItem(
                    queue_item_id="item_002_PARTIAL",
                    original_url="https://link.amazon/2",
                    asin="B002",
                    product_name="Product 2",
                    scheduled_date="2026-10-11",
                    daily_slot=2,
                    scheduled_at="2026-10-11T19:00:00+02:00",
                    package_reference="",
                    status=STATUS_PARTIALLY_PUBLISHED,
                ),
                QueueItem(
                    queue_item_id="item_003_READY_DUE",
                    original_url="https://link.amazon/3",
                    asin="B003",
                    product_name="Product 3",
                    scheduled_date="2026-10-11",
                    daily_slot=1,
                    scheduled_at="2026-10-11T12:00:00+02:00",
                    package_reference="",
                    status=STATUS_READY,
                ),
            ]
        )

        current_dt = datetime.fromisoformat("2026-10-11T13:00:00+02:00")
        due_items = QueuePublisher.find_due_items(queue, current_dt)

        # Only item_003 should be due. item_001 and item_002 must be skipped!
        self.assertEqual(len(due_items), 1)
        self.assertEqual(due_items[0].queue_item_id, "item_003_READY_DUE")

    # -------------------------------------------------------------------------
    # 4. Dry-Run Queue File Integrity Test
    # -------------------------------------------------------------------------

    def test_dry_run_simulation_preserves_real_queue_file(self):
        """Verify dry-run mode in run_cloud_publisher leaves on-disk queue untouched."""
        test_pkg = self.temp_dir / "asin_B003_content_package.json"
        with open(test_pkg, "w", encoding="utf-8") as f:
            json.dump({
                "asin": "B003",
                "status": "READY_FOR_REVIEW",
                "platforms": {
                    "facebook": {"status": "VALID", "content": "FB post", "image_url": "https://img.com/1"},
                    "threads": {"status": "VALID", "content": "Threads post", "image_url": "https://img.com/1"},
                    "pinterest": {"status": "VALID", "title": "Pin", "description": "Pin desc", "image_url": "https://img.com/1"},
                    "x": {"status": "VALID", "content": "X post", "image_url": "https://img.com/1"},
                }
            }, f)

        initial_queue_data = {
            "queue_id": "test_q",
            "created_at": "2026-10-01T00:00:00Z",
            "start_date": "2026-10-11",
            "total_days=1": 1,
            "products_per_day": 2,
            "target_capacity": 2,
            "total_submitted": 1,
            "total_processed": 1,
            "total_ready": 1,
            "total_failed": 0,
            "scheduled_posts": 1,
            "unfilled_slots": 1,
            "items": [
                {
                    "queue_item_id": "item_003_READY_DUE",
                    "original_url": "https://link.amazon/3",
                    "asin": "B003",
                    "product_name": "Product 3",
                    "scheduled_date": "2026-10-11",
                    "daily_slot": 1,
                    "scheduled_at": "2026-10-11T12:00:00+02:00",
                    "package_reference": str(test_pkg),
                    "status": "READY",
                    "validation_status": "READY_FOR_REVIEW",
                    "errors": [],
                    "created_at": "2026-10-01T00:00:00Z",
                    "published_at": None,
                    "publishing_results": None,
                }
            ]
        }
        with open(self.queue_file, "w", encoding="utf-8") as f:
            json.dump(initial_queue_data, f, indent=2)

        # Run cloud publisher script with --dry-run
        from scripts.run_cloud_publisher import main as cloud_main
        test_args = [
            "run_cloud_publisher.py",
            "--dry-run",
            "--ignore-window",
            "--now", "2026-10-11T12:30:00+02:00",
            "--queue-file", str(self.queue_file),
            "--log-file", str(self.temp_dir / "test.log")
        ]
        with patch.object(sys, "argv", test_args):
            cloud_main()

        # Verify on-disk queue file status is STILL 'READY' (not modified by dry-run!)
        with open(self.queue_file, "r", encoding="utf-8") as f:
            reloaded = json.load(f)
        self.assertEqual(reloaded["items"][0]["status"], "READY")
        self.assertIsNone(reloaded["items"][0]["published_at"])

    # -------------------------------------------------------------------------
    # 5. Workflow Configuration & Structure Test
    # -------------------------------------------------------------------------

    def test_workflow_file_contents_and_triggers(self):
        """Verify .github/workflows/scheduled_publishing.yml structure and security."""
        workflow_path = PROJECT_ROOT / ".github" / "workflows" / "scheduled_publishing.yml"
        self.assertTrue(workflow_path.exists(), "Workflow file must exist")

        content = workflow_path.read_text(encoding="utf-8")

        # Verify cron schedule covers CET/CEST shifts for the 15:00/21:00 slots
        self.assertIn("0 13,14,19,20 * * *", content)

        # Verify workflow_dispatch dry_run default is preserved (existing behavior: false)
        self.assertIn("workflow_dispatch:", content)
        self.assertIn("default: false", content)

        # Verify write permissions for git queue persistence
        self.assertIn("contents: write", content)

        # Verify no hardcoded secrets exist in workflow
        self.assertNotIn("ghp_", content)
        self.assertNotIn("EAAB", content)
        self.assertNotIn("pina_", content)
        self.assertNotIn("THQ", content)

        # Verify exact required secret mappings
        self.assertIn("FACEBOOK_PAGE_ACCESS_TOKEN", content)
        self.assertIn("FACEBOOK_PAGE_ID", content)
        self.assertIn("THREADS_ACCESS_TOKEN", content)
        self.assertIn("THREADS_USER_ID", content)
        self.assertIn("PINTEREST_ACCESS_TOKEN", content)
        self.assertIn("PINTEREST_LIFEMODEHQ_ACCESS_TOKEN", content)

        # Verify git commit occurs before git pull --rebase in persistence step
        persistence_idx = content.find("Persist Queue and Logs")
        self.assertNotEqual(persistence_idx, -1)
        commit_idx = content.find('git commit -m "Automated publishing update [skip ci]"', persistence_idx)
        rebase_idx = content.find("git pull --rebase origin", persistence_idx)
        self.assertTrue(commit_idx != -1 and rebase_idx != -1)
        self.assertLess(commit_idx, rebase_idx, "git commit must occur before git pull --rebase to prevent index failure")

    # -------------------------------------------------------------------------
    # 6. Manual Window Bypass & Queue Audit Tests
    # -------------------------------------------------------------------------

    def test_audit_queue_items_reports_counts_and_reasons(self):
        """Verify audit_queue_items reports scheduled counts, due counts, and clear skip reasons."""
        from scripts.run_cloud_publisher import audit_queue_items

        queue = PublishingQueue(
            queue_id="test_audit_q",
            created_at="2026-10-01T00:00:00Z",
            start_date="2026-10-11",
            total_days=1,
            products_per_day=3,
            target_capacity=3,
            total_submitted=3,
            total_processed=3,
            total_ready=1,
            total_failed=1,
            scheduled_posts=2,
            unfilled_slots=0,
            items=[
                QueueItem(
                    queue_item_id="item_001_FAIL",
                    original_url="https://link.amazon/1",
                    asin="B001",
                    product_name="Product 1",
                    scheduled_date="2026-10-11",
                    daily_slot=1,
                    scheduled_at="2026-10-11T12:00:00+02:00",
                    package_reference="",
                    status=STATUS_FAILED,
                ),
                QueueItem(
                    queue_item_id="item_002_PUBLISHED",
                    original_url="https://link.amazon/2",
                    asin="B002",
                    product_name="Product 2",
                    scheduled_date="2026-10-11",
                    daily_slot=1,
                    scheduled_at="2026-10-11T12:00:00+02:00",
                    published_at="2026-10-11T07:30:00+02:00",
                    package_reference="",
                    status=STATUS_PUBLISHED,
                ),
                QueueItem(
                    queue_item_id="item_003_FUTURE",
                    original_url="https://link.amazon/3",
                    asin="B003",
                    product_name="Product 3",
                    scheduled_date="2026-10-11",
                    daily_slot=2,
                    scheduled_at="2026-10-11T19:00:00+02:00",
                    package_reference="",
                    status=STATUS_READY,
                ),
                QueueItem(
                    queue_item_id="item_004_DUE",
                    original_url="https://link.amazon/4",
                    asin="B004",
                    product_name="Product 4",
                    scheduled_date="2026-10-11",
                    daily_slot=1,
                    scheduled_at="2026-10-11T09:00:00+02:00",
                    package_reference="",
                    status=STATUS_READY,
                ),
            ]
        )

        current_dt = datetime.fromisoformat("2026-10-11T10:00:00+02:00")
        audit = audit_queue_items(queue, current_dt)

        self.assertEqual(audit["total_items"], 4)
        self.assertEqual(audit["scheduled_items_count"], 2)  # item_003 and item_004 are READY and scheduled
        self.assertEqual(audit["due_items_count"], 1)        # item_004 is due (09:00 <= 10:00)
        self.assertEqual(audit["skipped_items_count"], 3)    # item_001, item_002, item_003 skipped

        # Verify reasons are populated and human-readable
        skipped_by_id = {x["item_id"]: x["reason"] for x in audit["skipped_items"]}
        self.assertIn("Status is FAILED", skipped_by_id["item_001_FAIL"])
        self.assertIn("Status is PUBLISHED", skipped_by_id["item_002_PUBLISHED"])
        self.assertIn("Scheduled in future", skipped_by_id["item_003_FUTURE"])

        self.assertEqual(audit["due_items"][0]["item_id"], "item_004_DUE")

    def test_manual_dry_run_bypasses_window_check(self):
        """Verify manual dry_run=true bypasses publishing window and evaluates queue."""
        from scripts.run_cloud_publisher import main as cloud_main

        # Create dummy queue
        with open(self.queue_file, "w", encoding="utf-8") as f:
            json.dump({
                "queue_id": "test_q",
                "items": []
            }, f)

        # Current time outside 15:00/21:00 window (10:00 AM)
        test_args = [
            "run_cloud_publisher.py",
            "--dry-run",
            "--now", "2026-10-11T10:00:00+02:00",
            "--queue-file", str(self.queue_file),
            "--log-file", str(self.temp_dir / "test.log")
        ]
        # Simulate workflow_dispatch event
        with patch.dict(os.environ, {"EVENT_NAME": "workflow_dispatch", "MANUAL_DRY_RUN": "true"}):
            with patch.object(sys, "argv", test_args):
                # Should NOT exit with SystemExit(0) early from window check; should complete cleanly
                cloud_main()

    def test_scheduled_event_enforces_window_check(self):
        """Verify scheduled event enforces window check and skips outside 15:00/21:00 hours."""
        from scripts.run_cloud_publisher import main as cloud_main

        with open(self.queue_file, "w", encoding="utf-8") as f:
            json.dump({
                "queue_id": "test_q",
                "items": []
            }, f)

        # Current time outside 15:00/21:00 window (10:00 AM)
        test_args = [
            "run_cloud_publisher.py",
            "--now", "2026-10-11T10:00:00+02:00",
            "--queue-file", str(self.queue_file),
            "--log-file", str(self.temp_dir / "test.log")
        ]
        # Simulate schedule cron event
        with patch.dict(os.environ, {"EVENT_NAME": "schedule"}):
            with patch.object(sys, "argv", test_args):
                with self.assertRaises(SystemExit) as ctx:
                    cloud_main()
                self.assertEqual(ctx.exception.code, 0)

    def test_manual_live_run_enforces_window_check_by_default(self):
        """Verify manual live run (dry_run=false) enforces window check unless ignore_window is given."""
        from scripts.run_cloud_publisher import main as cloud_main

        with open(self.queue_file, "w", encoding="utf-8") as f:
            json.dump({
                "queue_id": "test_q",
                "items": []
            }, f)

        test_args = [
            "run_cloud_publisher.py",
            "--live",
            "--now", "2026-10-11T10:00:00+02:00",
            "--queue-file", str(self.queue_file),
            "--log-file", str(self.temp_dir / "test.log")
        ]
        # Outside window at 10:00 AM, should exit 0 with window skip message
        with patch.dict(os.environ, {"EVENT_NAME": "workflow_dispatch", "MANUAL_DRY_RUN": "false"}):
            with patch.object(sys, "argv", test_args):
                with self.assertRaises(SystemExit) as ctx:
                    cloud_main()
                self.assertEqual(ctx.exception.code, 0)

    def test_manual_live_run_bypasses_window_check_with_ignore_window(self):
        """Verify manual live run with ignore_window evaluates queue outside window."""
        from scripts.run_cloud_publisher import main as cloud_main

        with open(self.queue_file, "w", encoding="utf-8") as f:
            json.dump({
                "queue_id": "test_q",
                "items": []
            }, f)

        test_args = [
            "run_cloud_publisher.py",
            "--live",
            "--ignore-window",
            "--now", "2026-10-11T10:00:00+02:00",
            "--queue-file", str(self.queue_file),
            "--log-file", str(self.temp_dir / "test.log")
        ]
        mock_secrets = {
            "FACEBOOK_PAGE_ACCESS_TOKEN": "mock_fb",
            "FACEBOOK_PAGE_ID": "123456",
            "THREADS_ACCESS_TOKEN": "mock_th",
            "THREADS_USER_ID": "mock_uid",
            "PINTEREST_ACCESS_TOKEN": "mock_pin",
            "PINTEREST_LIFEMODEHQ_ACCESS_TOKEN": "mock_pin_life",
        }
        with patch.dict(os.environ, mock_secrets, clear=True):
            with patch.object(sys, "argv", test_args):
                # Empty queue, should evaluate and complete cleanly without error
                cloud_main()

    def test_live_run_exits_with_error_on_publishing_failure(self):
        """Verify live run exits with code 1 when due items encounter publishing failure."""
        from scripts.run_cloud_publisher import main as cloud_main

        # Create queue with a ready item pointing to non-existent package
        with open(self.queue_file, "w", encoding="utf-8") as f:
            json.dump({
                "queue_id": "test_q",
                "items": [
                    {
                        "queue_item_id": "item_fail_test",
                        "original_url": "https://link.amazon/fail",
                        "asin": "B0FAIL",
                        "product_name": "Failing Product",
                        "scheduled_date": "2026-10-11",
                        "daily_slot": 1,
                        "scheduled_at": "2026-10-11T12:00:00+02:00",
                        "package_reference": "non_existent_package.json",
                        "status": "READY",
                        "validation_status": "READY_FOR_REVIEW",
                        "errors": [],
                        "created_at": "2026-10-01T00:00:00Z",
                    }
                ]
            }, f)

        test_args = [
            "run_cloud_publisher.py",
            "--live",
            "--ignore-window",
            "--now", "2026-10-11T12:30:00+02:00",
            "--queue-file", str(self.queue_file),
            "--log-file", str(self.temp_dir / "test.log")
        ]
        mock_secrets = {
            "FACEBOOK_PAGE_ACCESS_TOKEN": "mock_fb",
            "FACEBOOK_PAGE_ID": "123456",
            "THREADS_ACCESS_TOKEN": "mock_th",
            "THREADS_USER_ID": "mock_uid",
            "PINTEREST_ACCESS_TOKEN": "mock_pin",
            "PINTEREST_LIFEMODEHQ_ACCESS_TOKEN": "mock_pin_life",
        }
        with patch.dict(os.environ, mock_secrets, clear=True):
            with patch.object(sys, "argv", test_args):
                with self.assertRaises(SystemExit) as ctx:
                    cloud_main()
                self.assertEqual(ctx.exception.code, 1)


if __name__ == "__main__":
    unittest.main()

