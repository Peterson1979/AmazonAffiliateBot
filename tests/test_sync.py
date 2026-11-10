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
    merge_slot_claims,
    merge_import_batches,
    find_sync_stage_files,
    sync_with_github,
    SYNC_LOCK,
)
from publishing.queue import (
    PublishingQueue,
    QueueItem,
    next_queue_item_id,
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

    def test_merge_preserves_local_published_over_remote_partial_and_ready(self):
        """Local LIVE PUBLISHED status and IDs never downgrade to remote PARTIALLY_PUBLISHED or READY."""
        local = {
            "queue_id": "test_q",
            "items": [
                {
                    "queue_item_id": "item_026_B09X5BQ969",
                    "asin": "B09X5BQ969",
                    "status": STATUS_PUBLISHED,
                    "scheduled_date": "2026-11-03",
                    "daily_slot": 2,
                    "scheduled_at": "2026-11-03T19:00:00+01:00",
                    "published_at": "2026-11-04T12:22:38+01:00",
                    "errors": [],
                    "publishing_results": {
                        "facebook": {"status": "PUBLISHED", "mode": "LIVE", "post_id": "fb_1"},
                        "pinterest": {"status": "PUBLISHED", "mode": "LIVE", "pin_id": "pin_1"},
                        "pinterest_lifemodehq": {"status": "PUBLISHED", "mode": "LIVE", "pin_id": "pin_2"},
                        "threads": {"status": "PUBLISHED", "mode": "LIVE", "post_id": "th_1"},
                    },
                },
                {
                    "queue_item_id": "item_028_B07H9PZDQW",
                    "asin": "B07H9PZDQW",
                    "status": STATUS_PUBLISHED,
                    "scheduled_date": "2026-11-04",
                    "daily_slot": 2,
                    "scheduled_at": "2026-11-04T21:00:00+01:00",
                    "published_at": "2026-11-04T22:21:40+01:00",
                    "errors": [],
                    "publishing_results": {
                        "facebook": {"status": "PUBLISHED", "mode": "LIVE", "post_id": "fb_28"},
                    },
                },
                {
                    "queue_item_id": "item_031_B0BW93676T",
                    "asin": "B0BW93676T",
                    "status": STATUS_READY,
                    "scheduled_date": "2026-11-06",
                    "daily_slot": 1,
                    "scheduled_at": "2026-11-06T13:00:00+01:00",
                },
                {
                    "queue_item_id": "item_032_B0CTJGJL2T",
                    "asin": "B0CTJGJL2T",
                    "status": STATUS_READY,
                    "scheduled_date": "2026-11-06",
                    "daily_slot": 2,
                    "scheduled_at": "2026-11-06T21:00:00+01:00",
                    "media_type": "video",
                    "video_url": "https://pub-example.r2.dev/amazon-affiliate-bot/videos/B0CTJGJL2T/clip.mp4",
                },
            ],
        }
        remote = {
            "queue_id": "test_q",
            "items": [
                {
                    "queue_item_id": "item_026_B09X5BQ969",
                    "asin": "B09X5BQ969",
                    "status": STATUS_PARTIALLY_PUBLISHED,
                    "scheduled_date": "2026-11-03",
                    "daily_slot": 2,
                    "scheduled_at": "2026-11-03T21:00:00+01:00",
                    "published_at": "2026-11-04T11:13:47+01:00",
                    "errors": ["Pinterest 401"],
                    "publishing_results": {
                        "pinterest": {"status": "FAILED", "mode": "LIVE"},
                        "pinterest_lifemodehq": {"status": "PUBLISHED", "mode": "LIVE", "pin_id": "pin_2"},
                    },
                },
                {
                    "queue_item_id": "item_028_B07H9PZDQW",
                    "asin": "B07H9PZDQW",
                    "status": STATUS_READY,
                    "scheduled_date": "2026-11-04",
                    "daily_slot": 2,
                    "scheduled_at": "2026-11-04T21:00:00+01:00",
                },
                {
                    "queue_item_id": "item_031_B0BW93676T",
                    "asin": "B0BW93676T",
                    "status": STATUS_READY,
                    "scheduled_date": "2026-11-06",
                    "daily_slot": 1,
                    "scheduled_at": "2026-11-06T15:00:00+01:00",
                },
            ],
        }

        merged, _ = merge_publishing_queues(local, remote)
        by_id = {it["queue_item_id"]: it for it in merged["items"]}

        self.assertEqual(by_id["item_026_B09X5BQ969"]["status"], STATUS_PUBLISHED)
        self.assertEqual(by_id["item_026_B09X5BQ969"]["errors"], [])
        self.assertEqual(by_id["item_026_B09X5BQ969"]["publishing_results"]["pinterest"]["status"], "PUBLISHED")
        self.assertEqual(by_id["item_026_B09X5BQ969"]["publishing_results"]["facebook"]["post_id"], "fb_1")
        self.assertEqual(by_id["item_028_B07H9PZDQW"]["status"], STATUS_PUBLISHED)
        self.assertEqual(by_id["item_031_B0BW93676T"]["scheduled_at"], "2026-11-06T15:00:00+01:00")
        self.assertEqual(by_id["item_032_B0CTJGJL2T"]["scheduled_at"], "2026-11-06T21:00:00+01:00")
        self.assertEqual(by_id["item_032_B0CTJGJL2T"]["media_type"], "video")


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

    def test_stage_files_include_creatives_directory(self):
        """Image creatives (.jpg, .png, .json) are staged with the queue."""
        queue_file = self.temp_dir / "data" / "queue" / "publishing_queue.json"
        queue_file.write_text("{}", encoding="utf-8")

        creatives_dir = self.temp_dir / "data" / "creatives" / "B0FLOW"
        creatives_dir.mkdir(parents=True, exist_ok=True)
        (creatives_dir / "flow_creatives.json").write_text("{}", encoding="utf-8")
        (creatives_dir / "everyday_pick_flow.jpg").write_bytes(b"\xff\xd8\xff\xe0")

        merged_queue = {
            "items": [
                {
                    "asin": "B0FLOW",
                    "package_reference": "data/generated/asin_B0FLOW_content_package.json",
                }
            ]
        }
        stage_files = find_sync_stage_files(merged_queue, self.temp_dir)
        self.assertIn("data/creatives/B0FLOW/flow_creatives.json", stage_files)
        self.assertIn("data/creatives/B0FLOW/everyday_pick_flow.jpg", stage_files)


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


class TestSlotClaimMerging(unittest.TestCase):
    """Regression tests: merge_publishing_queues must preserve slot_claims.

    Slot claims are what prevents a scheduled slot from running twice, and
    they live at queue level (not on the items), so an implicit merge drops
    them silently.
    """

    KEY = "2026-10-30-slot-1"

    def test_claims_existing_on_only_one_side_are_preserved(self):
        local = {
            "queue_id": "test_q",
            "slot_claims": {
                "2026-10-30-slot-1": {
                    "state": "CLAIMED",
                    "claimed_at": "2026-10-30T13:05:00+01:00",
                    "attempt": 1,
                }
            },
            "items": [],
        }
        remote = {
            "queue_id": "test_q",
            "slot_claims": {
                "2026-10-31-slot-2": {
                    "state": "COMPLETED",
                    "claimed_at": "2026-10-31T19:00:00+01:00",
                    "attempt": 2,
                }
            },
            "items": [],
        }
        merged, _ = merge_publishing_queues(local, remote)
        self.assertEqual(
            set(merged["slot_claims"]),
            {"2026-10-30-slot-1", "2026-10-31-slot-2"},
        )
        self.assertEqual(merged["slot_claims"]["2026-10-30-slot-1"]["state"], "CLAIMED")
        self.assertEqual(merged["slot_claims"]["2026-10-31-slot-2"]["state"], "COMPLETED")

    def test_conflicting_claim_prefers_completed(self):
        completed = {"state": "COMPLETED", "claimed_at": "2026-10-30T13:10:00+01:00", "attempt": 1}
        claimed = {"state": "CLAIMED", "claimed_at": "2026-10-30T13:30:00+01:00", "attempt": 3}

        local, remote = (
            {"queue_id": "q", "slot_claims": {self.KEY: dict(claimed)}, "items": []},
            {"queue_id": "q", "slot_claims": {self.KEY: dict(completed)}, "items": []},
        )
        merged, _ = merge_publishing_queues(local, remote)
        self.assertEqual(merged["slot_claims"][self.KEY]["state"], "COMPLETED")

        # The conflict must resolve the same way regardless of which side is local.
        merged_reversed, _ = merge_publishing_queues(remote, local)
        self.assertEqual(merged_reversed["slot_claims"][self.KEY]["state"], "COMPLETED")

    def test_conflicting_claims_prefer_newer_claimed_at(self):
        older = {"state": "CLAIMED", "claimed_at": "2026-10-30T19:00:00+01:00", "attempt": 1}
        newer = {"state": "RETRYABLE", "claimed_at": "2026-10-30T20:15:00+01:00", "attempt": 2}

        local_newer = {"queue_id": "q", "slot_claims": {self.KEY: dict(newer)}, "items": []}
        remote_older = {"queue_id": "q", "slot_claims": {self.KEY: dict(older)}, "items": []}

        merged, _ = merge_publishing_queues(local_newer, remote_older)
        self.assertEqual(merged["slot_claims"][self.KEY]["attempt"], 2)

        merged_reversed, _ = merge_publishing_queues(remote_older, local_newer)
        self.assertEqual(merged_reversed["slot_claims"][self.KEY]["attempt"], 2)

    def test_merge_helper_direct_contract(self):
        # Key only on one side, key on both sides, malformed input ignored.
        merged = merge_slot_claims(
            {"a": {"state": "CLAIMED", "claimed_at": "2026-10-30T13:00:00+01:00"}, "b": "garbage"},
            {"c": {"state": "COMPLETED"}},
        )
        self.assertEqual(set(merged), {"a", "c"})
        self.assertEqual(merge_slot_claims(None, "not-a-dict"), {})

    def test_merged_queue_round_trips_claims_through_persistence(self):
        local = {
            "queue_id": "q",
            "slot_claims": {self.KEY: {"state": "COMPLETED", "claimed_at": "2026-10-30T13:10:00+01:00"}},
            "items": [
                {
                    "queue_item_id": "item_001_B001",
                    "asin": "B001",
                    "status": STATUS_READY,
                    "scheduled_date": "2026-10-30",
                    "daily_slot": 1,
                }
            ],
        }
        merged, _ = merge_publishing_queues(local, {"queue_id": "q", "items": []})
        reloaded = PublishingQueue.from_dict(merged)
        self.assertEqual(reloaded.slot_claims[self.KEY]["state"], "COMPLETED")


class TestDivergentQueueIdCollision(unittest.TestCase):
    """Regression tests: queue IDs must never be derived from len(items) + 1."""

    def test_next_id_uses_highest_suffix_plus_one(self):
        ids = ["item_001_B0A", "item_004_B0B", "item_017_B0C"]
        self.assertEqual(next_queue_item_id(ids, "B0D"), "item_018_B0D")

    def test_next_id_accepts_queue_item_objects_and_dicts(self):
        items = [
            QueueItem(queue_item_id="item_003_B0A", original_url=""),
            {"queue_item_id": "item_009_B0B"},
        ]
        self.assertEqual(next_queue_item_id(items, "B0C"), "item_010_B0C")

    def test_local_product_with_colliding_id_is_kept_under_a_fresh_id(self):
        """Diverged queues can reuse an identifier for two products; neither may vanish."""
        local = {
            "queue_id": "test_q",
            "items": [
                {
                    "queue_item_id": "item_004_B0NEW",
                    "asin": "B0NEW",
                    "status": STATUS_READY,
                    "scheduled_date": "2026-11-02",
                    "daily_slot": 1,
                }
            ],
        }
        remote = {
            "queue_id": "test_q",
            "items": [
                {
                    "queue_item_id": "item_004_B0NEW",
                    "asin": "B0OTHER",
                    "status": STATUS_READY,
                    "scheduled_date": "2026-11-02",
                    "daily_slot": 2,
                }
            ],
        }
        merged, summary = merge_publishing_queues(local, remote)

        by_asin = {it["asin"]: it for it in merged["items"]}
        self.assertEqual(set(by_asin), {"B0NEW", "B0OTHER"})
        self.assertEqual(summary["local_added_count"], 1)

        ids = [it["queue_item_id"] for it in merged["items"]]
        self.assertEqual(len(ids), len(set(ids)), "queue item identifiers must stay unique")
        self.assertNotEqual(by_asin["B0NEW"]["queue_item_id"], "item_004_B0NEW")

    def test_new_local_product_without_collision_keeps_its_id(self):
        local = {
            "queue_id": "test_q",
            "items": [
                {
                    "queue_item_id": "item_002_B0LOCAL",
                    "asin": "B0LOCAL",
                    "status": STATUS_READY,
                    "scheduled_date": "2026-11-03",
                    "daily_slot": 1,
                }
            ],
        }
        remote = {
            "queue_id": "test_q",
            "items": [
                {
                    "queue_item_id": "item_001_B0REMOTE",
                    "asin": "B0REMOTE",
                    "status": STATUS_READY,
                    "scheduled_date": "2026-11-03",
                    "daily_slot": 2,
                }
            ],
        }
        merged, summary = merge_publishing_queues(local, remote)
        self.assertEqual(summary["local_added_count"], 1)
        self.assertIn("item_002_B0LOCAL", [it["queue_item_id"] for it in merged["items"]])

    def test_same_asin_on_both_sides_is_deduplicated(self):
        local = {
            "queue_id": "test_q",
            "items": [{"queue_item_id": "item_009_B0DUP", "asin": "B0DUP", "status": STATUS_READY}],
        }
        remote = {
            "queue_id": "test_q",
            "items": [{"queue_item_id": "item_002_B0DUP", "asin": "B0DUP", "status": STATUS_PUBLISHED}],
        }
        merged, summary = merge_publishing_queues(local, remote)
        self.assertEqual(len(merged["items"]), 1)
        self.assertEqual(summary["local_added_count"], 0)

    def test_merge_paired_image_video_entries_both_preserved(self):
        """Paired image and video items for the same ASIN must both be preserved during merge."""
        remote = {
            "queue_id": "test_q",
            "items": [
                {
                    "queue_item_id": "item_036_B0PAIR",
                    "asin": "B0PAIR",
                    "media_type": "video",
                    "video_url": "https://pub.r2.dev/video.mp4",
                    "status": STATUS_PARTIALLY_PUBLISHED,
                    "scheduled_date": "2026-11-08",
                    "daily_slot": 1,
                }
            ],
        }
        local = {
            "queue_id": "test_q",
            "items": [
                {
                    "queue_item_id": "item_036_B0PAIR",
                    "asin": "B0PAIR",
                    "media_type": "video",
                    "video_url": "https://pub.r2.dev/video.mp4",
                    "status": STATUS_READY,
                    "scheduled_date": "2026-11-08",
                    "daily_slot": 1,
                },
                {
                    "queue_item_id": "item_038_B0PAIR",
                    "asin": "B0PAIR",
                    "media_type": "image",
                    "video_url": None,
                    "status": STATUS_READY,
                    "scheduled_date": "2026-11-09",
                    "daily_slot": 1,
                },
            ],
        }
        merged, summary = merge_publishing_queues(local, remote)
        self.assertEqual(len(merged["items"]), 2)
        self.assertEqual(summary["local_added_count"], 1)
        item_ids = [it["queue_item_id"] for it in merged["items"]]
        self.assertIn("item_036_B0PAIR", item_ids)
        self.assertIn("item_038_B0PAIR", item_ids)

    def test_merge_upgrades_status_to_published_when_all_destinations_confirmed(self):
        """When local + remote combined platform results cover all 4 destinations, status is PUBLISHED."""
        remote = {
            "queue_id": "test_q",
            "items": [
                {
                    "queue_item_id": "item_001_B0TEST",
                    "asin": "B0TEST",
                    "status": STATUS_PARTIALLY_PUBLISHED,
                    "publishing_results": {
                        "facebook": {"platform": "facebook", "status": STATUS_FAILED, "mode": "LIVE"},
                        "pinterest": {"platform": "pinterest", "status": STATUS_PUBLISHED, "mode": "LIVE", "pin_id": "p1"},
                        "pinterest_lifemodehq": {"platform": "pinterest", "status": STATUS_PUBLISHED, "mode": "LIVE", "pin_id": "p2"},
                        "threads": {"platform": "threads", "status": STATUS_PUBLISHED, "mode": "LIVE", "post_id": "t1"},
                    },
                }
            ],
        }
        local = {
            "queue_id": "test_q",
            "items": [
                {
                    "queue_item_id": "item_001_B0TEST",
                    "asin": "B0TEST",
                    "status": STATUS_PARTIALLY_PUBLISHED,
                    "publishing_results": {
                        "facebook": {"platform": "facebook", "status": STATUS_PUBLISHED, "mode": "LIVE", "video_id": "fb1"},
                    },
                }
            ],
        }
        merged, summary = merge_publishing_queues(local, remote)
        merged_item = merged["items"][0]
        self.assertEqual(merged_item["status"], STATUS_PUBLISHED)
        self.assertEqual(merged_item["errors"], [])
        self.assertEqual(merged_item["publishing_results"]["facebook"]["status"], STATUS_PUBLISHED)
        self.assertEqual(merged_item["publishing_results"]["pinterest"]["pin_id"], "p1")


class TestImportBatchHistorySync(unittest.TestCase):
    """Focused tests for synchronizing data/batches/import_batches.json."""

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_batches_sync_"))
        (self.temp_dir / "data" / "batches").mkdir(parents=True)
        (self.temp_dir / "data" / "queue").mkdir(parents=True)

    def tearDown(self):
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_stage_files_include_import_batch_history(self):
        batches_file = self.temp_dir / "data" / "batches" / "import_batches.json"
        batches_file.write_text("[]", encoding="utf-8")

        stage_files = find_sync_stage_files({"items": []}, self.temp_dir)
        self.assertIn("data/batches/import_batches.json", stage_files)
        self.assertIn("data/queue/publishing_queue.json", stage_files)

    def test_stage_files_omit_absent_batch_history(self):
        stage_files = find_sync_stage_files({"items": []}, self.temp_dir)
        self.assertNotIn("data/batches/import_batches.json", stage_files)

    def test_merge_keeps_local_batches_and_prefers_newer_remote(self):
        local = [
            {"batch_id": "batch_1", "created_at": "2026-10-01T10:00:00+02:00", "accepted_count": 2},
            {"batch_id": "batch_2", "created_at": "2026-10-05T10:00:00+02:00", "accepted_count": 1},
        ]
        remote = [
            {"batch_id": "batch_2", "created_at": "2026-10-20T09:00:00+02:00", "accepted_count": 5},
            {"batch_id": "batch_3", "created_at": "2026-10-25T09:00:00+02:00", "accepted_count": 3},
        ]
        merged = merge_import_batches(local, remote)

        self.assertEqual([b["batch_id"] for b in merged], ["batch_1", "batch_2", "batch_3"])
        by_id = {b["batch_id"]: b for b in merged}
        # Local-only history is never dropped.
        self.assertEqual(by_id["batch_1"]["accepted_count"], 2)
        # Newer remote history wins over the stale local record.
        self.assertEqual(by_id["batch_2"]["created_at"], "2026-10-20T09:00:00+02:00")
        self.assertEqual(by_id["batch_2"]["accepted_count"], 5)
        # Remote-only batches are appended.
        self.assertEqual(by_id["batch_3"]["accepted_count"], 3)

    def test_stale_local_batch_never_overwrites_newer_remote(self):
        local = [{"batch_id": "batch_x", "created_at": "2026-10-01T10:00:00+02:00", "accepted_count": 1}]
        remote = [{"batch_id": "batch_x", "created_at": "2026-10-28T10:00:00+02:00", "accepted_count": 9}]
        merged = merge_import_batches(local, remote)
        self.assertEqual(merged[0]["accepted_count"], 9)

    def test_invalid_ledger_inputs_are_safe(self):
        self.assertEqual(merge_import_batches(None, "not-a-list"), [])
        self.assertEqual(merge_import_batches([{"no_id": True}], []), [])


class TestAutoSyncAfterProductImport(unittest.TestCase):
    """POST /add-product synchronizes accepted products to GitHub automatically."""

    def setUp(self):
        # No TESTING flag here: this class exercises the production path with
        # execute_sync_with_github() itself mocked out (sync never executes).
        self.app = create_app({"SECRET_KEY": "test-auto-sync"})
        self.client = self.app.test_client()
        self.addCleanup(os.environ.pop, "AUTO_SYNC_AFTER_IMPORT", None)
        os.environ.pop("AUTO_SYNC_AFTER_IMPORT", None)

    @staticmethod
    def _import_result(accepted=0, rejected=0, success=True):
        return {
            "success": success,
            "total": accepted + rejected,
            "accepted": accepted,
            "rejected": rejected,
            "results": [],
            "message": "ok",
        }

    @patch("web.app.execute_sync_with_github")
    @patch("web.app.add_products_from_urls")
    def test_accepted_products_are_synchronized(self, mock_add, mock_sync):
        mock_add.return_value = self._import_result(accepted=2)
        mock_sync.return_value = {
            "success": True,
            "message": "Synchronized with GitHub: uploaded 2 new product(s).",
        }
        response = self.client.post("/add-product", data={"urls": "https://amazon.test/dp/B0SYNCTEST"})

        self.assertEqual(response.status_code, 200)
        mock_sync.assert_called_once()
        html = response.get_data(as_text=True)
        self.assertIn("synchronized to GitHub", html)
        self.assertIn("uploaded 2 new product(s)", html)

    @patch("web.app.execute_sync_with_github")
    @patch("web.app.add_products_from_urls")
    def test_sync_failure_preserves_local_data_and_warns(self, mock_add, mock_sync):
        mock_add.return_value = self._import_result(accepted=1)
        mock_sync.return_value = {"success": False, "error": "push to GitHub failed"}
        response = self.client.post("/add-product", data={"urls": "https://amazon.test/dp/B0SYNCTEST"})

        html = response.get_data(as_text=True)
        self.assertIn("Automatic GitHub synchronization failed", html)
        self.assertIn("push to GitHub failed", html)
        self.assertIn("may not yet be available to scheduled GitHub Actions publishing", html)
        # Never claim a synchronization that did not happen.
        self.assertNotIn("were synchronized to GitHub", html)

    @patch("web.app.execute_sync_with_github")
    @patch("web.app.add_products_from_urls")
    def test_no_accepted_product_means_no_sync(self, mock_add, mock_sync):
        mock_add.return_value = self._import_result(accepted=0, rejected=2)
        self.client.post("/add-product", data={"urls": "https://amazon.test/dp/B0DUPLICATE"})
        mock_sync.assert_not_called()

    @patch("web.app.execute_sync_with_github")
    @patch("web.app.add_products_from_urls")
    def test_failed_import_never_triggers_sync(self, mock_add, mock_sync):
        mock_add.return_value = {"success": False, "message": "No valid URLs provided.",
                                 "total": 0, "accepted": 0, "rejected": 0, "results": []}
        self.client.post("/add-product", data={"urls": "https://amazon.test/dp/B0INVALID"})
        mock_sync.assert_not_called()

    @patch("web.app.execute_sync_with_github")
    @patch("web.app.add_products_from_urls")
    def test_testing_mode_skips_sync(self, mock_add, mock_sync):
        app = create_app({"TESTING": True, "SECRET_KEY": "test-auto-sync-testing"})
        mock_add.return_value = self._import_result(accepted=1)
        app.test_client().post("/add-product", data={"urls": "https://amazon.test/dp/B0SYNCTEST"})
        mock_sync.assert_not_called()

    @patch("web.app.execute_sync_with_github")
    @patch("web.app.add_products_from_urls")
    def test_auto_sync_opt_out_environment_variable(self, mock_add, mock_sync):
        os.environ["AUTO_SYNC_AFTER_IMPORT"] = "0"
        mock_add.return_value = self._import_result(accepted=1)
        self.client.post("/add-product", data={"urls": "https://amazon.test/dp/B0SYNCTEST"})
        mock_sync.assert_not_called()

    @patch("web.app.execute_sync_with_github")
    @patch("web.app.add_products_from_urls")
    def test_sync_is_enabled_by_default(self, mock_add, mock_sync):
        os.environ.pop("AUTO_SYNC_AFTER_IMPORT", None)
        mock_add.return_value = self._import_result(accepted=1)
        mock_sync.return_value = {"success": True, "message": "Synchronized with GitHub: up to date."}
        self.client.post("/add-product", data={"urls": "https://amazon.test/dp/B0SYNCTEST"})
        mock_sync.assert_called_once()


if __name__ == "__main__":
    unittest.main()
