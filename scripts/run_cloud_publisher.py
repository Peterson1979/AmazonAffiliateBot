#!/usr/bin/env python3
"""
scripts/run_cloud_publisher.py - GitHub Actions Cloud Publishing Runner

Executes the AmazonAffiliateBot publishing queue in the cloud (GitHub Actions).
Handles:
1. Europe/Budapest timezone evaluation across CET (UTC+1) and CEST (UTC+2) DST shifts.
2. Window check: Only triggers publishing during 12:00-12:59 and 19:00-19:59 Budapest time for scheduled runs.
3. Safe Dry-Run: Defaults to dry-run on manual triggers; simulates without modifying real queue on disk.
4. Live Validation: Validates that Facebook, Threads, and Pinterest credentials are present before live publishing.
5. Idempotency & Persistence: Only processes eligible READY items; reports queue update status for Git persistence.
"""

import sys
import os
import argparse
import shutil
import tempfile
from pathlib import Path
from datetime import datetime
from typing import Optional, Tuple, List, Dict, Any

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


def check_budapest_publishing_window(current_dt: datetime) -> Optional[int]:
    """
    Check if current Budapest time falls into a scheduled slot window:
    - Hour 12 (12:00 - 12:59) -> Slot 1
    - Hour 19 (19:00 - 19:59) -> Slot 2
    Returns slot number (1 or 2) if in window, otherwise None.
    """
    tz = get_budapest_timezone()
    b_dt = current_dt.astimezone(tz) if current_dt.tzinfo else current_dt.replace(tzinfo=tz)
    if b_dt.hour == 12:
        return 1
    elif b_dt.hour == 19:
        return 2
    return None


def validate_required_live_secrets() -> Tuple[bool, List[str]]:
    """
    Check presence of required environment secrets for live publishing (Facebook, Threads, Pinterest).
    X (Twitter) is optional and safely skipped if missing.
    """
    fb_token = os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN") or os.environ.get("META_ACCESS_TOKEN")
    fb_page = os.environ.get("FACEBOOK_PAGE_ID")
    th_token = os.environ.get("THREADS_ACCESS_TOKEN")
    th_user = os.environ.get("THREADS_USER_ID")
    pin_token = os.environ.get("PINTEREST_ACCESS_TOKEN")

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
    parser.add_argument("--queue-file", default=str(DEFAULT_QUEUE_FILE),
                        help="Path to publishing queue JSON file")
    parser.add_argument("--log-file", default=str(DEFAULT_LOG_FILE),
                        help="Path to daily publisher log file")

    args = parser.parse_args()

    # Determine event context if running in GitHub Actions
    github_event = os.environ.get("EVENT_NAME", "").lower()
    manual_dry_run_env = os.environ.get("MANUAL_DRY_RUN", "").strip().lower()
    ignore_window_env = os.environ.get("IGNORE_WINDOW", "").strip().lower()

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
    # Scheduled runs enforce window checking by default; manual dispatch allows ignore_window
    should_check_window = False
    if args.ignore_window or ignore_window_env in ("true", "1", "yes"):
        should_check_window = False
    elif args.check_window or github_event == "schedule":
        should_check_window = True

    # 1. Timezone and Window Evaluation
    current_dt = get_current_budapest_time(args.now)
    slot_window = check_budapest_publishing_window(current_dt)

    print(f"\n=======================================================")
    print(f" AmazonAffiliateBot - Cloud Publisher")
    print(f" Current Europe/Budapest Time : {current_dt.strftime('%Y-%m-%d %H:%M:%S %Z')}")
    print(f" Publish Window Match         : Slot {slot_window if slot_window else 'None (Outside Window)'}")
    print(f" Mode                         : {'DRY RUN' if dry_run else 'LIVE PUBLISHING'}")
    print(f" Event Source                 : {github_event or 'local/manual'}")
    print(f"=======================================================\n", flush=True)

    if should_check_window and slot_window is None:
        msg = (
            f"[INFO] Current time ({current_dt.strftime('%H:%M %Z')}) is outside Europe/Budapest "
            f"scheduled windows (12:00-12:59, 19:00-19:59). Skipping scheduled execution."
        )
        print(msg, flush=True)
        set_github_output("queue_updated", "false")
        set_github_output("published_count", 0)
        set_github_output("due_count", 0)
        set_github_output("dry_run", str(dry_run).lower())
        sys.exit(0)

    # 2. Live Credential Validation
    if not dry_run:
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

    # 3. Queue Execution
    queue_file = Path(args.queue_file)
    if not queue_file.exists():
        print(f"[ERROR] Queue file not found at: {queue_file}", file=sys.stderr, flush=True)
        sys.exit(1)

    effective_slot = args.slot or (slot_window if should_check_window else None)

    # If dry-run: run on a temporary copy to protect on-disk queue from simulation changes
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
            now_str=args.now,
            dry_run=dry_run,
            slot_filter=effective_slot,
            log_file=args.log_file
        )

        due_count = report.get("due_items_count", 0)
        published_count = report.get("published_count", 0)
        partially_count = report.get("partially_published_count", 0)
        failed_count = report.get("failed_count", 0)

        print(f"\n--- PUBLISHING EXECUTION SUMMARY ---", flush=True)
        print(f"Mode                 : {report.get('mode')}", flush=True)
        print(f"Evaluated At         : {report.get('evaluated_at')}", flush=True)
        print(f"Due Items Found      : {due_count}", flush=True)
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

        # In live mode, if items were processed, queue was updated and needs persistence
        queue_updated = False
        if not dry_run and (published_count > 0 or partially_count > 0 or failed_count > 0):
            queue_updated = True
            print(f"\n[INFO] Queue file updated on disk: {queue_file}", flush=True)

        set_github_output("queue_updated", str(queue_updated).lower())
        set_github_output("published_count", published_count + partially_count)
        set_github_output("due_count", due_count)
        set_github_output("dry_run", str(dry_run).lower())

    finally:
        if temp_dir and Path(temp_dir).exists():
            shutil.rmtree(temp_dir, ignore_errors=True)

    print("\n[OK] Cloud publisher run complete.\n", flush=True)


if __name__ == "__main__":
    main()
