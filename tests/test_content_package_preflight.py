"""
tests/test_content_package_preflight.py - Focused tests for the scheduled
publishing safety gate (scripts/preflight_content_packages.py).

The GitHub Actions workflow must refuse to publish when a due queue item has
lost its content package, because publish_due_items() would otherwise mark that
item FAILED permanently. The preflight must stay read-only: no queue mutation,
no publishing, and it reuses the publisher's own package path resolution.
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from publishing.queue import (  # noqa: E402
    PublishingQueue,
    QueueItem,
    resolve_content_package_path,
    STATUS_READY,
)
from scripts.preflight_content_packages import evaluate_preflight, main as preflight_main  # noqa: E402

NOW = "2026-10-30T14:00:00+01:00"
DUE_DATE = "2026-10-30"
DUE_SLOT = 1
DUE_AT = "2026-10-30T13:00:00+01:00"
CLAIM_KEY = "2026-10-30-slot-1"

# ASINs that deliberately do not exist under data/generated/, so the ASIN
# fallback of the publisher's package resolution can never mask a missing file.
MISSING_ASIN = "B0PREFLIGHTMISS"
OK_ASIN = "B0PREFLIGHTOK"


class TestContentPackagePreflight(unittest.TestCase):
    def setUp(self):
        # Inside the repository root so repository-relative package
        # references can be exercised (mirrors tests/test_batch_management.py).
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_preflight_", dir=PROJECT_ROOT))
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)

        self.queue_file = self.temp_dir / "queue.json"
        self.ok_package = self.temp_dir / "generated" / f"asin_{OK_ASIN}_content_package.json"
        self.ok_package.parent.mkdir(parents=True, exist_ok=True)
        self.ok_package.write_text(
            json.dumps({"status": "READY_FOR_REVIEW", "asin": OK_ASIN}), encoding="utf-8"
        )
        self.missing_package = self.ok_package.parent / f"asin_{MISSING_ASIN}_content_package.json"

        self.ok_item = QueueItem(
            queue_item_id="item_001_" + OK_ASIN,
            original_url="https://amazon.test/dp/" + OK_ASIN,
            asin=OK_ASIN,
            product_name="Resolvable product",
            scheduled_date=DUE_DATE,
            daily_slot=DUE_SLOT,
            scheduled_at=DUE_AT,
            package_reference=str(self.ok_package),
            status=STATUS_READY,
        )
        self.missing_item = QueueItem(
            queue_item_id="item_002_" + MISSING_ASIN,
            original_url="https://amazon.test/dp/" + MISSING_ASIN,
            asin=MISSING_ASIN,
            product_name="Lost package product",
            scheduled_date=DUE_DATE,
            daily_slot=DUE_SLOT,
            scheduled_at=DUE_AT,
            package_reference=str(self.missing_package),
            status=STATUS_READY,
        )
        self.future_item = QueueItem(
            queue_item_id="item_003_B0PREFLIGHTFUT",
            original_url="https://amazon.test/dp/B0PREFLIGHTFUT",
            asin="B0PREFLIGHTFUT",
            product_name="Future product",
            scheduled_date="2099-01-01",
            daily_slot=1,
            scheduled_at="2099-01-01T13:00:00+01:00",
            package_reference=str(self.temp_dir / "generated" / "asin_B0PREFLIGHTFUT_content_package.json"),
            status=STATUS_READY,
        )
        # Sanity: the ASIN fallback must not silently resolve the missing file.
        self.assertFalse(self.missing_package.exists())
        self.assertFalse(
            (PROJECT_ROOT / "data" / "generated" / f"asin_{MISSING_ASIN}_content_package.json").exists()
        )

        self._save_queue([self.ok_item, self.missing_item, self.future_item])

    def _save_queue(self, items, slot_claims=None):
        PublishingQueue(
            queue_id="queue_preflight_test",
            created_at="2026-10-30T08:00:00+01:00",
            start_date=DUE_DATE,
            items=items,
            slot_claims=slot_claims or {},
        ).save(self.queue_file)

    # ---------- package resolution shared with the publisher ----------

    def test_resolution_reuses_publisher_rules(self):
        resolved = resolve_content_package_path(self.ok_item)
        self.assertIsNotNone(resolved)
        self.assertEqual(Path(resolved), self.ok_package)
        self.assertIsNone(resolve_content_package_path(self.missing_item))

    def test_repository_relative_reference_resolves_from_repository_root(self):
        """Portable data/generated/... references must keep resolving."""
        rel_dir = self.temp_dir.relative_to(PROJECT_ROOT).as_posix()
        item = QueueItem(
            queue_item_id="item_009_B0RELREF",
            original_url="https://amazon.test/dp/B0RELREF",
            asin="B0RELREF",
            package_reference=f"{rel_dir}/generated/asin_{OK_ASIN}_content_package.json",
            status=STATUS_READY,
        )
        original_cwd = Path.cwd()
        try:
            # Leave the repository root so only the repository-relative
            # branch of the resolution can succeed.
            os.chdir(self.temp_dir)
            resolved = resolve_content_package_path(item)
        finally:
            os.chdir(original_cwd)
        self.assertIsNotNone(resolved)
        self.assertEqual(Path(resolved), self.ok_package)

    # ---------- preflight behaviour ----------

    def test_missing_package_fails_preflight_with_clear_diagnostic(self):
        report = evaluate_preflight(self.queue_file, now_override=NOW)

        self.assertFalse(report["ok"])
        self.assertEqual(report["due_count"], 2)
        self.assertEqual(len(report["checked"]), 1)
        self.assertEqual(len(report["missing"]), 1)

        entry = report["missing"][0]
        self.assertEqual(entry["item_id"], self.missing_item.queue_item_id)
        self.assertEqual(entry["asin"], MISSING_ASIN)
        self.assertEqual(entry["package_reference"], str(self.missing_package))
        self.assertEqual(entry["scheduled_at"], DUE_AT)

    def test_preflight_never_mutates_the_queue(self):
        before = self.queue_file.read_bytes()
        evaluate_preflight(self.queue_file, now_override=NOW)
        after = self.queue_file.read_bytes()

        self.assertEqual(before, after, "preflight must not write to the queue file")
        reloaded = PublishingQueue.load(self.queue_file)
        self.assertEqual(
            {item.queue_item_id: item.status for item in reloaded.items},
            {
                self.ok_item.queue_item_id: STATUS_READY,
                self.missing_item.queue_item_id: STATUS_READY,
                self.future_item.queue_item_id: STATUS_READY,
            },
        )

    def test_preflight_passes_when_every_due_package_exists(self):
        self._save_queue([self.ok_item, self.future_item])
        report = evaluate_preflight(self.queue_file, now_override=NOW)

        self.assertTrue(report["ok"])
        self.assertEqual(report["due_count"], 1)
        self.assertEqual(report["missing"], [])
        self.assertEqual(Path(report["checked"][0]["package_path"]), self.ok_package)

    def test_future_items_are_not_checked(self):
        self._save_queue([self.future_item])
        report = evaluate_preflight(self.queue_file, now_override=NOW)

        self.assertTrue(report["ok"])
        self.assertEqual(report["due_count"], 0)

    def test_claim_gate_skips_when_no_slot_is_claimed(self):
        report = evaluate_preflight(self.queue_file, now_override=NOW, require_claim=True)

        self.assertTrue(report["ok"])
        self.assertEqual(report["due_count"], 0)
        self.assertIn("No persisted slot claim", report["note"])

    def test_claim_gate_still_detects_missing_package(self):
        self._save_queue(
            [self.ok_item, self.missing_item],
            slot_claims={CLAIM_KEY: {"state": "CLAIMED", "claimed_at": DUE_AT, "attempt": 1}},
        )
        report = evaluate_preflight(self.queue_file, now_override=NOW, require_claim=True)

        self.assertFalse(report["ok"])
        self.assertEqual(report["missing"][0]["item_id"], self.missing_item.queue_item_id)

    # ---------- CLI / workflow exit behaviour ----------

    def _run_main(self, event_name="", require_claim="", *extra_args):
        argv = ["preflight_content_packages.py", "--queue-file", str(self.queue_file), "--now", NOW,
                *extra_args]
        env = {"EVENT_NAME": event_name, "REQUIRE_SLOT_CLAIM": require_claim, "NOW_OVERRIDE": ""}
        with patch.object(sys, "argv", argv), patch.dict(os.environ, env, clear=False):
            return preflight_main()

    def test_cli_exits_non_zero_when_a_package_is_missing(self):
        before = self.queue_file.read_bytes()
        self.assertEqual(self._run_main(), 1)
        self.assertEqual(before, self.queue_file.read_bytes())

    def test_cli_exits_zero_when_all_packages_exist(self):
        self._save_queue([self.ok_item, self.future_item])
        self.assertEqual(self._run_main(), 0)

    def test_cli_exits_zero_when_a_scheduled_run_has_no_claim(self):
        # A scheduled run without a persisted claim would publish nothing, so
        # the preflight must not fail the workflow for it.
        self.assertEqual(self._run_main(event_name="schedule"), 0)


if __name__ == "__main__":
    unittest.main()
