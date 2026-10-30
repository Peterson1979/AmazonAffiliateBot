#!/usr/bin/env python3
"""
scripts/preflight_content_packages.py - Scheduled publishing safety gate.

Runs in the scheduled GitHub Actions workflow *before*
``run_cloud_publisher.py --execute-claimed`` and verifies that every queue item
that is due for publishing still has its content package available on disk.

Why: ``QueuePublisher.publish_due_items`` permanently marks a due item FAILED
when its content package is missing, with no automatic recovery path. Failing
the workflow before publishing starts protects the queue from that outcome and
guarantees no other item in the execution is published either.

Guarantees:
- Read-only: the queue file is never written and no queue item is mutated.
- Reuses the publisher's own package path resolution
  (``publishing.queue.resolve_content_package_path``) and due-item selection.
- Exits non-zero (failing the workflow step) when any due item is missing its
  package, printing a diagnostic for the affected queue item.
"""

import argparse
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from publishing.queue import (  # noqa: E402
    PublishingQueue,
    QueuePublisher,
    DEFAULT_QUEUE_FILE,
)
from scripts.run_cloud_publisher import get_current_budapest_time  # noqa: E402


def evaluate_preflight(
    queue_file: Path,
    now_override: Optional[str] = None,
    require_claim: bool = False,
) -> Dict[str, Any]:
    """Inspect the due items of ``queue_file`` without modifying anything.

    Selection mirrors ``run_cloud_publisher.py --execute-claimed``: when a
    scheduled run requires a persisted slot claim, only items whose slot key is
    currently CLAIMED or RETRYABLE are due, and with no claim at all nothing
    would publish.
    """
    queue = PublishingQueue.load(queue_file)
    current_dt = get_current_budapest_time(now_override)

    slot_filter = None
    slot_keys = None
    if require_claim:
        slot_keys = {
            key
            for key, value in (queue.slot_claims or {}).items()
            if isinstance(value, dict) and value.get("state") in ("CLAIMED", "RETRYABLE")
        }
        if not slot_keys:
            return {
                "ok": True,
                "evaluated_at": current_dt.isoformat(),
                "due_count": 0,
                "checked": [],
                "missing": [],
                "note": "No persisted slot claim is available; publishing would not run.",
            }

    return QueuePublisher.preflight_due_content_packages(
        queue,
        current_dt,
        slot_filter=slot_filter,
        slot_keys=slot_keys,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify content packages for every due queue item before publishing."
    )
    parser.add_argument("--queue-file", default=str(DEFAULT_QUEUE_FILE),
                        help="Path to publishing queue JSON file")
    parser.add_argument("--now", default=None,
                        help="Override current datetime (ISO format), mainly for tests")
    args = parser.parse_args()

    now_override = args.now or os.environ.get("NOW_OVERRIDE", "").strip() or None
    event_name = os.environ.get("EVENT_NAME", "").strip().lower()
    require_claim = (
        os.environ.get("REQUIRE_SLOT_CLAIM", "").strip().lower() in ("true", "1", "yes")
        or event_name == "schedule"
    )

    queue_file = Path(args.queue_file)
    if not queue_file.exists():
        print(f"[ERROR] Queue file not found at: {queue_file}", file=sys.stderr, flush=True)
        return 1

    try:
        report = evaluate_preflight(queue_file, now_override=now_override, require_claim=require_claim)
    except Exception as exc:  # unreadable queue must fail closed, never publish
        print(f"[ERROR] Content package preflight could not evaluate the queue: {exc}",
              file=sys.stderr, flush=True)
        return 1

    if report.get("note"):
        print(f"[INFO] {report['note']}", flush=True)

    print(
        f"Content package preflight | due items: {report.get('due_count', 0)} "
        f"| verified: {len(report.get('checked', []))} "
        f"| missing: {len(report.get('missing', []))}",
        flush=True,
    )

    if not report.get("ok"):
        print("", file=sys.stderr, flush=True)
        print("[ERROR] Content package preflight failed. Publishing is blocked for this "
              "execution and no queue item was modified:", file=sys.stderr, flush=True)
        for entry in report.get("missing", []):
            asin = (entry.get("asin") or "").upper()
            print(
                f"  - queue item '{entry.get('item_id')}' (ASIN {asin or 'unknown'}, "
                f"scheduled {entry.get('scheduled_at') or 'unscheduled'}, "
                f"slot {entry.get('slot')}) is missing its content package: "
                f"package_reference={entry.get('package_reference')!r} could not be resolved "
                f"(expected data/generated/asin_{asin or '<ASIN>'}_content_package.json).",
                file=sys.stderr,
                flush=True,
            )
        print("Restore the missing package file (or re-sync the repository) and re-run.",
              file=sys.stderr, flush=True)
        return 1

    print("[OK] Every due item has its content package available.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
