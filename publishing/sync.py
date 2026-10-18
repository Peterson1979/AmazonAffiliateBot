"""
publishing/sync.py - Safe Bidirectional Synchronization between Local Dashboard and GitHub Actions.

Safely synchronizes the AmazonAffiliateBot publishing queue between the local Flask dashboard
and remote GitHub Actions (origin/master):
1. Preserves remote cloud-updated statuses (PUBLISHED, PARTIALLY_PUBLISHED, FAILED),
   timestamps, and publishing results.
2. Preserves locally added queue items and resolves any slot collisions.
3. Stages ONLY the merged queue file and the product/package JSON files referenced by the queue.
4. Never stages .env, credentials, logs, or unrelated user files.
5. Uses a thread lock to prevent concurrent sync operations.
6. Uses [skip ci] on sync commits to avoid triggering unnecessary GitHub Actions workflow runs.
"""

import os
import sys
import json
import shutil
import threading
import subprocess
from pathlib import Path
from datetime import datetime, date, timedelta
from typing import Dict, Any, List, Tuple, Optional, Set

# Ensure project root in sys.path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from publishing.queue import (
    DEFAULT_QUEUE_FILE,
    DEFAULT_TIMEZONE,
    SLOT_1_TIME,
    SLOT_2_TIME,
    get_budapest_timezone,
    STATUS_READY,
    STATUS_PUBLISHED,
    STATUS_PARTIALLY_PUBLISHED,
    STATUS_FAILED,
)

# Global lock to ensure only one synchronization operation runs at any time
SYNC_LOCK = threading.Lock()


def merge_publishing_queues(
    local_data: Dict[str, Any],
    remote_data: Dict[str, Any]
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Deterministically and safely merge local queue state with remote cloud queue state.

    Rules:
    - Remote cloud status (PUBLISHED, PARTIALLY_PUBLISHED, FAILED) takes precedence
      for items published in the cloud. Platform timestamps and results are preserved.
    - Locally added items (present in local but not remote) are preserved with their allocated slots.
    - If a slot collision occurs between a local new item and a remote slot, the local item
      is reallocated to the next available vacant slot in Europe/Budapest.
    - Summary counts are recalculated deterministically.

    Raises:
        ValueError: If either input is invalid or missing required queue structure.
    """
    if not isinstance(local_data, dict) or "items" not in local_data:
        raise ValueError("Invalid local queue format: expected dictionary with 'items' list")
    if not isinstance(remote_data, dict) or "items" not in remote_data:
        raise ValueError("Invalid remote queue format: expected dictionary with 'items' list")

    local_items = list(local_data.get("items", []))
    remote_items = list(remote_data.get("items", []))

    # Index items
    local_by_id = {it.get("queue_item_id"): it for it in local_items if it.get("queue_item_id")}
    local_by_asin = {it.get("asin", "").upper(): it for it in local_items if it.get("asin")}

    remote_by_id = {it.get("queue_item_id"): it for it in remote_items if it.get("queue_item_id")}
    remote_by_asin = {it.get("asin", "").upper(): it for it in remote_items if it.get("asin")}

    merged_items: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()
    seen_asins: Set[str] = set()

    remote_updated_count = 0
    local_added_count = 0

    # 1. Process all remote items as the cloud baseline
    for r_item in remote_items:
        item_id = r_item.get("queue_item_id")
        asin = r_item.get("asin", "").upper()

        # Match corresponding local item
        l_item = local_by_id.get(item_id) or (local_by_asin.get(asin) if asin else None)

        if l_item:
            merged = dict(r_item)
            r_status = r_item.get("status")
            l_status = l_item.get("status")

            terminal_statuses = (STATUS_PUBLISHED, STATUS_PARTIALLY_PUBLISHED, STATUS_FAILED)

            if r_status in terminal_statuses:
                # Cloud publisher published or attempted the item -> remote record wins
                merged["status"] = r_status
                merged["published_at"] = r_item.get("published_at")
                merged["publishing_results"] = r_item.get("publishing_results")
                merged["errors"] = r_item.get("errors", [])
                if l_status == STATUS_READY:
                    remote_updated_count += 1
            elif l_status in terminal_statuses and r_status == STATUS_READY:
                # Local recorded a terminal status while remote was still READY
                merged["status"] = l_status
                merged["published_at"] = l_item.get("published_at")
                merged["publishing_results"] = l_item.get("publishing_results")
                merged["errors"] = l_item.get("errors", [])
            else:
                merged["status"] = r_status or l_status

            # Retain locally enriched references if missing on remote
            if l_item.get("package_reference") and not r_item.get("package_reference"):
                merged["package_reference"] = l_item.get("package_reference")
            if l_item.get("product_name") and not r_item.get("product_name"):
                merged["product_name"] = l_item.get("product_name")

            merged_items.append(merged)
            if item_id:
                seen_ids.add(item_id)
            if asin:
                seen_asins.add(asin)
        else:
            # Item exists only on remote
            merged_items.append(dict(r_item))
            if item_id:
                seen_ids.add(item_id)
            if asin:
                seen_asins.add(asin)

    # 2. Process locally added items not present on remote
    for l_item in local_items:
        item_id = l_item.get("queue_item_id")
        asin = l_item.get("asin", "").upper()

        if (item_id and item_id in seen_ids) or (asin and asin in seen_asins):
            continue

        merged_items.append(dict(l_item))
        if item_id:
            seen_ids.add(item_id)
        if asin:
            seen_asins.add(asin)
        local_added_count += 1

    # 3. Detect and resolve any slot collisions among scheduled items
    occupied_slots: Dict[Tuple[str, int], str] = {}
    colliding_items: List[Dict[str, Any]] = []

    for item in merged_items:
        d = item.get("scheduled_date")
        s = item.get("daily_slot")
        if d and s:
            slot_key = (d, s)
            if slot_key in occupied_slots:
                colliding_items.append(item)
            else:
                occupied_slots[slot_key] = item.get("queue_item_id", "")

    if colliding_items:
        tz = get_budapest_timezone()
        taken_slot_tuples: Set[Tuple[date, int]] = set()
        for (d_str, s_num) in occupied_slots.keys():
            try:
                d_obj = datetime.strptime(d_str, "%Y-%m-%d").date()
                taken_slot_tuples.add((d_obj, s_num))
            except Exception:
                pass

        for col_item in colliding_items:
            candidate_date = datetime.now(tz).date()
            candidate_slot = 1
            if taken_slot_tuples:
                max_date, max_slot = max(taken_slot_tuples, key=lambda x: (x[0], x[1]))
                if candidate_date <= max_date:
                    candidate_date = max_date
                    candidate_slot = 2 if max_slot == 1 else 1
                    if max_slot == 2:
                        candidate_date += timedelta(days=1)

            while (candidate_date, candidate_slot) in taken_slot_tuples:
                if candidate_slot == 1:
                    candidate_slot = 2
                else:
                    candidate_slot = 1
                    candidate_date += timedelta(days=1)

            slot_t = SLOT_1_TIME if candidate_slot == 1 else SLOT_2_TIME
            dt = datetime(
                candidate_date.year,
                candidate_date.month,
                candidate_date.day,
                slot_t.hour,
                slot_t.minute,
                slot_t.second,
                tzinfo=tz
            )

            col_item["scheduled_date"] = candidate_date.strftime("%Y-%m-%d")
            col_item["daily_slot"] = candidate_slot
            col_item["scheduled_at"] = dt.isoformat()
            taken_slot_tuples.add((candidate_date, candidate_slot))

    # 4. Chronological sort of merged items
    merged_items.sort(
        key=lambda x: (
            x.get("scheduled_date") or "9999-99-99",
            x.get("daily_slot") or 0,
            x.get("queue_item_id") or ""
        )
    )

    # 5. Recalculate summary counters
    target_capacity = max(
        local_data.get("target_capacity", 60),
        remote_data.get("target_capacity", 60),
        len(merged_items)
    )
    ready_count = sum(1 for it in merged_items if it.get("status") == STATUS_READY)
    failed_count = sum(
        1 for it in merged_items
        if it.get("status") == STATUS_FAILED or it.get("validation_status") == "FAILED_VALIDATION"
    )

    merged_queue_dict = {
        "queue_id": remote_data.get("queue_id") or local_data.get("queue_id", "queue_merged"),
        "created_at": remote_data.get("created_at") or local_data.get("created_at"),
        "start_date": remote_data.get("start_date") or local_data.get("start_date"),
        "total_days": remote_data.get("total_days", 30),
        "products_per_day": remote_data.get("products_per_day", 2),
        "target_capacity": target_capacity,
        "total_submitted": max(len(merged_items), local_data.get("total_submitted", 0), remote_data.get("total_submitted", 0)),
        "total_processed": len(merged_items),
        "total_ready": ready_count,
        "total_failed": failed_count,
        "scheduled_posts": ready_count,
        "unfilled_slots": max(0, target_capacity - ready_count),
        "items": merged_items,
    }

    summary = {
        "remote_updated_count": remote_updated_count,
        "local_added_count": local_added_count,
        "total_merged_items": len(merged_items),
        "ready_items": ready_count,
        "failed_items": failed_count,
    }

    return merged_queue_dict, summary


def find_sync_stage_files(merged_queue_dict: Dict[str, Any], repo_dir: Path) -> List[str]:
    """
    Identify ONLY the valid, queue-related product and package files to stage.
    Never stages .env, logs, credentials, or unrelated repository files.
    """
    stage_set: Set[str] = {"data/queue/publishing_queue.json"}

    for item in merged_queue_dict.get("items", []):
        asin = item.get("asin")
        if asin:
            prod_candidate = repo_dir / "data" / "products" / f"asin_{asin.upper()}.json"
            if prod_candidate.exists():
                stage_set.add(f"data/products/asin_{asin.upper()}.json")

            pkg_candidate = repo_dir / "data" / "generated" / f"asin_{asin.upper()}_content_package.json"
            if pkg_candidate.exists():
                stage_set.add(f"data/generated/asin_{asin.upper()}_content_package.json")

        pkg_ref = item.get("package_reference")
        if pkg_ref:
            norm_ref = pkg_ref.replace("\\", "/")
            if norm_ref.startswith("data/"):
                ref_path = repo_dir / norm_ref
                if ref_path.exists():
                    stage_set.add(norm_ref)

    # Exclude any prohibited patterns
    prohibited = {".env", "daily_publisher.log", "daily_maintenance.log", "config.json"}
    cleaned = []
    for rel_path in stage_set:
        basename = Path(rel_path).name
        if basename in prohibited or basename.endswith(".log") or basename.startswith(".env"):
            continue
        cleaned.append(rel_path)

    return sorted(cleaned)


def sync_with_github(
    repo_dir: Optional[Path] = None,
    remote_name: str = "origin",
    branch_name: str = "master"
) -> Dict[str, Any]:
    """
    Execute user-triggered synchronization between local dashboard and GitHub Actions.

    Steps:
    1. Acquire thread lock (abort if another sync is currently running).
    2. Fetch latest remote state via git fetch origin master.
    3. Load remote queue from git show and local queue from disk.
    4. Deterministically merge both queue states.
    5. Safely fast-forward local Git history to remote HEAD.
    6. Write merged queue to disk.
    7. Stage ONLY the merged queue and new/updated product and content-package JSONs.
    8. Commit with [skip ci] and push to origin/master.
    """
    target_repo = (repo_dir or PROJECT_ROOT).resolve()

    if not SYNC_LOCK.acquire(blocking=False):
        return {
            "success": False,
            "error": "Synchronization is already in progress. Please wait for the current sync to complete."
        }

    queue_path = target_repo / "data" / "queue" / "publishing_queue.json"
    backup_path = target_repo / "data" / "queue" / "publishing_queue.json.sync_bak"

    def _run_git(args: List[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git"] + args,
            cwd=str(target_repo),
            capture_output=True,
            text=True,
            check=False
        )

    try:
        # Verify Git repo
        git_check = _run_git(["rev-parse", "--is-inside-work-tree"])
        if git_check.returncode != 0:
            return {
                "success": False,
                "error": "Current directory is not a valid Git repository."
            }

        # 1. Fetch remote tracking branch
        fetch_res = _run_git(["fetch", remote_name, branch_name])
        if fetch_res.returncode != 0:
            err_msg = fetch_res.stderr.strip() or fetch_res.stdout.strip()
            return {
                "success": False,
                "error": f"Failed to fetch from GitHub: {err_msg}. Please check internet connection."
            }

        # 2. Load remote queue from git show
        remote_ref = f"{remote_name}/{branch_name}"
        show_res = _run_git(["show", f"{remote_ref}:data/queue/publishing_queue.json"])
        if show_res is None:
            return {
                "success": False,
                "error": f"Git did not return the remote queue from {remote_ref}; no local data was changed."
            }
        if show_res.returncode != 0:
            err_text = (show_res.stderr or show_res.stdout or "remote queue file is unavailable").strip()
            return {
                "success": False,
                "error": f"Could not read data/queue/publishing_queue.json from {remote_ref}: {err_text}. No local data was changed."
            }

        remote_payload = show_res.stdout
        if not isinstance(remote_payload, (str, bytes, bytearray)) or not remote_payload.strip():
            return {
                "success": False,
                "error": f"The remote queue from {remote_ref} was empty or unavailable. No local data was changed."
            }
        try:
            remote_queue_data = json.loads(remote_payload)
        except (TypeError, json.JSONDecodeError) as e:
            return {
                "success": False,
                "error": f"Failed to parse remote queue JSON from {remote_ref}: {e}. No local data was changed."
            }
        if not isinstance(remote_queue_data, dict) or not isinstance(remote_queue_data.get("items"), list):
            return {
                "success": False,
                "error": f"Remote queue from {remote_ref} is invalid: expected a JSON object with an items list. No local data was changed."
            }

        # 3. Load local queue from disk
        if not queue_path.exists():
            if remote_queue_data:
                local_queue_data = remote_queue_data
            else:
                return {
                    "success": False,
                    "error": f"Local queue file not found at {queue_path} and no remote queue exists."
                }
        else:
            try:
                with open(queue_path, "r", encoding="utf-8") as f:
                    local_queue_data = json.load(f)
            except Exception as e:
                return {
                    "success": False,
                    "error": f"Failed to read local queue JSON from {queue_path}: {str(e)}"
                }

        # 4. Safely merge queue states
        try:
            merged_queue_data, summary = merge_publishing_queues(
                local_data=local_queue_data,
                remote_data=remote_queue_data
            )
        except Exception as e:
            return {
                "success": False,
                "error": f"Queue merge validation failed: {str(e)}"
            }

        # Create safety backup of local queue
        if queue_path.exists():
            shutil.copyfile(queue_path, backup_path)

        # 5. Check remote commits ahead
        rev_res = _run_git(["rev-list", "--count", f"HEAD..{remote_ref}"])
        remote_ahead = 0
        if rev_res.returncode == 0 and rev_res.stdout.strip().isdigit():
            remote_ahead = int(rev_res.stdout.strip())

        # If remote has new commits, safely advance local HEAD
        if remote_ahead > 0:
            # Checkout queue to HEAD so git merge doesn't conflict on local uncommitted changes
            _run_git(["checkout", "HEAD", "--", "data/queue/publishing_queue.json"])
            merge_res = _run_git(["merge", "--ff-only", remote_ref])

            if merge_res.returncode != 0:
                # If ff-only is not possible, try clean rebase
                rebase_res = _run_git(["rebase", remote_ref])
                if rebase_res.returncode != 0:
                    _run_git(["rebase", "--abort"])
                    if backup_path.exists():
                        shutil.copyfile(backup_path, queue_path)
                        backup_path.unlink(missing_ok=True)
                    return {
                        "success": False,
                        "error": "Remote history has diverged and could not be fast-forwarded safely. Aborted to protect local changes."
                    }

        # 6. Write merged queue to disk
        queue_path.parent.mkdir(parents=True, exist_ok=True)
        with open(queue_path, "w", encoding="utf-8") as f:
            json.dump(merged_queue_data, f, indent=2)

        # 7. Identify specific files to stage
        stage_files = find_sync_stage_files(merged_queue_data, target_repo)

        for rel_file in stage_files:
            _run_git(["add", rel_file])

        # 8. Check for staged changes
        diff_res = _run_git(["diff", "--staged", "--quiet"])
        has_staged_changes = (diff_res.returncode != 0)

        # Also check if local HEAD is ahead of remote
        ahead_res = _run_git(["rev-list", "--count", f"{remote_ref}..HEAD"])
        local_ahead = 0
        if ahead_res.returncode == 0 and ahead_res.stdout.strip().isdigit():
            local_ahead = int(ahead_res.stdout.strip())

        if has_staged_changes:
            if summary["local_added_count"] > 0:
                commit_msg = f"Sync with GitHub: upload {summary['local_added_count']} new product(s) [skip ci]"
            else:
                commit_msg = "Sync with GitHub: update publishing queue state [skip ci]"

            commit_res = _run_git(["commit", "-m", commit_msg])
            if commit_res.returncode != 0:
                if backup_path.exists():
                    shutil.copyfile(backup_path, queue_path)
                    backup_path.unlink(missing_ok=True)
                return {
                    "success": False,
                    "error": f"Failed to commit synchronized changes: {commit_res.stderr.strip()}"
                }
            local_ahead += 1

        # Push to remote if local has commits ahead
        if local_ahead > 0:
            push_res = _run_git(["push", remote_name, branch_name])
            if push_res.returncode != 0:
                err_text = push_res.stderr.strip() or push_res.stdout.strip()
                return {
                    "success": False,
                    "error": f"Changes were committed locally, but push to GitHub failed: {err_text}"
                }

        # Clean up backup
        if backup_path.exists():
            backup_path.unlink(missing_ok=True)

        # Build friendly result message
        actions = []
        if summary["remote_updated_count"] > 0:
            actions.append(f"received {summary['remote_updated_count']} cloud status update(s)")
        if summary["local_added_count"] > 0:
            actions.append(f"uploaded {summary['local_added_count']} new product(s)")

        if actions:
            msg = f"Synchronized with GitHub: {', '.join(actions)}."
        else:
            msg = "Synchronized with GitHub: queue is fully up to date."

        return {
            "success": True,
            "message": msg,
            "details": summary
        }

    except Exception as e:
        if backup_path.exists():
            shutil.copyfile(backup_path, queue_path)
            backup_path.unlink(missing_ok=True)
        return {
            "success": False,
            "error": f"Unexpected synchronization error: {str(e)}"
        }

    finally:
        SYNC_LOCK.release()
