"""Tests for the deterministic same-day overdue policy and safe retries."""

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from publishing.queue import (
    PublishingQueue,
    QueueItem,
    QueuePublisher,
    STATUS_FAILED,
    STATUS_PARTIALLY_PUBLISHED,
    STATUS_READY,
    find_overdue_held_items,
    is_item_overdue_held,
)


def _dt(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _queue(*items: QueueItem) -> PublishingQueue:
    return PublishingQueue(
        queue_id="test_overdue",
        created_at="2026-10-01T00:00:00+00:00",
        start_date="2026-10-01",
        items=list(items),
    )


def _item(item_id: str, asin: str, scheduled_at: str, status: str = STATUS_READY) -> QueueItem:
    return QueueItem(
        queue_item_id=item_id,
        original_url=f"https://example.invalid/{asin}",
        asin=asin,
        product_name=f"Product {asin}",
        scheduled_date=scheduled_at[:10],
        daily_slot=1 if "T13:" in scheduled_at else 2,
        scheduled_at=scheduled_at,
        status=status,
    )


class TestOverduePolicy(unittest.TestCase):
    def test_overdue_ready_item_is_held_and_not_due(self):
        held_item = _item("item_001_OLD", "OLD1", "2026-10-29T13:00:00+01:00")
        due_item = _item("item_002_NEW", "NEW1", "2026-10-30T13:00:00+01:00")
        queue = _queue(held_item, due_item)
        now = _dt("2026-10-30T14:00:00+01:00")

        due = QueuePublisher.find_due_items(queue, now)

        self.assertEqual([item.queue_item_id for item in due], ["item_002_NEW"])
        self.assertTrue(is_item_overdue_held(held_item, now))
        self.assertEqual([item.queue_item_id for item in find_overdue_held_items(queue, now)], ["item_001_OLD"])

    def test_future_item_is_not_due_and_not_held(self):
        future_item = _item("item_001_FUTURE", "FUT1", "2026-10-30T19:00:00+01:00")
        queue = _queue(future_item)
        now = _dt("2026-10-30T14:00:00+01:00")

        self.assertEqual(QueuePublisher.find_due_items(queue, now), [])
        self.assertEqual(find_overdue_held_items(queue, now), [])

    def test_overdue_policy_never_mutates_held_records(self):
        held_item = _item("item_001_OLD", "OLD1", "2026-10-28T19:00:00+01:00")
        held_item.errors = ["prior failure"]
        queue = _queue(held_item)
        before = held_item.to_dict()
        now = _dt("2026-10-30T14:00:00+01:00")

        find_overdue_held_items(queue, now)
        QueuePublisher.find_due_items(queue, now)

        self.assertEqual(held_item.to_dict(), before)
        self.assertEqual(held_item.status, STATUS_READY)

    def test_same_day_partial_and_failed_retry_only_with_explicit_claim(self):
        partial = _item(
            "item_001_PART", "PART1", "2026-10-30T13:00:00+01:00", STATUS_PARTIALLY_PUBLISHED
        )
        failed = _item(
            "item_002_FAIL", "FAIL1", "2026-10-30T13:00:00+01:00", STATUS_FAILED
        )
        queue = _queue(partial, failed)
        now = _dt("2026-10-30T14:00:00+01:00")
        slot_keys = {"2026-10-30-slot-1"}

        self.assertEqual(QueuePublisher.find_due_items(queue, now), [])
        retry_ids = [item.queue_item_id for item in QueuePublisher.find_due_items(
            queue, now, slot_keys=slot_keys
        )]
        self.assertEqual(retry_ids, ["item_001_PART", "item_002_FAIL"])

    def test_retry_never_crosses_a_budapest_day(self):
        partial = _item(
            "item_001_PART", "PART1", "2026-10-29T13:00:00+01:00", STATUS_PARTIALLY_PUBLISHED
        )
        queue = _queue(partial)
        now = _dt("2026-10-30T14:00:00+01:00")

        self.assertEqual(
            QueuePublisher.find_due_items(queue, now, slot_keys={"2026-10-29-slot-1"}),
            [],
        )

    def test_retry_respects_slot_filter(self):
        partial = _item(
            "item_001_PART", "PART1", "2026-10-30T19:00:00+01:00", STATUS_PARTIALLY_PUBLISHED
        )
        queue = _queue(partial)
        now = _dt("2026-10-30T20:00:00+01:00")

        self.assertEqual(
            QueuePublisher.find_due_items(
                queue, now, slot_filter=1, slot_keys={"2026-10-30-slot-2"}
            ),
            [],
        )

    def test_publish_report_exposes_held_items_without_processing_them(self):
        held_item = _item("item_001_OLD", "OLD1", "2026-10-27T19:00:00+01:00")
        queue = _queue(held_item)

        class _StubPublisher:
            config = SimpleNamespace(dry_run=True)

        with tempfile.TemporaryDirectory() as tmp:
            queue_file = Path(tmp) / "queue.json"
            log_file = Path(tmp) / "publisher.log"
            queue.save(queue_file)

            report = QueuePublisher.publish_due_items(
                queue_file=queue_file,
                now_override="2026-10-30T14:00:00+01:00",
                dry_run=True,
                publisher=_StubPublisher(),
                log_file=log_file,
            )

            self.assertEqual(report["due_items_count"], 0)
            self.assertEqual(report["overdue_held_count"], 1)
            self.assertEqual(report["overdue_held_items"][0]["item_id"], "item_001_OLD")
            self.assertEqual(report["overdue_policy"], "same_day")

            reloaded = PublishingQueue.load(queue_file)
            self.assertEqual(reloaded.items[0].status, STATUS_READY)
            self.assertEqual(reloaded.items[0].scheduled_at, "2026-10-27T19:00:00+01:00")
            self.assertIsNone(reloaded.items[0].published_at)

    def test_preflight_ignores_held_items(self):
        held_item = _item("item_001_OLD", "OLD1", "2026-10-27T19:00:00+01:00")
        held_item.package_reference = "data/generated/asin_MISSING_content_package.json"
        queue = _queue(held_item)

        report = QueuePublisher.preflight_due_content_packages(
            queue, _dt("2026-10-30T14:00:00+01:00")
        )

        self.assertTrue(report["ok"])
        self.assertEqual(report["due_count"], 0)


if __name__ == "__main__":
    unittest.main()
