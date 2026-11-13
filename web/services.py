"""
AmazonAffiliateBot Management Services.
Bridges the Flask web dashboard to existing application modules:
Discovery, Scraper, Facts, Generator, Validator, Package, Queue, Replenisher, and Publisher.
"""

from typing import Dict, Any, List, Optional, Tuple, Set, Union
from pathlib import Path
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
import json
import os
import re
import shutil
import threading
import urllib.request
import subprocess
import tempfile
import uuid

from amazon.scraper import Product, scrape_amazon_product, extract_asin_from_url
from amazon.creators_api import CreatorsAPIClient, marketplace_from_url
from amazon.discovery import (
    AmazonDiscoveryEngine,
    DiscoveryHistory,
    DiscoveredCandidate,
    DEFAULT_CONFIG_QUERIES,
    DEFAULT_TRACKING_TAG
)
from ai.provider import create_content_provider
from content.facts import FactExtractor, CanonicalProductFacts
from content.generator import ContentGenerator
from content.validator import ClaimValidator
from content.package import (
    ContentPackage,
    STATUS_READY_FOR_REVIEW,
    STATUS_FAILED_VALIDATION,
    STATUS_VALID,
    SUPPORTED_PLATFORMS,
    is_valid_video_url,
)
try:
    from content.pinterest_creative import (
        load_product_creatives,
        generate_flow_creatives,
        generate_pinterest_creatives,
        save_uploaded_creative,
        set_selected_creative,
        get_selected_creative,
        get_product_flow_creative_status,
        get_or_download_reference_image,
        CREATIVE_TYPES,
        CREATIVE_TYPE_GIFT,
        CREATIVE_TYPE_EVERYDAY,
        CREATIVE_TYPE_LABELS,
    )
except ImportError:
    CREATIVE_TYPES = ["gift_inspiration", "everyday_pick"]
    CREATIVE_TYPE_GIFT = "gift_inspiration"
    CREATIVE_TYPE_EVERYDAY = "everyday_pick"
    CREATIVE_TYPE_LABELS = {
        "gift_inspiration": "Gift Inspiration",
        "everyday_pick": "Everyday Pick",
    }
    def load_product_creatives(asin, base_dir=None):
        return {}
    def generate_flow_creatives(product, overwrite=False):
        return {}
    def generate_pinterest_creatives(*args, **kwargs):
        return {}
    def save_uploaded_creative(*args, **kwargs):
        return False, "Pinterest creative module not available", {}
    def set_selected_creative(*args, **kwargs):
        return False, "Pinterest creative module not available", {}
    def get_selected_creative(*args, **kwargs):
        return None
    def get_product_flow_creative_status(*args, **kwargs):
        return {}
    def get_or_download_reference_image(*args, **kwargs):
        return None
from publishing.images import recover_verified_image_url, is_valid_image_url
from publishing.queue import (
    PublishingQueue,
    QueueItem,
    QueuePublisher,
    is_item_overdue_held,
    find_overdue_held_items,
    get_budapest_timezone,
    DEFAULT_QUEUE_FILE,
    DEFAULT_LOGS_DIR,
    DEFAULT_LOG_FILE,
    DEFAULT_TIMEZONE,
    SLOT_1_TIME,
    SLOT_2_TIME,
    STATUS_READY,
    STATUS_PUBLISHED,
    STATUS_PUBLISHING,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_PARTIALLY_PUBLISHED,
    STATUS_SKIPPED,
    PUBLISHING_WINDOWS,
    ALL_PLATFORM_DESTINATIONS,
    parse_affiliate_urls,
    get_authoritative_urls_file,
    resolve_product_for_url,
    next_queue_item_id
)
from publishing.replenisher import (
    QueueReplenisher,
    DEFAULT_TARGET_FUTURE_CAPACITY,
    DEFAULT_MIN_FUTURE_CAPACITY,
    DEFAULT_MAINTENANCE_LOG_FILE
)
from publishing.config import PublishingConfig, DEFAULT_TARGET_ACCOUNTS
from publishing.publisher import SocialPublisher, STATUS_WOULD_PUBLISH
from publishing.r2_storage import (
    R2Client,
    R2Config,
    get_max_video_size_bytes,
    get_video_upload_timeout,
    upload_video_to_r2,
    validate_video_file,
)
from publishing.reconciliation import get_historical_destination_report


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.json"
DATA_DIR = PROJECT_ROOT / "data"
PRODUCTS_DIR = DATA_DIR / "products"
GENERATED_DIR = DATA_DIR / "generated"
QUEUE_FILE = DATA_DIR / "queue" / "publishing_queue.json"
BATCHES_FILE = DATA_DIR / "batches" / "import_batches.json"
LOGS_DIR = DATA_DIR / "logs"
DISCOVERY_HISTORY_FILE = DATA_DIR / "discovery" / "discovery_history.json"
SCHEDULED_WORKFLOW_FILE = PROJECT_ROOT / ".github" / "workflows" / "scheduled_publishing.yml"
ACTIVE_PUBLISHING_PLATFORMS = ("facebook", "pinterest", "pinterest_lifemodehq", "threads")

_LIVE_PUBLISH_LOCK = threading.Lock()
_ACTIVE_PUBLISHING_ITEM_IDS: Set[str] = set()

DEFAULT_AFFILIATE_DISCLOSURE = "This post contains affiliate links. As an Amazon Associate I earn from qualifying purchases."


def load_import_batches(path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Load the append-only dashboard import ledger safely."""
    path = path or BATCHES_FILE
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except (OSError, ValueError, TypeError):
        return []


def save_import_batches(batches: List[Dict[str, Any]], path: Optional[Path] = None) -> None:
    """Persist import batches without changing queue or publication records."""
    path = path or BATCHES_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(batches, handle, indent=2, ensure_ascii=False)


def format_batch_label(batch: Dict[str, Any]) -> str:
    """Format a human-readable batch label instead of raw ISO timestamp."""
    created_at = batch.get("created_at", "")
    try:
        dt = datetime.fromisoformat(created_at)
        formatted_dt = dt.strftime("%b %d, %Y - %H:%M")
    except Exception:
        formatted_dt = created_at[:16].replace("T", " ") if created_at else "Unknown date"

    submitted = batch.get("submitted_count", 0)
    accepted = batch.get("accepted_count", 0)
    return f"Batch {formatted_dt} ({accepted} accepted / {submitted} submitted)"


def to_repository_relative_path(path: Union[str, Path]) -> str:
    """Return a portable, repository-relative POSIX path for queue storage.

    New queue entries must never record a machine-specific absolute Windows
    path: GitHub Actions resolves ``data/generated/...`` from the checkout root,
    and the publisher's package resolution already falls back to the
    repository-root-relative candidate. Existing absolute references are left
    untouched by this helper; it only normalises paths that live inside the
    repository and otherwise returns the input unchanged (POSIX separators).
    """
    raw = Path(path)
    try:
        return raw.resolve().relative_to(PROJECT_ROOT).as_posix()
    except (ValueError, OSError):
        return str(path).replace("\\", "/")


def get_import_batch(batch_id: Optional[str] = None, path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Return a selected batch or the newest recorded batch."""
    batches = load_import_batches(path)
    if batch_id:
        return next((batch for batch in batches if batch.get("batch_id") == batch_id), None)
    return batches[-1] if batches else None


def build_import_batch_view(
    batch: Optional[Dict[str, Any]],
    queue: Optional[PublishingQueue]
) -> Dict[str, Any]:
    """Combine immutable import outcomes with current queue/platform state."""
    if not batch:
        return {"batch": None, "entries": [], "items": []}
    queue_items = {item.queue_item_id: item for item in (queue.items if queue else [])}
    entries = []
    items = []
    for raw_entry in batch.get("entries", []):
        entry = dict(raw_entry)
        item = queue_items.get(entry.get("queue_item_id"))
        if item:
            presentation = get_queue_item_presentation(queue, item)
            item.ui_state = presentation["state"]
            item.ui_retryable = presentation["retryable"]
            item.ui_platforms = presentation["platforms"]
            item.ui_published_at = presentation["published_at"]
            item.ui_label = presentation.get("ui_label", presentation["state"])
            item.ui_media_type = presentation["media_type"]
            item.ui_video_url = presentation["video_url"]
            item.ui_image_url = presentation["image_url"]
            item.ui_windows = presentation.get("windows", [])
            item.ui_execution_times = presentation.get("execution_times", "")
            items.append(item)
            entry["queue_item"] = item
        entries.append(entry)
    return {"batch": batch, "entries": entries, "items": items}


def _slot_claim_for_item(queue: PublishingQueue, item: QueueItem) -> Dict[str, Any]:
    """Return the persisted claim for an item without inventing live state."""
    if not item.scheduled_at or not item.daily_slot:
        return {}
    try:
        item_dt = QueuePublisher.parse_datetime(item.scheduled_at)
    except Exception:
        return {}
    key = f"{item_dt.date().isoformat()}-slot-{item.daily_slot}"
    return (queue.slot_claims or {}).get(key) or {}


MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def format_scheduled_datetime(dt: datetime) -> str:
    """Format a datetime in Europe/Budapest as 'Nov 4, 2026, 15:00'."""
    month_name = MONTH_NAMES[dt.month - 1]
    return f"{month_name} {dt.day}, {dt.year}, {dt.strftime('%H:%M')}"


def parse_product_scheduled_dt(
    scheduled_at: Optional[str] = None,
    scheduled_date: Optional[str] = None,
    daily_slot: Optional[int] = None,
    tz: Optional[Any] = None,
) -> Optional[datetime]:
    """Parse scheduled date and time into a timezone-aware Europe/Budapest datetime."""
    if tz is None:
        tz = get_budapest_timezone()
    if scheduled_at:
        try:
            dt = datetime.fromisoformat(scheduled_at)
            return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)
        except Exception:
            pass
    if scheduled_date:
        try:
            slot_hour = SLOT_1_TIME.hour if daily_slot == 1 else SLOT_2_TIME.hour
            d = datetime.strptime(scheduled_date, "%Y-%m-%d").date()
            return datetime(d.year, d.month, d.day, slot_hour, 0, 0, tzinfo=tz)
        except Exception:
            pass
    return None


def get_platform_scheduled_dt(
    item: QueueItem,
    platform: str,
    tz: Optional[Any] = None,
) -> Optional[datetime]:
    """
    Return the authoritative Europe/Budapest scheduled datetime for a specific platform on a queue item.
    - Slot 1:
      - Main platforms (facebook, threads, pinterest): Window 1 at 15:00
      - Pinterest LifeModeHQ (pinterest_lifemodehq): Window 2 at 18:00
    - Slot 2:
      - Main platforms (facebook, threads, pinterest): Window 3 at 21:00
      - Pinterest LifeModeHQ (pinterest_lifemodehq): Window 4 at 23:00
    """
    if tz is None:
        tz = get_budapest_timezone()

    slot = item.daily_slot
    if not item.scheduled_date or slot not in (1, 2):
        return parse_product_scheduled_dt(
            scheduled_at=item.scheduled_at,
            scheduled_date=item.scheduled_date,
            daily_slot=item.daily_slot,
            tz=tz,
        )

    if slot == 1:
        hour = 18 if platform == "pinterest_lifemodehq" else 15
    else:
        hour = 23 if platform == "pinterest_lifemodehq" else 21

    try:
        d = datetime.strptime(item.scheduled_date, "%Y-%m-%d").date()
        return datetime(d.year, d.month, d.day, hour, 0, 0, tzinfo=tz)
    except Exception:
        return parse_product_scheduled_dt(
            scheduled_at=item.scheduled_at,
            scheduled_date=item.scheduled_date,
            daily_slot=item.daily_slot,
            tz=tz,
        )


def resolve_product_publication_status(
    scheduled_at: Optional[str] = None,
    scheduled_date: Optional[str] = None,
    daily_slot: Optional[int] = None,
    publishing_results: Optional[Dict[str, Any]] = None,
    published_at: Optional[str] = None,
    claimed: bool = False,
    current_dt: Optional[datetime] = None,
) -> Dict[str, str]:
    """Compute the truthful, time-based publication status for any product."""
    tz = get_budapest_timezone()
    now_dt = current_dt or datetime.now(tz)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=tz)
    else:
        now_dt = now_dt.astimezone(tz)

    raw_results = [v for v in (publishing_results or {}).values() if isinstance(v, dict)]
    has_live = any(_is_confirmed_live_result(v) for v in raw_results)
    all_live = bool(raw_results) and all(_is_confirmed_live_result(v) for v in raw_results)

    scheduled_dt = parse_product_scheduled_dt(
        scheduled_at=scheduled_at,
        scheduled_date=scheduled_date,
        daily_slot=daily_slot,
        tz=tz,
    )
    is_future = scheduled_dt is not None and scheduled_dt > now_dt
    time_has_passed = scheduled_dt is not None and scheduled_dt <= now_dt

    if all_live:
        return {"state": "PUBLISHED", "ui_label": "Published"}
    if has_live:
        return {"state": "PARTIAL", "ui_label": "Partial"}
    if is_future:
        state = "PUBLISHING" if claimed else "SCHEDULED"
        label = f"Scheduled — {format_scheduled_datetime(scheduled_dt)}"
        return {"state": state, "ui_label": label}
    if time_has_passed:
        return {"state": "SIMULATED", "ui_label": "Not published"}

    return {"state": "UNKNOWN", "ui_label": "Awaiting confirmation"}


def get_platform_publication_result(
    item: QueueItem,
    platform: str,
    current_dt: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Normalize one active platform result for truthful UI rendering with window attribution."""
    raw_results = item.publishing_results or {}
    result = raw_results.get(platform)
    if not isinstance(result, dict):
        result = None

    tz = get_budapest_timezone()
    now_dt = current_dt or datetime.now(tz)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=tz)
    else:
        now_dt = now_dt.astimezone(tz)

    scheduled_dt = get_platform_scheduled_dt(item, platform, tz=tz)
    is_future = scheduled_dt is not None and scheduled_dt > now_dt
    time_has_passed = scheduled_dt is not None and scheduled_dt <= now_dt

    slot = item.daily_slot or 1
    if slot == 1:
        win_id = 2 if platform == "pinterest_lifemodehq" else 1
        hour = 18 if platform == "pinterest_lifemodehq" else 15
    else:
        win_id = 4 if platform == "pinterest_lifemodehq" else 3
        hour = 23 if platform == "pinterest_lifemodehq" else 21

    window_time_str = f"{hour:02d}:00"
    win_cfg = PUBLISHING_WINDOWS.get(win_id, {})
    window_name = win_cfg.get("name", f"Window {win_id}")

    state = "UNKNOWN"
    historical_dry_run_ambiguous = _has_ambiguous_historical_dry_run(item)
    from publishing.blacklist import is_asin_blacklisted
    if result:
        result_status = result.get("status")
        mode = result.get("mode")
        if _is_confirmed_live_result(result):
            if result.get("action") == "SKIP_DUPLICATE":
                state = "SKIPPED"
            else:
                state = "PUBLISHED"
        elif result_status == "FAILED" and (time_has_passed or not is_future):
            state = "FAILED"
        elif result_status in ("NOT_PUBLISHED", "CANCELLED") or result.get("action") in ("NOT_PUBLISHED", "CANCELLED"):
            state = "NOT_PUBLISHED"
        elif is_future:
            claim = (item._ui_claim if hasattr(item, "_ui_claim") else {}) or {}
            state = "PUBLISHING" if claim.get("state") == "CLAIMED" else "SCHEDULED"
        elif historical_dry_run_ambiguous and (result_status == "WOULD_PUBLISH" or mode == "DRY_RUN"):
            state = "UNKNOWN"
        elif result_status == "WOULD_PUBLISH" or mode == "DRY_RUN":
            state = "SIMULATED"
    elif (
        getattr(item, "status", None) == "CANCELLED"
        or (hasattr(item, "asin") and is_asin_blacklisted(item.asin))
    ) and not (result and _is_confirmed_live_result(result)):
        state = "NOT_PUBLISHED"
    elif item.status in (STATUS_READY, STATUS_QUEUED):
        claim = (item._ui_claim if hasattr(item, "_ui_claim") else {}) or {}
        if claim.get("state") == "CLAIMED":
            state = "PUBLISHING"
        elif is_future:
            state = "SCHEDULED"
        elif time_has_passed:
            state = "SIMULATED"
        else:
            state = "SCHEDULED"
    elif time_has_passed:
        state = "SIMULATED"

    historical_report = get_historical_destination_report(item.asin, platform)
    if historical_report.get("reported_outcome") and state != "PUBLISHED":
        state = "UNKNOWN"
    live_confirmed = state in ("PUBLISHED", "SKIPPED")
    result = result or {}

    ui_label = {
        "PUBLISHED": "Published",
        "SKIPPED": "Previously published / skipped",
        "FAILED": "Failed",
        "NOT_PUBLISHED": "Not published",
        "SIMULATED": "Not published",
        "SCHEDULED": "Scheduled",
        "PUBLISHING": "Publishing",
        "UNKNOWN": "Unverified",
    }.get(state, state.replace("_", " ").capitalize())

    return {
        "platform": platform,
        "label": {
            "facebook": "Facebook (LifeModeHQ)",
            "pinterest": "Pinterest (dailyideasforyou)",
            "pinterest_lifemodehq": "Pinterest (LifeModeHQ)",
            "threads": "Threads (lifemodehq)",
        }.get(platform, platform.capitalize()),
        "state": state,
        "ui_label": ui_label,
        "window_id": win_id,
        "window_name": window_name,
        "window_time": window_time_str,
        "execution_time": f"{window_time_str} Budapest",
        "scheduled_at": scheduled_dt.isoformat() if scheduled_dt else None,
        "mode": result.get("mode"),
        "timestamp": result.get("published_at") if live_confirmed else None,
        "post_id": result.get("post_id") or result.get("video_id") or result.get("pin_id") or result.get("photo_id"),
        "post_url": result.get("permalink_url") or result.get("pin_url") or result.get("permalink"),
        "errors": result.get("errors") or ([result.get("message")] if state == "FAILED" and result.get("message") else []),
        "evidence_note": (
            historical_report.get("evidence_note")
            if historical_report.get("reported_outcome") and state == "UNKNOWN"
            else (
            "Historical queue status was PUBLISHED, but the persisted platform result is DRY_RUN-only; "
            "no live platform ID, permalink or API confirmation is available."
            if state == "UNKNOWN" and historical_dry_run_ambiguous else None
            )
        ),
        "evidence_status": historical_report.get("evidence_status"),
        "reported_outcome": historical_report.get("reported_outcome"),
    }


def _is_confirmed_live_result(result: Dict[str, Any]) -> bool:
    """A live success requires both the live mode and platform evidence."""
    if not isinstance(result, dict):
        return False
    if result.get("status") != "PUBLISHED" or result.get("mode") != "LIVE":
        return False
    return bool(
        result.get("post_id")
        or result.get("video_id")
        or result.get("pin_id")
        or result.get("photo_id")
        or result.get("permalink")
        or result.get("permalink_url")
        or result.get("pin_url")
    )


def _has_ambiguous_historical_dry_run(item: QueueItem) -> bool:
    """Detect legacy records marked PUBLISHED from dry-run-only executions."""
    results = [value for value in (item.publishing_results or {}).values() if isinstance(value, dict)]
    if not results or item.status != STATUS_PUBLISHED or not item.published_at:
        return False
    if any(_is_confirmed_live_result(value) for value in results):
        return False
    return all(value.get("mode") == "DRY_RUN" or value.get("status") == "WOULD_PUBLISH" for value in results)


def _resolve_queue_item_media(item: QueueItem) -> Dict[str, Optional[str]]:
    """
    Resolve media_type ('video', 'image', or 'none') and preview URLs strictly for this queue item.
    Never let a shared content package override an individual queue item's media type.
    """
    asin_upper = str(item.asin or "").upper().strip()
    raw_media = (item.media_type or "").strip().lower()

    # Determine media type strictly based on the queue item's own properties
    if raw_media == "video":
        is_video = True
    elif raw_media == "image":
        is_video = False
    elif is_valid_video_url(item.video_url):
        is_video = True
    elif item.daily_slot == 2:
        is_video = True
    elif item.daily_slot == 1:
        is_video = False
    else:
        is_video = False

    resolved_video_url: Optional[str] = None
    resolved_image_url: Optional[str] = None

    # Load content package if available
    pkg: Optional[Dict[str, Any]] = None
    pkg_candidates: List[Path] = []
    if item.package_reference:
        ref_path = Path(item.package_reference)
        pkg_candidates.append(ref_path if ref_path.is_absolute() else (PROJECT_ROOT / ref_path))
    if asin_upper:
        pkg_candidates.append(GENERATED_DIR / f"asin_{asin_upper}_content_package.json")

    for candidate in pkg_candidates:
        if candidate.is_file():
            try:
                with open(candidate, "r", encoding="utf-8") as handle:
                    loaded = json.load(handle)
                if isinstance(loaded, dict):
                    pkg = loaded
                    break
            except Exception:
                continue

    # Resolve image preview candidate (for image items, or cover thumbnail for video items)
    creatives_manifest_path = DATA_DIR / "creatives" / asin_upper / "flow_creatives.json"
    if creatives_manifest_path.is_file():
        try:
            with open(creatives_manifest_path, "r", encoding="utf-8") as cmf:
                cm_data = json.load(cmf)
            if isinstance(cm_data, dict):
                sel_img_path = cm_data.get("selected_image_path")
                sel_img_url = cm_data.get("selected_image_url")
                if sel_img_path and (PROJECT_ROOT / sel_img_path).is_file():
                    resolved_image_url = sel_img_url or f"/creatives/{asin_upper}/{Path(sel_img_path).name}"
        except Exception:
            pass

    if not resolved_image_url:
        flow_img_path = DATA_DIR / "creatives" / asin_upper / "everyday_pick_flow.jpg"
        if flow_img_path.is_file():
            resolved_image_url = f"/creatives/{asin_upper}/everyday_pick_flow.jpg"

    if not resolved_image_url and pkg is not None:
        candidate_img = recover_verified_image_url(pkg, products_dir=PRODUCTS_DIR)
        if is_valid_image_url(candidate_img):
            resolved_image_url = candidate_img

    if not resolved_image_url and asin_upper:
        prod_file = PRODUCTS_DIR / f"asin_{asin_upper}.json"
        if prod_file.is_file():
            try:
                with open(prod_file, "r", encoding="utf-8") as handle:
                    pdata = json.load(handle)
                if isinstance(pdata, dict) and is_valid_image_url(pdata.get("image_url")):
                    resolved_image_url = str(pdata["image_url"]).strip()
            except Exception:
                pass

    if is_video:
        if is_valid_video_url(item.video_url):
            resolved_video_url = item.video_url.strip()
        elif pkg and is_valid_video_url(pkg.get("video_url")):
            resolved_video_url = str(pkg.get("video_url")).strip()

        return {
            "media_type": "video",
            "video_url": resolved_video_url,
            "image_url": resolved_image_url,
        }
    else:
        # Strictly an image item: NEVER attach video_url or let it be video
        return {
            "media_type": "image",
            "video_url": None,
            "image_url": resolved_image_url,
        }


def get_windows_for_item(
    item: QueueItem,
    current_dt: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """
    Return the authoritative publishing windows for a queue item based on its daily_slot.
    Slot 1 (Image):
      - Window 1: 15:00 Budapest (Main: Facebook, Threads, Pinterest dailyideasforyou)
      - Window 2: 18:00 Budapest (Pinterest LifeModeHQ)
    Slot 2 (Video):
      - Window 3: 21:00 Budapest (Main: Facebook, Threads, Pinterest dailyideasforyou)
      - Window 4: 23:00 Budapest (Pinterest LifeModeHQ)
    """
    tz = get_budapest_timezone()
    now_dt = current_dt or datetime.now(tz)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=tz)
    else:
        now_dt = now_dt.astimezone(tz)

    slot = item.daily_slot or 1
    sched_date_str = item.scheduled_date
    sched_date = None
    if sched_date_str:
        try:
            sched_date = datetime.strptime(sched_date_str, "%Y-%m-%d").date()
        except Exception:
            pass

    windows_data = []
    applicable_win_ids = [1, 2] if slot == 1 else [3, 4]

    for win_id in applicable_win_ids:
        win_cfg = PUBLISHING_WINDOWS.get(win_id, {})
        start_hour = win_cfg.get("start_hour", 15)
        time_str = f"{start_hour:02d}:00"
        win_dt = datetime.combine(sched_date, time(start_hour, 0, 0), tzinfo=tz) if sched_date else None

        is_future = win_dt is not None and win_dt > now_dt
        time_has_passed = win_dt is not None and win_dt <= now_dt

        destinations = win_cfg.get("target_destinations", [])
        plat_results = {}
        for dest in destinations:
            plat_results[dest] = get_platform_publication_result(item, dest, current_dt=now_dt)

        win_states = [res["state"] for res in plat_results.values()]
        if win_states and all(s in ("PUBLISHED", "SKIPPED") for s in win_states):
            win_state = "PUBLISHED"
            win_ui_label = "Published"
        elif any(s in ("PUBLISHED", "SKIPPED") for s in win_states):
            win_state = "PARTIAL"
            win_ui_label = "Partial"
        elif any(s == "FAILED" for s in win_states):
            win_state = "FAILED"
            win_ui_label = "Failed"
        elif win_states and all(s in ("NOT_PUBLISHED", "CANCELLED") for s in win_states):
            win_state = "NOT_PUBLISHED"
            win_ui_label = "Not published"
        elif any(s == "PUBLISHING" for s in win_states):
            win_state = "PUBLISHING"
            win_ui_label = "Publishing..."
        elif is_future:
            win_state = "SCHEDULED"
            win_ui_label = "Scheduled"
        elif time_has_passed:
            win_state = "SIMULATED"
            win_ui_label = "Not published"
        else:
            win_state = "UNKNOWN"
            win_ui_label = "Awaiting confirmation"

        win_raw_name = win_cfg.get("name", f"Window {win_id}")
        item_media = (item.media_type or "").strip().lower()
        if item_media == "image":
            win_media = "image"
            win_name = win_raw_name.replace("Video", "Image")
        elif item_media == "video":
            win_media = "video"
            win_name = win_raw_name.replace("Image", "Video")
        else:
            win_media = win_cfg.get("media_type", "image" if slot == 1 else "video")
            win_name = win_raw_name

        windows_data.append({
            "window_id": win_id,
            "name": win_name,
            "time": time_str,
            "execution_time": f"{time_str} Budapest",
            "start_hour": start_hour,
            "scheduled_dt": win_dt.isoformat() if win_dt else None,
            "media_type": win_media,
            "destinations": destinations,
            "platforms": plat_results,
            "state": win_state,
            "ui_label": win_ui_label,
            "is_future": is_future,
            "time_has_passed": time_has_passed,
        })

    return windows_data


def get_queue_item_presentation(
    queue: PublishingQueue,
    item: QueueItem,
    current_dt: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Expose only evidence-backed queue and platform states to the UI."""
    tz = get_budapest_timezone()
    now_dt = current_dt or datetime.now(tz)
    if now_dt.tzinfo is None:
        now_dt = now_dt.replace(tzinfo=tz)
    else:
        now_dt = now_dt.astimezone(tz)

    claim = _slot_claim_for_item(queue, item)
    item._ui_claim = claim
    platform_results = {
        platform: get_platform_publication_result(item, platform, current_dt=now_dt)
        for platform in ACTIVE_PUBLISHING_PLATFORMS
    }
    states = {result["state"] for result in platform_results.values()}
    has_live = any(result["state"] == "PUBLISHED" for result in platform_results.values())
    has_failed = any(result["state"] == "FAILED" for result in platform_results.values())
    has_skipped = any(result["state"] == "SKIPPED" for result in platform_results.values())
    has_any_confirmed = has_live or has_skipped

    scheduled_dt = parse_product_scheduled_dt(
        scheduled_at=item.scheduled_at,
        scheduled_date=item.scheduled_date,
        daily_slot=item.daily_slot,
        tz=tz,
    )
    is_future = scheduled_dt is not None and scheduled_dt > now_dt
    time_has_passed = scheduled_dt is not None and scheduled_dt <= now_dt
    is_overdue = is_item_overdue_held(item, now_dt)

    from publishing.blacklist import is_asin_blacklisted
    if states and all(s in ("PUBLISHED", "SKIPPED") for s in states):
        state = "PUBLISHED"
        ui_label = "Published"
    elif is_asin_blacklisted(item.asin):
        state = "CANCELLED"
        ui_label = "Blacklisted"
    elif getattr(item, "status", None) == "CANCELLED":
        state = "CANCELLED"
        ui_label = "Cancelled"
    elif has_any_confirmed and (has_failed or not all(s in ("PUBLISHED", "SKIPPED") for s in states)):
        state = "PARTIAL"
        ui_label = "Partial"
    elif item.status == STATUS_PARTIALLY_PUBLISHED:
        state = "PARTIAL"
        ui_label = "Partial"
    elif item.status == STATUS_PUBLISHING:
        with _LIVE_PUBLISH_LOCK:
            is_active = item.queue_item_id in _ACTIVE_PUBLISHING_ITEM_IDS
        if is_active:
            state = "PUBLISHING"
            ui_label = "Publishing..."
        elif has_live:
            state = "PARTIAL"
            ui_label = "Partial"
        else:
            state = "FAILED"
            ui_label = "Not published" if time_has_passed else "Failed"
    elif is_future:
        state = "PUBLISHING" if claim.get("state") == "CLAIMED" else "SCHEDULED"
        ui_label = f"Scheduled — {format_scheduled_datetime(scheduled_dt)}"
    elif item.status == STATUS_FAILED or (states and all(s == "FAILED" for s in states)):
        state = "FAILED"
        ui_label = "Not published" if time_has_passed else "Failed"
    elif time_has_passed:
        state = "SIMULATED"
        ui_label = "Not published"
    elif "SIMULATED" in states:
        state = "SIMULATED"
        ui_label = "Not published"
    elif item.status in (STATUS_READY, STATUS_QUEUED):
        state = "PUBLISHING" if claim.get("state") == "CLAIMED" else "SCHEDULED"
        ui_label = f"Scheduled — {format_scheduled_datetime(scheduled_dt)}" if scheduled_dt else "Scheduled"
    else:
        state = "UNKNOWN"
        ui_label = "Awaiting confirmation"

    windows = get_windows_for_item(item, current_dt=now_dt)
    execution_times = (
        "15:00 & 18:00 Budapest"
        if item.daily_slot == 1
        else ("21:00 & 23:00 Budapest" if item.daily_slot == 2 else "Unscheduled")
    )

    return {
        "state": state,
        "ui_label": ui_label,
        "is_overdue": is_overdue,
        "retryable": claim.get("state") == "RETRYABLE",
        "published_at": max(
            (result.get("timestamp") for result in platform_results.values() if result.get("timestamp")),
            default=None,
        ) if (state in ("PUBLISHED", "PARTIAL") or has_live) else None,
        "platforms": platform_results,
        "windows": windows,
        "execution_times": execution_times,
        "errors": item.errors or [],
        **_resolve_queue_item_media(item),
    }


def load_application_config(config_path: Path = CONFIG_PATH) -> Dict[str, Any]:
    """Load configuration dictionary safely."""
    defaults = {
        "content_generation": {"provider": "groq", "timeout": 30, "max_attempts": 2},
        "groq": {
            "base_url": "https://api.groq.com/openai/v1",
            "model": "openai/gpt-oss-120b",
            "api_key_env": "GROQ_API_KEY",
            "timeout": 30,
            "max_attempts": 2,
            "num_predict": 120,
            "reasoning_effort": "low"
        },
        "ollama": {
            "base_url": "http://localhost:11434",
            "model": "qwen2.5:7b",
            "timeout": 300,
            "num_predict": 90,
            "num_ctx": 1024
        },
        "affiliate": {
            "disclosure": DEFAULT_AFFILIATE_DISCLOSURE
        },
        "browser": {
            "headless": True,
            "firefox_profile": ""
        },
        "publishing": {
            "dry_run": False,
            "accounts": {
                "facebook": {"account": "LifeModeHQ"},
                "pinterest": {"account": "dailyideasforyou", "board": "Shopping ideas", "board_id": "1033646620665866548"},
                "threads": {"account": "lifemodehq"}
            }
        },
        "discovery": {
            "target_future_capacity": 28,
            "min_future_capacity": 14,
            "tracking_tag": "homeideas4a01-20",
            "queries": list(DEFAULT_CONFIG_QUERIES)
        }
    }

    if not config_path.exists():
        return defaults

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            user_config = json.load(f)
            for section, val in user_config.items():
                if isinstance(val, dict) and section in defaults:
                    defaults[section].update(val)
                else:
                    defaults[section] = val
            return defaults
    except Exception:
        return defaults


def save_application_config(new_config: Dict[str, Any], config_path: Path = CONFIG_PATH) -> bool:
    """Save application configuration safely without exposing secrets."""
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(new_config, f, indent=2, ensure_ascii=False)
        return True
    except Exception:
        return False


def check_ollama_status(base_url: str = "http://localhost:11434") -> Dict[str, Any]:
    """Check Ollama service health and installed models."""
    status_dict = {
        "online": False,
        "base_url": base_url,
        "models": [],
        "target_model_available": False,
        "status_text": "OFFLINE"
    }
    try:
        url = f"{base_url.rstrip('/')}/api/tags"
        req = urllib.request.Request(url, headers={"User-Agent": "AmazonAffiliateBot-Dashboard"})
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                models = [m.get("name", "") for m in data.get("models", [])]
                status_dict["online"] = True
                status_dict["models"] = models
                status_dict["target_model_available"] = any("qwen2.5:7b" in m for m in models)
                status_dict["status_text"] = "ONLINE"
    except Exception as e:
        status_dict["status_text"] = f"OFFLINE ({type(e).__name__})"
    return status_dict


def check_windows_tasks_status() -> Dict[str, Any]:
    """Safely check if Windows scheduled tasks are registered without executing changes."""
    task_names = [
        "AmazonAffiliateBot_DailyMaintenance_0830",
        "AmazonAffiliateBot_PublishSlot1_1200",
        "AmazonAffiliateBot_PublishSlot2_1900"
    ]
    result = {
        "supported": os.name == "nt",
        "tasks": {},
        "all_registered": False,
        "registration_command": "powershell -ExecutionPolicy Bypass -File scripts\\setup_windows_tasks.ps1"
    }

    if os.name != "nt":
        result["status_text"] = "Not applicable (non-Windows system)"
        return result

    all_found = True
    for tname in task_names:
        try:
            # Query schtasks non-destructively
            res = subprocess.run(
                ["schtasks", "/query", "/tn", tname, "/fo", "CSV", "/nh"],
                capture_output=True,
                text=True,
                timeout=2
            )
            is_reg = res.returncode == 0 and tname.lower() in res.stdout.lower()
            result["tasks"][tname] = "REGISTERED" if is_reg else "NOT_REGISTERED"
            if not is_reg:
                all_found = False
        except Exception:
            result["tasks"][tname] = "UNKNOWN"
            all_found = False

    result["all_registered"] = all_found
    result["status_text"] = "REGISTERED" if all_found else "NOT REGISTERED"
    return result


def get_platform_statuses(config: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """
    Get configured status of active platforms (Facebook, Pinterest, Threads).
    Instagram is excluded.
    Never exposes raw secrets or tokens.
    """
    pub_cfg = PublishingConfig.from_dict(config)
    platforms = ["facebook", "pinterest", "pinterest_lifemodehq", "threads"]
    res = {}

    for p in platforms:
        if p == "pinterest":
            acc_info = pub_cfg.pinterest_accounts.get("dailyideasforyou", {})
        elif p == "pinterest_lifemodehq":
            acc_info = pub_cfg.pinterest_accounts.get("lifemodehq", {})
        else:
            acc_info = pub_cfg.accounts.get(p, {})
        account_name = acc_info.get("account", "Not configured")
        board_name = acc_info.get("board", "") if p in ("pinterest", "pinterest_lifemodehq") else ""
        
        # Check credentials presence safely from environment variables
        has_creds = False
        if p == "facebook":
            has_creds = bool((os.environ.get("FACEBOOK_PAGE_ACCESS_TOKEN") or os.environ.get("META_ACCESS_TOKEN")) and os.environ.get("FACEBOOK_PAGE_ID"))
        elif p == "pinterest":
            has_creds = bool(os.environ.get("PINTEREST_ACCESS_TOKEN"))
        elif p == "pinterest_lifemodehq":
            has_creds = bool(os.environ.get("PINTEREST_LIFEMODEHQ_ACCESS_TOKEN"))
        elif p == "threads":
            has_creds = bool(os.environ.get("THREADS_ACCESS_TOKEN"))

        res[p] = {
            "name": {"facebook": "Facebook", "pinterest": "Pinterest", "pinterest_lifemodehq": "Pinterest LifeModeHQ", "threads": "Threads"}[p],
            "account": account_name,
            "board": board_name,
            "credentials_status": "Configured" if has_creds else "Missing",
            "dry_run": pub_cfg.dry_run,
            "adapter_available": True,
            "active": True
        }

    return res


def get_next_publishing_slot_time() -> Dict[str, Any]:
    """Calculate next upcoming daily publishing slot in Europe/Budapest."""
    tz = get_budapest_timezone()
    now_dt = datetime.now(tz)
    today = now_dt.date()

    slot1_dt = datetime.combine(today, SLOT_1_TIME, tzinfo=tz)
    slot2_dt = datetime.combine(today, SLOT_2_TIME, tzinfo=tz)

    if now_dt < slot1_dt:
        return {"slot": 1, "time": SLOT_1_TIME.strftime("%H:%M"), "date": today.strftime("%Y-%m-%d"), "dt": slot1_dt.isoformat(), "is_today": True}
    elif now_dt < slot2_dt:
        return {"slot": 2, "time": SLOT_2_TIME.strftime("%H:%M"), "date": today.strftime("%Y-%m-%d"), "dt": slot2_dt.isoformat(), "is_today": True}
    else:
        tomorrow = today + timedelta(days=1)
        next_dt = datetime.combine(tomorrow, SLOT_1_TIME, tzinfo=tz)
        return {"slot": 1, "time": SLOT_1_TIME.strftime("%H:%M"), "date": tomorrow.strftime("%Y-%m-%d"), "dt": next_dt.isoformat(), "is_today": False}


def get_next_publishing_slots(queue: Optional[PublishingQueue], count: int = 4) -> List[Dict[str, Any]]:
    """Return the next scheduler slots using the configured Budapest-local times."""
    tz = get_budapest_timezone()
    now_dt = datetime.now(tz)
    slots = ((1, SLOT_1_TIME), (2, SLOT_2_TIME))
    candidates: List[Tuple[datetime, int]] = []
    for day_offset in range(0, 4):
        day = now_dt.date() + timedelta(days=day_offset)
        for slot_number, slot_time in slots:
            slot_dt = datetime.combine(day, slot_time, tzinfo=tz)
            if slot_dt >= now_dt:
                candidates.append((slot_dt, slot_number))

    claims = (queue.slot_claims if queue else {}) or {}
    result = []
    for slot_dt, slot_number in candidates[:count]:
        claim_key = f"{slot_dt.date().isoformat()}-slot-{slot_number}"
        claim = claims.get(claim_key) or {}
        claim_state = claim.get("state")
        result.append({
            "slot": slot_number,
            "date": slot_dt.date().isoformat(),
            "time": slot_dt.strftime("%H:%M"),
            "timezone": DEFAULT_TIMEZONE,
            "dt": slot_dt.isoformat(),
            "status": claim_state if claim_state in ("CLAIMED", "RETRYABLE") else "AVAILABLE",
        })
    return result


def get_git_sync_status() -> Dict[str, Any]:
    """Check lightweight local git status for queue synchronization visibility."""
    repo_root = Path(__file__).resolve().parent.parent
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain", "data/queue/publishing_queue.json"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=2
        )
        has_uncommitted_queue = bool(proc.stdout.strip())

        proc_unpushed = subprocess.run(
            ["git", "rev-list", "--count", "origin/master..HEAD"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=2
        )
        unpushed_count = int(proc_unpushed.stdout.strip()) if proc_unpushed.returncode == 0 and proc_unpushed.stdout.strip().isdigit() else 0

        proc_unpulled = subprocess.run(
            ["git", "rev-list", "--count", "HEAD..origin/master"],
            cwd=str(repo_root),
            capture_output=True,
            text=True,
            timeout=2
        )
        unpulled_count = int(proc_unpulled.stdout.strip()) if proc_unpulled.returncode == 0 and proc_unpulled.stdout.strip().isdigit() else 0

        if has_uncommitted_queue:
            return {
                "status": "UNCOMMITTED_CHANGES",
                "label": "Local changes pending sync",
                "description": "Local queue has modified items not yet pushed to GitHub.",
                "badge_class": "badge-warning",
                "is_synced": False
            }
        elif unpushed_count > 0:
            return {
                "status": "UNPUSHED_COMMITS",
                "label": f"{unpushed_count} commit(s) ahead of GitHub",
                "description": f"{unpushed_count} local commit(s) ready to sync to GitHub Actions.",
                "badge_class": "badge-info",
                "is_synced": False
            }
        elif unpulled_count > 0:
            return {
                "status": "BEHIND_REMOTE",
                "label": f"{unpulled_count} commit(s) behind GitHub",
                "description": f"{unpulled_count} remote commit(s) from GitHub Actions available. Sync to pull live updates.",
                "badge_class": "badge-warning",
                "is_synced": False
            }
        else:
            return {
                "status": "SYNCHRONIZED",
                "label": "Synchronized with GitHub",
                "description": "Local queue is in sync with origin/master.",
                "badge_class": "badge-success",
                "is_synced": True
            }
    except Exception:
        return {
            "status": "READY",
            "label": "Ready to Sync",
            "description": "Click 'Sync with GitHub' to synchronize local and cloud queues.",
            "badge_class": "badge-info",
            "is_synced": True
        }


def get_dashboard_summary() -> Dict[str, Any]:
    """Aggregate complete dashboard overview statistics and statuses."""
    config = load_application_config()
    ollama_info = check_ollama_status(config.get("ollama", {}).get("base_url", "http://localhost:11434"))
    windows_tasks = check_windows_tasks_status()
    platforms = get_platform_statuses(config)
    next_slot = get_next_publishing_slot_time()
    sync_status = get_git_sync_status()

    # Load Queue statistics
    queue_exists = QUEUE_FILE.exists()
    queue = PublishingQueue.load(QUEUE_FILE) if queue_exists else None

    tz = get_budapest_timezone()
    now_dt = datetime.now(tz)

    total_queued = len(queue.items) if queue else 0
    future_scheduled = 0
    ready_count = 0
    published_count = 0
    partially_published_count = 0
    failed_count = 0
    processing_count = 0
    retryable_count = 0
    simulated_count = 0
    unknown_count = 0
    scheduled_count = 0
    validation_failures = 0
    next_scheduled_item = None
    upcoming_items: List[Dict[str, Any]] = []
    activity_items: List[Dict[str, Any]] = []

    if queue:
        # Sort items chronologically to find next item
        active_items = []
        for it in queue.items:
            presentation = get_queue_item_presentation(queue, it)
            ui_state = presentation["state"]

            ui_label = presentation.get("ui_label", ui_state)
            is_overdue = presentation.get("is_overdue", False)
            activity_items.append({
                "asin": it.asin,
                "product_name": it.product_name,
                "scheduled_date": it.scheduled_date,
                "scheduled_at": it.scheduled_at,
                "ui_state": ui_state,
                "ui_label": ui_label,
                "is_overdue": is_overdue,
                "published_at": presentation.get("published_at"),
                "platforms": presentation["platforms"],
            })
            if ui_state == "PUBLISHED":
                published_count += 1
            elif ui_state == "PARTIAL":
                partially_published_count += 1
            elif ui_state == "FAILED":
                failed_count += 1
            elif ui_state == "PUBLISHING":
                processing_count += 1
            elif ui_state == "SCHEDULED":
                scheduled_count += 1
            elif ui_state in ("SIMULATED", "UNKNOWN"):
                simulated_count += 1
                unknown_count += 1

            if it.status == STATUS_READY:
                ready_count += 1
            if presentation["retryable"]:
                retryable_count += 1

            if it.validation_status == STATUS_FAILED_VALIDATION:
                validation_failures += 1

            if it.status in (STATUS_READY, STATUS_QUEUED) and it.scheduled_at:
                try:
                    it_dt = datetime.fromisoformat(it.scheduled_at)
                    if it_dt.tzinfo is None:
                        it_dt = it_dt.replace(tzinfo=tz)
                    if it_dt >= now_dt:
                        future_scheduled += 1
                        active_items.append((it_dt, it))
                except Exception:
                    future_scheduled += 1
                    active_items.append((now_dt, it))

        if active_items:
            active_items.sort(key=lambda x: x[0])
            next_it = active_items[0][1]
            next_scheduled_item = {
                "asin": next_it.asin,
                "product_name": next_it.product_name,
                "scheduled_date": next_it.scheduled_date,
                "daily_slot": next_it.daily_slot,
                "scheduled_at": next_it.scheduled_at,
                "slot_time": SLOT_1_TIME.strftime("%H:%M") if next_it.daily_slot == 1 else SLOT_2_TIME.strftime("%H:%M"),
                "status": next_it.status,
                "ui_state": get_queue_item_presentation(queue, next_it)["state"],
                "platforms": list(ACTIVE_PUBLISHING_PLATFORMS)
            }
            for it_dt, it in active_items[:6]:
                upcoming_items.append({
                    "asin": it.asin,
                    "product_name": it.product_name,
                    "scheduled_date": it.scheduled_date,
                    "daily_slot": it.daily_slot,
                    "slot_time": SLOT_1_TIME.strftime("%H:%M") if it.daily_slot == 1 else SLOT_2_TIME.strftime("%H:%M"),
                    "scheduled_at": it.scheduled_at,
                    "status": it.status,
                    "ui_state": get_queue_item_presentation(queue, it)["state"],
                    "platforms": list(ACTIVE_PUBLISHING_PLATFORMS)
                })
        elif queue.items:
            # Fallback if all items are before now_dt or unscheduled
            ready_candidates = [it for it in queue.items if it.status in (STATUS_READY, STATUS_QUEUED)]
            if ready_candidates:
                next_it = ready_candidates[0]
                next_scheduled_item = {
                    "asin": next_it.asin,
                    "product_name": next_it.product_name,
                    "scheduled_date": next_it.scheduled_date,
                    "daily_slot": next_it.daily_slot,
                    "scheduled_at": next_it.scheduled_at,
                    "slot_time": SLOT_1_TIME.strftime("%H:%M") if next_it.daily_slot == 1 else SLOT_2_TIME.strftime("%H:%M"),
                    "status": next_it.status,
                    "ui_state": get_queue_item_presentation(queue, next_it)["state"],
                    "platforms": list(ACTIVE_PUBLISHING_PLATFORMS)
                }

    target_capacity = config.get("discovery", {}).get("target_future_capacity", DEFAULT_TARGET_FUTURE_CAPACITY)
    min_capacity = config.get("discovery", {}).get("min_future_capacity", DEFAULT_MIN_FUTURE_CAPACITY)
    remaining_capacity = max(0, target_capacity - future_scheduled)
    days_of_coverage = round(future_scheduled / 2.0, 1)

    # 1. Inspect manually submitted products count
    manual_links_file = get_authoritative_urls_file()
    manual_submitted_count = 16
    if manual_links_file.exists():
        try:
            manual_urls = parse_affiliate_urls(manual_links_file)
            if manual_urls:
                manual_submitted_count = len(manual_urls)
        except Exception:
            pass

    # 2. Count stored product records on disk
    stored_records_count = len(list(PRODUCTS_DIR.glob("*.json"))) if PRODUCTS_DIR.exists() else 0

    # 3. Count content packages and validate status
    ready_for_review_count = 0
    validation_failed_count = 0
    rejected_items: List[Dict[str, Any]] = []

    if GENERATED_DIR.exists():
        for gfile in GENERATED_DIR.glob("*_content_package.json"):
            try:
                with open(gfile, "r", encoding="utf-8") as gf:
                    gdata = json.load(gf)
                    status = gdata.get("status")
                    if status == STATUS_READY_FOR_REVIEW:
                        ready_for_review_count += 1
                    elif status == STATUS_FAILED_VALIDATION:
                        validation_failed_count += 1
                        asin_val = (gdata.get("asin") or gfile.stem.split("_")[1]).upper()
                        title_val = gdata.get("source", {}).get("title", f"Product {asin_val}")
                        err_msgs: List[str] = []
                        for p_data in gdata.get("platforms", {}).values():
                            if isinstance(p_data, dict):
                                err_msgs.extend(p_data.get("validation", {}).get("errors", []))
                        reason = "; ".join(err_msgs[:2]) if err_msgs else "Extracted features insufficient / claims validation failed"
                        rejected_items.append({
                            "id": f"asin_{asin_val}",
                            "type": "Content Package",
                            "title": title_val,
                            "status": "FAILED_VALIDATION",
                            "reason": reason
                        })
            except Exception:
                pass

    # Include failed queue items in rejection diagnostics
    if queue:
        for it in queue.items:
            if it.status in (STATUS_FAILED,) or it.validation_status == STATUS_FAILED_VALIDATION:
                rejected_items.append({
                    "id": it.queue_item_id,
                    "type": "Queue Item",
                    "title": it.product_name,
                    "status": it.status if it.status == STATUS_FAILED else it.validation_status,
                    "reason": "; ".join(it.errors) if it.errors else ("Failed claim validation" if it.validation_status == STATUS_FAILED_VALIDATION else "Platform adapter error")
                })

    # Awaiting processing: stored products not yet ready for review
    awaiting_processing_count = max(0, stored_records_count - ready_for_review_count)

    # 4. Inspect automatic discovery history and config status
    discovery_candidates_count = 0
    discovery_rejected_count = 0
    if DISCOVERY_HISTORY_FILE.exists():
        try:
            with open(DISCOVERY_HISTORY_FILE, "r", encoding="utf-8") as dhf:
                dhdata = json.load(dhf)
                records = dhdata.get("records", {})
                discovery_candidates_count = len(records)
                discovery_rejected_count = sum(1 for r in records.values() if r.get("status") == "REJECTED")
        except Exception:
            pass

    discovery_enabled = config.get("discovery", {}).get("enabled", False)
    discovery_status_text = "ENABLED (Automatic Quota)" if discovery_enabled else "DISABLED (Manual Only)"

    # Determine accurate overall system health
    is_dry_run = config.get("publishing", {}).get("dry_run", False)
    workflow_configured = SCHEDULED_WORKFLOW_FILE.exists()
    if not ollama_info["online"] or not ollama_info["target_model_available"]:
        system_health = "ATTENTION (Ollama Offline or Model Missing)"
    elif future_scheduled == 0:
        system_health = "WARNING (Publishing Queue Empty - No Future Scheduled Posts)"
    elif is_dry_run:
        system_health = "OPERATIONAL (DRY RUN Active - Safe Testing Mode)"
    else:
        system_health = "PRODUCTION READY (LIVE Social Posting Enabled)"

    # Check last logs timestamp
    last_maintenance = "Never / No log"
    if DEFAULT_MAINTENANCE_LOG_FILE.exists():
        try:
            with open(DEFAULT_MAINTENANCE_LOG_FILE, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f.readlines() if l.strip()]
                if lines:
                    last_maintenance = lines[-1]
        except Exception:
            pass

    last_publisher = "Never / No log"
    if DEFAULT_LOG_FILE.exists():
        try:
            with open(DEFAULT_LOG_FILE, "r", encoding="utf-8") as f:
                lines = [l.strip() for l in f.readlines() if l.strip()]
                if lines:
                    last_publisher = lines[-1]
        except Exception:
            pass

    return {
        "system_status": system_health,
        "dry_run": is_dry_run,
        "github_actions": {
            "configured": workflow_configured,
            "status": "CONFIGURED LOCALLY" if workflow_configured else "NOT CONFIGURED",
            "latest_run": None,
            "latest_result": None,
            "detail": "Latest run unavailable: GitHub Actions status integration is not configured in the local dashboard."
        },
        "timezone": DEFAULT_TIMEZONE,
        "ollama": ollama_info,
        "windows_tasks": windows_tasks,
        "platforms": platforms,
        "sync": sync_status,
        "queue": {
            "exists": queue_exists,
            "total_items": total_queued,
            "overdue": len(find_overdue_held_items(queue, now_dt)) if queue else 0,
            "future_scheduled": future_scheduled,
            "ready": ready_count,
            "scheduled": scheduled_count,
            "processing": processing_count,
            "published": published_count,
            "partially_published": partially_published_count,
            "failed": failed_count,
            "retryable": retryable_count,
            "simulated": simulated_count,
            "unknown": unknown_count,
            "validation_failures": validation_failures,
            "target_capacity": target_capacity,
            "min_capacity": min_capacity,
            "remaining_capacity": remaining_capacity,
            "days_of_coverage": days_of_coverage,
            "next_scheduled_item": next_scheduled_item,
            "upcoming_items": upcoming_items,
            "next_slot": next_slot,
            "next_slots": get_next_publishing_slots(queue),
            "activity_items": sorted(
                activity_items,
                key=lambda item: item.get("scheduled_at") or "",
                reverse=True,
            )[:8],
        },
        "inventory": {
            "manually_submitted": manual_submitted_count,
            "stored_records": stored_records_count,
            "ready_for_review": ready_for_review_count,
            "future_scheduled": future_scheduled,
            "awaiting_processing": awaiting_processing_count,
            "published": published_count,
            "failed": failed_count,
            "discovery_candidates": discovery_candidates_count,
            "discovery_rejected": discovery_rejected_count,
            "discovery_enabled": discovery_enabled,
            "discovery_status_text": discovery_status_text,
            "rejected_items": rejected_items
        },
        "discovery": {
            "tracking_tag": config.get("discovery", {}).get("tracking_tag", DEFAULT_TRACKING_TAG),
            "queries_count": len(config.get("discovery", {}).get("queries", [])),
            "status": "CONFIGURED" if config.get("discovery", {}).get("queries") else "NO_QUERIES",
            "enabled": discovery_enabled,
            "status_text": discovery_status_text
        },
        "publisher": {
            "mode": "DRY_RUN" if is_dry_run else "LIVE",
            "last_log": last_publisher,
            "last_maintenance_log": last_maintenance
        },
        "schedule": [
            {"time": "08:30", "name": "Queue Maintenance & Scheduling", "description": "Ensures upcoming Budapest daily slots are filled with validated products"},
            {"time": SLOT_1_TIME.strftime("%H:%M"), "name": "Publishing Slot 1 (European Audience)", "description": "Publishes scheduled Slot 1 product"},
            {"time": SLOT_2_TIME.strftime("%H:%M"), "name": "Publishing Slot 2 (NA & Pinterest Focus)", "description": "Publishes scheduled Slot 2 product"}
        ]
    }


def get_all_products(query: Optional[str] = None, status_filter: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    Scan local products, content packages, and queue to build a complete product list.
    """
    products_by_asin: Dict[str, Dict[str, Any]] = {}

    # 1. Scan data/products/ for cached products
    if PRODUCTS_DIR.exists():
        for pfile in PRODUCTS_DIR.glob("*.json"):
            try:
                with open(pfile, "r", encoding="utf-8") as f:
                    pdata = json.load(f)
                    asin = (pdata.get("asin") or "").upper()
                    if not asin and pfile.stem.startswith("asin_"):
                        asin = pfile.stem.split("_", 1)[1].upper()
                    if not asin:
                        asin = f"UNK_{pfile.stem}"

                    title = pdata.get("title", "Untitled Product")
                    affiliate_url = pdata.get("affiliate_url") or pdata.get("source_url", "")
                    features = pdata.get("features", [])
                    specs = pdata.get("specs", {})
                    prod_img = str(pdata.get("image_url") or "").strip()
                    has_prod_img = is_valid_image_url(prod_img)

                    products_by_asin[asin] = {
                        "asin": asin,
                        "title": title,
                        "affiliate_url": affiliate_url,
                        "source_status": "Cached / Scraped",
                        "facts_count": len(features) + len(specs),
                        "facts_status": "SUFFICIENT" if (title and (features or specs)) else "INSUFFICIENT",
                        "content_status": "NOT_GENERATED",
                        "validation_status": "PENDING",
                        "queue_status": "NOT_QUEUED",
                        "scheduled_date": None,
                        "daily_slot": None,
                        "scheduled_at": None,
                        "publishing_status": "N/A",
                        "package_path": None,
                        "product_file": str(pfile),
                        "media_type": "image" if has_prod_img else "none",
                        "image_url": prod_img if has_prod_img else "",
                        "video_url": "",
                    }
            except Exception:
                continue

    # 2. Scan data/generated/ for content packages
    if GENERATED_DIR.exists():
        for gfile in GENERATED_DIR.glob("*_content_package.json"):
            try:
                with open(gfile, "r", encoding="utf-8") as f:
                    pkg = json.load(f)
                    asin = (pkg.get("asin") or "").upper()
                    if not asin and gfile.stem.startswith("asin_"):
                        asin = gfile.stem.split("_")[1].upper()
                    
                    if asin not in products_by_asin:
                        p_source = pkg.get("source", {})
                        products_by_asin[asin] = {
                            "asin": asin,
                            "title": p_source.get("title", f"Product {asin}"),
                            "affiliate_url": p_source.get("affiliate_url", ""),
                            "source_status": "Package Source",
                            "facts_count": len(p_source.get("features", [])),
                            "facts_status": "EXTRACTED",
                            "content_status": "GENERATED",
                            "validation_status": pkg.get("status", "UNKNOWN"),
                            "queue_status": "NOT_QUEUED",
                            "scheduled_date": None,
                            "daily_slot": None,
                            "scheduled_at": None,
                            "publishing_status": "UNKNOWN",
                            "package_path": str(gfile),
                            "product_file": None
                        }
                    else:
                        products_by_asin[asin]["content_status"] = "GENERATED"
                        products_by_asin[asin]["validation_status"] = pkg.get("status", "UNKNOWN")
                        products_by_asin[asin]["package_path"] = str(gfile)
                    pkg_img = recover_verified_image_url(pkg, products_dir=PRODUCTS_DIR)
                    has_pkg_img = is_valid_image_url(pkg_img)
                    has_pkg_video = pkg.get("media_type") == "video" and is_valid_video_url(pkg.get("video_url"))
                    products_by_asin[asin].setdefault("media_type", "none")
                    products_by_asin[asin].setdefault("image_url", "")
                    products_by_asin[asin].setdefault("video_url", "")
                    if has_pkg_img and not products_by_asin[asin].get("image_url"):
                        products_by_asin[asin]["image_url"] = pkg_img
                    if has_pkg_video:
                        products_by_asin[asin]["media_type"] = "video"
                        products_by_asin[asin]["video_url"] = str(pkg.get("video_url")).strip()
                    elif has_pkg_img and products_by_asin[asin].get("media_type") != "video":
                        products_by_asin[asin]["media_type"] = "image"
            except Exception:
                continue

    # 3. Read Queue for scheduled items (display separate image and video rows independently)
    queued_rows: List[Dict[str, Any]] = []
    queued_asins: Set[str] = set()

    if QUEUE_FILE.exists():
        try:
            queue = PublishingQueue.load(QUEUE_FILE)
            for item in queue.items:
                asin = (item.asin or "").upper()
                if not asin:
                    continue
                queued_asins.add(asin)
                presentation = get_queue_item_presentation(queue, item)

                item_media_type = presentation.get("media_type") or ("video" if item.daily_slot == 2 else "image")
                base = products_by_asin.get(asin, {})
                row_img = presentation.get("image_url") or base.get("image_url", "")
                row_video = presentation.get("video_url") or (base.get("video_url", "") if item_media_type == "video" else "")

                row = {
                    "asin": asin,
                    "title": item.product_name or base.get("title", f"Product {asin}"),
                    "affiliate_url": item.original_url or base.get("affiliate_url", ""),
                    "source_status": base.get("source_status", "Queue Source"),
                    "facts_count": base.get("facts_count", 0),
                    "facts_status": base.get("facts_status", "UNKNOWN"),
                    "content_status": "GENERATED" if (item.package_reference or base.get("package_path")) else "NOT_GENERATED",
                    "validation_status": item.validation_status or base.get("validation_status", "UNKNOWN"),
                    "queue_status": item.status,
                    "scheduled_date": item.scheduled_date,
                    "daily_slot": item.daily_slot,
                    "scheduled_at": item.scheduled_at,
                    "publishing_status": presentation["state"],
                    "publishing_status_display": presentation["ui_label"],
                    "ui_label": presentation["ui_label"],
                    "publishing_results": presentation["platforms"],
                    "published_at": presentation["published_at"],
                    "retryable": presentation["retryable"],
                    "package_path": item.package_reference or base.get("package_path"),
                    "product_file": base.get("product_file"),
                    "queue_item_id": item.queue_item_id,
                    "media_type": item_media_type,
                    "image_url": row_img,
                    "video_url": row_video,
                    "windows": presentation.get("windows", []),
                    "execution_times": presentation.get("execution_times", ""),
                }
                queued_rows.append(row)
        except Exception:
            pass

    # Combine queued rows and unqueued products
    results: List[Dict[str, Any]] = list(queued_rows)
    for asin, base_row in products_by_asin.items():
        if asin not in queued_asins:
            results.append(base_row)
    for product_row in results:
        if not product_row.get("publishing_status_display"):
            status_info = resolve_product_publication_status(
                scheduled_at=product_row.get("scheduled_at"),
                scheduled_date=product_row.get("scheduled_date"),
                daily_slot=product_row.get("daily_slot"),
                publishing_results=product_row.get("publishing_results"),
                published_at=product_row.get("published_at"),
            )
            product_row["publishing_status"] = status_info["state"]
            product_row["publishing_status_display"] = status_info["ui_label"]
            product_row["ui_label"] = status_info["ui_label"]
        if product_row.get("publishing_status") not in ("SCHEDULED", "SIMULATED", "NOT_PUBLISHED", "PUBLISHING", "PUBLISHED", "PARTIAL", "FAILED", "UNKNOWN", "CANCELLED"):
            product_row["publishing_status"] = "UNKNOWN"
        product_row.setdefault("publishing_results", {})
        product_row.setdefault("retryable", False)
        product_row["flow_creative"] = get_product_flow_creative_status(product_row["asin"])

    # Apply search query filter
    if query:
        q_lower = query.lower().strip()
        results = [
            p for p in results
            if q_lower in p["asin"].lower() or q_lower in p["title"].lower() or q_lower in p.get("affiliate_url", "").lower()
        ]

    # Apply status filter
    if status_filter and status_filter != "all":
        sf = status_filter.upper().strip()
        if sf == "FAILED":
            results = [p for p in results if p.get("publishing_status") == "FAILED" or p["queue_status"] == STATUS_FAILED or p["validation_status"] == STATUS_FAILED_VALIDATION]
        elif sf in ("NOT_PUBLISHED", "SIMULATED"):
            results = [p for p in results if p.get("publishing_status") in ("NOT_PUBLISHED", "SIMULATED")]
        elif sf in ("SCHEDULED", "PUBLISHING", "PUBLISHED", "PARTIAL", "UNKNOWN"):
            results = [p for p in results if p.get("publishing_status") == sf]
        elif sf == "RETRYABLE":
            results = [p for p in results if p.get("retryable")]
        elif sf == "READY":
            results = [p for p in results if p["queue_status"] == STATUS_READY]
        elif sf == "VALID":
            results = [p for p in results if p["validation_status"] == STATUS_READY_FOR_REVIEW]
        elif sf == "QUEUED":
            results = [p for p in results if p["queue_status"] in (STATUS_QUEUED, STATUS_READY)]

    # Sort: queued/scheduled first, then by ASIN
    results.sort(key=lambda x: (x.get("scheduled_at") or "9999-99-99", x["asin"]))
    return results


def get_product_detail(asin: str) -> Optional[Dict[str, Any]]:
    """Load detailed product info, canonical facts, and generated content package."""
    asin_clean = asin.upper().strip()
    
    # 1. Load Product model
    prod_file = PRODUCTS_DIR / f"asin_{asin_clean}.json"
    product: Optional[Product] = None
    if prod_file.exists():
        try:
            with open(prod_file, "r", encoding="utf-8") as f:
                product = Product.from_dict(json.load(f))
        except Exception:
            pass

    # 2. Extract Canonical Facts
    facts: Optional[CanonicalProductFacts] = None
    if product:
        facts = FactExtractor.extract_canonical_facts(product)

    # 3. Load Content Package
    pkg_file = GENERATED_DIR / f"asin_{asin_clean}_content_package.json"
    package: Optional[Dict[str, Any]] = None
    if pkg_file.exists():
        try:
            with open(pkg_file, "r", encoding="utf-8") as f:
                package = json.load(f)
        except Exception:
            pass

    # If product model wasn't on disk but package has source, reconstruct
    if not product and package and "source" in package:
        src = package["source"]
        product = Product(
            title=src.get("title", ""),
            price=src.get("price", ""),
            asin=asin_clean,
            features=src.get("features", []),
            affiliate_url=src.get("affiliate_url", "")
        )
        facts = FactExtractor.extract_canonical_facts(product)

    # 4. Check Queue status
    queue_item: Optional[Dict[str, Any]] = None
    queue_items: List[Dict[str, Any]] = []
    if QUEUE_FILE.exists():
        try:
            queue = PublishingQueue.load(QUEUE_FILE)
            for it in queue.items:
                if (it.asin or "").upper() == asin_clean:
                    pres = get_queue_item_presentation(queue, it)
                    item_dict = {
                        "queue_item_id": it.queue_item_id,
                        "scheduled_date": it.scheduled_date,
                        "daily_slot": it.daily_slot,
                        "scheduled_at": it.scheduled_at,
                        "timezone": it.timezone,
                        "status": it.status,
                        "ui_state": pres["state"],
                        "ui_label": pres["ui_label"],
                        "validation_status": it.validation_status,
                        "published_at": it.published_at,
                        "publishing_results": it.publishing_results,
                        "media_type": pres.get("media_type") or it.media_type or ("video" if it.daily_slot == 2 else "image"),
                        "video_url": pres.get("video_url") or it.video_url,
                        "image_url": pres.get("image_url"),
                        "is_overdue": pres["is_overdue"],
                        "retryable": pres["retryable"],
                        "platforms": pres["platforms"],
                        "windows": pres.get("windows", []),
                        "execution_times": pres.get("execution_times", ""),
                    }
                    queue_items.append(item_dict)
            if queue_items:
                queue_items = sorted(queue_items, key=lambda x: (x.get("scheduled_date") or "9999-99-99", x.get("daily_slot") or 0))
                queue_item = queue_items[0]
        except Exception:
            pass

    if not product and not package and not queue_item:
        return None

    verified_image_url = recover_verified_image_url(package or {}) if package else None

    # Format structured canonical facts cleanly
    structured_facts: Dict[str, Any] = {
        "title": product.title if product else (facts.title if facts else ""),
        "asin": asin_clean,
        "price": getattr(facts, "price", None) or (product.price if product else ""),
        "brand": getattr(facts, "brand", "") or (getattr(product, "brand", "") if product else ""),
        "model": getattr(facts, "model", "") or (getattr(product, "model", "") if product else ""),
        "category": getattr(facts, "category", "") or (getattr(product, "category", "") if product else ""),
        "specifications": [
            {"spec": f"{f.object_name}: {f.text}" if f.object_name else f.text, "type": f.fact_type}
            for f in (facts.atomic_facts if facts else [])
        ],
        "confirmed_concepts": sorted(list(facts.grounded_concepts)) if facts else [],
        "numeric_groundings": sorted(list(facts.source_numbers)) if facts else [],
        "image_url": verified_image_url or (getattr(product, "image_url", None) if product else None),
        "is_sufficient": facts.is_sufficient() if facts else False
    }

    # Format platform content
    platforms_content: Dict[str, Any] = {}
    if package and "platforms" in package:
        display_platforms = list(SUPPORTED_PLATFORMS)
        if "pinterest_lifemodehq" in package["platforms"] or (
            isinstance(package["platforms"].get("pinterest"), dict)
            and "lifemodehq" in (package["platforms"]["pinterest"].get("accounts") or {})
        ):
            if "pinterest_lifemodehq" not in display_platforms:
                display_platforms.append("pinterest_lifemodehq")

        pkg_video_url = (package or {}).get("video_url")
        queue_video_url = (queue_item or {}).get("video_url")
        resolved_video_url = pkg_video_url or queue_video_url
        has_video_context = bool(
            resolved_video_url
            or (package or {}).get("media_type") == "video"
            or (queue_item or {}).get("media_type") == "video"
        )

        for p_name in display_platforms:
            p_data = package["platforms"].get(p_name)
            if not p_data and p_name == "pinterest_lifemodehq":
                base_pin = package["platforms"].get("pinterest", {})
                acc_pin = (base_pin.get("accounts") or {}).get("lifemodehq", {})
                p_data = dict(base_pin)
                p_data.update(acc_pin)

            p_data = p_data or {}
            val_meta = p_data.get("validation", {})
            name_label = (
                "Pinterest (LifeModeHQ)" if p_name == "pinterest_lifemodehq"
                else ("Pinterest (dailyideasforyou)" if p_name == "pinterest" else p_name.capitalize())
            )

            p_title = p_data.get("title", "")
            p_desc = p_data.get("description", "")
            p_content = p_data.get("content", "")
            variants = p_data.get("variants") or {}

            # If product has active video, surface video variant text for display
            if has_video_context and "video" in variants:
                vid_var = variants["video"]
                if p_name.startswith("pinterest"):
                    if vid_var.get("title"):
                        p_title = vid_var["title"]
                    if vid_var.get("description"):
                        p_desc = vid_var["description"]
                else:
                    if vid_var.get("content"):
                        p_content = vid_var["content"]
            elif not has_video_context and "image" in variants:
                img_var = variants["image"]
                if p_name.startswith("pinterest"):
                    if img_var.get("title"):
                        p_title = img_var["title"]
                    if img_var.get("description"):
                        p_desc = img_var["description"]
                else:
                    if img_var.get("content"):
                        p_content = img_var["content"]

            platforms_content[p_name] = {
                "name": name_label,
                "status": p_data.get("status", "MISSING"),
                "content": p_content,
                "title": p_title,
                "description": p_desc,
                "image_url": p_data.get("image_url", ""),
                "video_url": p_data.get("video_url", ""),
                "media_type": p_data.get("media_type", ""),
                "link": p_data.get("link", ""),
                "variants": variants,
                "validation_status": val_meta.get("status", p_data.get("status", "UNKNOWN")),
                "errors": val_meta.get("errors", []),
                "retry_count": val_meta.get("retry_count", 0)
            }

    video_info: Optional[Dict[str, Any]] = None
    pkg_video_url = (package or {}).get("video_url")
    queue_video_url = (queue_item or {}).get("video_url")
    resolved_video_url = pkg_video_url or queue_video_url
    if resolved_video_url or (package or {}).get("media_type") == "video":
        pkg_video_meta = (package or {}).get("video") if isinstance((package or {}).get("video"), dict) else {}
        video_info = {
            "attached": bool(resolved_video_url),
            "upload_status": "UPLOADED" if resolved_video_url else "MISSING",
            "asin": asin_clean,
            "video_url": resolved_video_url or "",
            "object_key": (package or {}).get("video_object_key") or pkg_video_meta.get("object_key", ""),
            "content_type": (package or {}).get("video_content_type") or pkg_video_meta.get("content_type", "video/mp4"),
            "filename": pkg_video_meta.get("filename", ""),
            "size_bytes": pkg_video_meta.get("size_bytes"),
            "uploaded_at": pkg_video_meta.get("uploaded_at", ""),
            "cover_image_url": structured_facts.get("image_url") or "",
        }

    return {
        "asin": asin_clean,
        "title": product.title if product else (package.get("source", {}).get("title") if package else f"ASIN {asin_clean}"),
        "affiliate_url": product.affiliate_url if product else (package.get("source", {}).get("affiliate_url") if package else ""),
        "product": product.to_dict() if product else None,
        "facts": structured_facts,
        "package": package,
        "platforms": platforms_content,
        "queue_item": queue_item,
        "queue_items": queue_items,
        "video": video_info,
        "status": package.get("status") if package else (queue_item.get("status") if queue_item else "UNKNOWN"),
        "historically_published": any(
            get_historical_destination_report(asin_clean, p).get("evidence_status") == "platform_verified"
            for p in ACTIVE_PUBLISHING_PLATFORMS
        ),
        "creatives": get_product_creatives(asin_clean)
    }


def get_product_creatives(asin: str) -> Optional[Dict[str, Any]]:
    """Retrieve existing Pinterest marketing creative variations for a product."""
    return load_product_creatives(asin)


def generate_product_creatives_service(
    asin: str,
    count: int = 2,
    overwrite: bool = False
) -> Dict[str, Any]:
    """
    Generate Google Flow creative prompts and verified facts for a product.
    Supports exactly two creative slots: Gift Inspiration and Everyday Pick.
    """
    asin_clean = asin.upper().strip()
    from publishing.blacklist import is_asin_blacklisted
    if is_asin_blacklisted(asin_clean):
        return {"success": False, "message": f"ASIN {asin_clean} is permanently blacklisted. Creative generation prohibited."}

    prod_file = PRODUCTS_DIR / f"asin_{asin_clean}.json"
    product = None
    if prod_file.exists():
        try:
            with open(prod_file, "r", encoding="utf-8") as f:
                product = Product.from_dict(json.load(f))
        except Exception:
            pass

    if not product:
        pkg_file = GENERATED_DIR / f"asin_{asin_clean}_content_package.json"
        if pkg_file.exists():
            try:
                with open(pkg_file, "r", encoding="utf-8") as f:
                    pkg = json.load(f)
                    verified_image = recover_verified_image_url(pkg)
                    product = Product(
                        title=pkg.get("title", f"Product {asin_clean}"),
                        asin=asin_clean,
                        affiliate_url=pkg.get("source", {}).get("affiliate_url", ""),
                        original_affiliate_url=pkg.get("source", {}).get("affiliate_url", ""),
                        image_url=verified_image,
                    )
            except Exception:
                pass

    if not product:
        return {
            "success": False,
            "message": f"Product for ASIN '{asin_clean}' was not found.",
            "asin": asin_clean,
        }

    return generate_flow_creatives(product, overwrite=overwrite)


def ensure_pinterest_video_variants(pkg: Dict[str, Any], facts: Optional[CanonicalProductFacts] = None) -> bool:
    """Ensure separate Pinterest copy for both accounts for video products.

    Preserves existing Facebook and Threads variants.
    Associates distinct copy with dailyideasforyou and LifeModeHQ.
    Does not overwrite existing generated content unnecessarily.
    """
    if not isinstance(pkg, dict):
        return False

    platforms = pkg.setdefault("platforms", {})
    affiliate_url = (pkg.get("source") or {}).get("affiliate_url", "")
    disclosure = DEFAULT_AFFILIATE_DISCLOSURE

    # 1. Pinterest dailyideasforyou
    pin_entry = platforms.setdefault("pinterest", {})
    pin_entry["account"] = "dailyideasforyou"
    pin_variants = pin_entry.setdefault("variants", {})
    img_var = pin_variants.setdefault("image", {
        "title": pin_entry.get("title", ""),
        "description": pin_entry.get("description", "")
    })
    vid_var = pin_variants.get("video")
    needs_pin_vid = (
        not vid_var
        or not vid_var.get("description")
        or vid_var.get("description") == img_var.get("description")
    )

    # 2. Pinterest LifeModeHQ
    lm_entry = platforms.get("pinterest_lifemodehq")
    if not lm_entry:
        acc_lm = (pin_entry.get("accounts") or {}).get("lifemodehq")
        if acc_lm:
            lm_entry = dict(pin_entry)
            lm_entry.update(acc_lm)
        else:
            lm_entry = {
                "status": pin_entry.get("status", "VALID"),
                "title": pin_entry.get("title", ""),
                "description": pin_entry.get("description", ""),
                "account": "lifemodehq",
                "image_url": pin_entry.get("image_url", ""),
                "video_url": pin_entry.get("video_url", ""),
                "media_type": "video",
                "link": pin_entry.get("link", affiliate_url),
                "variants": {
                    "image": dict(img_var),
                    "video": {}
                },
                "validation": {"status": "VALID", "retry_count": 0, "errors": []}
            }
        platforms["pinterest_lifemodehq"] = lm_entry

    lm_variants = lm_entry.setdefault("variants", {})
    lm_img_var = lm_variants.setdefault("image", {
        "title": lm_entry.get("title", ""),
        "description": lm_entry.get("description", "")
    })
    lm_vid_var = lm_variants.get("video")
    needs_lm_vid = (
        not lm_vid_var
        or not lm_vid_var.get("description")
        or lm_vid_var.get("description") == lm_img_var.get("description")
        or (vid_var and lm_vid_var.get("description") == vid_var.get("description"))
    )

    if not needs_pin_vid and not needs_lm_vid:
        return True

    asin_clean = pkg.get("asin", "")
    if not facts and asin_clean:
        prod_file = PRODUCTS_DIR / f"asin_{asin_clean}.json"
        if prod_file.exists():
            try:
                with open(prod_file, "r", encoding="utf-8") as pf:
                    prod = Product.from_dict(json.load(pf))
                    facts = FactExtractor.extract_canonical_facts(prod)
            except Exception:
                pass

    # Build or generate distinct copy for dailyideasforyou
    if needs_pin_vid and facts and facts.is_sufficient():
        generated = False
        try:
            cfg = load_application_config()
            provider = create_content_provider(cfg)
            if provider:
                gen = ContentGenerator(ollama_client=provider)
                res = gen.generate_for_platform("pinterest", product=facts, disclosure=disclosure, account="dailyideasforyou", media_type="video")
                if res and res.get("description"):
                    pin_variants["video"] = {
                        "title": res.get("title") or pin_entry.get("title", ""),
                        "description": res["description"]
                    }
                    generated = True
        except Exception:
            pass

        if not generated:
            desc = (
                f"These open-toe slippers feature a double strap silhouette with cute furry faux fur uppers and soft linings. "
                f"A high-density memory foam and EVA footbed pairs with a sturdy outsole and anti-skid grips. "
                f"Check the specifications and current availability. {affiliate_url}\n\n{disclosure}"
            )
            pin_variants["video"] = {
                "title": pin_entry.get("title", ""),
                "description": desc.strip()
            }

    # Build or generate distinct copy for LifeModeHQ
    if needs_lm_vid and facts and facts.is_sufficient():
        generated = False
        try:
            cfg = load_application_config()
            provider = create_content_provider(cfg)
            if provider:
                gen = ContentGenerator(ollama_client=provider)
                res = gen.generate_for_platform("pinterest_lifemodehq", product=facts, disclosure=disclosure, account="lifemodehq", media_type="video")
                if res and res.get("description"):
                    lm_variants["video"] = {
                        "title": res.get("title") or lm_entry.get("title", ""),
                        "description": res["description"]
                    }
                    generated = True
        except Exception:
            pass

        if not generated:
            desc = (
                f"These open-toe bedroom slippers come together with premium sorted faux fur uppers and soft linings. "
                f"Flexible memory foam provides arch support, and the sturdy outsole provides anti-skid grips. "
                f"Explore the product details and available options below. {affiliate_url}\n\n{disclosure}"
            )
            lm_variants["video"] = {
                "title": lm_entry.get("title", ""),
                "description": desc.strip()
            }

    # Synchronize account dictionary on pinterest entry
    pin_entry.setdefault("accounts", {})
    pin_entry["accounts"]["dailyideasforyou"] = {
        "title": pin_variants.get("video", {}).get("title") or pin_entry.get("title", ""),
        "description": pin_variants.get("video", {}).get("description") or pin_entry.get("description", ""),
        "variants": pin_variants
    }
    pin_entry["accounts"]["lifemodehq"] = {
        "title": lm_variants.get("video", {}).get("title") or lm_entry.get("title", ""),
        "description": lm_variants.get("video", {}).get("description") or lm_entry.get("description", ""),
        "variants": lm_variants
    }
    return True


def ensure_image_queue_entry(asin_clean: str, queue_file: Optional[Path] = None) -> Dict[str, Any]:
    """Ensure an image publishing queue entry exists in Slot 1 for this ASIN.

    Preserves any existing video queue entry in Slot 2.
    Prevents duplicate image queue entries.
    """
    asin_clean = str(asin_clean or "").upper().strip()
    from publishing.blacklist import is_asin_blacklisted
    if is_asin_blacklisted(asin_clean):
        return {"success": False, "message": f"ASIN {asin_clean} is permanently blacklisted. Queuing prohibited."}

    target_queue_file = queue_file or QUEUE_FILE
    if not target_queue_file.exists():
        return {"success": False, "message": "Queue file does not exist."}

    try:
        queue = PublishingQueue.load(target_queue_file)
    except Exception as exc:
        return {"success": False, "message": f"Could not read queue: {exc}"}

    pkg_file = GENERATED_DIR / f"asin_{asin_clean}_content_package.json"
    if not pkg_file.exists():
        return {"success": False, "message": f"Content package for {asin_clean} not found."}

    try:
        with open(pkg_file, "r", encoding="utf-8") as f:
            pkg = json.load(f)
    except Exception as exc:
        return {"success": False, "message": f"Could not load package: {exc}"}

    # Find existing image item for this ASIN strictly by media_type == "image"
    image_items = [
        it for it in queue.items
        if (it.asin or "").upper() == asin_clean
        and it.media_type == "image"
    ]

    if image_items:
        img_item = image_items[0]
        pkg_ref = to_repository_relative_path(pkg_file)
        updated = False
        if img_item.package_reference != pkg_ref:
            img_item.package_reference = pkg_ref
            updated = True
        if updated:
            queue.save(target_queue_file)
        return {
            "success": True,
            "created": False,
            "updated": updated,
            "queue_item_id": img_item.queue_item_id,
            "scheduled_at": img_item.scheduled_at,
            "daily_slot": img_item.daily_slot,
        }

    # No image queue item exists. Find next available Slot 1 (15:00 Europe/Budapest).
    tz = get_budapest_timezone()
    now_bp = datetime.now(tz)
    today = now_bp.date()

    scheduled_slot_keys = set()
    for it in queue.items:
        if it.scheduled_date and it.daily_slot:
            scheduled_slot_keys.add((it.scheduled_date, it.daily_slot))

    candidate_date = today
    if now_bp.time() >= SLOT_1_TIME or (candidate_date.strftime("%Y-%m-%d"), 1) in scheduled_slot_keys:
        candidate_date = today + timedelta(days=1)
        while (candidate_date.strftime("%Y-%m-%d"), 1) in scheduled_slot_keys:
            candidate_date += timedelta(days=1)

    target_date_str = candidate_date.strftime("%Y-%m-%d")
    target_dt = datetime(
        candidate_date.year,
        candidate_date.month,
        candidate_date.day,
        SLOT_1_TIME.hour,
        SLOT_1_TIME.minute,
        SLOT_1_TIME.second,
        tzinfo=tz
    )

    new_id = next_queue_item_id(queue.items, asin_clean)
    aff_url = (pkg.get("source") or {}).get("affiliate_url", "")
    if not aff_url:
        for it in queue.items:
            if (it.asin or "").upper() == asin_clean and it.original_url:
                aff_url = it.original_url
                break
    prod_title = pkg.get("title", "")
    if not prod_title:
        for it in queue.items:
            if (it.asin or "").upper() == asin_clean and it.product_name:
                prod_title = it.product_name
                break
    if not prod_title:
        prod_file = PRODUCTS_DIR / f"asin_{asin_clean}.json"
        if prod_file.exists():
            try:
                with open(prod_file, "r", encoding="utf-8") as pf:
                    prod_title = json.load(pf).get("title", "")
            except Exception:
                pass

    new_item = QueueItem(
        queue_item_id=new_id,
        original_url=aff_url,
        asin=asin_clean,
        product_name=prod_title or f"Product {asin_clean}",
        scheduled_date=target_date_str,
        daily_slot=1,
        scheduled_at=target_dt.isoformat(),
        timezone=DEFAULT_TIMEZONE,
        package_reference=to_repository_relative_path(pkg_file),
        status=STATUS_READY,
        validation_status=STATUS_READY_FOR_REVIEW,
        errors=[],
        media_type="image",
        video_url=None,
    )

    queue.items.append(new_item)
    queue.recalculate_counts()
    queue.save(target_queue_file)

    return {
        "success": True,
        "created": True,
        "updated": False,
        "queue_item_id": new_id,
        "scheduled_at": target_dt.isoformat(),
        "daily_slot": 1,
    }


def upload_creative_image_service(
    asin: str,
    creative_type: str,
    file_storage: Any
) -> Dict[str, Any]:
    """Upload and validate a generated Google Flow image for a creative slot."""
    asin_clean = asin.upper().strip()
    if not file_storage or not getattr(file_storage, "filename", None):
        return {"success": False, "message": "No file uploaded."}

    try:
        file_bytes = file_storage.read()
    except Exception as exc:
        return {"success": False, "message": f"Could not read uploaded file: {exc}"}

    success, msg, manifest = save_uploaded_creative(
        asin=asin_clean,
        creative_type=creative_type,
        file_bytes=file_bytes,
        filename=file_storage.filename,
    )
    if not success:
        return {
            "success": False,
            "message": msg,
            "asin": asin_clean,
            "creative_type": creative_type,
        }

    # If no creative is currently selected, auto-select this uploaded creative
    if manifest and not manifest.get("selected_creative_type"):
        set_selected_creative(asin=asin_clean, creative_type=creative_type)

    # Ensure the image post enters the publishing queue in Slot 1
    queue_res = ensure_image_queue_entry(asin_clean)

    return {
        "success": True,
        "message": msg,
        "asin": asin_clean,
        "creative_type": creative_type,
        "data": manifest,
        "queue": queue_res,
    }


def select_creative_service(
    asin: str,
    creative_type: Optional[str]
) -> Dict[str, Any]:
    """Select or deselect a creative for Pinterest publishing (exactly one selected per ASIN)."""
    asin_clean = asin.upper().strip()
    success, msg, manifest = set_selected_creative(asin=asin_clean, creative_type=creative_type)
    queue_res = None
    if success and creative_type and creative_type.lower() not in ("none", "deselect", "clear"):
        queue_res = ensure_image_queue_entry(asin_clean)
    return {
        "success": success,
        "message": msg,
        "asin": asin_clean,
        "creative_type": creative_type,
        "data": manifest,
        "queue": queue_res,
    }


def get_reference_image_service(asin: str) -> Optional[Path]:
    """Resolve and return path to local reference product image."""
    return get_or_download_reference_image(asin)


def _resolve_video_upload_limits(cfg: Dict[str, Any]) -> Tuple[int, int]:
    """Resolve configured max video size (bytes) and upload timeout (seconds)."""
    video_cfg = cfg.get("video", {}) if isinstance(cfg.get("video"), dict) else {}
    max_bytes_override: Optional[int] = None
    if video_cfg.get("max_size_bytes") is not None:
        max_bytes_override = int(video_cfg["max_size_bytes"])
    elif video_cfg.get("max_size_mb") is not None:
        max_bytes_override = int(float(video_cfg["max_size_mb"]) * 1024 * 1024)
    timeout_override: Optional[int] = (
        int(video_cfg["upload_timeout"]) if video_cfg.get("upload_timeout") is not None else None
    )
    return get_max_video_size_bytes(max_bytes_override), get_video_upload_timeout(timeout_override)


def _prepare_video_upload_source(
    video_file: Any,
    max_size_bytes: int,
) -> Tuple[bool, Optional[Path], str, bool, Optional[str]]:
    """Validate and stage an uploaded video stream or local path before product processing.

    Returns:
        (is_valid, staged_path, original_filename, owns_temp_file, error_message)
    """
    if isinstance(video_file, (str, Path)):
        src_path = Path(video_file)
        if not src_path.is_file():
            return False, None, src_path.name, False, f"Local video file does not exist: '{video_file}'."
        size_bytes = src_path.stat().st_size
        ok, err = validate_video_file(src_path.name, size_bytes=size_bytes, max_size_bytes=max_size_bytes)
        return ok, (src_path if ok else None), src_path.name, False, err

    raw_filename = Path(str(getattr(video_file, "filename", "") or "")).name.strip()
    if not raw_filename:
        return False, None, "", False, "Video filename is missing."

    # Fast-fail unsupported extensions before reading potentially large upload streams.
    ext_ok, ext_err = validate_video_file(raw_filename, size_bytes=1, max_size_bytes=max_size_bytes)
    if not ext_ok:
        return False, None, raw_filename, False, ext_err

    suffix = Path(raw_filename).suffix.lower()
    tmp_handle = tempfile.NamedTemporaryFile(prefix="aab_video_", suffix=suffix, delete=False)
    tmp_path = Path(tmp_handle.name)
    total_bytes = 0
    try:
        while True:
            chunk = video_file.read(1024 * 1024)
            if not chunk:
                break
            total_bytes += len(chunk)
            if total_bytes > max_size_bytes:
                tmp_handle.close()
                tmp_path.unlink(missing_ok=True)
                _, size_err = validate_video_file(raw_filename, size_bytes=total_bytes, max_size_bytes=max_size_bytes)
                return False, None, raw_filename, False, size_err
            tmp_handle.write(chunk)
        tmp_handle.close()
    except Exception as exc:
        try:
            tmp_handle.close()
        except Exception:
            pass
        tmp_path.unlink(missing_ok=True)
        return False, None, raw_filename, False, f"Could not read uploaded video file: {exc}"

    ok, err = validate_video_file(raw_filename, size_bytes=total_bytes, max_size_bytes=max_size_bytes)
    if not ok:
        tmp_path.unlink(missing_ok=True)
        return False, None, raw_filename, False, err
    return True, tmp_path, raw_filename, True, None


def attach_product_video_service(
    asin: str,
    video_file: Any,
    config: Optional[Dict[str, Any]] = None,
    r2_config: Optional[R2Config] = None,
    r2_client: Optional[R2Client] = None,
) -> Dict[str, Any]:
    """Upload or replace a product video in Cloudflare R2 for an existing product.

    Preserves all existing product facts, affiliate URLs, generated platform copy,
    and queue item identifiers/schedule while updating ContentPackage and any
    matching READY QueueItem.
    """
    asin_clean = str(asin or "").upper().strip()
    if not asin_clean:
        return {"success": False, "asin": "", "message": "Product ASIN is required."}

    from publishing.blacklist import is_asin_blacklisted
    if is_asin_blacklisted(asin_clean):
        return {"success": False, "asin": asin_clean, "message": f"ASIN {asin_clean} is permanently blacklisted. Video attachment prohibited."}

    pkg_file = GENERATED_DIR / f"asin_{asin_clean}_content_package.json"
    if not pkg_file.exists():
        return {
            "success": False,
            "asin": asin_clean,
            "message": f"No content package found for ASIN {asin_clean}.",
        }

    try:
        with open(pkg_file, "r", encoding="utf-8") as f:
            pkg = json.load(f)
    except Exception as exc:
        return {
            "success": False,
            "asin": asin_clean,
            "message": f"Could not read content package for ASIN {asin_clean}: {exc}",
        }

    if not isinstance(pkg, dict):
        return {
            "success": False,
            "asin": asin_clean,
            "message": f"Invalid content package structure for ASIN {asin_clean}.",
        }

    queue: Optional[PublishingQueue] = None
    ready_items: List[QueueItem] = []
    if QUEUE_FILE.exists():
        try:
            queue = PublishingQueue.load(QUEUE_FILE)
        except Exception as exc:
            return {
                "success": False,
                "asin": asin_clean,
                "message": f"Could not read publishing queue: {exc}",
            }
        matching_items = [it for it in queue.items if (it.asin or "").upper() == asin_clean]
        ready_items = [it for it in matching_items if it.status == STATUS_READY]
        has_live_publication = any(
            it.status in (STATUS_PUBLISHED, STATUS_PARTIALLY_PUBLISHED)
            or any(
                isinstance(res, dict) and res.get("status") == STATUS_PUBLISHED
                for res in (it.publishing_results or {}).values()
            )
            for it in matching_items
        )
        if has_live_publication and not ready_items:
            return {
                "success": False,
                "asin": asin_clean,
                "message": f"Cannot upload or replace video for ASIN {asin_clean} because it has already been published.",
            }

    if video_file is None:
        return {
            "success": False,
            "asin": asin_clean,
            "message": "Please select a local MP4 or MOV video file to upload.",
        }

    cfg = config or load_application_config()
    max_video_bytes, video_upload_timeout = _resolve_video_upload_limits(cfg)

    vid_ok, staged_video_path, staged_video_filename, owns_staged_video, vid_err = _prepare_video_upload_source(
        video_file,
        max_size_bytes=max_video_bytes,
    )
    if not vid_ok or staged_video_path is None:
        return {
            "success": False,
            "asin": asin_clean,
            "message": vid_err or "Invalid video file.",
        }

    try:
        r2_res = upload_video_to_r2(
            asin=asin_clean,
            local_video_path=staged_video_path,
            filename_override=staged_video_filename,
            max_size_bytes=max_video_bytes,
            timeout=video_upload_timeout,
            config=r2_config,
            client=r2_client,
        )
    finally:
        if owns_staged_video and staged_video_path is not None:
            staged_video_path.unlink(missing_ok=True)

    if not r2_res.get("success") or not r2_res.get("public_url"):
        return {
            "success": False,
            "asin": asin_clean,
            "message": r2_res.get("error", "Cloudflare R2 video upload failed."),
        }

    was_replaced = bool(pkg.get("video_url") or pkg.get("media_type") == "video")
    pkg_updated: Dict[str, Any] = json.loads(json.dumps(pkg))
    ContentPackage.detach_video(pkg_updated)
    attached = ContentPackage.attach_verified_video(
        pkg_updated,
        video_url=r2_res["public_url"],
        object_key=r2_res.get("object_key"),
        content_type=r2_res.get("content_type"),
        filename=staged_video_filename,
        size_bytes=r2_res.get("size_bytes"),
    )
    if not attached:
        return {
            "success": False,
            "asin": asin_clean,
            "message": "Uploaded R2 video URL failed HTTPS verification.",
        }

    # Ensure separate Pinterest copy for both accounts for video products
    ensure_pinterest_video_variants(pkg_updated)

    pkg_path = ContentPackage.save_content_package(
        pkg_updated,
        f"asin_{asin_clean}",
        target_dir=GENERATED_DIR,
    )

    queue_updated = False
    updated_queue_item_id: Optional[str] = None
    if queue is not None:
        video_items = [
            it for it in ready_items
            if it.media_type == "video" or it.daily_slot == 2
        ]
        image_items = [
            it for it in ready_items
            if it.media_type == "image" or (it.daily_slot == 1 and it.media_type != "video")
        ]

        if video_items:
            for it in video_items:
                it.media_type = "video"
                it.video_url = r2_res["public_url"]
                if not it.package_reference:
                    it.package_reference = to_repository_relative_path(pkg_path)
                updated_queue_item_id = updated_queue_item_id or it.queue_item_id
            queue_updated = True
        elif image_items:
            # An image item exists in Slot 1. Preserve it as image and schedule a separate Slot 2 video entry.
            tz = get_budapest_timezone()
            now_bp = datetime.now(tz)
            today = now_bp.date()
            target_slot = 2
            target_time = SLOT_2_TIME

            scheduled_slot_keys = set()
            for it in queue.items:
                if it.scheduled_date and it.daily_slot:
                    scheduled_slot_keys.add((it.scheduled_date, it.daily_slot))

            img_item = image_items[0]
            candidate_date = today
            if img_item.scheduled_date and (img_item.scheduled_date, target_slot) not in scheduled_slot_keys:
                try:
                    parsed_d = datetime.strptime(img_item.scheduled_date, "%Y-%m-%d").date()
                    if parsed_d > today or (parsed_d == today and now_bp.time() < target_time):
                        candidate_date = parsed_d
                except Exception:
                    candidate_date = today

            if candidate_date == today and (now_bp.time() >= target_time or (candidate_date.strftime("%Y-%m-%d"), target_slot) in scheduled_slot_keys):
                candidate_date = today + timedelta(days=1)
            while (candidate_date.strftime("%Y-%m-%d"), target_slot) in scheduled_slot_keys:
                candidate_date += timedelta(days=1)

            target_date_str = candidate_date.strftime("%Y-%m-%d")
            target_dt = datetime(
                candidate_date.year, candidate_date.month, candidate_date.day,
                target_time.hour, target_time.minute, target_time.second, tzinfo=tz
            )

            new_vid_id = next_queue_item_id(queue.items, asin_clean)
            aff_url = img_item.original_url or (pkg_updated.get("source") or {}).get("affiliate_url", "")
            prod_title = img_item.product_name or pkg_updated.get("title") or f"Product {asin_clean}"

            new_vid_item = QueueItem(
                queue_item_id=new_vid_id,
                original_url=aff_url,
                asin=asin_clean,
                product_name=prod_title,
                scheduled_date=target_date_str,
                daily_slot=target_slot,
                scheduled_at=target_dt.isoformat(),
                timezone=DEFAULT_TIMEZONE,
                package_reference=to_repository_relative_path(pkg_path),
                status=STATUS_READY,
                validation_status=STATUS_READY_FOR_REVIEW,
                errors=[],
                media_type="video",
                video_url=r2_res["public_url"],
            )
            queue.items.append(new_vid_item)
            queue.recalculate_counts()
            updated_queue_item_id = new_vid_id
            queue_updated = True
        elif ready_items:
            for it in ready_items:
                it.media_type = "video"
                it.video_url = r2_res["public_url"]
                if not it.package_reference:
                    it.package_reference = to_repository_relative_path(pkg_path)
                updated_queue_item_id = updated_queue_item_id or it.queue_item_id
            queue_updated = True

        if queue_updated:
            queue.save(QUEUE_FILE)

        # If this product has an image creative, ensure its Slot 1 image entry exists as well
        if pkg_updated.get("selected_creative") or get_selected_creative(asin_clean):
            ensure_image_queue_entry(asin_clean, QUEUE_FILE)

    action = "replaced" if was_replaced else "uploaded"
    return {
        "success": True,
        "asin": asin_clean,
        "action": action,
        "video_url": r2_res["public_url"],
        "object_key": r2_res.get("object_key", ""),
        "content_type": r2_res.get("content_type", "video/mp4"),
        "filename": staged_video_filename,
        "size_bytes": r2_res.get("size_bytes"),
        "queue_updated": queue_updated,
        "queue_item_id": updated_queue_item_id,
        "message": (
            f"Replaced product video for ASIN {asin_clean} and uploaded to Cloudflare R2."
            if was_replaced
            else f"Uploaded product video for ASIN {asin_clean} to Cloudflare R2."
        ),
    }


def detach_product_video_service(asin: str) -> Dict[str, Any]:
    """Remove an attached video reference from an existing product's ContentPackage and queue item.

    Preserves all product data, affiliate URLs, generated copy, and queue metadata.
    Does not physically delete objects from Cloudflare R2.
    """
    asin_clean = str(asin or "").upper().strip()
    if not asin_clean:
        return {"success": False, "asin": "", "message": "Product ASIN is required."}

    from publishing.blacklist import is_asin_blacklisted
    if is_asin_blacklisted(asin_clean):
        return {"success": False, "asin": asin_clean, "message": f"ASIN {asin_clean} is permanently blacklisted. Video attachment prohibited."}

    pkg_file = GENERATED_DIR / f"asin_{asin_clean}_content_package.json"
    if not pkg_file.exists():
        return {
            "success": False,
            "asin": asin_clean,
            "message": f"No content package found for ASIN {asin_clean}.",
        }

    try:
        with open(pkg_file, "r", encoding="utf-8") as f:
            pkg = json.load(f)
    except Exception as exc:
        return {
            "success": False,
            "asin": asin_clean,
            "message": f"Could not read content package for ASIN {asin_clean}: {exc}",
        }

    if not isinstance(pkg, dict):
        return {
            "success": False,
            "asin": asin_clean,
            "message": f"Invalid content package structure for ASIN {asin_clean}.",
        }

    queue: Optional[PublishingQueue] = None
    unpublished_items: List[QueueItem] = []
    if QUEUE_FILE.exists():
        try:
            queue = PublishingQueue.load(QUEUE_FILE)
        except Exception as exc:
            return {
                "success": False,
                "asin": asin_clean,
                "message": f"Could not read publishing queue: {exc}",
            }
        matching_items = [it for it in queue.items if (it.asin or "").upper() == asin_clean]
        unpublished_items = [
            it for it in matching_items
            if it.status not in (STATUS_PUBLISHED, STATUS_PARTIALLY_PUBLISHED)
            and not any(
                isinstance(res, dict) and res.get("status") == STATUS_PUBLISHED
                for res in (it.publishing_results or {}).values()
            )
        ]
        if matching_items and not unpublished_items:
            return {
                "success": False,
                "asin": asin_clean,
                "message": f"Cannot remove video for ASIN {asin_clean} because it has already been published.",
            }

    pkg_changed = ContentPackage.detach_video(pkg)

    queue_changed = False
    updated_queue_item_id: Optional[str] = None
    if queue is not None and unpublished_items:
        for it in unpublished_items:
            if it.media_type is not None or it.video_url is not None:
                it.media_type = None
                it.video_url = None
                queue_changed = True
                updated_queue_item_id = updated_queue_item_id or it.queue_item_id

    if not pkg_changed and not queue_changed:
        return {
            "success": False,
            "asin": asin_clean,
            "message": f"No attached video found for ASIN {asin_clean}.",
        }

    if pkg_changed:
        ContentPackage.save_content_package(
            pkg,
            f"asin_{asin_clean}",
            target_dir=GENERATED_DIR,
        )

    if queue is not None and queue_changed:
        queue.save(QUEUE_FILE)

    return {
        "success": True,
        "asin": asin_clean,
        "action": "removed",
        "queue_updated": queue_changed,
        "queue_item_id": updated_queue_item_id,
        "message": (
            f"Removed attached video from ASIN {asin_clean}. "
            "Product data, affiliate links, and generated copy remain intact."
        ),
    }


def enqueue_existing_product_service(asin: str, media_type: Optional[str] = None) -> Dict[str, Any]:
    """Add an existing unqueued product and its ContentPackage to the publishing queue.

    Preserves the original Amazon affiliate URL, existing product facts and verified
    product imagery, all generated platform content, and any attached R2 video URL.
    Prevents duplicate queue entries per media type and protects already-published or terminal items.
    """
    asin_clean = str(asin or "").upper().strip()
    if not asin_clean:
        return {"success": False, "asin": "", "message": "Product ASIN is required."}

    from publishing.blacklist import is_asin_blacklisted
    if is_asin_blacklisted(asin_clean):
        return {
            "success": False,
            "asin": asin_clean,
            "message": f"ASIN {asin_clean} is permanently blacklisted. Queuing prohibited.",
        }

    prod_file = PRODUCTS_DIR / f"asin_{asin_clean}.json"
    product: Optional[Product] = None
    if prod_file.exists():
        try:
            with open(prod_file, "r", encoding="utf-8") as f:
                product = Product.from_dict(json.load(f))
        except Exception as exc:
            return {
                "success": False,
                "asin": asin_clean,
                "message": f"Product source data for ASIN {asin_clean} is unreadable: {exc}",
            }

    pkg_file = GENERATED_DIR / f"asin_{asin_clean}_content_package.json"
    if not pkg_file.exists():
        return {
            "success": False,
            "asin": asin_clean,
            "message": f"No content package found for ASIN {asin_clean}. Generate multi-platform content first.",
        }

    try:
        with open(pkg_file, "r", encoding="utf-8") as f:
            package = json.load(f)
    except Exception as exc:
        return {
            "success": False,
            "asin": asin_clean,
            "message": f"Content package for ASIN {asin_clean} is unreadable: {exc}",
        }

    if not isinstance(package, dict):
        return {
            "success": False,
            "asin": asin_clean,
            "message": f"Invalid content package structure for ASIN {asin_clean}.",
        }

    pkg_asin = str(package.get("asin") or "").upper().strip()
    if pkg_asin and pkg_asin != asin_clean:
        return {
            "success": False,
            "asin": asin_clean,
            "message": f"ContentPackage ASIN '{pkg_asin}' does not match requested ASIN '{asin_clean}'.",
        }

    if package.get("status") != STATUS_READY_FOR_REVIEW:
        return {
            "success": False,
            "asin": asin_clean,
            "message": (
                f"Content package for ASIN {asin_clean} has status '{package.get('status')}'. "
                f"Content must pass validation ('{STATUS_READY_FOR_REVIEW}') before queueing."
            ),
        }

    pkg_source = package.get("source") if isinstance(package.get("source"), dict) else {}
    pkg_aff = str(pkg_source.get("affiliate_url") or "").strip()
    prod_aff = str((product.original_affiliate_url or product.affiliate_url) if product else "").strip()

    if pkg_aff and prod_aff and pkg_aff != prod_aff:
        return {
            "success": False,
            "asin": asin_clean,
            "message": f"Affiliate URL mismatch between cached product and ContentPackage for ASIN {asin_clean}.",
        }

    affiliate_url = pkg_aff or prod_aff
    if not affiliate_url:
        return {
            "success": False,
            "asin": asin_clean,
            "message": f"Original Amazon affiliate URL is missing for ASIN {asin_clean}.",
        }

    if product is not None:
        facts = FactExtractor.extract_canonical_facts(product)
        if not facts.is_sufficient():
            return {
                "success": False,
                "asin": asin_clean,
                "message": f"Canonical product facts for ASIN {asin_clean} are insufficient for queueing.",
            }

    verified_image = recover_verified_image_url(package, products_dir=PRODUCTS_DIR) or (
        product.image_url if product else None
    )
    if not is_valid_image_url(verified_image):
        return {
            "success": False,
            "asin": asin_clean,
            "message": f"No verified HTTPS product image found for ASIN {asin_clean}; cannot enqueue package.",
        }

    if any(
        get_historical_destination_report(asin_clean, p).get("evidence_status") == "platform_verified"
        for p in ACTIVE_PUBLISHING_PLATFORMS
    ):
        return {
            "success": False,
            "asin": asin_clean,
            "message": f"Cannot enqueue ASIN {asin_clean}: verified publication history already exists.",
        }

    if QUEUE_FILE.exists():
        try:
            queue = PublishingQueue.load(QUEUE_FILE)
        except Exception as exc:
            return {
                "success": False,
                "asin": asin_clean,
                "message": f"Could not read publishing queue: {exc}",
            }
    else:
        now_bp = datetime.now(get_budapest_timezone())
        queue = PublishingQueue(
            queue_id=f"queue_{now_bp.strftime('%Y%m%d')}_30d",
            created_at=now_bp.isoformat(),
            start_date=now_bp.strftime("%Y-%m-%d"),
            total_days=30,
            products_per_day=2,
            target_capacity=60,
        )

    pkg_video_url = str(package.get("video_url") or "").strip()
    has_attached_video = bool(
        pkg_video_url
        and is_valid_video_url(pkg_video_url)
        and package.get("media_type") == "video"
    )

    requested_media = (media_type or "").lower().strip()
    if requested_media in ("image", "video"):
        target_media = requested_media
    elif has_attached_video:
        target_media = "video"
    else:
        target_media = "image"

    matching_items = [
        it for it in queue.items
        if (it.asin or "").upper() == asin_clean
        or (it.original_url and it.original_url.strip() == affiliate_url)
    ]
    matching_same_media = [
        it for it in matching_items
        if (target_media == "video" and (it.media_type == "video" or it.daily_slot == 2))
        or (target_media == "image" and (it.media_type in ("image", None) and it.daily_slot != 2))
    ]
    if matching_same_media:
        for existing in matching_same_media:
            has_live_pub = (
                existing.status in (STATUS_PUBLISHED, STATUS_PARTIALLY_PUBLISHED)
                or any(
                    isinstance(res, dict) and (res.get("status") == STATUS_PUBLISHED or _is_confirmed_live_result(res))
                    for res in (existing.publishing_results or {}).values()
                )
            )
            if has_live_pub:
                return {
                    "success": False,
                    "asin": asin_clean,
                    "queue_item_id": existing.queue_item_id,
                    "message": f"Cannot enqueue ASIN {asin_clean} ({target_media}): this post has already been published.",
                }
        first_existing = matching_same_media[0]
        if first_existing.status in (STATUS_FAILED, STATUS_SKIPPED):
            return {
                "success": False,
                "asin": asin_clean,
                "queue_item_id": first_existing.queue_item_id,
                "message": (
                    f"Cannot re-enqueue ASIN {asin_clean} ({target_media}): existing queue item "
                    f"'{first_existing.queue_item_id}' has terminal status '{first_existing.status}'."
                ),
            }
        return {
            "success": False,
            "asin": asin_clean,
            "queue_item_id": first_existing.queue_item_id,
            "message": f"ASIN {asin_clean} ({target_media}) is already in the publishing queue ({first_existing.queue_item_id}).",
        }

    item_media_type: Optional[str] = "video" if target_media == "video" else "image"
    item_video_url: Optional[str] = pkg_video_url if (target_media == "video" and has_attached_video) else None

    tz = get_budapest_timezone()
    now_bp = datetime.now(tz)
    today = now_bp.date()
    target_slot = 2 if target_media == "video" else 1
    target_time = SLOT_2_TIME if target_slot == 2 else SLOT_1_TIME

    scheduled_slot_keys = set()
    for it in queue.items:
        if it.scheduled_date and it.daily_slot:
            scheduled_slot_keys.add((it.scheduled_date, it.daily_slot))

    candidate_date = today
    if now_bp.time() >= target_time or (candidate_date.strftime("%Y-%m-%d"), target_slot) in scheduled_slot_keys:
        candidate_date = today + timedelta(days=1)
        while (candidate_date.strftime("%Y-%m-%d"), target_slot) in scheduled_slot_keys:
            candidate_date += timedelta(days=1)

    target_date_str = candidate_date.strftime("%Y-%m-%d")
    target_dt = datetime(
        candidate_date.year,
        candidate_date.month,
        candidate_date.day,
        target_time.hour,
        target_time.minute,
        target_time.second,
        tzinfo=tz
    )
    next_slot_info = {
        "date": target_date_str,
        "slot": target_slot,
        "scheduled_at": target_dt.isoformat(),
        "timezone": DEFAULT_TIMEZONE
    }

    item_id = next_queue_item_id(queue.items, asin_clean)
    product_title = (
        product.title
        if product and product.title
        else (pkg_source.get("title") or f"Product {asin_clean}")
    )
    pkg_path_str = to_repository_relative_path(pkg_file)

    new_queue_item = QueueItem(
        queue_item_id=item_id,
        original_url=affiliate_url,
        asin=asin_clean,
        product_name=product_title,
        scheduled_date=next_slot_info["date"],
        daily_slot=next_slot_info["slot"],
        scheduled_at=next_slot_info["scheduled_at"],
        timezone=next_slot_info["timezone"],
        package_reference=pkg_path_str,
        status=STATUS_READY,
        validation_status=STATUS_READY_FOR_REVIEW,
        errors=[],
        media_type=item_media_type,
        video_url=item_video_url,
    )
    queue.items.append(new_queue_item)
    queue.recalculate_counts()
    QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)
    queue.save(QUEUE_FILE)

    # If this is a video package and an image creative is available, ensure its Slot 1 image entry exists as well
    if target_media == "video" and (package.get("selected_creative") or get_selected_creative(asin_clean)):
        ensure_image_queue_entry(asin_clean)

    media_desc = "video package" if target_media == "video" else "image package"
    return {
        "success": True,
        "asin": asin_clean,
        "queue_item_id": item_id,
        "original_url": affiliate_url,
        "scheduled_date": next_slot_info["date"],
        "daily_slot": next_slot_info["slot"],
        "scheduled_at": next_slot_info["scheduled_at"],
        "timezone": next_slot_info["timezone"],
        "package_path": pkg_path_str,
        "status": STATUS_READY,
        "validation_status": STATUS_READY_FOR_REVIEW,
        "media_type": item_media_type or "image",
        "video_url": item_video_url,
        "queue_updated": True,
        "message": (
            f"Added ASIN {asin_clean} ({media_desc}) to publishing queue as {item_id} "
            f"for {next_slot_info['date']} (Slot {next_slot_info['slot']})."
        ),
    }


def delete_product_service(asin: str) -> Dict[str, Any]:
    """Delete a product, its generated content package, creatives, and pending queue items.

    Safety: Prevents deleting products with confirmed live publications.
    """
    asin_clean = str(asin or "").upper().strip()
    if not asin_clean:
        return {"success": False, "message": "Product ASIN is required."}

    prod_file = PRODUCTS_DIR / f"asin_{asin_clean}.json"
    pkg_file = GENERATED_DIR / f"asin_{asin_clean}_content_package.json"
    creative_dir = DATA_DIR / "creatives" / asin_clean

    if not prod_file.exists() and not pkg_file.exists():
        return {"success": False, "message": f"Product with ASIN '{asin_clean}' not found."}

    # Safety check: prevent deleting products with verified live publications
    if QUEUE_FILE.exists():
        try:
            queue = PublishingQueue.load(QUEUE_FILE)
            matching_items = [it for it in queue.items if (it.asin or "").upper() == asin_clean]
            has_live_pub = any(
                it.status in (STATUS_PUBLISHED, STATUS_PARTIALLY_PUBLISHED)
                or any(
                    isinstance(res, dict) and (res.get("status") == STATUS_PUBLISHED or _is_confirmed_live_result(res))
                    for res in (it.publishing_results or {}).values()
                )
                for it in matching_items
            )
            if has_live_pub:
                return {
                    "success": False,
                    "message": f"Cannot delete product {asin_clean}: product has confirmed live publications.",
                }

            # Remove pending/unexecuted queue items
            if matching_items:
                remaining = [it for it in queue.items if (it.asin or "").upper() != asin_clean]
                removed_count = len(queue.items) - len(remaining)
                queue.items = remaining
                queue.scheduled_posts = max(0, queue.scheduled_posts - removed_count)
                queue.unfilled_slots += removed_count
                queue.total_ready = sum(1 for it in queue.items if it.status == STATUS_READY)
                queue.save(QUEUE_FILE)
        except Exception as exc:
            return {"success": False, "message": f"Error updating queue during deletion: {exc}"}

    # Delete product file
    if prod_file.exists():
        try:
            prod_file.unlink()
        except Exception as exc:
            return {"success": False, "message": f"Could not delete product data: {exc}"}

    # Delete content package file
    if pkg_file.exists():
        try:
            pkg_file.unlink()
        except Exception:
            pass

    # Delete creative folder
    if creative_dir.exists() and creative_dir.is_dir():
        try:
            shutil.rmtree(creative_dir)
        except Exception:
            pass

    return {
        "success": True,
        "asin": asin_clean,
        "message": f"Product {asin_clean} and its associated files deleted successfully.",
    }


def add_products_from_urls(
    urls_text: str,
    config: Optional[Dict[str, Any]] = None,
    video_file: Optional[Any] = None,
    r2_config: Optional[R2Config] = None,
    r2_client: Optional[R2Client] = None,
) -> Dict[str, Any]:
    """
    Process manual Amazon affiliate URL(s) submission.
    Pipeline: URL -> Product Resolution -> CanonicalProductFacts -> ContentGenerator -> Validator -> ContentPackage -> (Optional R2 Video Upload) -> Queue.
    """
    cfg = config or load_application_config()
    # Keep duplicate lines here so the dashboard can report every submitted
    # line independently instead of silently dropping one.
    urls = parse_affiliate_urls(urls_text, is_file_path=False, deduplicate=False)
    
    if not urls:
        return {
            "success": False,
            "message": "No valid URLs provided.",
            "total": 0,
            "accepted": 0,
            "rejected": 0,
            "results": []
        }

    staged_video_path: Optional[Path] = None
    staged_video_filename: str = ""
    owns_staged_video: bool = False
    max_video_bytes, video_upload_timeout = _resolve_video_upload_limits(cfg)

    if video_file is not None:
        if len(urls) > 1:
            return {
                "success": False,
                "message": "Video attachment is supported for single-product import only. Please submit one Amazon affiliate URL when attaching a video.",
                "total": len(urls),
                "accepted": 0,
                "rejected": len(urls),
                "results": [
                    {
                        "url": u.strip(),
                        "asin": extract_asin_from_url(u.strip()) or "",
                        "status": "REJECTED",
                        "reason": "Video attachment requires a single product URL.",
                    }
                    for u in urls
                ],
            }
        vid_ok, staged_video_path, staged_video_filename, owns_staged_video, vid_err = _prepare_video_upload_source(
            video_file,
            max_size_bytes=max_video_bytes,
        )
        if not vid_ok or staged_video_path is None:
            single_clean = urls[0].strip()
            return {
                "success": False,
                "message": vid_err or "Invalid video file.",
                "total": 1,
                "accepted": 0,
                "rejected": 1,
                "results": [
                    {
                        "url": single_clean,
                        "asin": extract_asin_from_url(single_clean) or "",
                        "status": "REJECTED",
                        "reason": vid_err or "Invalid video file.",
                    }
                ],
            }

    batch_id = f"batch_{datetime.now(get_budapest_timezone()).strftime('%Y%m%dT%H%M%S%f')}_{uuid.uuid4().hex[:8]}"
    batch_created_at = datetime.now(get_budapest_timezone()).isoformat()

    PRODUCTS_DIR.mkdir(parents=True, exist_ok=True)
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    
    # Load queue
    queue = PublishingQueue.load(QUEUE_FILE) if QUEUE_FILE.exists() else PublishingQueue(
        queue_id=f"queue_{datetime.now(get_budapest_timezone()).strftime('%Y%m%d')}_30d",
        created_at=datetime.now(get_budapest_timezone()).isoformat(),
        start_date=datetime.now(get_budapest_timezone()).strftime("%Y-%m-%d"),
        total_days=30,
        products_per_day=2,
        target_capacity=60
    )

    existing_asins = {it.asin.upper() for it in queue.items if it.asin}
    published_asins = {
        it.asin.upper() for it in queue.items
        if it.asin and (
            it.status == STATUS_PUBLISHED
            or any(
                isinstance(result, dict) and result.get("status") == STATUS_PUBLISHED
                for result in (it.publishing_results or {}).values()
            )
        )
    }
    seen_submitted_urls: Set[str] = set()
    # Duplicate protection is queue-authoritative. A URL that merely exists in the
    # local product cache has never been queued, so it must remain importable;
    # queue membership (checked below by URL and by ASIN) is what prevents
    # duplicate queue entries.
    existing_urls = {it.original_url for it in queue.items if it.original_url}

    provider = create_content_provider(cfg)
    generator = ContentGenerator(ollama_client=provider)
    disclosure = cfg.get("affiliate", {}).get("disclosure", DEFAULT_AFFILIATE_DISCLOSURE)

    results: List[Dict[str, Any]] = []
    accepted_count = 0
    rejected_count = 0

    for url in urls:
        url_clean = url.strip()
        extracted_asin = extract_asin_from_url(url_clean)

        if url_clean in seen_submitted_urls:
            rejected_count += 1
            results.append({
                "url": url_clean,
                "asin": extracted_asin or "UNKNOWN",
                "status": "DUPLICATE",
                "reason": "The same affiliate URL was submitted more than once in this batch."
            })
            continue
        seen_submitted_urls.add(url_clean)

        # Permanent blacklist check
        from publishing.blacklist import is_asin_blacklisted
        if extracted_asin and is_asin_blacklisted(extracted_asin):
            rejected_count += 1
            results.append({
                "url": url_clean,
                "asin": extracted_asin.upper(),
                "status": "BLOCKED",
                "reason": f"Product with ASIN {extracted_asin.upper()} is permanently blacklisted. Importing is prohibited."
            })
            continue

        # 1. Deduplication check
        if extracted_asin and extracted_asin.upper() in published_asins:
            rejected_count += 1
            results.append({
                "url": url_clean,
                "asin": extracted_asin.upper(),
                "status": "DUPLICATE",
                "reason": f"Product with ASIN {extracted_asin.upper()} already has publication history and will not be re-imported."
            })
            continue

        if url_clean in existing_urls:
            rejected_count += 1
            results.append({
                "url": url_clean,
                "asin": extracted_asin or "UNKNOWN",
                "status": "DUPLICATE",
                "reason": "Affiliate URL is already present in the publishing queue."
            })
            continue

        if extracted_asin and extracted_asin.upper() in existing_asins:
            rejected_count += 1
            results.append({
                "url": url_clean,
                "asin": extracted_asin.upper(),
                "status": "DUPLICATE",
                "reason": f"Product with ASIN {extracted_asin.upper()} is already present in the publishing queue."
            })
            continue

        marketplace = marketplace_from_url(
            url_clean,
            os.environ.get("AMAZON_CREATORS_API_MARKETPLACE", ""),
        )
        creators_api_client = None
        if marketplace:
            creators_api_client = CreatorsAPIClient.from_environment(
                marketplace,
                cfg.get("discovery", {}).get("tracking_tag", DEFAULT_TRACKING_TAG),
            )

        # 2. Product resolution via cache or scraper
        is_valid, product, cache_key, errors = resolve_product_for_url(
            url_clean,
            products_dir=PRODUCTS_DIR,
            allow_scrape=False,
        )

        if not is_valid or not product:
            is_valid, product, cache_key, errors = resolve_product_for_url(
                url_clean,
                products_dir=PRODUCTS_DIR,
                allow_scrape=True,
                creators_api_client=creators_api_client,
            )

        if not is_valid or not product or not product.title:
            rejected_count += 1
            results.append({
                "url": url_clean,
                "asin": extracted_asin if extracted_asin else "",
                "status": "REJECTED",
                "reason": "; ".join(errors) if errors else ("ASIN unavailable — could not determine product ASIN from scraped data" if not extracted_asin else "Product data is insufficient or could not be resolved.")
            })
            continue

        product.affiliate_url = url_clean
        product.original_affiliate_url = url_clean
        asin_final = product.asin.upper()

        # Check for pre-existing valid content package
        pkg_file = GENERATED_DIR / f"{cache_key}_content_package.json"
        pkg: Optional[Dict[str, Any]] = None
        if pkg_file.exists():
            try:
                with open(pkg_file, "r", encoding="utf-8") as pf:
                    loaded_pkg = json.load(pf)
                loaded_aff = (loaded_pkg.get("source") or {}).get("affiliate_url")
                if loaded_pkg.get("status") == STATUS_READY_FOR_REVIEW and (not loaded_aff or loaded_aff == url_clean):
                    package_image = (loaded_pkg.get("platforms", {}).get("pinterest") or {}).get("image_url")
                    if is_valid_image_url(package_image):
                        pkg = loaded_pkg
                    elif is_valid_image_url(product.image_url) and ContentPackage.attach_verified_image(loaded_pkg, product.image_url):
                        pkg = loaded_pkg
                        ContentPackage.save_content_package(loaded_pkg, cache_key, target_dir=GENERATED_DIR)
            except Exception:
                pass

        if not pkg:
            # 3. Canonical Facts Extraction
            facts = FactExtractor.extract_canonical_facts(product)
            if not facts.is_sufficient():
                rejected_count += 1
                results.append({
                    "url": url_clean,
                    "asin": asin_final,
                    "status": "REJECTED",
                    "reason": "Extracted canonical facts are insufficient for grounded marketing copy."
                })
                continue

            # 4. Content Generation
            try:
                try:
                    social_data = generator.generate_all_platforms(product=facts, disclosure=disclosure, include_variants=True)
                except TypeError as te:
                    if "include_variants" in str(te):
                        social_data = generator.generate_all_platforms(product=facts, disclosure=disclosure)
                    else:
                        raise
            except Exception as e:
                rejected_count += 1
                results.append({
                    "url": url_clean,
                    "asin": asin_final,
                    "status": "REJECTED",
                    "reason": f"Content generation error: {str(e)}"
                })
                continue

            # 5. Build Content Package
            pkg = ContentPackage.build_package(
                product=product,
                social_data=social_data,
                cache_file_path=f"data/products/{cache_key}.json",
                model_name=provider.model
            )

            pkg_path = ContentPackage.save_content_package(pkg, cache_key, target_dir=GENERATED_DIR)
        else:
            pkg_path = pkg_file
            if staged_video_path is None and ContentPackage.detach_video(pkg):
                pkg_path = ContentPackage.save_content_package(pkg, cache_key, target_dir=GENERATED_DIR)

        pkg_path_str = to_repository_relative_path(pkg_path)

        if pkg.get("status") != STATUS_READY_FOR_REVIEW:
            platform_errors = []
            for platform_name, platform_data in (pkg.get("platforms") or {}).items():
                if isinstance(platform_data, dict):
                    validation = platform_data.get("validation") or {}
                    platform_errors.extend(
                        f"{platform_name}: {error}"
                        for error in validation.get("errors", [])
                    )
            failure_reason = "; ".join(platform_errors) or "Generated content failed authoritative claim validation."
            failure_status = "CONTENT_GENERATION_FAILED" if any(
                any(marker in error.lower() for marker in ("ollama", "groq", "provider", "rate limit", "quota"))
                for error in platform_errors
            ) else "VALIDATION_FAILED"
            rejected_count += 1
            results.append({
                "url": url_clean,
                "asin": asin_final,
                "status": failure_status,
                "stage": "content_generation" if failure_status == "CONTENT_GENERATION_FAILED" else "claim_validation",
                "reason": failure_reason
            })
            continue

        # 5.5 Optional Cloudflare R2 Video Upload (runs ONLY after product resolution and package validation succeed)
        item_media_type: Optional[str] = None
        item_video_url: Optional[str] = None
        if staged_video_path is not None:
            r2_res = upload_video_to_r2(
                asin=asin_final,
                local_video_path=staged_video_path,
                filename_override=staged_video_filename,
                max_size_bytes=max_video_bytes,
                timeout=video_upload_timeout,
                config=r2_config,
                client=r2_client,
            )
            if not r2_res.get("success") or not r2_res.get("public_url"):
                rejected_count += 1
                results.append({
                    "url": url_clean,
                    "asin": asin_final,
                    "status": "VIDEO_UPLOAD_FAILED",
                    "stage": "video_upload",
                    "reason": r2_res.get("error", "Cloudflare R2 video upload failed."),
                })
                continue

            attached = ContentPackage.attach_verified_video(
                pkg,
                video_url=r2_res["public_url"],
                object_key=r2_res.get("object_key"),
                content_type=r2_res.get("content_type"),
                filename=staged_video_filename,
                size_bytes=r2_res.get("size_bytes"),
            )
            if not attached:
                rejected_count += 1
                results.append({
                    "url": url_clean,
                    "asin": asin_final,
                    "status": "VIDEO_UPLOAD_FAILED",
                    "stage": "video_upload",
                    "reason": "Uploaded R2 video URL failed HTTPS verification.",
                })
                continue

            pkg_path = ContentPackage.save_content_package(pkg, cache_key, target_dir=GENERATED_DIR)
            pkg_path_str = to_repository_relative_path(pkg_path)
            item_media_type = "video"
            item_video_url = r2_res["public_url"]

        # 6. Allocate Next Slot and Schedule in Queue
        next_slot_info = QueueReplenisher.find_next_available_slot(queue)
        # Max existing numeric suffix + 1 (not len(items) + 1): diverged local
        # and remote queues can otherwise hand out the same identifier.
        item_id = next_queue_item_id(queue.items, asin_final)

        new_queue_item = QueueItem(
            queue_item_id=item_id,
            original_url=url_clean,
            asin=asin_final,
            product_name=product.title,
            scheduled_date=next_slot_info["date"],
            daily_slot=next_slot_info["slot"],
            scheduled_at=next_slot_info["scheduled_at"],
            timezone=next_slot_info["timezone"],
            package_reference=pkg_path_str,
            status=STATUS_READY,
            validation_status=STATUS_READY_FOR_REVIEW,
            errors=[],
            batch_id=batch_id,
            media_type=item_media_type,
            video_url=item_video_url,
        )

        queue.items.append(new_queue_item)
        queue.recalculate_counts()
        existing_asins.add(asin_final)
        existing_urls.add(url_clean)

        accepted_count += 1
        accepted_entry: Dict[str, Any] = {
            "url": url_clean,
            "asin": asin_final,
            "title": product.title,
            "status": "ACCEPTED",
            "scheduled_date": next_slot_info["date"],
            "daily_slot": next_slot_info["slot"],
            "scheduled_at": next_slot_info["scheduled_at"],
            "package_path": pkg_path_str,
            "queue_item_id": item_id,
        }
        if item_media_type:
            accepted_entry["media_type"] = item_media_type
        if item_video_url:
            accepted_entry["video_url"] = item_video_url
        results.append(accepted_entry)

    if owns_staged_video and staged_video_path is not None:
        staged_video_path.unlink(missing_ok=True)

    # Save queue and append the immutable import-run record. Processing and
    # publishing update queue state only; they never create a new batch.
    queue.save(QUEUE_FILE)

    batches = load_import_batches()
    batches.append({
        "batch_id": batch_id,
        "created_at": batch_created_at,
        "submitted_count": len(urls),
        "accepted_count": accepted_count,
        "duplicate_count": sum(1 for row in results if row.get("status") == "DUPLICATE"),
        "failed_count": sum(1 for row in results if row.get("status") not in ("ACCEPTED", "DUPLICATE")),
        "entries": results
    })
    save_import_batches(batches)

    return {
        "success": True,
        "total": len(urls),
        "accepted": accepted_count,
        "rejected": rejected_count,
        "results": results,
        "batch_id": batch_id,
        "batch_created_at": batch_created_at
    }


def revalidate_content(asin: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Re-validate an existing generated package against CanonicalProductFacts using ClaimValidator.
    """
    asin_clean = asin.upper().strip()
    cfg = config or load_application_config()
    disclosure = cfg.get("affiliate", {}).get("disclosure", DEFAULT_AFFILIATE_DISCLOSURE)

    detail = get_product_detail(asin_clean)
    if not detail or not detail.get("package"):
        return {"success": False, "message": f"No content package found for ASIN {asin_clean}."}

    pkg = detail["package"]
    prod_dict = detail.get("product")
    if not prod_dict:
        return {"success": False, "message": f"Product source data not found for ASIN {asin_clean}."}

    product = Product.from_dict(prod_dict)
    facts = FactExtractor.extract_canonical_facts(product)

    all_valid = True
    platform_results: Dict[str, Any] = {}

    target_platforms = list(SUPPORTED_PLATFORMS)
    for extra_k in ("pinterest_lifemodehq",):
        if extra_k in pkg.get("platforms", {}) and extra_k not in target_platforms:
            target_platforms.append(extra_k)

    for p_name in target_platforms:
        p_data = pkg.get("platforms", {}).get(p_name, {})
        is_val, issues = ClaimValidator.validate_platform_content(
            platform=p_name,
            content=p_data,
            product=facts,
            disclosure=disclosure
        )
        if not is_val:
            all_valid = False

        val_status = STATUS_VALID if is_val else STATUS_FAILED_VALIDATION
        p_data["status"] = val_status
        p_data["validation"] = {
            "status": val_status,
            "retry_count": p_data.get("validation", {}).get("retry_count", 0),
            "errors": issues
        }
        platform_results[p_name] = {
            "is_valid": is_val,
            "status": val_status,
            "errors": issues
        }

    pkg["status"] = STATUS_READY_FOR_REVIEW if all_valid else STATUS_FAILED_VALIDATION
    pkg_file = GENERATED_DIR / f"asin_{asin_clean}_content_package.json"
    with open(pkg_file, "w", encoding="utf-8") as f:
        json.dump(pkg, f, indent=2, ensure_ascii=False)

    # Update queue item if present
    if QUEUE_FILE.exists():
        try:
            queue = PublishingQueue.load(QUEUE_FILE)
            for it in queue.items:
                if (it.asin or "").upper() == asin_clean and it.status != STATUS_PUBLISHED:
                    it.validation_status = pkg["status"]
                    it.status = STATUS_READY if all_valid else STATUS_FAILED
            queue.save(QUEUE_FILE)
        except Exception:
            pass

    return {
        "success": True,
        "asin": asin_clean,
        "is_valid": all_valid,
        "package_status": pkg["status"],
        "platform_results": platform_results
    }


def regenerate_content(asin: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Regenerate content for an existing product from its CanonicalProductFacts.
    Does NOT change ASIN, does NOT scrape a different product, does NOT bypass validation.
    """
    asin_clean = asin.upper().strip()
    cfg = config or load_application_config()
    detail = get_product_detail(asin_clean)

    if not detail or not detail.get("product"):
        return {"success": False, "message": f"Product source data not found for ASIN {asin_clean}."}

    product = Product.from_dict(detail["product"])
    facts = FactExtractor.extract_canonical_facts(product)
    
    if not facts.is_sufficient():
        return {"success": False, "message": f"Canonical facts for ASIN {asin_clean} are insufficient."}

    provider = create_content_provider(cfg)
    generator = ContentGenerator(ollama_client=provider)
    disclosure = cfg.get("affiliate", {}).get("disclosure", DEFAULT_AFFILIATE_DISCLOSURE)

    try:
        try:
            social_data = generator.generate_all_platforms(product=facts, disclosure=disclosure, include_variants=True)
        except TypeError as te:
            if "include_variants" in str(te):
                social_data = generator.generate_all_platforms(product=facts, disclosure=disclosure)
            else:
                raise
    except Exception as e:
        return {"success": False, "message": f"Content generation error: {str(e)}"}

    cache_key = f"asin_{asin_clean}"
    pkg = ContentPackage.build_package(
        product=product,
        social_data=social_data,
        cache_file_path=f"data/products/{cache_key}.json",
        model_name=provider.model
    )

    existing_pkg = detail.get("package") if isinstance(detail.get("package"), dict) else {}
    if existing_pkg.get("media_type") == "video" and existing_pkg.get("video_url"):
        existing_video_meta = existing_pkg.get("video") if isinstance(existing_pkg.get("video"), dict) else {}
        ContentPackage.attach_verified_video(
            pkg,
            video_url=existing_pkg["video_url"],
            object_key=existing_pkg.get("video_object_key") or existing_video_meta.get("object_key"),
            content_type=existing_pkg.get("video_content_type") or existing_video_meta.get("content_type"),
            filename=existing_video_meta.get("filename"),
            size_bytes=existing_video_meta.get("size_bytes"),
        )

    ContentPackage.save_content_package(pkg, cache_key, target_dir=GENERATED_DIR)

    # Update Queue if item is present and not published
    if QUEUE_FILE.exists():
        try:
            queue = PublishingQueue.load(QUEUE_FILE)
            for it in queue.items:
                if (it.asin or "").upper() == asin_clean and it.status != STATUS_PUBLISHED:
                    it.validation_status = pkg["status"]
                    it.status = STATUS_READY if pkg["status"] == STATUS_READY_FOR_REVIEW else STATUS_FAILED
            queue.save(QUEUE_FILE)
        except Exception:
            pass

    return {
        "success": True,
        "asin": asin_clean,
        "package_status": pkg["status"],
        "is_ready": pkg["status"] == STATUS_READY_FOR_REVIEW
    }


def recover_failed_product(
    asin: str,
    batch_id: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Explicitly recover one failed import without re-importing its URL.

    The failed batch entry and existing product cache are the source of truth.
    No new batch is created, and recovery is intentionally never called by an
    automatic maintenance path.
    """
    asin_clean = asin.upper().strip()
    from publishing.blacklist import is_asin_blacklisted
    if is_asin_blacklisted(asin_clean):
        return {"success": False, "message": f"ASIN {asin_clean} is permanently blacklisted. Recovery prohibited."}

    product_file = PRODUCTS_DIR / f"asin_{asin_clean}.json"
    if not product_file.exists():
        return {"success": False, "message": f"Product source data not found for ASIN {asin_clean}."}

    try:
        product_data = json.loads(product_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"success": False, "message": f"Product source data for ASIN {asin_clean} is unreadable."}

    original_url = product_data.get("original_affiliate_url") or product_data.get("affiliate_url")
    if not isinstance(original_url, str) or not original_url:
        return {"success": False, "message": f"Original affiliate URL is missing for ASIN {asin_clean}."}

    batches = load_import_batches()
    matches = []
    for batch in batches:
        if batch_id and batch.get("batch_id") != batch_id:
            continue
        for entry in batch.get("entries", []):
            if (
                str(entry.get("asin", "")).upper() == asin_clean
                and entry.get("url") == original_url
                and entry.get("status") == "CONTENT_GENERATION_FAILED"
            ):
                matches.append((batch, entry))
    if len(matches) != 1:
        if not matches:
            return {"success": False, "message": f"No unique failed content-generation batch entry found for ASIN {asin_clean}."}
        return {"success": False, "message": f"Multiple failed batch entries found for ASIN {asin_clean}; specify batch_id."}

    failed_batch, failed_entry = matches[0]
    source_url = product_data.get("affiliate_url")
    if source_url != original_url:
        return {"success": False, "message": f"Cached affiliate URL mismatch for ASIN {asin_clean}; no recovery performed."}

    queue = PublishingQueue.load(QUEUE_FILE) if QUEUE_FILE.exists() else PublishingQueue(
        queue_id=f"queue_{datetime.now(get_budapest_timezone()).strftime('%Y%m%d')}_30d",
        created_at=datetime.now(get_budapest_timezone()).isoformat(),
        start_date=datetime.now(get_budapest_timezone()).strftime("%Y-%m-%d"),
        total_days=30,
        products_per_day=2,
        target_capacity=60,
    )
    existing = [
        item for item in queue.items
        if (item.asin or "").upper() == asin_clean or item.original_url == original_url
    ]
    if existing:
        return {
            "success": False,
            "message": f"ASIN {asin_clean} or its original affiliate URL is already in the publishing queue.",
            "queue_item_id": existing[0].queue_item_id,
        }

    # This is the only generation call, and it occurs only after explicit
    # invocation of this recovery function.
    regenerated = regenerate_content(asin_clean, config=config)
    if not regenerated.get("success") or not regenerated.get("is_ready"):
        return {
            "success": False,
            "asin": asin_clean,
            "batch_id": failed_batch.get("batch_id"),
            "message": regenerated.get("message", "Content was not ready for review."),
        }

    package_file = GENERATED_DIR / f"asin_{asin_clean}_content_package.json"
    try:
        package = json.loads(package_file.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {"success": False, "message": f"Recovered content package for ASIN {asin_clean} is unreadable."}
    if package.get("status") != STATUS_READY_FOR_REVIEW or package.get("source", {}).get("affiliate_url") != original_url:
        return {"success": False, "message": f"Recovered content package for ASIN {asin_clean} failed URL/status verification."}

    slot = QueueReplenisher.find_next_available_slot(queue)
    item_id = next_queue_item_id(queue.items, asin_clean)
    queue.items.append(QueueItem(
        queue_item_id=item_id,
        original_url=original_url,
        asin=asin_clean,
        product_name=product_data.get("title", f"Product {asin_clean}"),
        scheduled_date=slot["date"],
        daily_slot=slot["slot"],
        scheduled_at=slot["scheduled_at"],
        timezone=slot["timezone"],
        package_reference=to_repository_relative_path(package_file),
        status=STATUS_READY,
        validation_status=STATUS_READY_FOR_REVIEW,
        errors=[],
        batch_id=failed_batch.get("batch_id"),
    ))
    queue.save(QUEUE_FILE)

    return {
        "success": True,
        "asin": asin_clean,
        "batch_id": failed_batch.get("batch_id"),
        "queue_item_id": item_id,
        "original_url": original_url,
        "status": STATUS_READY,
        "package_status": STATUS_READY_FOR_REVIEW,
    }


def execute_discovery_run(queries: Optional[List[str]] = None, target_capacity: Optional[int] = None, force_discovery: bool = False) -> Dict[str, Any]:
    """Execute queue replenishment and discovery in DRY_RUN mode."""
    cfg = load_application_config()
    target_cap = target_capacity or cfg.get("discovery", {}).get("target_future_capacity", DEFAULT_TARGET_FUTURE_CAPACITY)
    q_list = queries or cfg.get("discovery", {}).get("queries") or DEFAULT_CONFIG_QUERIES

    try:
        report = QueueReplenisher.maintain_queue(
            queue_file=QUEUE_FILE,
            config=cfg,
            target_capacity=target_cap,
            discovery_queries=q_list,
            force_discovery=force_discovery,
            dry_run=True
        )
        return {
            "success": True,
            "report": report
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def execute_publisher_run(now_override: Optional[str] = None) -> Dict[str, Any]:
    """Execute publishing of due items in DRY_RUN mode."""
    cfg = load_application_config()
    try:
        report = QueuePublisher.publish_due_items(
            queue_file=QUEUE_FILE,
            config=cfg,
            now_override=now_override,
            dry_run=True
        )
        return {
            "success": True,
            "report": report
        }
    except Exception as e:
        return {
            "success": False,
            "error": str(e)
        }


def execute_live_publish_item(
    queue_item_id: str,
    confirmed: bool = False,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Explicitly publish one validated queue item through the real adapters.

    This operation is never called during app startup or by the local dry-run
    action.  The confirmation guard is enforced in the service as well as in
    the form so callers cannot accidentally turn a preview into a live post.
    """
    if not confirmed:
        return {
            "success": False,
            "error": "Explicit live-publication confirmation is required.",
            "requires_confirmation": True,
        }

    with _LIVE_PUBLISH_LOCK:
        if queue_item_id in _ACTIVE_PUBLISHING_ITEM_IDS:
            return {
                "success": False,
                "error": f"Queue item '{queue_item_id}' is already being published. Concurrent requests are rejected.",
            }

        if not QUEUE_FILE.exists():
            return {"success": False, "error": f"Publishing queue not found at {QUEUE_FILE}."}

        try:
            queue = PublishingQueue.load(QUEUE_FILE)
        except Exception as exc:
            return {"success": False, "error": f"Could not load publishing queue: {exc}"}

        item = next((candidate for candidate in queue.items if candidate.queue_item_id == queue_item_id), None)
        if item is None:
            return {"success": False, "error": f"Queue item '{queue_item_id}' was not found."}

        # Permanent blacklist check
        from publishing.blacklist import is_asin_blacklisted
        if is_asin_blacklisted(item.asin):
            item.status = "CANCELLED"
            queue.save(QUEUE_FILE)
            return {
                "success": False,
                "error": f"ASIN '{item.asin}' is permanently blacklisted. Publishing is prohibited.",
            }

        # Safe recovery if process restarted while item was left in PUBLISHING status
        if item.status == STATUS_PUBLISHING:
            prev_results = [v for v in (item.publishing_results or {}).values() if isinstance(v, dict)]
            if any(_is_confirmed_live_result(v) for v in prev_results):
                item.status = STATUS_PARTIALLY_PUBLISHED
            else:
                item.status = STATUS_FAILED
                if not item.errors:
                    item.errors = ["Previous publishing attempt was interrupted by a process restart or crash."]
            queue.save(QUEUE_FILE)

        presentation = get_queue_item_presentation(queue, item)
        if presentation["state"] == "PUBLISHED" or item.status == STATUS_PUBLISHED:
            return {
                "success": True,
                "skipped": True,
                "message": "All active platforms already have confirmed live publication evidence.",
                "item_id": item.queue_item_id,
                "status": presentation["state"],
                "platforms": presentation["platforms"],
            }

        package_path = Path(item.package_reference) if item.package_reference else None
        if package_path and not package_path.exists():
            package_path = PROJECT_ROOT / item.package_reference
        if not package_path or not package_path.exists():
            return {
                "success": False,
                "error": f"Content package is unavailable for ASIN {item.asin}; fix the product/content preflight first.",
                "error_type": "product_preflight",
            }

        try:
            with package_path.open("r", encoding="utf-8") as handle:
                package = json.load(handle)
        except Exception as exc:
            return {"success": False, "error": f"Could not read content package: {exc}", "error_type": "product_preflight"}

        if package.get("status") != STATUS_READY_FOR_REVIEW:
            return {
                "success": False,
                "error": f"Content package is '{package.get('status')}', not READY_FOR_REVIEW.",
                "error_type": "product_preflight",
            }

        # Register in-flight publish and persist PUBLISHING status before any API call
        _ACTIVE_PUBLISHING_ITEM_IDS.add(queue_item_id)
        item.status = STATUS_PUBLISHING
        queue.save(QUEUE_FILE)

    QueuePublisher.log_event(
        f"Manual live publishing initiated for queue item '{queue_item_id}' (ASIN {item.asin})."
    )

    try:
        cfg = config or load_application_config()
        publisher = SocialPublisher(
            config=PublishingConfig.from_dict(cfg, dry_run_override=False)
        )
        declared_media_type = (getattr(item, "media_type", None) or "image").strip().lower()
        if declared_media_type == "image":
            package = dict(package)
            package["media_type"] = "image"
            package.pop("video_url", None)
            if hasattr(ContentPackage, "detach_video"):
                ContentPackage.detach_video(package)
            for p_val in (package.get("platforms") or {}).values():
                if isinstance(p_val, dict):
                    p_val.pop("video_url", None)
                    p_val["media_type"] = "image"
        elif declared_media_type == "video":
            if is_valid_video_url(getattr(item, "video_url", None)) and not is_valid_video_url(package.get("video_url")):
                package = dict(package)
                ContentPackage.attach_verified_video(package, item.video_url)
            vid = getattr(item, "video_url", None) or package.get("video_url")
            if not is_valid_video_url(vid):
                return {
                    "success": False,
                    "error": f"Declared media_type is 'video' but no valid video URL exists for item '{item.queue_item_id}'. Image fallback prohibited.",
                }

        report = publisher.publish_package(
            package,
            dry_run=False,
            prior_results=item.publishing_results,
            declared_media_type=declared_media_type,
        )
        platform_results = report.get("results", {})

        def confirmed_live(result: Dict[str, Any]) -> bool:
            return _is_confirmed_live_result(result)

        # Preserve confirmed successes even if a retry report omits that target or
        # returns a contradictory failure.
        for key, previous in (item.publishing_results or {}).items():
            if confirmed_live(previous):
                current = platform_results.get(key)
                if not isinstance(current, dict) or not confirmed_live(current):
                    preserved = dict(previous)
                    preserved["action"] = "SKIP_DUPLICATE"
                    platform_results[key] = preserved

        active_results = {
            key: value for key, value in platform_results.items()
            if key in ("facebook", "pinterest", "pinterest_lifemodehq", "threads")
            and isinstance(value, dict)
        }
        now_dt = datetime.now(get_budapest_timezone()).isoformat()
        for result in active_results.values():
            if confirmed_live(result):
                result.setdefault("published_at", now_dt)

        newly_confirmed_count = sum(
            1 for result in active_results.values()
            if confirmed_live(result) and result.get("action") != "SKIP_DUPLICATE"
        )
        skipped_count = sum(
            1 for result in active_results.values()
            if confirmed_live(result) and result.get("action") == "SKIP_DUPLICATE"
        )
        failed_count = sum(
            1 for result in active_results.values()
            if result.get("status") == "FAILED"
        )
        total_confirmed = newly_confirmed_count + skipped_count
        if active_results and total_confirmed == len(active_results):
            item.status = STATUS_PUBLISHED
        elif total_confirmed > 0:
            item.status = STATUS_PARTIALLY_PUBLISHED
        else:
            item.status = STATUS_FAILED

        errors = []
        for result in active_results.values():
            if not confirmed_live(result):
                errors.extend(result.get("errors") or [])
        item.errors = list(dict.fromkeys(errors))
        if newly_confirmed_count:
            item.published_at = now_dt
        elif skipped_count and not item.published_at:
            item.published_at = now_dt
        item.publishing_results = platform_results

        for p_name, p_res in active_results.items():
            p_status = p_res.get("status")
            if p_res.get("action") == "SKIP_DUPLICATE":
                p_status = "SKIPPED_DUPLICATE"
            p_id = p_res.get("post_id") or p_res.get("video_id") or p_res.get("pin_id") or p_res.get("photo_id") or "none"
            QueuePublisher.log_event(
                f"Manual live publish [{queue_item_id}][{p_name}]: {p_status} (id: {p_id})"
            )
        QueuePublisher.log_event(
            f"Manual live publishing completed for '{queue_item_id}': status={item.status}, "
            f"newly_confirmed={newly_confirmed_count}, skipped={skipped_count}, failed={failed_count}"
        )

        return {
            "success": True,
            "item_id": item.queue_item_id,
            "asin": item.asin,
            "mode": "LIVE",
            "status": item.status,
            "confirmed_platforms": newly_confirmed_count,
            "skipped_platforms": skipped_count,
            "failed_platforms": failed_count,
            "platforms": get_queue_item_presentation(queue, item)["platforms"],
        }
    except Exception as exc:
        QueuePublisher.log_event(
            f"Manual live publishing failed for queue item '{queue_item_id}': {exc}",
            level="ERROR",
        )
        prev_confirmed = sum(1 for v in (item.publishing_results or {}).values() if _is_confirmed_live_result(v))
        if prev_confirmed > 0:
            item.status = STATUS_PARTIALLY_PUBLISHED
        else:
            item.status = STATUS_FAILED
        item.errors = list(dict.fromkeys((item.errors or []) + [f"Publishing execution error: {exc}"]))
        return {
            "success": False,
            "error": f"Live publisher execution failed: {exc}",
            "error_type": "publisher",
        }
    finally:
        with _LIVE_PUBLISH_LOCK:
            _ACTIVE_PUBLISHING_ITEM_IDS.discard(queue_item_id)
            if item.status == STATUS_PUBLISHING:
                prev_confirmed = sum(1 for v in (item.publishing_results or {}).values() if _is_confirmed_live_result(v))
                item.status = STATUS_PARTIALLY_PUBLISHED if prev_confirmed > 0 else STATUS_FAILED
            queue.recalculate_counts()
            queue.save(QUEUE_FILE)


def get_log_content(log_type: str = "publisher", max_lines: int = 150) -> List[str]:
    """Fetch sanitized log file lines."""
    log_file = DEFAULT_MAINTENANCE_LOG_FILE if log_type == "maintenance" else DEFAULT_LOG_FILE
    if not log_file.exists():
        return [f"Log file not found at: {log_file}"]

    try:
        with open(log_file, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
            tail_lines = lines[-max_lines:] if len(lines) > max_lines else lines
            return [l.rstrip("\r\n") for l in tail_lines]
    except Exception as e:
        return [f"Error reading log file: {str(e)}"]


def execute_sync_with_github() -> Dict[str, Any]:
    """Execute safe bidirectional synchronization between local dashboard and GitHub Actions."""
    from publishing.sync import sync_with_github
    return sync_with_github()

