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
import urllib.request
import subprocess
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
    SUPPORTED_PLATFORMS
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
    get_budapest_timezone,
    DEFAULT_QUEUE_FILE,
    DEFAULT_LOGS_DIR,
    DEFAULT_LOG_FILE,
    DEFAULT_TIMEZONE,
    SLOT_1_TIME,
    SLOT_2_TIME,
    STATUS_READY,
    STATUS_PUBLISHED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_PARTIALLY_PUBLISHED,
    STATUS_SKIPPED,
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
    """Format a datetime in Europe/Budapest as 'Nov 4, 2026, 13:00'."""
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
    """Normalize one active platform result for truthful UI rendering."""
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

    scheduled_dt = parse_product_scheduled_dt(
        scheduled_at=item.scheduled_at,
        scheduled_date=item.scheduled_date,
        daily_slot=item.daily_slot,
        tz=tz,
    )
    is_future = scheduled_dt is not None and scheduled_dt > now_dt
    time_has_passed = scheduled_dt is not None and scheduled_dt <= now_dt

    state = "UNKNOWN"
    historical_dry_run_ambiguous = _has_ambiguous_historical_dry_run(item)
    if result:
        result_status = result.get("status")
        mode = result.get("mode")
        if _is_confirmed_live_result(result):
            state = "PUBLISHED"
        elif result_status == "FAILED" and (time_has_passed or not is_future):
            state = "FAILED"
        elif is_future:
            claim = (item._ui_claim if hasattr(item, "_ui_claim") else {}) or {}
            state = "PUBLISHING" if claim.get("state") == "CLAIMED" else "SCHEDULED"
        elif historical_dry_run_ambiguous and (result_status == "WOULD_PUBLISH" or mode == "DRY_RUN"):
            state = "UNKNOWN"
        elif result_status == "WOULD_PUBLISH" or mode == "DRY_RUN":
            state = "SIMULATED"
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
    live_confirmed = state == "PUBLISHED"
    result = result or {}

    ui_label = {
        "PUBLISHED": "Published",
        "FAILED": "Failed",
        "SIMULATED": "Not published",
        "SCHEDULED": "Scheduled",
        "PUBLISHING": "Publishing",
        "UNKNOWN": "Awaiting confirmation",
    }.get(state, state.replace("_", " ").capitalize())

    return {
        "platform": platform,
        "label": {
            "facebook": "Facebook",
            "pinterest": "Pinterest",
            "pinterest_lifemodehq": "Pinterest LifeModeHQ",
            "threads": "Threads",
        }.get(platform, platform.capitalize()),
        "state": state,
        "ui_label": ui_label,
        "mode": result.get("mode"),
        "timestamp": result.get("published_at") if live_confirmed else None,
        "post_id": result.get("post_id") or result.get("pin_id") or result.get("photo_id"),
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

    scheduled_dt = parse_product_scheduled_dt(
        scheduled_at=item.scheduled_at,
        scheduled_date=item.scheduled_date,
        daily_slot=item.daily_slot,
        tz=tz,
    )
    is_future = scheduled_dt is not None and scheduled_dt > now_dt
    time_has_passed = scheduled_dt is not None and scheduled_dt <= now_dt
    is_overdue = is_item_overdue_held(item, now_dt)

    if states and all(s == "PUBLISHED" for s in states):
        state = "PUBLISHED"
        ui_label = "Published"
    elif has_live and (has_failed or not all(s == "PUBLISHED" for s in states)):
        state = "PARTIAL"
        ui_label = "Partial"
    elif item.status == STATUS_PARTIALLY_PUBLISHED:
        state = "PARTIAL"
        ui_label = "Partial"
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

    return {
        "state": state,
        "ui_label": ui_label,
        "is_overdue": is_overdue,
        "retryable": claim.get("state") == "RETRYABLE",
        "published_at": max(
            (result.get("timestamp") for result in platform_results.values() if result.get("timestamp")),
            default=None,
        ) if state in ("PUBLISHED", "PARTIAL") else None,
        "platforms": platform_results,
        "errors": item.errors or [],
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
                        "product_file": str(pfile)
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
            except Exception:
                continue

    # 3. Read Queue for scheduled items
    if QUEUE_FILE.exists():
        try:
            queue = PublishingQueue.load(QUEUE_FILE)
            for item in queue.items:
                asin = (item.asin or "").upper()
                if not asin:
                    continue
                presentation = get_queue_item_presentation(queue, item)
                if asin not in products_by_asin:
                    products_by_asin[asin] = {
                        "asin": asin,
                        "title": item.product_name,
                        "affiliate_url": item.original_url,
                        "source_status": "Queue Source",
                        "facts_count": 0,
                        "facts_status": "UNKNOWN",
                        "content_status": "GENERATED" if item.package_reference else "NOT_GENERATED",
                        "validation_status": item.validation_status or "UNKNOWN",
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
                        "package_path": item.package_reference,
                        "product_file": None
                    }
                else:
                    products_by_asin[asin]["queue_status"] = item.status
                    products_by_asin[asin]["scheduled_date"] = item.scheduled_date
                    products_by_asin[asin]["daily_slot"] = item.daily_slot
                    products_by_asin[asin]["scheduled_at"] = item.scheduled_at
                    products_by_asin[asin]["validation_status"] = item.validation_status or products_by_asin[asin]["validation_status"]
                    products_by_asin[asin]["publishing_status"] = presentation["state"]
                    products_by_asin[asin]["publishing_status_display"] = presentation["ui_label"]
                    products_by_asin[asin]["ui_label"] = presentation["ui_label"]
                    products_by_asin[asin]["publishing_results"] = presentation["platforms"]
                    products_by_asin[asin]["published_at"] = presentation["published_at"]
                    products_by_asin[asin]["retryable"] = presentation["retryable"]
                    if item.package_reference:
                        products_by_asin[asin]["package_path"] = item.package_reference
        except Exception:
            pass

    # Convert to list and sort
    results = list(products_by_asin.values())
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
        if product_row.get("publishing_status") not in ("SCHEDULED", "SIMULATED", "NOT_PUBLISHED", "PUBLISHING", "PUBLISHED", "PARTIAL", "FAILED", "UNKNOWN"):
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
    if QUEUE_FILE.exists():
        try:
            queue = PublishingQueue.load(QUEUE_FILE)
            for it in queue.items:
                if (it.asin or "").upper() == asin_clean:
                    pres = get_queue_item_presentation(queue, it)
                    queue_item = {
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
                        "publishing_results": it.publishing_results
                    }
                    break
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
        for p_name in SUPPORTED_PLATFORMS:
            p_data = package["platforms"].get(p_name, {})
            val_meta = p_data.get("validation", {})
            platforms_content[p_name] = {
                "name": p_name.capitalize(),
                "status": p_data.get("status", "MISSING"),
                "content": p_data.get("content", ""),
                "title": p_data.get("title", ""),
                "description": p_data.get("description", ""),
                "image_url": p_data.get("image_url", ""),
                "link": p_data.get("link", ""),
                "validation_status": val_meta.get("status", p_data.get("status", "UNKNOWN")),
                "errors": val_meta.get("errors", []),
                "retry_count": val_meta.get("retry_count", 0)
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
        "status": package.get("status") if package else (queue_item.get("status") if queue_item else "UNKNOWN"),
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
    return {
        "success": success,
        "message": msg,
        "asin": asin_clean,
        "creative_type": creative_type,
        "data": manifest,
    }


def select_creative_service(
    asin: str,
    creative_type: Optional[str]
) -> Dict[str, Any]:
    """Select or deselect a creative for Pinterest publishing (exactly one selected per ASIN)."""
    asin_clean = asin.upper().strip()
    success, msg, manifest = set_selected_creative(asin=asin_clean, creative_type=creative_type)
    return {
        "success": success,
        "message": msg,
        "asin": asin_clean,
        "creative_type": creative_type,
        "data": manifest,
    }


def get_reference_image_service(asin: str) -> Optional[Path]:
    """Resolve and return path to local reference product image."""
    return get_or_download_reference_image(asin)


def add_products_from_urls(urls_text: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Process manual Amazon affiliate URL(s) submission.
    Pipeline: URL -> Product Resolution -> CanonicalProductFacts -> ContentGenerator -> Validator -> ContentPackage -> Queue.
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
                if loaded_pkg.get("status") == STATUS_READY_FOR_REVIEW:
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
                social_data = generator.generate_all_platforms(product=facts, disclosure=disclosure)
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
            batch_id=batch_id
        )

        queue.items.append(new_queue_item)
        queue.recalculate_counts()
        existing_asins.add(asin_final)
        existing_urls.add(url_clean)

        accepted_count += 1
        results.append({
            "url": url_clean,
            "asin": asin_final,
            "title": product.title,
            "status": "ACCEPTED",
            "scheduled_date": next_slot_info["date"],
            "daily_slot": next_slot_info["slot"],
            "scheduled_at": next_slot_info["scheduled_at"],
            "package_path": pkg_path_str,
            "queue_item_id": item_id
        })

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

    for p_name in SUPPORTED_PLATFORMS:
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
        social_data = generator.generate_all_platforms(product=facts, disclosure=disclosure)
    except Exception as e:
        return {"success": False, "message": f"Content generation error: {str(e)}"}

    cache_key = f"asin_{asin_clean}"
    pkg = ContentPackage.build_package(
        product=product,
        social_data=social_data,
        cache_file_path=f"data/products/{cache_key}.json",
        model_name=provider.model
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

    if not QUEUE_FILE.exists():
        return {"success": False, "error": f"Publishing queue not found at {QUEUE_FILE}."}

    try:
        queue = PublishingQueue.load(QUEUE_FILE)
    except Exception as exc:
        return {"success": False, "error": f"Could not load publishing queue: {exc}"}

    item = next((candidate for candidate in queue.items if candidate.queue_item_id == queue_item_id), None)
    if item is None:
        return {"success": False, "error": f"Queue item '{queue_item_id}' was not found."}

    presentation = get_queue_item_presentation(queue, item)
    if presentation["state"] == "PUBLISHED":
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

    cfg = config or load_application_config()
    publisher = SocialPublisher(
        config=PublishingConfig.from_dict(cfg, dry_run_override=False)
    )
    try:
        report = publisher.publish_package(
            package,
            dry_run=False,
            prior_results=item.publishing_results,
        )
    except Exception as exc:
        return {"success": False, "error": f"Live publisher failed before platform execution: {exc}", "error_type": "publisher"}

    platform_results = report.get("results", {})

    def confirmed_live(result: Dict[str, Any]) -> bool:
        return (
            result.get("status") == "PUBLISHED"
            and result.get("mode") == "LIVE"
            and bool(
                result.get("post_id")
                or result.get("pin_id")
                or result.get("photo_id")
                or result.get("permalink")
                or result.get("permalink_url")
                or result.get("pin_url")
            )
        )

    # Preserve confirmed successes even if a retry report omits that target or
    # returns a contradictory failure.  The existing publisher normally emits
    # SKIP_DUPLICATE itself; this defensive merge keeps persistence idempotent.
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

    confirmed_count = sum(1 for result in active_results.values() if confirmed_live(result))
    failed_count = sum(1 for result in active_results.values() if result.get("status") == "FAILED")
    if active_results and confirmed_count == len(active_results):
        item.status = STATUS_PUBLISHED
    elif confirmed_count > 0:
        item.status = STATUS_PARTIALLY_PUBLISHED
    else:
        item.status = STATUS_FAILED

    errors = []
    for result in active_results.values():
        if not confirmed_live(result):
            errors.extend(result.get("errors") or [])
    item.errors = list(dict.fromkeys(errors))
    if confirmed_count:
        item.published_at = now_dt
    item.publishing_results = platform_results
    queue.recalculate_counts()
    queue.save(QUEUE_FILE)

    return {
        "success": True,
        "item_id": item.queue_item_id,
        "asin": item.asin,
        "mode": "LIVE",
        "status": item.status,
        "confirmed_platforms": confirmed_count,
        "failed_platforms": failed_count,
        "platforms": get_queue_item_presentation(queue, item)["platforms"],
    }


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

