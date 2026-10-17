"""
Publishing Queue and Daily Scheduler module for AmazonAffiliateBot (V4/V5).
Provides deterministic 30-day multi-product scheduling, capacity management,
URL list parsing with deduplication, product isolation, local JSON persistence,
and daily scheduled publishing with Europe/Budapest timezone handling.
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional, Tuple, Union, Callable, Set
from pathlib import Path
from datetime import datetime, date, time, timedelta, timezone, tzinfo
import json
import re
import os

from zoneinfo import ZoneInfo

from amazon.scraper import Product, extract_asin_from_url, is_valid_image_url
from ai.ollama import OllamaClient
from content.generator import ContentGenerator
from content.package import ContentPackage, STATUS_READY_FOR_REVIEW, STATUS_FAILED_VALIDATION
from publishing.publisher import (
    SocialPublisher,
    PublishingConfig,
    STATUS_WOULD_PUBLISH,
    STATUS_PUBLISHED as PLATFORM_STATUS_PUBLISHED,
    STATUS_FAILED as PLATFORM_STATUS_FAILED,
    STATUS_SKIPPED as PLATFORM_STATUS_SKIPPED,
)
from publishing.images import recover_verified_image_url


DEFAULT_QUEUE_DIR = Path("data/queue")
DEFAULT_QUEUE_FILE = DEFAULT_QUEUE_DIR / "publishing_queue.json"
DEFAULT_INPUT_DIR = Path("data/input")
DEFAULT_AFFILIATE_URLS_FILE = DEFAULT_INPUT_DIR / "affiliate_urls.txt"
AUTHORITATIVE_SOURCE_FILE = Path(r"C:\Users\opeti\LifeModeHub\Amazon linkek.txt")
DEFAULT_LOGS_DIR = Path("data/logs")
DEFAULT_LOG_FILE = DEFAULT_LOGS_DIR / "daily_publisher.log"

DEFAULT_SCHEDULE_DAYS = 30
DEFAULT_PRODUCTS_PER_DAY = 2
DEFAULT_TIMEZONE = "Europe/Budapest"

# Slot times in Europe/Budapest
SLOT_1_TIME = time(12, 0, 0)  # 12:00 (European audience focus)
SLOT_2_TIME = time(19, 0, 0)  # 19:00 (North American audience / Pinterest focus)

STATUS_QUEUED = "QUEUED"
STATUS_READY = "READY"
STATUS_FAILED = "FAILED"
STATUS_PUBLISHED = "PUBLISHED"
STATUS_PARTIALLY_PUBLISHED = "PARTIALLY_PUBLISHED"
STATUS_SKIPPED = "SKIPPED"


def get_budapest_timezone() -> ZoneInfo:
    """Return standard IANA ZoneInfo object for Europe/Budapest."""
    return ZoneInfo(DEFAULT_TIMEZONE)


def get_authoritative_urls_file() -> Path:
    """Return the authoritative Amazon affiliate URLs source file path."""
    if AUTHORITATIVE_SOURCE_FILE.exists():
        return AUTHORITATIVE_SOURCE_FILE
    return DEFAULT_AFFILIATE_URLS_FILE


def resolve_product_for_url(
    url: str,
    products_dir: Optional[Union[str, Path]] = None,
    allow_scrape: bool = False
) -> Tuple[bool, Optional[Product], Optional[str], List[str]]:
    """
    Reliably resolve a product on disk or via scraper strictly attached to the given URL.
    Returns:
        (is_valid, product, cache_key, error_list)
    """
    pdir = Path(products_dir) if products_dir else Path("data/products")
    url_clean = url.strip()

    # 1. Search cached products for matching affiliate_url or source_url
    if pdir.exists():
        for pfile in sorted(pdir.glob("*.json")):
            try:
                with open(pfile, "r", encoding="utf-8") as f:
                    pdata = json.load(f)
                aff_url = (pdata.get("affiliate_url") or "").strip()
                src_url = (pdata.get("source_url") or "").strip()
                if aff_url == url_clean or src_url == url_clean:
                    prod = Product.from_dict(pdata)
                    prod.affiliate_url = url_clean
                    asin = (prod.asin or "").upper()
                    if not asin or asin == "UNKNOWN":
                        return False, prod, pfile.stem, [f"Product for '{url_clean}' has unresolvable ASIN."]
                    if not prod.title:
                        return False, prod, pfile.stem, [f"Product for '{url_clean}' has empty title."]
                    if not prod.features or len(prod.features) == 0:
                        return False, prod, pfile.stem, [f"Product for '{url_clean}' has insufficient/empty features."]
                    if not is_valid_image_url(prod.image_url):
                        image_error = (
                            f"Product for '{url_clean}' has no verified HTTPS image URL in the same-ASIN cache; "
                            "re-scraping is required before publishing."
                        )
                        if not allow_scrape:
                            return False, prod, pfile.stem, [image_error]
                        # Ignore the stale cache entry and let the scrape path recover the image.
                        continue
                    return True, prod, pfile.stem, []
            except Exception:
                continue

    # 2. If not found in cache and scraping is allowed
    if allow_scrape:
        try:
            from amazon.scraper import scrape_amazon_product
            prod = scrape_amazon_product(url_clean, headless=True)
            prod.affiliate_url = url_clean
            if not prod.asin or prod.asin == "UNKNOWN":
                return False, prod, None, [f"Scraped product for '{url_clean}' has missing ASIN."]
            if not prod.title or not prod.features:
                return False, prod, None, [f"Scraped product for '{url_clean}' has insufficient title or features."]
            if not is_valid_image_url(prod.image_url):
                return False, prod, None, [
                    f"Scraped product for '{url_clean}' has no verified HTTPS image URL; "
                    "the product was not cached or queued."
                ]
            key = f"asin_{prod.asin.upper()}"
            save_file = pdir / f"{key}.json"
            save_file.parent.mkdir(parents=True, exist_ok=True)
            with open(save_file, "w", encoding="utf-8") as f:
                json.dump(prod.to_dict(), f, indent=2, ensure_ascii=False)
            return True, prod, key, []
        except Exception as e:
            return False, None, None, [f"Scraping failed for '{url_clean}': {e}"]

    return False, None, None, [f"No verified product found in local cache for '{url_clean}'."]


def recover_pending_queue_images(
    queue: "PublishingQueue",
    products_dir: Optional[Union[str, Path]] = None,
    generated_dir: Optional[Union[str, Path]] = None,
    allow_scrape: bool = False,
) -> Dict[str, Any]:
    """Repair image-blocked unpublished queue items without retrying publications."""
    pdir = Path(products_dir) if products_dir else Path("data/products")
    gdir = Path(generated_dir) if generated_dir else Path("data/generated")
    report: Dict[str, Any] = {
        "examined": 0,
        "already_valid": [],
        "recovered": [],
        "already_published": [],
        "intentionally_skipped": [],
        "permanently_failed": [],
        "unresolved": [],
    }

    for item in queue.items:
        if not item.asin or item.asin.upper() == "UNKNOWN":
            if item.status == STATUS_FAILED:
                report["permanently_failed"].append(item.asin or "UNKNOWN")
            continue

        if item.status in (STATUS_PUBLISHED, STATUS_PARTIALLY_PUBLISHED):
            report["already_published"].append(item.asin.upper())
            continue
        if item.status == STATUS_SKIPPED and not item.published_at:
            # Preserve intentional skips. Only image-preflight skips are eligible
            # for automatic recovery; an empty error list is intentional state.
            image_preflight_skip = any(
                isinstance(error, str)
                and ("image" in error.lower() or "pinterest" in error.lower())
                for error in (item.errors or [])
            )
            if not image_preflight_skip:
                report["intentionally_skipped"].append(item.asin.upper())
                continue
        elif item.status not in (STATUS_READY, STATUS_FAILED, STATUS_SKIPPED):
            continue

        report["examined"] += 1
        asin = item.asin.upper()
        product_path = pdir / f"asin_{asin}.json"
        package_path = gdir / f"asin_{asin}_content_package.json"
        package: Dict[str, Any] = {}
        if package_path.exists():
            try:
                with package_path.open("r", encoding="utf-8") as handle:
                    package = json.load(handle)
            except (OSError, ValueError, TypeError):
                package = {}

        image_url = recover_verified_image_url(package, products_dir=pdir) if package else None
        product: Optional[Product] = None
        if product_path.exists():
            try:
                with product_path.open("r", encoding="utf-8") as handle:
                    product = Product.from_dict(json.load(handle))
            except (OSError, ValueError, TypeError):
                product = None

        if not image_url and item.original_url:
            ok, resolved, _, errors = resolve_product_for_url(
                item.original_url,
                products_dir=pdir,
                allow_scrape=allow_scrape,
            )
            if ok and resolved:
                product = resolved
                image_url = resolved.image_url
            elif not package.get("platforms"):
                report["unresolved"].append({"asin": asin, "reason": errors[0] if errors else "No verified image found."})
                continue

        if not is_valid_image_url(image_url):
            report["unresolved"].append({
                "asin": asin,
                "reason": "No verified HTTPS image URL in package, same-ASIN cache, or recovery scrape.",
            })
            continue

        package_pin = (package.get("platforms", {}).get("pinterest") or {}) if package else {}
        if (
            product is not None
            and is_valid_image_url(product.image_url)
            and is_valid_image_url(package_pin.get("image_url"))
            and package.get("status") == STATUS_READY_FOR_REVIEW
        ):
            report["already_valid"].append(asin)
            continue

        if package.get("status") != STATUS_READY_FOR_REVIEW:
            report["unresolved"].append({
                "asin": asin,
                "reason": f"Content package status is '{package.get('status')}', so it was not requeued.",
            })
            continue

        # Persist a recovered package image and cache image before changing queue state.
        if product is not None:
            product.image_url = image_url
            product_path.parent.mkdir(parents=True, exist_ok=True)
            with product_path.open("w", encoding="utf-8") as handle:
                json.dump(product.to_dict(), handle, indent=2, ensure_ascii=False)
        if not package:
            report["unresolved"].append({"asin": asin, "reason": "Content package is missing; image recovery cannot safely requeue it."})
            continue
        if not ContentPackage.attach_verified_image(package, image_url):
            report["unresolved"].append({"asin": asin, "reason": "Recovered image failed verified HTTPS validation."})
            continue
        package_path.parent.mkdir(parents=True, exist_ok=True)
        with package_path.open("w", encoding="utf-8") as handle:
            json.dump(package, handle, indent=2, ensure_ascii=False)

        has_published_platform = any(
            isinstance(result, dict) and result.get("status") == PLATFORM_STATUS_PUBLISHED
            for result in (item.publishing_results or {}).values()
        )
        if not has_published_platform and item.status in (STATUS_FAILED, STATUS_SKIPPED):
            item.status = STATUS_READY
            item.validation_status = STATUS_READY_FOR_REVIEW
            item.errors = []
            item.published_at = None
            item.publishing_results = None
        report["recovered"].append(asin)

    queue.recalculate_counts()
    return report


def parse_affiliate_urls(
    source: Union[str, Path],
    is_file_path: bool = True
) -> List[str]:
    """
    Parse a list of Amazon affiliate URLs from a file or multiline string.
    - Ignores blank lines.
    - Ignores comment lines starting with '#'.
    - Preserves every affiliate URL exactly as supplied without modification or regeneration.
    - Deterministically deduplicates identical URLs while preserving first appearance order.
    """
    lines: List[str] = []
    if is_file_path:
        p = Path(source)
        if not p.exists():
            return []
        with open(p, "r", encoding="utf-8") as f:
            lines = f.readlines()
    else:
        lines = str(source).splitlines()

    unique_urls: List[str] = []
    seen: Set[str] = set()

    for raw_line in lines:
        cleaned = raw_line.strip()
        if not cleaned or cleaned.startswith("#"):
            continue
        if cleaned not in seen:
            seen.add(cleaned)
            unique_urls.append(cleaned)

    return unique_urls


@dataclass
class QueueItem:
    """
    Deterministic item entry in the Publishing Queue.
    Includes concrete Europe/Budapest scheduled datetime, 7-hour slot separation, and publishing results.
    """
    queue_item_id: str
    original_url: str
    asin: str = ""
    product_name: str = ""
    scheduled_date: Optional[str] = None       # YYYY-MM-DD
    daily_slot: Optional[int] = None           # 1 (12:00) or 2 (19:00)
    scheduled_at: Optional[str] = None         # ISO 8601 string e.g. 2026-10-07T12:00:00+02:00
    timezone: str = DEFAULT_TIMEZONE           # Europe/Budapest
    package_reference: Optional[str] = None
    status: str = STATUS_QUEUED                # READY, FAILED, PUBLISHED, PARTIALLY_PUBLISHED, QUEUED, SKIPPED
    validation_status: str = STATUS_FAILED_VALIDATION
    errors: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    published_at: Optional[str] = None
    publishing_results: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "queue_item_id": self.queue_item_id,
            "original_url": self.original_url,
            "asin": self.asin,
            "product_name": self.product_name,
            "scheduled_date": self.scheduled_date,
            "daily_slot": self.daily_slot,
            "scheduled_at": self.scheduled_at,
            "timezone": self.timezone,
            "package_reference": self.package_reference,
            "status": self.status,
            "validation_status": self.validation_status,
            "errors": self.errors,
            "created_at": self.created_at,
            "published_at": self.published_at,
            "publishing_results": self.publishing_results,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "QueueItem":
        scheduled_date = data.get("scheduled_date")
        daily_slot = data.get("daily_slot")
        scheduled_at = data.get("scheduled_at")
        tz_name = data.get("timezone", DEFAULT_TIMEZONE)

        # Compute scheduled_at if missing but date and slot are present
        if not scheduled_at and scheduled_date and daily_slot:
            try:
                base_d = datetime.strptime(scheduled_date, "%Y-%m-%d").date()
                slot_t = SLOT_1_TIME if daily_slot == 1 else SLOT_2_TIME
                tz = get_budapest_timezone()
                dt = datetime(base_d.year, base_d.month, base_d.day, slot_t.hour, slot_t.minute, slot_t.second, tzinfo=tz)
                scheduled_at = dt.isoformat()
            except Exception:
                pass

        return cls(
            queue_item_id=data.get("queue_item_id", ""),
            original_url=data.get("original_url", ""),
            asin=data.get("asin", ""),
            product_name=data.get("product_name", ""),
            scheduled_date=scheduled_date,
            daily_slot=daily_slot,
            scheduled_at=scheduled_at,
            timezone=tz_name,
            package_reference=data.get("package_reference"),
            status=data.get("status", STATUS_QUEUED),
            validation_status=data.get("validation_status", STATUS_FAILED_VALIDATION),
            errors=data.get("errors", []),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            published_at=data.get("published_at"),
            publishing_results=data.get("publishing_results"),
        )


@dataclass
class PublishingQueue:
    """
    Deterministic Publishing Queue for 30-day multi-product scheduling.
    """
    queue_id: str
    created_at: str
    start_date: str
    total_days: int = DEFAULT_SCHEDULE_DAYS
    products_per_day: int = DEFAULT_PRODUCTS_PER_DAY
    target_capacity: int = DEFAULT_SCHEDULE_DAYS * DEFAULT_PRODUCTS_PER_DAY
    total_submitted: int = 0
    total_processed: int = 0
    total_ready: int = 0
    total_failed: int = 0
    scheduled_posts: int = 0
    unfilled_slots: int = 0
    # Repository-persisted claims prevent repeated scheduled runs from
    # re-entering the same Budapest slot.  Values are keyed as YYYY-MM-DD-slot-N.
    slot_claims: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    items: List[QueueItem] = field(default_factory=list)

    def recalculate_counts(self) -> None:
        """Derive queue summary counters directly from actual item states."""
        self.total_processed = len(self.items)
        self.total_ready = len([i for i in self.items if i.status == STATUS_READY])
        self.total_failed = len([i for i in self.items if i.status == STATUS_FAILED])
        self.scheduled_posts = len([i for i in self.items if i.status == STATUS_READY and i.scheduled_at])
        self.unfilled_slots = max(0, self.target_capacity - self.scheduled_posts)

    def summary(self) -> Dict[str, Any]:
        """Return a structured summary dictionary."""
        self.recalculate_counts()
        published_count = len([i for i in self.items if i.status == STATUS_PUBLISHED])
        ready_count = len([i for i in self.items if i.status == STATUS_READY])
        return {
            "queue_id": self.queue_id,
            "start_date": self.start_date,
            "total_days": self.total_days,
            "products_per_day": self.products_per_day,
            "target_capacity": self.target_capacity,
            "total_submitted": self.total_submitted,
            "total_processed": self.total_processed,
            "total_ready": self.total_ready,
            "total_failed": self.total_failed,
            "scheduled_posts": self.scheduled_posts,
            "published_posts": published_count,
            "unfilled_slots": self.unfilled_slots,
            "active_days": len({item.scheduled_date for item in self.items if item.scheduled_date and item.status in (STATUS_READY, STATUS_PUBLISHED)}),
        }

    def format_summary(self) -> str:
        """Format concise human-readable summary matching specification."""
        self.recalculate_counts()
        active_days = len({item.scheduled_date for item in self.items if item.scheduled_date and item.status in (STATUS_READY, STATUS_PUBLISHED)})
        published_count = len([i for i in self.items if i.status == STATUS_PUBLISHED])
        return (
            f"Products submitted: {self.total_submitted}\n"
            f"Products processed: {self.total_processed}\n"
            f"READY: {self.total_ready}\n"
            f"PUBLISHED: {published_count}\n"
            f"FAILED: {self.total_failed}\n"
            f"Queue days: {active_days}\n"
            f"Scheduled posts: {self.scheduled_posts}\n"
            f"Unfilled slots: {self.unfilled_slots}"
        )

    def to_dict(self) -> Dict[str, Any]:
        self.recalculate_counts()
        return {
            "queue_id": self.queue_id,
            "created_at": self.created_at,
            "start_date": self.start_date,
            "total_days": self.total_days,
            "products_per_day": self.products_per_day,
            "target_capacity": self.target_capacity,
            "total_submitted": self.total_submitted,
            "total_processed": self.total_processed,
            "total_ready": self.total_ready,
            "total_failed": self.total_failed,
            "scheduled_posts": self.scheduled_posts,
            "unfilled_slots": self.unfilled_slots,
            "slot_claims": self.slot_claims,
            "items": [item.to_dict() for item in self.items]
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PublishingQueue":
        items = [QueueItem.from_dict(i) for i in data.get("items", [])]
        return cls(
            queue_id=data.get("queue_id", ""),
            created_at=data.get("created_at", datetime.now(timezone.utc).isoformat()),
            start_date=data.get("start_date", ""),
            total_days=data.get("total_days", DEFAULT_SCHEDULE_DAYS),
            products_per_day=data.get("products_per_day", DEFAULT_PRODUCTS_PER_DAY),
            target_capacity=data.get("target_capacity", DEFAULT_SCHEDULE_DAYS * DEFAULT_PRODUCTS_PER_DAY),
            total_submitted=data.get("total_submitted", 0),
            total_processed=data.get("total_processed", 0),
            total_ready=data.get("total_ready", 0),
            total_failed=data.get("total_failed", 0),
            scheduled_posts=data.get("scheduled_posts", 0),
            unfilled_slots=data.get("unfilled_slots", 0),
            slot_claims=data.get("slot_claims", {}),
            items=items
        )

    def save(self, file_path: Optional[Union[str, Path]] = None) -> Path:
        """Persist the PublishingQueue to a local JSON file."""
        self.recalculate_counts()
        target_path = Path(file_path) if file_path else DEFAULT_QUEUE_FILE
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with open(target_path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)
        return target_path

    @classmethod
    def load(cls, file_path: Optional[Union[str, Path]] = None) -> "PublishingQueue":
        """Load a PublishingQueue from a local JSON file."""
        target_path = Path(file_path) if file_path else DEFAULT_QUEUE_FILE
        with open(target_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)


class QueueBuilder:
    """
    Deterministic builder for 30-day publishing queues.
    Orchestrates isolated product processing, canonical fact extraction,
    multi-platform content generation, validation, package creation, and date/slot scheduling.
    """

    @staticmethod
    def compute_schedule_slots(
        start_date: str,
        total_days: int = DEFAULT_SCHEDULE_DAYS,
        products_per_day: int = DEFAULT_PRODUCTS_PER_DAY,
        tz_name: str = DEFAULT_TIMEZONE
    ) -> List[Dict[str, Any]]:
        """
        Generate deterministic list of slot dictionaries:
        - Slot 1: 12:00 Europe/Budapest
        - Slot 2: 19:00 Europe/Budapest (7-hour separation)
        Handles DST transitions accurately through Europe/Budapest.
        """
        base_date = datetime.strptime(start_date, "%Y-%m-%d").date()
        tz = get_budapest_timezone()
        slots: List[Dict[str, Any]] = []

        for day_idx in range(total_days):
            current_date = base_date + timedelta(days=day_idx)
            current_date_str = current_date.strftime("%Y-%m-%d")

            # Slot 1: 12:00 Europe/Budapest
            dt_slot1 = datetime(current_date.year, current_date.month, current_date.day, SLOT_1_TIME.hour, SLOT_1_TIME.minute, SLOT_1_TIME.second, tzinfo=tz)
            slots.append({
                "date": current_date_str,
                "slot": 1,
                "scheduled_at": dt_slot1.isoformat(),
                "timezone": tz_name,
            })

            # Slot 2: 19:00 Europe/Budapest (if products_per_day >= 2)
            if products_per_day >= 2:
                dt_slot2 = datetime(current_date.year, current_date.month, current_date.day, SLOT_2_TIME.hour, SLOT_2_TIME.minute, SLOT_2_TIME.second, tzinfo=tz)
                slots.append({
                    "date": current_date_str,
                    "slot": 2,
                    "scheduled_at": dt_slot2.isoformat(),
                    "timezone": tz_name,
                })

            # Additional slots if configured
            for extra_slot in range(3, products_per_day + 1):
                extra_hour = min(23, 19 + (extra_slot - 2))
                dt_extra = datetime(current_date.year, current_date.month, current_date.day, extra_hour, 0, 0, tzinfo=tz)
                slots.append({
                    "date": current_date_str,
                    "slot": extra_slot,
                    "scheduled_at": dt_extra.isoformat(),
                    "timezone": tz_name,
                })

        return slots

    @classmethod
    def build_queue(
        cls,
        urls: Optional[List[str]] = None,
        start_date: Optional[str] = None,
        total_days: int = DEFAULT_SCHEDULE_DAYS,
        products_per_day: int = DEFAULT_PRODUCTS_PER_DAY,
        product_processor: Optional[Callable[[str], Tuple[bool, Optional[Product], Optional[Dict[str, Any]], Optional[str], List[str]]]] = None,
        queue_id: Optional[str] = None,
        output_file: Optional[Union[str, Path]] = None,
        preserve_existing_history: bool = True,
    ) -> PublishingQueue:
        """
        Build and persist a deterministic PublishingQueue from a list of affiliate URLs.
        Uses authoritative URL source when urls is None.

        Args:
            urls: Deduplicated list of Amazon affiliate URLs (or None to use authoritative source).
            start_date: Start date YYYY-MM-DD (defaults to tomorrow).
            total_days: Number of days in schedule (default: 30).
            products_per_day: Number of products per day (default: 2).
            product_processor: Optional custom processor function for testing/injection.
                               Signature: url -> (is_valid, product, package, package_path_str, errors)
            queue_id: Optional deterministic queue identifier.
            output_file: Optional path to save the queue JSON.
            preserve_existing_history: Whether to preserve published/historical records in existing queue.

        Returns:
            PublishingQueue: The built and populated queue.
        """
        if urls is None:
            auth_file = get_authoritative_urls_file()
            urls = parse_affiliate_urls(auth_file)

        if not start_date:
            tomorrow = datetime.now(timezone.utc).date() + timedelta(days=1)
            start_date = tomorrow.strftime("%Y-%m-%d")

        total_capacity = total_days * products_per_day
        slots = cls.compute_schedule_slots(start_date, total_days, products_per_day)

        target_file = Path(output_file) if output_file else DEFAULT_QUEUE_FILE
        historical_published: Dict[str, QueueItem] = {}
        historical_other: List[QueueItem] = []

        if target_file.exists() and preserve_existing_history:
            try:
                old_queue = PublishingQueue.load(target_file)
                for itm in old_queue.items:
                    if itm.status == STATUS_PUBLISHED and itm.published_at:
                        historical_published[itm.original_url] = itm
                    elif itm.original_url not in urls:
                        historical_other.append(itm)
            except Exception:
                pass

        total_submitted = len(urls)
        queue_items: List[QueueItem] = []
        ready_count = 0
        failed_count = 0
        slot_pointer = 0

        q_id = queue_id or f"queue_{start_date.replace('-', '')}_{total_days}d"

        # Prepend historical items preserved from earlier runs
        for itm in historical_other:
            queue_items.append(itm)

        for idx, url in enumerate(urls, start=1):
            # Check if this exact URL has already been successfully published
            if url in historical_published:
                pub_item = historical_published[url]
                queue_items.append(pub_item)
                continue

            if product_processor:
                is_valid, prod, package, pkg_path, errs = product_processor(url)
            else:
                from amazon_affiliate import load_config
                from ai.ollama import OllamaClient
                from content.generator import ContentGenerator

                is_valid_prod, prod, found_key, prod_errs = resolve_product_for_url(url, allow_scrape=False)
                if not is_valid_prod or not prod:
                    is_valid = False
                    package = None
                    pkg_path = None
                    errs = prod_errs if prod_errs else [f"Could not resolve product for '{url}'."]
                else:
                    asin_key = f"asin_{prod.asin.upper()}"
                    pkg_candidate = Path("data/generated") / f"{asin_key}_content_package.json"
                    package = None
                    pkg_path = None
                    if pkg_candidate.exists():
                        try:
                            with open(pkg_candidate, "r", encoding="utf-8") as pf:
                                cand_pkg = json.load(pf)
                            if cand_pkg.get("status") == STATUS_READY_FOR_REVIEW:
                                package = cand_pkg
                                pkg_path = str(pkg_candidate).replace("\\", "/")
                                is_valid = True
                                errs = []
                        except Exception:
                            package = None

                    if not package:
                        try:
                            config = load_config()
                            ollama = OllamaClient(
                                base_url=config.get("ollama", {}).get("base_url", "http://localhost:11434"),
                                model=config.get("ollama", {}).get("model", "qwen2.5:7b"),
                                timeout=config.get("ollama", {}).get("timeout", 300)
                            )
                            generator = ContentGenerator(ollama_client=ollama)
                            disclosure = config.get("affiliate", {}).get(
                                "disclosure",
                                "This post contains affiliate links. As an Amazon Associate I earn from qualifying purchases."
                            )
                            social_data = generator.generate_all_platforms(product=prod, disclosure=disclosure)
                            ContentGenerator.save_social_content(social_data, asin_key)
                            package = ContentPackage.build_package(
                                product=prod,
                                social_data=social_data,
                                cache_file_path=f"data/products/{asin_key}.json",
                                model_name=ollama.model
                            )
                            pkg_file = ContentPackage.save_content_package(package, asin_key)
                            pkg_path = str(pkg_file).replace("\\", "/")
                            is_valid = (package.get("status") == STATUS_READY_FOR_REVIEW)
                            errs = [] if is_valid else ["ContentPackage failed validation."]
                        except Exception as e:
                            package = None
                            pkg_path = None
                            is_valid = False
                            errs = [f"Generation error: {str(e)}"]

            # Evaluate product result
            asin = getattr(prod, "asin", "") or extract_asin_from_url(url) or ""
            product_name = getattr(prod, "title", "") if prod else ""
            item_id = f"item_{len(queue_items) + 1:03d}_{asin or 'unknown'}"

            if is_valid and package and package.get("status") == STATUS_READY_FOR_REVIEW:
                if slot_pointer < len(slots):
                    slot_info = slots[slot_pointer]
                    slot_pointer += 1
                    sched_date = slot_info["date"]
                    sched_slot = slot_info["slot"]
                    sched_at = slot_info["scheduled_at"]
                    sched_tz = slot_info["timezone"]
                else:
                    sched_date, sched_slot, sched_at, sched_tz = None, None, None, DEFAULT_TIMEZONE

                item = QueueItem(
                    queue_item_id=item_id,
                    original_url=url,
                    asin=asin,
                    product_name=product_name,
                    scheduled_date=sched_date,
                    daily_slot=sched_slot,
                    scheduled_at=sched_at,
                    timezone=sched_tz,
                    package_reference=pkg_path,
                    status=STATUS_READY,
                    validation_status=STATUS_READY_FOR_REVIEW,
                    errors=[]
                )
                ready_count += 1
            else:
                # Failed product - does NOT receive a scheduled slot
                item = QueueItem(
                    queue_item_id=item_id,
                    original_url=url,
                    asin=asin,
                    product_name=product_name,
                    scheduled_date=None,
                    daily_slot=None,
                    scheduled_at=None,
                    timezone=DEFAULT_TIMEZONE,
                    package_reference=pkg_path,
                    status=STATUS_FAILED,
                    validation_status=STATUS_FAILED_VALIDATION,
                    errors=errs if errs else ["Product failed content validation."]
                )
                failed_count += 1

            queue_items.append(item)

        scheduled_posts = ready_count
        unfilled_slots = max(0, total_capacity - scheduled_posts)

        queue = PublishingQueue(
            queue_id=q_id,
            created_at=datetime.now(timezone.utc).isoformat(),
            start_date=start_date,
            total_days=total_days,
            products_per_day=products_per_day,
            target_capacity=total_capacity,
            total_submitted=total_submitted,
            total_processed=len(queue_items),
            total_ready=ready_count,
            total_failed=failed_count,
            scheduled_posts=scheduled_posts,
            unfilled_slots=unfilled_slots,
            items=queue_items
        )

        queue.recalculate_counts()
        queue.save(target_file)
        return queue


class QueuePublisher:
    """
    Daily Queue Publisher for AmazonAffiliateBot (V4/V5).
    Evaluates publishing queue against current Europe/Budapest time,
    identifies due items, dispatches them through platform adapters,
    and updates queue item statuses idempotently.
    """

    @staticmethod
    def log_event(
        message: str,
        log_file: Optional[Union[str, Path]] = None,
        level: str = "INFO"
    ) -> None:
        """Append a structured, timestamped log line to the daily publisher log file."""
        target_file = Path(log_file) if log_file else DEFAULT_LOG_FILE
        target_file.parent.mkdir(parents=True, exist_ok=True)
        now_dt = datetime.now(get_budapest_timezone())
        timestamp_str = now_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
        log_line = f"[{timestamp_str}] [{level.upper()}] {message}\n"
        with open(target_file, "a", encoding="utf-8") as f:
            f.write(log_line)

    @staticmethod
    def parse_datetime(dt_str: str) -> datetime:
        """Parse an ISO 8601 datetime string and ensure Europe/Budapest timezone awareness."""
        tz = get_budapest_timezone()
        clean_str = dt_str.strip()
        # Handle trailing Z
        if clean_str.endswith("Z"):
            clean_str = clean_str[:-1] + "+00:00"

        dt = datetime.fromisoformat(clean_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt

    @classmethod
    def get_current_budapest_time(
        cls,
        now_override: Optional[Union[str, datetime]] = None
    ) -> datetime:
        """Get current datetime in Europe/Budapest timezone."""
        tz = get_budapest_timezone()
        if now_override is not None:
            if isinstance(now_override, datetime):
                if now_override.tzinfo is None:
                    return now_override.replace(tzinfo=tz)
                return now_override.astimezone(tz)
            return cls.parse_datetime(str(now_override))
        return datetime.now(tz)

    @classmethod
    def find_due_items(
        cls,
        queue: PublishingQueue,
        current_dt: datetime,
        slot_filter: Optional[int] = None,
        slot_keys: Optional[Set[str]] = None
    ) -> List[QueueItem]:
        """
        Identify queue items eligible for publishing:
        - status == STATUS_READY
        - scheduled_at <= current_dt
        - slot_filter match if specified
        Sorted chronologically by (scheduled_at, daily_slot).
        """
        due_items: List[QueueItem] = []
        for item in queue.items:
            if item.status != STATUS_READY:
                continue
            if not item.scheduled_at:
                continue
            if slot_filter is not None and item.daily_slot != slot_filter:
                continue

            try:
                item_dt = cls.parse_datetime(item.scheduled_at)
                if slot_keys is not None:
                    item_key = f"{item_dt.date().isoformat()}-slot-{item.daily_slot}"
                    if item_key not in slot_keys:
                        continue
                if item_dt <= current_dt:
                    due_items.append(item)
            except Exception:
                continue

        # Deterministic sort
        due_items.sort(key=lambda x: (x.scheduled_at or "", x.daily_slot or 0, x.queue_item_id))
        return due_items

    @classmethod
    def publish_due_items(
        cls,
        queue_file: Optional[Union[str, Path]] = None,
        config: Optional[Dict[str, Any]] = None,
        now_str: Optional[Union[str, datetime]] = None,
        now_override: Optional[Union[str, datetime]] = None,
        dry_run: Optional[bool] = None,
        slot_filter: Optional[int] = None,
        publisher: Optional[SocialPublisher] = None,
        log_file: Optional[Union[str, Path]] = None,
        slot_keys: Optional[Set[str]] = None,
    ) -> Dict[str, Any]:
        """
        Evaluate and publish due items in the queue.
        Idempotent: Already published items are ignored.
        Updates and persists queue status.
        Logs all operations to data/logs/daily_publisher.log.
        """
        target_file = Path(queue_file) if queue_file else DEFAULT_QUEUE_FILE
        target_log = Path(log_file) if log_file else DEFAULT_LOG_FILE

        cls.log_event("=== DAILY PUBLISHER RUN START ===", log_file=target_log)

        if not target_file.exists():
            err_msg = f"Publishing queue file not found at: {target_file}"
            cls.log_event(f"ERROR: {err_msg}", log_file=target_log, level="ERROR")
            cls.log_event("=== DAILY PUBLISHER RUN END (FAILED) ===", log_file=target_log, level="ERROR")
            raise FileNotFoundError(err_msg)

        try:
            queue = PublishingQueue.load(target_file)
        except Exception as e:
            err_msg = f"Failed to load queue JSON from '{target_file}': {str(e)}"
            cls.log_event(f"ERROR: {err_msg}", log_file=target_log, level="ERROR")
            cls.log_event("=== DAILY PUBLISHER RUN END (FAILED) ===", log_file=target_log, level="ERROR")
            raise

        eval_now = now_override if now_override is not None else now_str
        current_dt = cls.get_current_budapest_time(eval_now)

        # Setup publisher
        if publisher is None:
            from publishing.config import PublishingConfig
            pub_cfg = PublishingConfig.from_dict(config or {}, dry_run_override=dry_run)
            publisher = SocialPublisher(config=pub_cfg)

        is_dry_run = publisher.config.dry_run if dry_run is None else dry_run
        mode_str = "DRY_RUN" if is_dry_run else "LIVE"

        cls.log_event(f"Current Europe/Budapest Time: {current_dt.isoformat()} | Mode: {mode_str}", log_file=target_log)
        cls.log_event(f"Queue File: {target_file} | Total Items in Queue: {len(queue.items)}", log_file=target_log)

        due_items = cls.find_due_items(queue, current_dt, slot_filter=slot_filter, slot_keys=slot_keys)
        cls.log_event(f"Due Eligible Items Found: {len(due_items)}", log_file=target_log)

        if not due_items:
            cls.log_event("No items due for publishing at this time.", log_file=target_log)

        published_count = 0
        partially_published_count = 0
        failed_count = 0
        items_processed: List[Dict[str, Any]] = []

        for item in due_items:
            cls.log_event(
                f"Processing Item '{item.queue_item_id}' | ASIN: {item.asin} | Slot {item.daily_slot} ({item.scheduled_at})",
                log_file=target_log
            )

            # 1. Resolve content package path
            pkg_path = None
            if item.package_reference:
                candidate_path = Path(item.package_reference)
                if candidate_path.exists():
                    pkg_path = candidate_path
                else:
                    # Try relative to project root
                    root_candidate = Path(__file__).resolve().parent.parent / item.package_reference
                    if root_candidate.exists():
                        pkg_path = root_candidate

            if not pkg_path and item.asin:
                fallback_candidate = Path(__file__).resolve().parent.parent / "data" / "generated" / f"asin_{item.asin.upper()}_content_package.json"
                if fallback_candidate.exists():
                    pkg_path = fallback_candidate

            if not pkg_path or not pkg_path.exists():
                item.status = STATUS_FAILED
                err_text = f"ContentPackage file not found for ASIN '{item.asin}' at '{item.package_reference}'."
                item.errors.append(err_text)
                failed_count += 1
                cls.log_event(f"ERROR on Item '{item.queue_item_id}': {err_text}", log_file=target_log, level="ERROR")
                items_processed.append({
                    "item_id": item.queue_item_id,
                    "asin": item.asin,
                    "slot": item.daily_slot,
                    "scheduled_at": item.scheduled_at,
                    "status": STATUS_FAILED,
                    "errors": item.errors
                })
                continue

            # 2. Load Content Package
            try:
                with open(pkg_path, "r", encoding="utf-8") as f:
                    package_data = json.load(f)
            except Exception as e:
                item.status = STATUS_FAILED
                err_text = f"Failed to read package file: {str(e)}"
                item.errors.append(err_text)
                failed_count += 1
                cls.log_event(f"ERROR on Item '{item.queue_item_id}': {err_text}", log_file=target_log, level="ERROR")
                items_processed.append({
                    "item_id": item.queue_item_id,
                    "asin": item.asin,
                    "slot": item.daily_slot,
                    "scheduled_at": item.scheduled_at,
                    "status": STATUS_FAILED,
                    "errors": item.errors
                })
                continue

            # 3. Publish via SocialPublisher
            try:
                pub_report = publisher.publish_package(
                    package_data,
                    dry_run=is_dry_run,
                    prior_results=item.publishing_results,
                )
                platform_results = pub_report.get("results", {})

                # Check active platform results
                active_statuses = [
                    res.get("status") for res in platform_results.values()
                ]

                def confirmed_live(result: Dict[str, Any]) -> bool:
                    return (
                        result.get("status") == PLATFORM_STATUS_PUBLISHED
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

                all_ok = all(
                    (result.get("status") == STATUS_WOULD_PUBLISH and is_dry_run)
                    or confirmed_live(result)
                    for result in platform_results.values()
                ) and len(active_statuses) > 0

                any_ok = any(
                    (result.get("status") == STATUS_WOULD_PUBLISH and is_dry_run)
                    or confirmed_live(result)
                    for result in platform_results.values()
                )

                if all_ok:
                    item.status = STATUS_PUBLISHED
                    published_count += 1
                elif any_ok:
                    item.status = STATUS_PARTIALLY_PUBLISHED
                    partially_published_count += 1
                else:
                    item.status = STATUS_FAILED
                    failed_count += 1

                # Preserve actionable platform failures for both fully failed
                # and partially published items. Successful platforms remain
                # in publishing_results so retries can skip them safely.
                failure_messages = []
                for result in platform_results.values():
                    if result.get("status") not in (STATUS_WOULD_PUBLISH, PLATFORM_STATUS_PUBLISHED):
                        failure_messages.extend(result.get("errors", []))
                for message in failure_messages:
                    if message not in item.errors:
                        item.errors.append(message)

                # A dry-run is not a publication and must not create a fake
                # publication timestamp.  Persist a timestamp only alongside
                # evidence-backed live responses.
                if not is_dry_run:
                    for result in platform_results.values():
                        if confirmed_live(result):
                            result.setdefault("published_at", current_dt.isoformat())
                    if any(confirmed_live(result) for result in platform_results.values()):
                        item.published_at = current_dt.isoformat()
                item.publishing_results = platform_results

                plat_status_summary = ", ".join([f"{p}={r.get('status')}" for p, r in platform_results.items()])
                cls.log_event(
                    f"Item '{item.queue_item_id}' Platform Results: [{plat_status_summary}] -> Status: {item.status}",
                    log_file=target_log
                )

                items_processed.append({
                    "item_id": item.queue_item_id,
                    "asin": item.asin,
                    "slot": item.daily_slot,
                    "scheduled_at": item.scheduled_at,
                    "status": item.status,
                    "mode": pub_report.get("mode", mode_str),
                    "platform_statuses": {k: v.get("status") for k, v in platform_results.items()}
                })

            except Exception as e:
                item.status = STATUS_FAILED
                err_text = f"Publishing exception: {str(e)}"
                item.errors.append(err_text)
                failed_count += 1
                cls.log_event(f"ERROR on Item '{item.queue_item_id}': {err_text}", log_file=target_log, level="ERROR")
                items_processed.append({
                    "item_id": item.queue_item_id,
                    "asin": item.asin,
                    "slot": item.daily_slot,
                    "scheduled_at": item.scheduled_at,
                    "status": STATUS_FAILED,
                    "errors": item.errors
                })

        # Save updated queue
        queue.save(target_file)
        cls.log_event(f"Queue state saved to: {target_file}", log_file=target_log)

        cls.log_event(
            f"Execution Summary: Processed={len(due_items)}, Published={published_count}, "
            f"Partially={partially_published_count}, Failed={failed_count}",
            log_file=target_log
        )
        cls.log_event("=== DAILY PUBLISHER RUN END (SUCCESS) ===", log_file=target_log)

        return {
            "queue_file": str(target_file),
            "evaluated_at": current_dt.isoformat(),
            "timezone": DEFAULT_TIMEZONE,
            "mode": mode_str,
            "total_items_in_queue": len(queue.items),
            "due_items_count": len(due_items),
            "published_count": published_count,
            "partially_published_count": partially_published_count,
            "failed_count": failed_count,
            "items_processed": items_processed
        }
