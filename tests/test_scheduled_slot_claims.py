"""
Focused tests for scheduled publishing slot-claim creation, persistence, and
execution retrieval.

Root cause covered here: a scheduled run in a valid Budapest slot window used to
claim only slots that already had due queue items. When the checked-out queue
had no currently due READY items, ``--claim-only`` persisted nothing, so
``--execute-claimed`` skipped the whole run. A valid window run must claim its
active window slot even when nothing is due yet, while completed/retryable
duplicate-prevention semantics stay intact.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.run_cloud_publisher import (
    claim_due_slots,
    finalize_slot_claims,
    main as cloud_main,
)
from publishing.queue import (
    PublishingQueue,
    QueueItem,
    STATUS_PUBLISHED,
    STATUS_READY,
)

WINDOW_NOW = datetime.fromisoformat("2026-10-28T22:35:00+01:00")
WINDOW_KEY = "2026-10-28-slot-2"

MOCK_SECRETS = {
    "FACEBOOK_PAGE_ACCESS_TOKEN": "mock_fb",
    "FACEBOOK_PAGE_ID": "123456",
    "THREADS_ACCESS_TOKEN": "mock_th",
    "THREADS_USER_ID": "mock_uid",
    "PINTEREST_ACCESS_TOKEN": "mock_pin",
    "PINTEREST_LIFEMODEHQ_ACCESS_TOKEN": "mock_life_pin",
}


def make_item(
    item_id="item_001",
    slot=2,
    scheduled_at="2026-10-28T19:00:00+01:00",
    status=STATUS_READY,
):
    return QueueItem(
        queue_item_id=item_id,
        original_url=f"https://link.amazon/{item_id}",
        asin="B0TEST0001",
        product_name="Test Product",
        scheduled_date=scheduled_at[:10],
        daily_slot=slot,
        scheduled_at=scheduled_at,
        package_reference="",
        status=status,
    )


def make_queue(items=None, claims=None):
    return PublishingQueue(
        queue_id="test_slot_claim_q",
        created_at="2026-10-01T00:00:00+00:00",
        start_date="2026-10-28",
        total_days=1,
        products_per_day=2,
        target_capacity=2,
        slot_claims=claims or {},
        items=items or [],
    )


class SlotClaimTestBase(unittest.TestCase):
    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_slot_claims_"))
        self.queue_file = self.temp_dir / "publishing_queue.json"
        self.log_file = self.temp_dir / "daily_publisher.log"
        self.output_file = self.temp_dir / "github_output.txt"

    def tearDown(self):
        if self.temp_dir.exists():
            shutil.rmtree(self.temp_dir, ignore_errors=True)

    def save_queue(self, queue):
        queue.save(self.queue_file)

    def load_queue(self):
        return PublishingQueue.load(self.queue_file)

    def read_outputs(self):
        if not self.output_file.exists():
            return {}
        outputs = {}
        for line in self.output_file.read_text(encoding="utf-8").splitlines():
            if "=" in line:
                name, value = line.split("=", 1)
                outputs[name] = value
        return outputs

    def run_main(self, extra_args, env=None):
        argv = [
            "run_cloud_publisher.py",
            "--now",
            WINDOW_NOW.isoformat(),
            "--queue-file",
            str(self.queue_file),
            "--log-file",
            str(self.log_file),
        ] + extra_args
        base_env = {
            "EVENT_NAME": "schedule",
            "GITHUB_OUTPUT": str(self.output_file),
            "NOW_OVERRIDE": "",
        }
        base_env.update(env or {})
        stdout = io.StringIO()
        with patch.object(sys, "argv", argv), \
             patch.dict(os.environ, base_env, clear=True), \
             redirect_stdout(stdout):
            try:
                cloud_main()
                exit_code = None
            except SystemExit as exc:
                exit_code = exc.code
        return exit_code, stdout.getvalue()


class TestWindowSlotClaimCreation(SlotClaimTestBase):
    def test_claim_only_persists_active_window_slot_when_nothing_is_due(self):
        """A valid Slot 2 run persists a claim even with no currently due items."""
        queue = make_queue(items=[
            make_item(item_id="item_future", scheduled_at="2026-10-30T19:00:00+01:00"),
        ])
        self.save_queue(queue)

        exit_code, output = self.run_main(["--claim-only"])

        self.assertEqual(exit_code, 0)
        self.assertIn("Persisted scheduled slot claims:", output)
        self.assertIn(WINDOW_KEY, output)
        self.assertEqual(self.read_outputs().get("claim_updated"), "true")

        reloaded = self.load_queue()
        claim = reloaded.slot_claims.get(WINDOW_KEY)
        self.assertIsNotNone(claim, "The active window slot claim must be persisted")
        self.assertEqual(claim["state"], "CLAIMED")
        self.assertEqual(claim["attempt"], 1)
        self.assertNotIn("2026-10-30-slot-2", reloaded.slot_claims)

    def test_claim_keeps_due_item_slots_alongside_window_slot(self):
        """Due-item recovery and the active window claim are both persisted."""
        queue = make_queue(items=[
            make_item(item_id="item_slot1", slot=1, scheduled_at="2026-10-28T13:00:00+01:00"),
        ])
        self.save_queue(queue)

        self.run_main(["--claim-only"])

        claims = self.load_queue().slot_claims
        self.assertEqual(claims["2026-10-28-slot-1"]["state"], "CLAIMED")
        self.assertEqual(claims[WINDOW_KEY]["state"], "CLAIMED")

    def test_completed_window_slot_is_not_reclaimed(self):
        """Duplicate prevention: a COMPLETED slot is never reopened."""
        completed_at = "2026-10-28T19:30:00+01:00"
        queue = make_queue(claims={
            WINDOW_KEY: {
                "state": "COMPLETED",
                "claimed_at": "2026-10-28T19:00:00+01:00",
                "attempt": 1,
                "completed_at": completed_at,
            },
        })

        claimed = claim_due_slots(queue, WINDOW_NOW, window_slot=2)

        self.assertEqual(claimed, set())
        self.assertEqual(queue.slot_claims[WINDOW_KEY]["state"], "COMPLETED")
        self.assertEqual(queue.slot_claims[WINDOW_KEY]["completed_at"], completed_at)

    def test_fresh_claimed_window_slot_is_not_overwritten(self):
        """A fresh in-flight claim is reused, not replaced with a new attempt."""
        claimed_at = "2026-10-28T22:00:00+01:00"
        queue = make_queue(claims={
            WINDOW_KEY: {"state": "CLAIMED", "claimed_at": claimed_at, "attempt": 1},
        })

        claimed = claim_due_slots(queue, WINDOW_NOW, window_slot=2)

        self.assertEqual(claimed, set())
        self.assertEqual(queue.slot_claims[WINDOW_KEY]["claimed_at"], claimed_at)
        self.assertEqual(queue.slot_claims[WINDOW_KEY]["attempt"], 1)

    def test_stale_claimed_window_slot_is_reclaimed(self):
        """Recovery: an abandoned claim older than the TTL can be reclaimed."""
        queue = make_queue(claims={
            WINDOW_KEY: {
                "state": "CLAIMED",
                "claimed_at": "2026-10-28T12:00:00+01:00",
                "attempt": 1,
            },
        })

        claimed = claim_due_slots(queue, WINDOW_NOW, window_slot=2)

        self.assertEqual(claimed, {WINDOW_KEY})
        self.assertEqual(queue.slot_claims[WINDOW_KEY]["state"], "CLAIMED")
        self.assertEqual(queue.slot_claims[WINDOW_KEY]["attempt"], 2)

    def test_retryable_window_slot_is_reclaimed(self):
        """Recovery: a failed/partial slot is retryable on the next run."""
        queue = make_queue(claims={
            WINDOW_KEY: {"state": "RETRYABLE", "claimed_at": "2026-10-28T19:05:00+01:00", "attempt": 1},
        })

        claimed = claim_due_slots(queue, WINDOW_NOW, window_slot=2)

        self.assertEqual(claimed, {WINDOW_KEY})
        self.assertEqual(queue.slot_claims[WINDOW_KEY]["attempt"], 2)


class TestClaimRetrievalAndFinalization(SlotClaimTestBase):
    def test_execute_claimed_reads_persisted_window_claim(self):
        """The execution step runs using the claim persisted by the claim step."""
        queue = make_queue(claims={
            WINDOW_KEY: {"state": "CLAIMED", "claimed_at": "2026-10-28T22:30:00+01:00", "attempt": 1},
        })
        self.save_queue(queue)

        exit_code, output = self.run_main(
            ["--execute-claimed"],
            env={**MOCK_SECRETS, "REQUIRE_SLOT_CLAIM": "true"},
        )

        self.assertIsNone(exit_code)
        self.assertNotIn("No persisted scheduled slot claim", output)
        self.assertIn("QUEUE AUDIT & SCHEDULING STATUS REPORT", output)
        # No due items were processed, so the claim stays recoverable.
        self.assertEqual(self.load_queue().slot_claims[WINDOW_KEY]["state"], "CLAIMED")
        self.assertEqual(self.read_outputs().get("queue_updated"), "false")

    def test_execute_claimed_still_skips_without_a_claim(self):
        """Duplicate/claim gate: execution never runs without a persisted claim."""
        self.save_queue(make_queue(items=[
            make_item(item_id="item_due", slot=2, scheduled_at="2026-10-28T19:00:00+01:00"),
        ]))

        exit_code, output = self.run_main(
            ["--execute-claimed"],
            env={**MOCK_SECRETS, "REQUIRE_SLOT_CLAIM": "true"},
        )

        self.assertEqual(exit_code, 0)
        self.assertIn("No persisted scheduled slot claim", output)
        self.assertEqual(self.load_queue().items[0].status, STATUS_READY)

    def test_empty_execution_keeps_claim_recoverable(self):
        """A run that processed nothing must not consume the slot."""
        queue = make_queue(claims={
            WINDOW_KEY: {"state": "CLAIMED", "claimed_at": "2026-10-28T22:30:00+01:00", "attempt": 1},
        })

        changed = finalize_slot_claims(
            queue,
            {WINDOW_KEY},
            {"due_items_count": 0, "items_processed": [], "failed_count": 0, "partially_published_count": 0},
            WINDOW_NOW,
        )

        self.assertFalse(changed)
        self.assertEqual(queue.slot_claims[WINDOW_KEY]["state"], "CLAIMED")

    def test_successful_processed_slot_completes_claim(self):
        queue = make_queue(claims={
            WINDOW_KEY: {"state": "CLAIMED", "claimed_at": "2026-10-28T19:00:00+01:00", "attempt": 1},
        })
        report = {
            "due_items_count": 1,
            "items_processed": [{
                "item_id": "item_001",
                "slot": 2,
                "scheduled_at": "2026-10-28T19:00:00+01:00",
                "status": STATUS_PUBLISHED,
            }],
            "published_count": 1,
            "partially_published_count": 0,
            "failed_count": 0,
        }

        changed = finalize_slot_claims(queue, {WINDOW_KEY}, report, WINDOW_NOW)

        self.assertTrue(changed)
        self.assertEqual(queue.slot_claims[WINDOW_KEY]["state"], "COMPLETED")
        self.assertEqual(queue.slot_claims[WINDOW_KEY]["completed_at"], WINDOW_NOW.isoformat())

    def test_failed_processed_slot_becomes_retryable(self):
        queue = make_queue(claims={
            WINDOW_KEY: {"state": "CLAIMED", "claimed_at": "2026-10-28T19:00:00+01:00", "attempt": 1},
        })
        report = {
            "due_items_count": 1,
            "items_processed": [{
                "item_id": "item_001",
                "slot": 2,
                "scheduled_at": "2026-10-28T19:00:00+01:00",
                "status": "FAILED",
            }],
            "published_count": 0,
            "partially_published_count": 0,
            "failed_count": 1,
        }

        changed = finalize_slot_claims(queue, {WINDOW_KEY}, report, WINDOW_NOW)

        self.assertTrue(changed)
        self.assertEqual(queue.slot_claims[WINDOW_KEY]["state"], "RETRYABLE")

    def test_finalize_only_touches_slots_that_processed_items(self):
        slot_1_key = "2026-10-28-slot-1"
        queue = make_queue(claims={
            slot_1_key: {"state": "CLAIMED", "claimed_at": "2026-10-28T13:00:00+01:00", "attempt": 1},
            WINDOW_KEY: {"state": "CLAIMED", "claimed_at": "2026-10-28T19:00:00+01:00", "attempt": 1},
        })
        report = {
            "due_items_count": 1,
            "items_processed": [{
                "item_id": "item_slot1",
                "slot": 1,
                "scheduled_at": "2026-10-28T13:00:00+01:00",
                "status": STATUS_PUBLISHED,
            }],
            "published_count": 1,
            "partially_published_count": 0,
            "failed_count": 0,
        }

        finalize_slot_claims(queue, {slot_1_key, WINDOW_KEY}, report, WINDOW_NOW)

        self.assertEqual(queue.slot_claims[slot_1_key]["state"], "COMPLETED")
        self.assertEqual(queue.slot_claims[WINDOW_KEY]["state"], "CLAIMED")


class TestWorkflowClaimOrdering(unittest.TestCase):
    def test_claims_are_created_before_execution_and_persisted(self):
        workflow = (PROJECT_ROOT / ".github" / "workflows" / "scheduled_publishing.yml").read_text(encoding="utf-8")

        claim_idx = workflow.index("--claim-only")
        persist_idx = workflow.index("Persist Slot Claims Before Publishing")
        execute_idx = workflow.index("--execute-claimed")
        self.assertLess(claim_idx, persist_idx)
        self.assertLess(persist_idx, execute_idx)
        self.assertIn("steps.claim.outputs.claim_updated == 'true'", workflow)
        self.assertIn("git add data/queue/publishing_queue.json", workflow)
        self.assertIn("REQUIRE_SLOT_CLAIM:", workflow)


if __name__ == "__main__":
    unittest.main()
