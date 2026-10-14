#!/usr/bin/env python3
"""
scripts/run_cloud_publisher.py - GitHub Actions Cloud Publishing Runner

Executes the AmazonAffiliateBot publishing queue in the cloud (GitHub Actions).
Handles:
1. Europe/Budapest timezone evaluation across CET (UTC+1) and CEST (UTC+2) DST shifts.
2. Window check: Only triggers publishing during 12:00-12:59 and 19:00-19:59 Budapest time for scheduled runs.
   Manual dry-run runs bypass the window restriction for evaluation only.
3. Safe Dry-Run: Evaluates queue, reports scheduled items, due items, and reasons for skipped items.
   Does not modify real queue on disk during simulation.
4. Live Validation: Validates that Facebook, Threads, and Pinterest credentials are present before live publishing.
5. Idempotency & Persistence: Only processes eligible READY items; reports queue update status for Git persistence.
"""

import sys
import os
import argparse
import shutil
import tempfile
from pathlib import Path
from datetime import datetime, timedelta
from typing import Optional, Tuple, List, Dict, Any, Set

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from publishing.queue import (
    QueuePublisher,
    PublishingQueue,
    DEFAULT_QUEUE_FILE,
    DEFAULT_LOG_FILE,
    get_budapest_timezone,
    STATUS_READY,
)
from publishing.config import PublishingConfig


def get_current_budapest_time(now_override: Optional[str] = None) -> datetime:
    """Return timezone-aware datetime for Europe/Budapest."""
    tz = get_budapest_timezone()
    if now_override:
        clean = now_override.strip()
        if clean.endswith("Z"):
            clean = clean[:-1] + "+00:00"
        dt = datetime.fromisoformat(clean)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=tz)
        return dt.astimezone(tz)
    return datetime.now(tz)


SLOT_CLAIM_TTL = timedelta(hours=6)


def check_budapest_publishing_window(current_dt: datetime) -> Optional[int]:
    """
    Return the currently recoverable Budapest slot.  Slot 1 remains eligible
    until Slot 2 begins and Slot 2 remains eligible through the local day,
    absorbing GitHub Actions startup delays without making either slot a
    recurring open-ended window.
    Returns slot number (1 or 2) if in window, otherwise None.
    """
    tz = get_budapest_timezone()
    b_dt = current_dt.astimezone(tz) if current_dt.tzinfo else current_dt.replace(tzinfo=tz)
    if 12 <= b_dt.hour < 19:
        return 1
    elif 19 <= b_dt.hour < 24:
        return 2
    return None


def slot_key_for_item(item: Any) -> Optional[str]:
    if not item.scheduled_at or not item.daily_slot:
        return None
    try:
        item_dt = QueuePublisher.parse_datetime(item.scheduled_at)
    except Exception:
        return None
    return f"{item_dt.date().isoformat()}-slot-{item.daily_slot}"


def claim_due_slots(queue: PublishingQueue, current_dt: datetime) -> Set[str]:
    """Claim due slots durably before any live adapter can be called."""
    due_items = QueuePublisher.find_due_items(queue, current_dt)
    due_keys = {key for item in due_items if (key := slot_key_for_item(item))}
    claimed: Set[str] = set()
    for key in sorted(due_keys):
        claim = queue.slot_claims.get(key) or {}
        state = claim.get("state")
        if state == "COMPLETED":
            continue
        if state == "CLAIMED":
            try:
                claimed_at = QueuePublisher.parse_datetime(str(claim.get("claimed_at", "")))
                if current_dt - claimed_at < SLOT_CLAIM_TTL:
                    continue
            except Exception:
                pass
        queue.slot_claims[key] = {
            "state": "CLAIMED",
            "claimed_at": current_dt.isoformat(),
            "attempt": int(claim.get("attempt", 0)) + 1,
        }
        claimed.add(key)
    return claimed


def finalize_slot_claims(queue: PublishingQueue, slot_keys: Set[str], report: Dict[str, Any], current_dt: datetime) -> None:
    """Complete successful claims; make failed/partial claims retryable."""
    retryable = report.get("failed_count", 0) > 0 or report.get("partially_published_count", 0) > 0
    for key in slot_keys:
        claim = queue.slot_claims.setdefault(key, {})
        claim.update({"state": "RETRYABLE" if retryable else "COMPLETED", "completed_at": current_dt.isoformat()})


def validate_required_live_secrets() -> Tuple[bool, List[str]]:
    """
    Check presence of required environment secrets for live publishing.
    """
    fb_token = os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN") or os.environ.get("META_ACCESS_TOKEN")
    fb_page = os.environ.get("FACEBOOK_PAGE_ID")
    th_token = os.environ.get("THREADS_ACCESS_TOKEN")
    th_user = os.environ.get("THREADS_USER_ID")
    pin_token = os.environ.get("PINTEREST_ACCESS_TOKEN")
    life_pin_token = os.environ.get("PINTEREST_LIFEMODEHQ_ACCESS_TOKEN")

    missing = []
    if not fb_token:
        missing.append("FACEBOOK_PAGE_ACCESS_TOKEN (or META_ACCESS_TOKEN)")
    if not fb_page:
        missing.append("FACEBOOK_PAGE_ID")
    if not th_token:
        missing.append("THREADS_ACCESS_TOKEN")
    if not th_user:
        missing.append("THREADS_USER_ID")
    if not pin_token:
        missing.append("PINTEREST_ACCESS_TOKEN")
    if not life_pin_token:
        missing.append("PINTEREST_LIFEMODEHQ_ACCESS_TOKEN")

    return len(missing) == 0, missing


def set_github_output(name: str, value: Any) -> None:
    """Set GitHub Actions step output if running in a GitHub runner."""
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        try:
            with open(output_file, "a", encoding="utf-8") as f:
                f.write(f"{name}={str(value)}\n")
        except Exception as e:
            print(f"[WARN] Failed to write to GITHUB_OUTPUT: {e}", file=sys.stderr)


def audit_queue_items(
    queue: PublishingQueue,
    current_dt: datetime,
    slot_filter: Optional[int] = None
) -> Dict[str, Any]:
    """
    Evaluates every item in the queue against current Europe/Budapest time.
    Returns counts and detailed reasons for due and skipped items.
    """
    total = len(queue.items)
    scheduled_items = []
    due_items = []
    skipped_items = []
    next_upcoming = None
    min_future_diff = None

    for item in queue.items:
        item_id = item.queue_item_id
        asin = item.asin
        status = item.status
        slot = item.daily_slot
        sched_at = item.scheduled_at

        if status != STATUS_READY:
            reason = f"Status is {status}"
            if status in ("PUBLISHED", "PARTIALLY_PUBLISHED"):
                pub_time = item.published_at or "prior run"
                reason += f" (completed at {pub_time})"
            skipped_items.append({
                "item_id": item_id,
                "asin": asin,
                "slot": slot,
                "scheduled_at": sched_at,
                "status": status,
                "reason": reason
            })
            continue

        if not sched_at:
            skipped_items.append({
                "item_id": item_id,
                "asin": asin,
                "slot": slot,
                "scheduled_at": sched_at,
                "status": status,
                "reason": "Missing scheduled_at datetime"
            })
            continue

        try:
            item_dt = QueuePublisher.parse_datetime(sched_at)
        except Exception as e:
            skipped_items.append({
                "item_id": item_id,
                "asin": asin,
                "slot": slot,
                "scheduled_at": sched_at,
                "status": status,
                "reason": f"Invalid scheduled_at format: {e}"
            })
            continue

        scheduled_items.append(item)

        if slot_filter is not None and slot != slot_filter:
            skipped_items.append({
                "item_id": item_id,
                "asin": asin,
                "slot": slot,
                "scheduled_at": sched_at,
                "status": status,
                "reason": f"Slot filter active (Slot {slot} != {slot_filter})"
            })
            continue

        if item_dt <= current_dt:
            due_items.append({
                "item_id": item_id,
                "asin": asin,
                "slot": slot,
                "scheduled_at": sched_at,
                "item_dt": item_dt,
                "status": status,
                "reason": "Currently DUE for publishing"
            })
        else:
            diff = item_dt - current_dt
            diff_hours = diff.total_seconds() / 3600
            diff_str = f"in {diff_hours:.1f}h" if diff_hours < 24 else f"in {diff.days}d {int(diff_hours % 24)}h"
            skipped_items.append({
                "item_id": item_id,
                "asin": asin,
                "slot": slot,
                "scheduled_at": sched_at,
                "item_dt": item_dt,
                "status": status,
                "reason": f"Scheduled in future ({sched_at}, {diff_str})"
            })
            if min_future_diff is None or diff < min_future_diff:
                min_future_diff = diff
                next_upcoming = {
                    "item_id": item_id,
                    "asin": asin,
                    "slot": slot,
                    "scheduled_at": sched_at,
                    "diff_str": diff_str
                }

    return {
        "total_items": total,
        "scheduled_items_count": len(scheduled_items),
        "due_items_count": len(due_items),
        "skipped_items_count": len(skipped_items),
        "due_items": due_items,
        "skipped_items": skipped_items,
        "next_upcoming": next_upcoming,
    }


def main():
    parser = argparse.ArgumentParser(description="Cloud Publishing Runner for GitHub Actions")
    parser.add_argument("--dry-run", action="store_true", default=None,
                        help="Run in DRY RUN simulation mode (does not send live posts or modify queue)")
    parser.add_argument("--live", action="store_true", default=False,
                        help="Explicitly enable live publishing mode")
    parser.add_argument("--check-window", action="store_true", default=False,
                        help="Enforce Europe/Budapest publish window check (12:00 or 19:00 hours)")
    parser.add_argument("--ignore-window", action="store_true", default=False,
                        help="Bypass Europe/Budapest publish window check")
    parser.add_argument("--now", default=None,
                        help="Override current datetime for testing/simulation (ISO format)")
    parser.add_argument("--slot", type=int, choices=[1, 2], default=None,
                        help="Optional slot filter (1 or 2)")
    parser.add_argument("--claim-only", action="store_true",
                        help="Persist claims for due scheduled slots without publishing")
    parser.add_argument("--execute-claimed", action="store_true",
                        help="Publish only slots with a persisted repository claim")
    parser.add_argument("--queue-file", default=str(DEFAULT_QUEUE_FILE),
                        help="Path to publishing queue JSON file")
    parser.add_argument("--log-file", default=str(DEFAULT_LOG_FILE),
                        help="Path to daily publisher log file")

    args = parser.parse_args()

    # Determine event context if running in GitHub Actions
    github_event = os.environ.get("EVENT_NAME", "").lower()
    manual_dry_run_env = os.environ.get("MANUAL_DRY_RUN", "").strip().lower()
    ignore_window_env = os.environ.get("IGNORE_WINDOW", "").strip().lower()
    now_override_env = os.environ.get("NOW_OVERRIDE", "").strip()

    # Resolve dry-run setting
    if args.dry_run is True:
        dry_run = True
    elif args.live is True:
        dry_run = False
    elif manual_dry_run_env in ("true", "1", "yes"):
        dry_run = True
    elif manual_dry_run_env in ("false", "0", "no"):
        dry_run = False
    elif github_event == "workflow_dispatch":
        # Manual trigger defaults to dry run unless explicitly turned off
        dry_run = True
    elif github_event == "schedule":
        # Scheduled runs are live runs by default
        dry_run = False
    else:
        # Default safety: dry_run = True unless DRY_RUN env var says otherwise
        env_dr = os.environ.get("DRY_RUN", "").strip().lower()
        dry_run = False if env_dr in ("false", "0", "no") else True

    # Resolve window checking:
    # 1. Scheduled runs must always enforce the window.
    # 2. Manual dry runs bypass window check by default (for evaluation) unless --check-window is requested.
    # 3. Manual live runs enforce window check unless --ignore-window / IGNORE_WINDOW is explicitly requested.
    if github_event == "schedule":
        should_check_window = True
    elif dry_run:
        should_check_window = True if args.check_window else False
    elif args.ignore_window or ignore_window_env in ("true", "1", "yes"):
        should_check_window = False
    else:
        should_check_window = True

    # 1. Timezone and Window Evaluation
    eval_now = args.now or (now_override_env if now_override_env else None)
    current_dt = get_current_budapest_time(eval_now)
    slot_window = check_budapest_publishing_window(current_dt)

    print(f"\n=======================================================")
    print(f" AmazonAffiliateBot - Cloud Publisher")
    print(f" Current Europe/Budapest Time : {current_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f" Publish Window Match         : Slot {slot_window if slot_window else 'None (Outside Window)'}")
    print(f" Window Enforcement           : {'ACTIVE (Scheduled Run)' if should_check_window else 'BYPASSED (Manual Evaluation)'}")
    print(f" Mode                         : {'DRY RUN' if dry_run else 'LIVE PUBLISHING'}")
    print(f" Event Source                 : {github_event or 'local/manual'}")
    print(f"=======================================================\n", flush=True)

    if should_check_window and slot_window is None:
        msg = (
            f"[INFO] Current time ({current_dt.strftime('%H:%M %Z')}) is outside Europe/Budapest "
            f"recoverable scheduled windows (12:00-18:59, 19:00-23:59). Skipping scheduled execution."
        )
        print(msg, flush=True)
        set_github_output("queue_updated", "false")
        set_github_output("published_count", 0)
        set_github_output("due_count", 0)
        set_github_output("dry_run", str(dry_run).lower())
        sys.exit(0)

    # A scheduled run must have a claim committed by the preceding workflow
    # step.  This makes late retries safe across separate Actions invocations.
    require_claim = args.execute_claimed or os.environ.get("REQUIRE_SLOT_CLAIM", "").lower() in ("true", "1", "yes")

    # 2. Live Credential Validation
    if not dry_run and not args.claim_only:
        creds_ok, missing_creds = validate_required_live_secrets()
        if not creds_ok:
            err_msg = (
                f"[ERROR] Live publishing requested but missing required secrets:\n"
                f"  - {', '.join(missing_creds)}\n"
                f"Configure these secrets in GitHub Repository Settings -> Secrets and variables -> Actions."
            )
            print(err_msg, file=sys.stderr, flush=True)
            set_github_output("queue_updated", "false")
            set_github_output("published_count", 0)
            set_github_output("due_count", 0)
            set_github_output("dry_run", "false")
            sys.exit(1)

    # 3. Queue Inspection & Pre-Execution Audit
    queue_file = Path(args.queue_file)
    if not queue_file.exists():
        print(f"[ERROR] Queue file not found at: {queue_file}", file=sys.stderr, flush=True)
        sys.exit(1)

    effective_slot = args.slot or (slot_window if should_check_window and not require_claim else None)

    try:
        raw_queue = PublishingQueue.load(queue_file)
        audit = audit_queue_items(raw_queue, current_dt, slot_filter=effective_slot)
    except Exception as e:
        print(f"[ERROR] Failed to load/audit queue: {e}", file=sys.stderr, flush=True)
        sys.exit(1)

    if args.claim_only:
        claimed = claim_due_slots(raw_queue, current_dt)
        if claimed:
            raw_queue.save(queue_file)
        print(f"[INFO] Persisted scheduled slot claims: {sorted(claimed) if claimed else 'none'}", flush=True)
        set_github_output("claim_updated", str(bool(claimed)).lower())
        set_github_output("queue_updated", str(bool(claimed)).lower())
        set_github_output("claimed_slot_keys", ",".join(sorted(claimed)))
        sys.exit(0)

    allowed_slot_keys: Optional[Set[str]] = None
    if require_claim:
        allowed_slot_keys = {
            key for key, value in raw_queue.slot_claims.items()
            if value.get("state") == "CLAIMED"
        }
        if not allowed_slot_keys:
            print("[INFO] No persisted scheduled slot claim is available; skipping execution.", flush=True)
            set_github_output("queue_updated", "false")
            set_github_output("published_count", 0)
            set_github_output("due_count", 0)
            set_github_output("dry_run", str(dry_run).lower())
            sys.exit(0)

    print("=======================================================")
    print(" QUEUE AUDIT & SCHEDULING STATUS REPORT")
    print(f" Current Europe/Budapest Time : {current_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f" Total Queue Items            : {audit['total_items']}")
    print(f" Scheduled (Active Future)    : {audit['scheduled_items_count']}")
    print(f" Currently Due Now            : {audit['due_items_count']}")
    print(f" Skipped / Ineligible Items   : {audit['skipped_items_count']}")
    print("=======================================================\n", flush=True)

    print("--- QUEUE ITEM EVALUATION BREAKDOWN ---", flush=True)
    for s_item in audit["skipped_items"]:
        slot_str = f"Slot {s_item['slot']} " if s_item.get("slot") else ""
        sched_str = f"({s_item['scheduled_at']}) " if s_item.get("scheduled_at") else ""
        print(f"  - [{s_item['item_id']}] ASIN: {s_item['asin']} | {slot_str}{sched_str}| Status: {s_item['status']}")
        print(f"      -> Reason: {s_item['reason']}")

    if audit["due_items"]:
        print(f"\n--- DUE ITEMS FOR PUBLISHING ({len(audit['due_items'])}) ---", flush=True)
        for d_item in audit["due_items"]:
            print(f"  * [{d_item['item_id']}] ASIN: {d_item['asin']} | Slot {d_item['slot']} ({d_item['scheduled_at']}) -> READY TO PUBLISH")
    else:
        print(f"\n--- DUE ITEMS FOR PUBLISHING (0) ---", flush=True)
        print("  (None currently due at this evaluation time)")
        if audit.get("next_upcoming"):
            nxt = audit["next_upcoming"]
            print(f"  Next Upcoming Item : [{nxt['item_id']}] ASIN: {nxt['asin']} | Slot {nxt['slot']} ({nxt['scheduled_at']}, {nxt['diff_str']})")

    print("-------------------------------------------------------\n", flush=True)

    # 4. Queue Publishing Execution
    temp_dir = None
    exec_queue_file = queue_file
    if dry_run:
        temp_dir = tempfile.mkdtemp(prefix="cloud_pub_dryrun_")
        exec_queue_file = Path(temp_dir) / "temp_queue.json"
        shutil.copyfile(queue_file, exec_queue_file)

    try:
        report = QueuePublisher.publish_due_items(
            queue_file=exec_queue_file,
            config={},
            now_str=eval_now,
            dry_run=dry_run,
            slot_filter=effective_slot,
            log_file=args.log_file,
            slot_keys=allowed_slot_keys
        )

        due_count = report.get("due_items_count", 0)
        published_count = report.get("published_count", 0)
        partially_count = report.get("partially_published_count", 0)
        failed_count = report.get("failed_count", 0)

        print(f"--- EXECUTION SUMMARY ---", flush=True)
        print(f"Mode                 : {report.get('mode')}", flush=True)
        print(f"Evaluated At         : {report.get('evaluated_at')}", flush=True)
        print(f"Due Items Processed  : {due_count}", flush=True)
        print(f"Published (All OK)   : {published_count}", flush=True)
        print(f"Partially Published  : {partially_count}", flush=True)
        print(f"Failed               : {failed_count}", flush=True)

        if report.get("items_processed"):
            print("\nProcessed Items:", flush=True)
            for item_info in report["items_processed"]:
                print(
                    f"  - [{item_info['item_id']}] ASIN: {item_info['asin']} "
                    f"| Slot {item_info['slot']} ({item_info['scheduled_at']}) "
                    f"| Status: {item_info['status']}",
                    flush=True
                )

        if not dry_run and allowed_slot_keys:
            persisted_queue = PublishingQueue.load(queue_file)
            finalize_slot_claims(persisted_queue, allowed_slot_keys, report, current_dt)
            persisted_queue.save(queue_file)

        # In live mode, if items were processed or claims were finalized, queue was updated and needs persistence
        queue_updated = False
        if not dry_run and (published_count > 0 or partially_count > 0 or failed_count > 0 or allowed_slot_keys):
            queue_updated = True
            print(f"\n[INFO] Queue file updated on disk: {queue_file}", flush=True)

        set_github_output("queue_updated", str(queue_updated).lower())
        set_github_output("published_count", published_count + partially_count)
        set_github_output("due_count", due_count)
        set_github_output("scheduled_count", audit["scheduled_items_count"])
        set_github_output("total_items", audit["total_items"])
        set_github_output("dry_run", str(dry_run).lower())

    finally:
        if temp_dir and Path(temp_dir).exists():
            shutil.rmtree(temp_dir, ignore_errors=True)

    if not dry_run and (failed_count > 0 or partially_count > 0):
        print(
            f"\n[ERROR] Publishing encountered errors in live run: "
            f"{failed_count} failed, {partially_count} partially published. Exiting with failure.",
            file=sys.stderr,
            flush=True
        )
        sys.exit(1)

    print("\n[OK] Cloud publisher run complete.\n", flush=True)


if __name__ == "__main__":
    main()
