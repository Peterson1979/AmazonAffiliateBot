"""Tests for portable package refs, claim reconciliation and crash recovery."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from publishing.sync import (
    SYNC_STATE_FILENAME,
    merge_publishing_queues,
    recover_interrupted_sync,
)
from scripts.reconcile_queue_state import reconcile_queue_state


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)


def _read_json(path: Path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _item(item_id: str, asin: str, reference: str) -> dict:
    return {
        "queue_item_id": item_id,
        "original_url": f"https://example.invalid/{asin}",
        "asin": asin,
        "product_name": f"Product {asin}",
        "scheduled_date": "2026-10-30",
        "daily_slot": 1,
        "scheduled_at": "2026-10-30T13:00:00+01:00",
        "package_reference": reference,
        "status": "READY",
        "validation_status": "READY_FOR_REVIEW",
        "errors": [],
        "created_at": "2026-10-01T00:00:00+00:00",
        "published_at": None,
        "publishing_results": None,
        "batch_id": "batch_001",
    }


class TestPortablePackageReferenceMerge(unittest.TestCase):
    def test_absolute_in_repo_reference_is_normalized_on_merge(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            package = repo / "data" / "generated" / "asin_B001_content_package.json"
            _write_json(package, {"asin": "B001"})

            local = {
                "queue_id": "q",
                "items": [_item("item_001_B001", "B001", str(package))],
            }
            merged, _ = merge_publishing_queues(local, {"queue_id": "q", "items": []}, repo_dir=repo)

            self.assertEqual(
                merged["items"][0]["package_reference"],
                "data/generated/asin_B001_content_package.json",
            )

    def test_remote_only_reference_is_normalized_and_outside_path_is_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            package = repo / "data" / "generated" / "asin_B002_content_package.json"
            _write_json(package, {"asin": "B002"})
            outside = str(Path(tmp).parent / "outside_package.json")

            remote = {
                "queue_id": "q",
                "items": [
                    _item("item_002_B002", "B002", str(package)),
                    _item("item_003_B003", "B003", outside),
                ],
            }
            merged, _ = merge_publishing_queues(
                {"queue_id": "q", "items": []}, remote, repo_dir=repo
            )
            by_asin = {item["asin"]: item for item in merged["items"]}

            self.assertEqual(
                by_asin["B002"]["package_reference"],
                "data/generated/asin_B002_content_package.json",
            )
            self.assertEqual(by_asin["B003"]["package_reference"], outside)


class TestReconcileQueueState(unittest.TestCase):
    def test_claims_are_merged_and_only_metadata_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            package = repo / "data" / "generated" / "asin_B001_content_package.json"
            _write_json(package, {"asin": "B001"})

            local_item = _item("item_001_B001", "B001", str(package))
            local_item["published_at"] = "2026-10-30T13:05:00+01:00"
            local_item["publishing_results"] = {"facebook": {"status": "PUBLISHED"}}
            local = {
                "queue_id": "q",
                "slot_claims": {
                    "2026-10-30-slot-1": {
                        "state": "CLAIMED",
                        "claimed_at": "2026-10-30T13:01:00+01:00",
                        "attempt": 2,
                    }
                },
                "items": [local_item],
            }
            source = {
                "queue_id": "q",
                "slot_claims": {
                    # COMPLETED must win over the newer CLAIMED local record.
                    "2026-10-30-slot-1": {
                        "state": "COMPLETED",
                        "claimed_at": "2026-10-30T13:00:00+01:00",
                        "attempt": 1,
                    },
                    "2026-10-29-slot-2": {
                        "state": "RETRYABLE",
                        "claimed_at": "2026-10-29T19:01:00+01:00",
                        "attempt": 1,
                    },
                },
                "items": [],
            }

            reconciled, report = reconcile_queue_state(local, source, repo_dir=repo)

            self.assertEqual(
                reconciled["slot_claims"]["2026-10-30-slot-1"]["state"], "COMPLETED"
            )
            self.assertIn("2026-10-29-slot-2", reconciled["slot_claims"])
            self.assertEqual(report["claims_added"], ["2026-10-29-slot-2"])
            self.assertEqual(
                reconciled["items"][0]["package_reference"],
                "data/generated/asin_B001_content_package.json",
            )

            # Everything except the normalized reference must be byte-for-byte
            # equivalent in meaning.
            expected_item = dict(local_item)
            expected_item["package_reference"] = "data/generated/asin_B001_content_package.json"
            self.assertEqual(reconciled["items"], [expected_item])
            self.assertEqual(reconciled["queue_id"], local["queue_id"])


class TestInterruptedSyncRecovery(unittest.TestCase):
    def _paths(self, root: Path):
        return (
            root / "data" / "queue" / "publishing_queue.json",
            root / "data" / "queue" / "publishing_queue.json.sync_bak",
            root / "data" / "batches" / "import_batches.json",
            root / "data" / "batches" / "import_batches.json.sync_bak",
            root / "data" / "queue" / SYNC_STATE_FILENAME,
        )

    def test_pre_write_interruption_restores_divergent_local_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, backup, batches, batches_backup, state = self._paths(Path(tmp))
            _write_json(queue, {"queue_id": "q", "items": [{"queue_item_id": "HEAD"}]})
            _write_json(backup, {"queue_id": "q", "items": [{"queue_item_id": "LOCAL"}, {"queue_item_id": "LOCAL2"}]})
            _write_json(state, {"phase": "backed_up"})

            result = recover_interrupted_sync(queue, backup, batches, batches_backup, state)

            self.assertTrue(result["recovered"])
            self.assertEqual(_read_json(queue)["items"][0]["queue_item_id"], "LOCAL")
            self.assertFalse(backup.exists())
            self.assertFalse(state.exists())

    def test_post_write_interruption_keeps_merged_queue(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, backup, batches, batches_backup, state = self._paths(Path(tmp))
            _write_json(queue, {"queue_id": "q", "items": [{"queue_item_id": "MERGED"}]})
            _write_json(backup, {"queue_id": "q", "items": [{"queue_item_id": "OLD"}]})
            _write_json(state, {"phase": "queue_written"})

            result = recover_interrupted_sync(queue, backup, batches, batches_backup, state)

            self.assertFalse(result["recovered"])
            self.assertEqual(_read_json(queue)["items"][0]["queue_item_id"], "MERGED")
            self.assertFalse(backup.exists())
            self.assertFalse(state.exists())

    def test_missing_batch_ledger_is_restored_from_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, backup, batches, batches_backup, state = self._paths(Path(tmp))
            _write_json(queue, {"queue_id": "q", "items": []})
            _write_json(batches_backup, [{"batch_id": "batch_001"}])
            _write_json(state, {"phase": "backed_up"})

            result = recover_interrupted_sync(queue, backup, batches, batches_backup, state)

            self.assertTrue(result["recovered"])
            self.assertEqual(_read_json(batches), [{"batch_id": "batch_001"}])
            self.assertFalse(batches_backup.exists())

    def test_legacy_orphan_backups_do_not_overwrite_valid_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            queue, backup, batches, batches_backup, state = self._paths(Path(tmp))
            _write_json(queue, {"queue_id": "q", "items": [{"queue_item_id": "CURRENT"}]})
            _write_json(backup, {"queue_id": "q", "items": [{"queue_item_id": "STALE"}]})
            _write_json(batches, [{"batch_id": "current"}])
            _write_json(batches_backup, [{"batch_id": "stale"}])

            result = recover_interrupted_sync(queue, backup, batches, batches_backup, state)

            self.assertFalse(result["recovered"])
            self.assertEqual(_read_json(queue)["items"][0]["queue_item_id"], "CURRENT")
            self.assertEqual(_read_json(batches), [{"batch_id": "current"}])
            self.assertFalse(backup.exists())
            self.assertFalse(batches_backup.exists())


if __name__ == "__main__":
    unittest.main()
