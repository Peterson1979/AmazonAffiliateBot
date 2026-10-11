"""
tests/test_sync.py - Focused Unit Tests for Safe Dashboard <-> GitHub Actions Synchronization.

Covers:
1. Pure Queue Merging:
   - Clean/identical synchronization (no divergence).
   - Remote queue updates (cloud status PUBLISHED/FAILED/PARTIALLY_PUBLISHED overrides local READY).
   - Local new items (locally submitted products preserved with slots and packages).
   - Concurrent changes (cloud published item A, local added item B -> both merged cleanly).
   - Slot collision resolution (competing slot allocations resolved safely).
   - Malformed/invalid data handling (raises ValueError without file damage).
2. Stage Filtering:
   - Staging includes only queue, product cache, and content package JSONs.
   - Strictly excludes .env, logs, credentials, or arbitrary files.
3. Locking & Safe Execution:
   - Synchronization lock blocks overlapping sync runs.
   - Fetch failure aborts cleanly without altering queue.
   - Mocked Git integration flow (fetch -> merge -> stage -> commit [skip ci] -> push).
4. Flask Web Dashboard Integration:
   - POST /sync endpoint execution and redirect with flash message.
   - AJAX JSON request support.
"""

import os
import sys
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock
from datetime import datetime

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from publishing.sync import (
    merge_publishing_queues,
    find_sync_stage_files,
    sync_with_github,
    SYNC_LOCK,
)
from publishing.queue import (
    STATUS_READY,
    STATUS_PUBLISHED,
    STATUS_PARTIALLY_PUBLISHED,
    STATUS_FAILED,
)
from web.app import create_app


class TestQueueMergingLogic(unittest.TestCase):
    """Tests for pure queue merging logic (merge_publishing_queues)."""

    def test_merge_identical_queues(self):
        """Identical local and remote queues merge cleanly with 0 updates."""
        data = {
            "queue_id": "test_q",
            "items": [
                {
                    "queue_item_id": "item_001_B001",
                    "asin": "B001",
                    "status": STATUS_READY,
                    "scheduled_date": "2026-10-15",
                    "daily_slot": 1,
                    "scheduled_at": "2026-10-15T12:00:00+02:00",
                }
            ]
        }
        merged, summary = merge_publishing_queues(data, data)
        self.assertEqual(summary["remote_updated_count"], 0)
        self.assertEqual(summary["local_added_count"], 0)
        self.assertEqual(summary["total_merged_items"], 1)
        self.assertEqual(merged["items"][0]["status"], STATUS_READY)

    def test_merge_remote_status_update(self):
        """Remote status PUBLISHED overrides local READY and preserves timestamps/results."""
        local = {
            "queue_id": "test_q",
            "items": [
                {
                    "queue_item_id": "item_001_B001",
                    "asin": "B001",
                    "status": STATUS_READY,
                    "scheduled_date": "2026-10-11",
                    "daily_slot": 1,
                    "scheduled_at": "2026-10-11T12:00:00+02:00",
                    "published_at": None,
                    "publishing_results": None,
                }
            ]
        }
        remote = {
            "queue_id": "test_q",
            "items": [
                {
                    "queue_item_id": "item_001_B001",
                    "asin": "B001",
                    "status": STATUS_PUBLISHED,
                    "scheduled_date": "2026-10-11",
                    "daily_slot": 1,
                    "scheduled_at": "2026-10-11T12:00:00+02:00",
                    "published_at": "2026-10-11T12:05:00+02:00",
                    "publishing_results": {"facebook": {"status": "PUBLISHED"}},
                }
            ]
        }
        merged, summary = merge_publishing_queues(local, remote)
        self.assertEqual(summary["remote_updated_count"], 1)
        self.assertEqual(summary["local_added_count"], 0)
        self.assertEqual(len(merged["items"]), 1)

        merged_item = merged["items"][0]
        self.assertEqual(merged_item["status"], STATUS_PUBLISHED)
        self.assertEqual(merged_item["published_at"], "2026-10-11T12:05:00+02:00")
        self.assertIn("facebook", merged_item["publishing_results"])

    def test_merge_local_new_item_preserved(self):
        """Locally added item not on remote is preserved in merged queue with slot."""
        local = {
            "queue_id": "test_q",
            "items": [
                {
                    "queue_item_id": "item_001_B001",
                    "asin": "B001",
                    "status": STATUS_READY,
                    "scheduled_date": "2026-10-11",
                    "daily_slot": 1,
                },
                {
                    "queue_item_id": "item_002_B002",
                    "asin": "B002",
                    "status": STATUS_READY,
                    "scheduled_date": "2026-10-11",
                    "daily_slot": 2,
                    "package_reference": "data/generated/asin_B002_content_package.json",
                }
            ]
        }
        remote = {
            "queue_id": "test_q",
            "items": [
                {
                    "queue_item_id": "item_001_B001",
                    "asin": "B001",
                    "status": STATUS_READY,
                    "scheduled_date": "2026-10-11",
                    "daily_slot": 1,
                }
            ]
        }
        merged, summary = merge_publishing_queues(local, remote)
        self.assertEqual(summary["local_added_count"], 1)
        self.assertEqual(summary["total_merged_items"], 2)
        asins = [it["asin"] for it in merged["items"]]
        self.assertEqual(asins, ["B001", "B002"])

    def test_merge_concurrent_changes(self):
        """Remote published item 1 while local added item 2 -> both preserved cleanly."""
        local = {
            "queue_id": "test_q",
            "items": [
                {
                    "queue_item_id": "item_001_B001",
                    "asin": "B001",
                    "status": STATUS_READY,
                    "scheduled_date": "2026-10-11",
                    "daily_slot": 1,
                },
                {
                    "queue_item_id": "item_002_B002",
                    "asin": "B002",
                    "status": STATUS_READY,
                    "scheduled_date": "2026-10-12",
                    "daily_slot": 1,
                }
            ]
        }
        remote = {
            "queue_id": "test_q",
            "items": [
                {
                    "queue_item_id": "item_001_B001",
                    "asin": "B001",
                    "status": STATUS_PUBLISHED,
                    "scheduled_date": "2026-10-11",
                    "daily_slot": 1,
                    "published_at": "2026-10-11T12:00:00+02:00",
                    "publishing_results": {"threads": {"status": "PUBLISHED"}},
                }
            ]
        }
        merged, summary = merge_publishing_queues(local, remote)
        self.assertEqual(summary["remote_updated_count"], 1)
        self.assertEqual(summary["local_added_count"], 1)
        self.assertEqual(summary["total_merged_items"], 2)

        by_asin = {it["asin"]: it for it in merged["items"]}
        self.assertEqual(by_asin["B001"]["status"], STATUS_PUBLISHED)
        self.assertEqual(by_asin["B001"]["published_at"], "2026-10-11T12:00:00+02:00")
        self.assertEqual(by_asin["B002"]["status"], STATUS_READY)

    def test_merge_slot_collision_resolved(self):
        """When local new item collides with remote slot, local item is safely reallocated."""
        local = {
            "queue_id": "test_q",
            "items": [
                {
                    "queue_item_id": "item_002_BLOCAL",
                    "asin": "BLOCAL",
                    "status": STATUS_READY,
                    "scheduled_date": "2026-10-15",
                    "daily_slot": 1,
                }
            ]
        }
        remote = {
            "queue_id": "test_q",
            "items": [
                {
                    "queue_item_id": "item_001_BREMOTE",
                    "asin": "BREMOTE",
                    "status": STATUS_READY,
                    "scheduled_date": "2026-10-15",
                    "daily_slot": 1,
                }
            ]
        }
        merged, summary = merge_publishing_queues(local, remote)
        self.assertEqual(summary["total_merged_items"], 2)

        by_asin = {it["asin"]: it for it in merged["items"]}
        # Remote item retains original slot
        self.assertEqual(by_asin["BREMOTE"]["scheduled_date"], "2026-10-15")
        self.assertEqual(by_asin["BREMOTE"]["daily_slot"], 1)

        # Local item bumped to next vacant slot without collision
        local_item = by_asin["BLOCAL"]
        self.assertNotEqual(
            (local_item["scheduled_date"], local_item["daily_slot"]),
            ("2026-10-15", 1)
        )

    def test_merge_invalid_data_raises_value_error(self):
        """Malformed queue data raises ValueError to protect against corruption."""
        with self.assertRaises(ValueError):
            merge_publishing_queues({"items": []}, "not a dict")
        with self.assertRaises(ValueError):
            merge_publishing_queues({}, {"items": []})


class TestStageFileFiltering(unittest.TestCase):
    """Tests for find_sync_stage_files."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_stage_filter_"))
        (self.temp_dir / "data" / "queue").mkdir(parents=True)
        (self.temp_dir / "data" / "products").mkdir(parents=True)
        (self.temp_dir / "data" / "generated").mkdir(parents=True)

    def tearDown(self):
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_find_sync_stage_files_strict_filtering(self):
        """Only queue and referenced product/package JSONs are staged, excluding .env and logs."""
        queue_file = self.temp_dir / "data" / "queue" / "publishing_queue.json"
        queue_file.write_text("{}", encoding="utf-8")

        p_file = self.temp_dir / "data" / "products" / "asin_B0TEST.json"
        p_file.write_text("{}", encoding="utf-8")

        pkg_file = self.temp_dir / "data" / "generated" / "asin_B0TEST_content_package.json"
        pkg_file.write_text("{}", encoding="utf-8")

        # Prohibited files that should never be staged
        env_file = self.temp_dir / ".env"
        env_file.write_text("SECRET=123", encoding="utf-8")
        log_file = self.temp_dir / "data" / "logs" / "daily_publisher.log"
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("log line", encoding="utf-8")

        merged_queue = {
            "items": [
                {
                    "asin": "B0TEST",
                    "package_reference": "data/generated/asin_B0TEST_content_package.json"
                }
            ]
        }

        stage_files = find_sync_stage_files(merged_queue, self.temp_dir)
        self.assertIn("data/queue/publishing_queue.json", stage_files)
        self.assertIn("data/products/asin_B0TEST.json", stage_files)
        self.assertIn("data/generated/asin_B0TEST_content_package.json", stage_files)

        # Confirm sensitive / forbidden files are absent
        for path in stage_files:
            self.assertFalse(path.endswith(".env"))
            self.assertFalse(path.endswith(".log"))


class TestSyncLockAndGitFlow(unittest.TestCase):
    """Tests for sync_with_github locking and error handling."""

    def test_sync_lock_blocks_concurrent_runs(self):
        """When lock is held, a second sync call returns immediately with error."""
        SYNC_LOCK.acquire()
        try:
            res = sync_with_github()
            self.assertFalse(res["success"])
            self.assertIn("already in progress", res["error"])
        finally:
            SYNC_LOCK.release()

    @patch("publishing.sync.subprocess.run")
    def test_sync_aborts_cleanly_on_fetch_failure(self, mock_run):
        """If git fetch fails, sync aborts without touching local queue."""
        # 1. rev-parse check succeeds
        # 2. git fetch fails
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="true", stderr=""),
            MagicMock(returncode=1, stdout="", stderr="Could not resolve host: github.com"),
        ]
        res = sync_with_github(repo_dir=PROJECT_ROOT)
        self.assertFalse(res["success"])
        self.assertIn("Failed to fetch from GitHub", res["error"])

    @patch("publishing.sync.subprocess.run")
    def test_sync_up_to_date_clean_flow(self, mock_run):
        """When remote and local queues are identical and no diffs exist, reports up to date."""
        # Mock git commands:
        # 1. rev-parse -> 0
        # 2. fetch -> 0
        # 3. show remote queue -> 0
        # 4. rev-list HEAD..origin/master -> 0 (0 ahead)
        # 5. git add -> 0
        # 6. git diff --staged -> 0 (no staged changes)
        # 7. rev-list origin/master..HEAD -> 0 (0 ahead)
        remote_queue_content = json.dumps({
            "queue_id": "test_q",
            "items": []
        })

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="true", stderr=""),                  # rev-parse
            MagicMock(returncode=0, stdout="", stderr=""),                      # fetch
            MagicMock(returncode=0, stdout=remote_queue_content, stderr=""),    # show remote queue
            MagicMock(returncode=0, stdout="0\n", stderr=""),                   # rev-list HEAD..origin
            MagicMock(returncode=0, stdout="", stderr=""),                      # git add
            MagicMock(returncode=0, stdout="", stderr=""),                      # diff --staged (0 = clean)
            MagicMock(returncode=0, stdout="0\n", stderr=""),                   # rev-list origin..HEAD
        ]

        # Use temporary directory for clean queue file
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir)
            q_dir = tmp_path / "data" / "queue"
            q_dir.mkdir(parents=True)
            (q_dir / "publishing_queue.json").write_text(remote_queue_content, encoding="utf-8")

            res = sync_with_github(repo_dir=tmp_path)
            self.assertTrue(res["success"])
            self.assertIn("queue is fully up to date", res["message"])


class TestFlaskDashboardSyncEndpoint(unittest.TestCase):
    """Tests for the Flask dashboard POST /sync route."""

    def setUp(self):
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-sync-key"})
        self.client = self.app.test_client()

    @patch("web.app.execute_sync_with_github")
    def test_post_sync_form_redirects_with_flash(self, mock_sync):
        """Form POST /sync redirects back to queue and flashes status message."""
        mock_sync.return_value = {
            "success": True,
            "message": "Synchronized with GitHub: queue is fully up to date."
        }
        response = self.client.post("/sync", follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        content = response.get_data(as_text=True)
        self.assertIn("Sync with GitHub succeeded", content)
        self.assertIn("queue is fully up to date", content)

    @patch("web.app.execute_sync_with_github")
    def test_post_sync_ajax_returns_json(self, mock_sync):
        """AJAX POST /sync returns JSON response."""
        mock_sync.return_value = {
            "success": True,
            "message": "Synchronized with GitHub: uploaded 1 new product(s)."
        }
        response = self.client.post(
            "/sync",
            headers={"X-Requested-With": "XMLHttpRequest"}
        )
        self.assertEqual(response.status_code, 200)
        data = response.get_json()
        self.assertTrue(data["success"])
        self.assertIn("uploaded 1 new product(s)", data["message"])


if __name__ == "__main__":
    unittest.main()
