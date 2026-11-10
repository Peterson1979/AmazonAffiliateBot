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
    next_queue_item_id,
    portable_package_reference,
    STATUS_READY,
    STATUS_PUBLISHED,
    STATUS_PARTIALLY_PUBLISHED,
    STATUS_FAILED,
)

# Global lock to ensure only one synchronization operation runs at any time
SYNC_LOCK = threading.Lock()


def _as_claim_dict(value: Any) -> Dict[str, Any]:
    """Return a claim record only when it is a non-empty dictionary."""
    return dict(value) if isinstance(value, dict) and value else {}


def _parse_claim_timestamp(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed


def _claim_is_newer(candidate: Dict[str, Any], reference: Dict[str, Any]) -> bool:
    """True when ``candidate`` has the newer ``claimed_at`` timestamp."""
    candidate_ts = str(candidate.get("claimed_at") or "")
    reference_ts = str(reference.get("claimed_at") or "")
    candidate_dt = _parse_claim_timestamp(candidate_ts)
    reference_dt = _parse_claim_timestamp(reference_ts)
    if candidate_dt is None or reference_dt is None:
        # Unparseable timestamps lose against parseable ones; if both are
        # unparseable fall back to a deterministic lexicographic comparison.
        if candidate_dt is None and reference_dt is None:
            return candidate_ts > reference_ts
        return candidate_dt is not None
    try:
        return candidate_dt > reference_dt
    except TypeError:
        # Mixed naive/aware timestamps: compare their string form instead.
        return candidate_ts > reference_ts


def merge_slot_claims(
    local_claims: Any,
    remote_claims: Any,
) -> Dict[str, Dict[str, Any]]:
    """Merge the persisted ``slot_claims`` maps of both queues by claim key.

    Rules:
    - A claim key present on only one side is always preserved.
    - On conflict, ``COMPLETED`` wins, so a finished slot can never be
      reopened by a stale claim from the other side.
    - Otherwise the claim with the newer ``claimed_at`` wins (ties resolve to
      the remote claim for deterministic, cloud-baseline behaviour).

    Claims dropped here would let a scheduled slot run twice, so the merge is
    explicit rather than incidental to the item merge.
    """
    local_map = local_claims if isinstance(local_claims, dict) else {}
    remote_map = remote_claims if isinstance(remote_claims, dict) else {}

    merged: Dict[str, Dict[str, Any]] = {}
    for key in sorted(set(local_map) | set(remote_map)):
        local_claim = _as_claim_dict(local_map.get(key))
        remote_claim = _as_claim_dict(remote_map.get(key))

        if local_claim and not remote_claim:
            merged[key] = local_claim
            continue
        if remote_claim and not local_claim:
            merged[key] = remote_claim
            continue
        if not local_claim and not remote_claim:
            continue

        local_completed = local_claim.get("state") == "COMPLETED"
        remote_completed = remote_claim.get("state") == "COMPLETED"
        if local_completed != remote_completed:
            merged[key] = local_claim if local_completed else remote_claim
        elif _claim_is_newer(local_claim, remote_claim):
            merged[key] = local_claim
        else:
            merged[key] = remote_claim
    return merged


def merge_import_batches(
    local_batches: Any,
    remote_batches: Any,
) -> List[Dict[str, Any]]:
    """Union two append-only import batch ledgers keyed by ``batch_id``.

    Local order is kept and remote-only batches are appended, while a shared
    ``batch_id`` resolves to the record with the newer ``created_at`` (ties and
    unparsable timestamps resolve to the remote record). This guarantees a
    stale local ledger can never overwrite newer remote batch history.
    """
    local_list = [b for b in local_batches if isinstance(b, dict)] if isinstance(local_batches, list) else []
    remote_list = [b for b in remote_batches if isinstance(b, dict)] if isinstance(remote_batches, list) else []

    def _key(batch: Dict[str, Any]) -> str:
        return str(batch.get("batch_id") or "")

    local_by_id = {_key(b): b for b in local_list if _key(b)}
    remote_by_id = {_key(b): b for b in remote_list if _key(b)}

    def _newer(candidate: Dict[str, Any], reference: Dict[str, Any]) -> bool:
        candidate_dt = _parse_claim_timestamp(candidate.get("created_at"))
        reference_dt = _parse_claim_timestamp(reference.get("created_at"))
        if candidate_dt is None or reference_dt is None:
            return candidate_dt is not None and reference_dt is None
        return candidate_dt >= reference_dt

    merged: List[Dict[str, Any]] = []
    seen: Set[str] = set()
    for batch in local_list + remote_list:
        batch_id = _key(batch)
        if not batch_id or batch_id in seen:
            continue
        local_batch = local_by_id.get(batch_id)
        remote_batch = remote_by_id.get(batch_id)
        if local_batch is not None and remote_batch is not None:
            winner = local_batch if _newer(local_batch, remote_batch) else remote_batch
        else:
            winner = local_batch if local_batch is not None else remote_batch
        merged.append(winner)
        seen.add(batch_id)
    return merged


SYNC_STATE_FILENAME = "publishing_queue.json.sync_state.json"


def _read_json_file(path: Path) -> Any:
    """Read a JSON file, returning ``None`` for missing or malformed content."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, ValueError, TypeError):
        return None


def _write_sync_state(state_path: Path, phase: str, **extra: Any) -> None:
    """Atomically persist the current sync phase for crash recovery."""
    payload = {
        "phase": phase,
        "updated_at": datetime.now().isoformat(),
        **extra,
    }
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = state_path.with_name(state_path.name + ".tmp")
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)
    os.replace(tmp_path, state_path)


def _is_valid_queue_file(path: Path) -> bool:
    data = _read_json_file(path)
    return isinstance(data, dict) and isinstance(data.get("items"), list)


def _is_valid_batches_file(path: Path) -> bool:
    return isinstance(_read_json_file(path), list)


def recover_interrupted_sync(
    queue_path: Path,
    queue_backup_path: Path,
    batches_path: Path,
    batches_backup_path: Path,
    state_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Repair files left behind by a sync process that was killed mid-run.

    A small phase manifest is written before any local file is touched. When it
    says the merged queue was not written yet, the pre-sync backup is restored
    even though the main file may still be a valid (but older) HEAD checkout.
    Once the merged queue/batch ledger has been written, the current files are
    authoritative and only stale backups are removed. Missing or unreadable
    main files are always restored from a valid backup, with or without a
    manifest (legacy crash recovery).
    """
    state_path = state_path or queue_path.parent / SYNC_STATE_FILENAME
    state = _read_json_file(state_path) if state_path.exists() else None
    phase = state.get("phase") if isinstance(state, dict) else None
    result: Dict[str, Any] = {"recovered": False, "phase": phase, "actions": []}

    queue_written_phases = {"queue_written", "batches_written", "staged", "committed", "pushed"}
    batches_written_phases = {"batches_written", "staged", "committed", "pushed"}

    if queue_backup_path.exists():
        if phase in queue_written_phases:
            queue_backup_path.unlink(missing_ok=True)
            result["actions"].append("discarded stale queue backup; merged queue was already written")
        elif phase is not None or not _is_valid_queue_file(queue_path):
            # The sync never wrote the merged queue. A checkout may already
            # have replaced the divergent local file, so restore the backup.
            shutil.copyfile(queue_backup_path, queue_path)
            queue_backup_path.unlink(missing_ok=True)
            result["recovered"] = True
            result["actions"].append("restored pre-sync queue after interrupted synchronization")
        else:
            # Legacy orphan backup with a valid main file: never overwrite it.
            queue_backup_path.unlink(missing_ok=True)
            result["actions"].append("discarded orphan queue backup")

    if batches_backup_path.exists():
        if phase in batches_written_phases:
            batches_backup_path.unlink(missing_ok=True)
            result["actions"].append("discarded stale batch backup; ledger was already written")
        elif phase is not None or not _is_valid_batches_file(batches_path):
            batches_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(batches_backup_path, batches_path)
            batches_backup_path.unlink(missing_ok=True)
            result["recovered"] = True
            result["actions"].append("restored local import batch ledger after interrupted synchronization")
        else:
            batches_backup_path.unlink(missing_ok=True)
            result["actions"].append("discarded orphan batch backup")

    if state_path.exists():
        state_path.unlink(missing_ok=True)
    return result


def _normalize_package_references(
    items: List[Dict[str, Any]],
    repo_dir: Optional[Path] = None,
) -> None:
    """Rewrite in-repository package references to portable relative paths."""
    root = repo_dir or PROJECT_ROOT
    for item in items:
        reference = item.get("package_reference")
        if not reference:
            continue
        item["package_reference"] = portable_package_reference(
            reference, asin=str(item.get("asin") or ""), repo_dir=root
        )


def _merge_platform_results(
    primary: Optional[Dict[str, Any]],
    secondary: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    """Merge per-platform publishing results without ever losing a confirmed PUBLISHED result."""
    if not isinstance(primary, dict) and not isinstance(secondary, dict):
        return primary if primary is not None else secondary
    if not isinstance(primary, dict):
        return dict(secondary)  # type: ignore[arg-type]
    if not isinstance(secondary, dict):
        return dict(primary)

    def _entry_rank(entry: Any) -> int:
        if not isinstance(entry, dict):
            return 0
        status = entry.get("status")
        mode = entry.get("mode")
        if status == STATUS_PUBLISHED and mode == "LIVE":
            return 4
        if status == STATUS_PUBLISHED:
            return 3
        if status == STATUS_FAILED:
            return 2
        return 1

    combined: Dict[str, Any] = dict(secondary)
    for k, p_val in primary.items():
        s_val = combined.get(k)
        if s_val is None or _entry_rank(p_val) >= _entry_rank(s_val):
            combined[k] = p_val
    return combined


REQUIRED_PUBLISHING_DESTINATIONS = ("facebook", "pinterest", "pinterest_lifemodehq", "threads")


def _normalize_media_type(item: Dict[str, Any]) -> str:
    m = item.get("media_type")
    if m:
        m_str = str(m).strip().lower()
        if m_str in ("video", "image"):
            return m_str
    if item.get("video_url"):
        return "video"
    return "image"


def _item_identity_key(item: Dict[str, Any]) -> Tuple[str, str]:
    asin = str(item.get("asin") or "").upper().strip()
    return (asin, _normalize_media_type(item))


def _is_all_destinations_confirmed(pub_results: Optional[Dict[str, Any]]) -> bool:
    """Return True if all 4 required platforms have confirmed live or duplicate-safe publications."""
    if not isinstance(pub_results, dict):
        return False
    for dest in REQUIRED_PUBLISHING_DESTINATIONS:
        res = pub_results.get(dest)
        if not isinstance(res, dict):
            return False
        if res.get("status") != STATUS_PUBLISHED:
            return False
        has_evidence = bool(
            res.get("post_id")
            or res.get("video_id")
            or res.get("pin_id")
            or res.get("photo_id")
            or res.get("permalink")
            or res.get("permalink_url")
            or res.get("pin_url")
            or res.get("action") == "SKIP_DUPLICATE"
        )
        if not has_evidence:
            return False
    return True


def merge_publishing_queues(
    local_data: Dict[str, Any],
    remote_data: Dict[str, Any],
    repo_dir: Optional[Path] = None,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """
    Deterministically and safely merge local queue state with remote cloud queue state.

    Rules:
    - Remote cloud status (PUBLISHED, PARTIALLY_PUBLISHED, FAILED) takes precedence
      for items published in the cloud. Platform timestamps and results are preserved.
    - Locally added items (present in local but not remote) are preserved with their allocated slots.
    - Paired image and video entries for the same ASIN are both preserved and indexed
      by (asin, media_type) to prevent accidental cross-media deduplication.
    - When all 4 publishing platforms are confirmed published across merged results,
      the item status is promoted to PUBLISHED.
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

    # Index items by ID and by composite key (asin, media_type)
    local_by_id = {it.get("queue_item_id"): it for it in local_items if it.get("queue_item_id")}
    local_by_key = {_item_identity_key(it): it for it in local_items if it.get("asin")}

    remote_by_id = {it.get("queue_item_id"): it for it in remote_items if it.get("queue_item_id")}
    remote_by_key = {_item_identity_key(it): it for it in remote_items if it.get("asin")}

    # Persisted slot claims live at queue level, not on the items, so they
    # must be merged explicitly or the merged queue silently drops them.
    merged_slot_claims = merge_slot_claims(
        local_data.get("slot_claims"),
        remote_data.get("slot_claims"),
    )

    merged_items: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()
    seen_item_keys: Set[Tuple[str, str]] = set()

    remote_updated_count = 0
    local_added_count = 0

    # 1. Process all remote items as the cloud baseline
    for r_item in remote_items:
        item_id = r_item.get("queue_item_id")
        asin = r_item.get("asin", "").upper()
        r_key = _item_identity_key(r_item) if asin else None

        # Match corresponding local item by ID first, then by (asin, media_type)
        l_item = local_by_id.get(item_id) or (local_by_key.get(r_key) if r_key else None)

        if l_item:
            merged = dict(r_item)
            r_status = r_item.get("status")
            l_status = l_item.get("status")

            terminal_statuses = (STATUS_PUBLISHED, STATUS_PARTIALLY_PUBLISHED, STATUS_FAILED)
            terminal_ranks = {
                STATUS_PUBLISHED: 3,
                STATUS_PARTIALLY_PUBLISHED: 2,
                STATUS_FAILED: 1,
            }
            r_rank = terminal_ranks.get(r_status, 0)
            l_rank = terminal_ranks.get(l_status, 0)

            if r_rank > 0 and r_rank >= l_rank:
                # Cloud publisher holds equal or more complete terminal status -> remote wins
                merged["status"] = r_status
                merged["published_at"] = r_item.get("published_at") or l_item.get("published_at")
                merged["publishing_results"] = _merge_platform_results(
                    r_item.get("publishing_results"),
                    l_item.get("publishing_results"),
                )
                merged["errors"] = r_item.get("errors", [])
                if l_status == STATUS_READY:
                    remote_updated_count += 1
            elif l_rank > r_rank:
                # Local recorded a more complete terminal status (e.g. PUBLISHED over
                # PARTIALLY_PUBLISHED or READY) -> preserve local publication evidence
                merged["status"] = l_status
                merged["published_at"] = l_item.get("published_at") or r_item.get("published_at")
                merged["publishing_results"] = _merge_platform_results(
                    l_item.get("publishing_results"),
                    r_item.get("publishing_results"),
                )
                merged["errors"] = l_item.get("errors", [])
            else:
                merged["status"] = r_status or l_status

            # Retain locally enriched references if missing on remote
            if l_item.get("package_reference") and not r_item.get("package_reference"):
                merged["package_reference"] = l_item.get("package_reference")
            if l_item.get("product_name") and not r_item.get("product_name"):
                merged["product_name"] = l_item.get("product_name")
            if r_status not in terminal_statuses and l_status not in terminal_statuses:
                # For unpublished items, local dashboard media edits (upload, replace, remove)
                # are authoritative over an older remote READY snapshot.
                if l_item.get("media_type"):
                    merged["media_type"] = l_item["media_type"]
                else:
                    merged.pop("media_type", None)
                if l_item.get("video_url"):
                    merged["video_url"] = l_item["video_url"]
                else:
                    merged.pop("video_url", None)
            else:
                if l_item.get("media_type") and not r_item.get("media_type"):
                    merged["media_type"] = l_item.get("media_type")
                if l_item.get("video_url") and not r_item.get("video_url"):
                    merged["video_url"] = l_item.get("video_url")

            # Upgrade status to PUBLISHED if all 4 required platforms are confirmed live
            if _is_all_destinations_confirmed(merged.get("publishing_results")):
                merged["status"] = STATUS_PUBLISHED
                merged["published_at"] = merged.get("published_at") or r_item.get("published_at") or l_item.get("published_at")
                merged["errors"] = []

            merged_items.append(merged)
            if item_id:
                seen_ids.add(item_id)
            if r_key:
                seen_item_keys.add(r_key)
        else:
            # Item exists only on remote
            merged_items.append(dict(r_item))
            if item_id:
                seen_ids.add(item_id)
            if r_key:
                seen_item_keys.add(r_key)

    # 2. Process locally added items not present on remote
    for l_item in local_items:
        item_id = l_item.get("queue_item_id")
        asin = l_item.get("asin", "").upper()
        l_key = _item_identity_key(l_item) if asin else None

        # Deduplicate by composite key (asin, media_type): the same product & media format on both sides is one item.
        if l_key and l_key in seen_item_keys:
            continue
        if item_id and item_id in seen_ids and not asin:
            # Identifier-only match without an ASIN cannot be told apart from
            # the already-merged record; keep the previous conservative skip.
            continue

        if item_id and item_id in seen_ids:
            # Diverged queues handed out the same identifier to two different
            # products. Re-allocate a fresh unique ID instead of silently
            # dropping the genuinely new local product.
            l_item = dict(l_item)
            item_id = next_queue_item_id(
                [it.get("queue_item_id") for it in merged_items] + list(seen_ids),
                asin,
            )
            l_item["queue_item_id"] = item_id

        merged_items.append(dict(l_item))
        if item_id:
            seen_ids.add(item_id)
        if l_key:
            seen_item_keys.add(l_key)
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

    # 3.5 Rewrite machine-specific package paths to repository-relative paths
    # so the merged queue is valid on the GitHub Actions Linux runner too.
    _normalize_package_references(merged_items, repo_dir)

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
        "slot_claims": merged_slot_claims,
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

    # Dashboard import batch history travels with the queue so the Dashboard's
    # batch history stays consistent with the files synchronized to GitHub.
    batches_candidate = repo_dir / "data" / "batches" / "import_batches.json"
    if batches_candidate.exists():
        stage_set.add("data/batches/import_batches.json")

    for item in merged_queue_dict.get("items", []):
        asin = item.get("asin")
        if asin:
            prod_candidate = repo_dir / "data" / "products" / f"asin_{asin.upper()}.json"
            if prod_candidate.exists():
                stage_set.add(f"data/products/asin_{asin.upper()}.json")

            pkg_candidate = repo_dir / "data" / "generated" / f"asin_{asin.upper()}_content_package.json"
            if pkg_candidate.exists():
                stage_set.add(f"data/generated/asin_{asin.upper()}_content_package.json")

            creatives_dir = repo_dir / "data" / "creatives" / asin.upper()
            if creatives_dir.is_dir():
                for c_file in creatives_dir.iterdir():
                    if c_file.is_file() and c_file.suffix.lower() in (".json", ".jpg", ".jpeg", ".png", ".webp"):
                        stage_set.add(f"data/creatives/{asin.upper()}/{c_file.name}")

        pkg_ref = item.get("package_reference")
        if pkg_ref:
            norm_ref = pkg_ref.replace("\\", "/")
            ref_path = Path(norm_ref)
            if not ref_path.is_absolute():
                ref_path = repo_dir / norm_ref
            try:
                relative_ref = ref_path.resolve().relative_to(repo_dir.resolve()).as_posix()
            except (OSError, ValueError):
                relative_ref = ""
            if relative_ref.startswith("data/") and (repo_dir / relative_ref).exists():
                stage_set.add(relative_ref)

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
    batches_path = target_repo / "data" / "batches" / "import_batches.json"
    batches_backup_path = target_repo / "data" / "batches" / "import_batches.json.sync_bak"
    state_path = target_repo / "data" / "queue" / SYNC_STATE_FILENAME
    batches_written = False
    local_batches_raw: Optional[str] = None

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

        # Repair any files left behind by a sync process that was interrupted
        # (for example a killed workflow or a closed dashboard). This runs
        # before reading the local queue so recovery cannot be overwritten.
        try:
            recover_interrupted_sync(
                queue_path, backup_path, batches_path, batches_backup_path, state_path
            )
        except OSError as recovery_error:
            return {
                "success": False,
                "error": f"Could not recover an interrupted synchronization safely: {recovery_error}"
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

        # Create safety backup of local queue and record the phase before any
        # file is mutated, so an interrupted run can restore the exact pre-sync
        # local state on the next attempt.
        if queue_path.exists():
            shutil.copyfile(queue_path, backup_path)
        _write_sync_state(state_path, "backed_up")

        # 4.5 Reconcile the dashboard's import batch history with the remote
        # copy. The union is written later, so a stale local ledger can never
        # overwrite newer remote batch history.
        local_batches_raw: Optional[str] = None
        local_batches_data: Optional[List[Dict[str, Any]]] = None
        remote_batches_data: Optional[List[Dict[str, Any]]] = None
        merged_batches_data: Optional[List[Dict[str, Any]]] = None
        if batches_path.exists():
            try:
                local_batches_raw = batches_path.read_text(encoding="utf-8")
                parsed_local = json.loads(local_batches_raw)
                if isinstance(parsed_local, list):
                    local_batches_data = parsed_local
            except (OSError, ValueError, TypeError):
                local_batches_data = None

        if local_batches_data is not None:
            batch_show = _run_git(["show", f"{remote_ref}:data/batches/import_batches.json"])
            if batch_show.returncode == 0 and str(batch_show.stdout or "").strip():
                try:
                    parsed_remote = json.loads(batch_show.stdout)
                    if isinstance(parsed_remote, list):
                        remote_batches_data = parsed_remote
                except (TypeError, ValueError):
                    remote_batches_data = None
            merged_batches_data = merge_import_batches(local_batches_data, remote_batches_data)
            if remote_batches_data is not None and batches_path.exists():
                # Move the (possibly still untracked) local ledger aside so a
                # remote commit adding the same path cannot abort the merge.
                shutil.copyfile(batches_path, batches_backup_path)
                batches_path.unlink(missing_ok=True)


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
        _write_sync_state(state_path, "queue_written")

        # 6.5 Write the reconciled import batch history (local records are
        # never dropped; remote-only batches are appended).
        if merged_batches_data is not None:
            batches_path.parent.mkdir(parents=True, exist_ok=True)
            with open(batches_path, "w", encoding="utf-8") as f:
                json.dump(merged_batches_data, f, indent=2, ensure_ascii=False)
            batches_written = True
            batches_backup_path.unlink(missing_ok=True)
            _write_sync_state(state_path, "batches_written")

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

        # Clean up backup and phase manifest
        if backup_path.exists():
            backup_path.unlink(missing_ok=True)
        state_path.unlink(missing_ok=True)

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
        state_path.unlink(missing_ok=True)
        return {
            "success": False,
            "error": f"Unexpected synchronization error: {str(e)}"
        }

    finally:
        try:
            if batches_written:
                batches_backup_path.unlink(missing_ok=True)
            elif batches_backup_path.exists() and local_batches_raw is not None:
                # Synchronization aborted before the merged ledger could be
                # written: restore the original local batch history untouched.
                shutil.copyfile(batches_backup_path, batches_path)
                batches_backup_path.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            if not backup_path.exists() and not batches_backup_path.exists():
                state_path.unlink(missing_ok=True)
        except OSError:
            pass
        SYNC_LOCK.release()
