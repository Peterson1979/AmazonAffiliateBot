"""
Publishing Queue Replenishment and Automatic Product Maintenance Module for AmazonAffiliateBot.
Calculates future publishing capacity, coordinates Amazon product discovery,
extracts canonical facts, generates validated content packages,
and appends new products to future available daily slots (12:00 & 19:00 Europe/Budapest).
"""

from typing import Dict, Any, List, Optional, Tuple, Set, Union, Callable
from pathlib import Path
from datetime import datetime, date, time, timedelta
from zoneinfo import ZoneInfo
import json
import os

from amazon.scraper import Product, extract_asin_from_url
from amazon.discovery import (
    AmazonDiscoveryEngine,
    DiscoveryHistory,
    DiscoveredCandidate,
    DEFAULT_CONFIG_QUERIES,
    DEFAULT_TRACKING_TAG
)
from content.facts import FactExtractor, CanonicalProductFacts
from content.generator import ContentGenerator
from content.package import ContentPackage, STATUS_READY_FOR_REVIEW, STATUS_FAILED_VALIDATION
from publishing.queue import (
    PublishingQueue,
    QueueItem,
    QueueBuilder,
    get_budapest_timezone,
    DEFAULT_QUEUE_FILE,
    DEFAULT_LOGS_DIR,
    DEFAULT_TIMEZONE,
    SLOT_1_TIME,
    SLOT_2_TIME,
    STATUS_QUEUED,
    STATUS_READY,
    STATUS_FAILED,
    STATUS_PUBLISHED,
    STATUS_PARTIALLY_PUBLISHED,
)
from ai.ollama import OllamaClient


DEFAULT_TARGET_FUTURE_CAPACITY = 28   # 14 days @ 2 products/day
DEFAULT_MIN_FUTURE_CAPACITY = 14      # 7 days threshold
DEFAULT_MAINTENANCE_LOG_FILE = DEFAULT_LOGS_DIR / "queue_maintenance.log"


class QueueReplenisher:
    """
    Automated queue replenisher and capacity maintainer.
    Monitors future scheduled capacity and fills free slots up to target capacity.
    """

    @staticmethod
    def log_event(
        message: str,
        log_file: Optional[Union[str, Path]] = None,
        level: str = "INFO"
    ) -> None:
        """Append timestamped log message to queue maintenance log."""
        target_file = Path(log_file) if log_file else DEFAULT_MAINTENANCE_LOG_FILE
        target_file.parent.mkdir(parents=True, exist_ok=True)
        now_dt = datetime.now(get_budapest_timezone())
        timestamp_str = now_dt.strftime("%Y-%m-%d %H:%M:%S %Z")
        log_line = f"[{timestamp_str}] [{level.upper()}] {message}\n"
        with open(target_file, "a", encoding="utf-8") as f:
            f.write(log_line)

    @classmethod
    def get_future_valid_count(
        cls,
        queue: PublishingQueue,
        current_dt: Optional[datetime] = None
    ) -> int:
        """
        Count scheduled future valid items in the queue:
        - status in (STATUS_READY, STATUS_QUEUED)
        - scheduled_at is set and scheduled_at >= current_dt (if provided)
        """
        eval_dt = current_dt or datetime.now(get_budapest_timezone())
        count = 0
        for item in queue.items:
            if item.status not in (STATUS_READY, STATUS_QUEUED):
                continue
            if not item.scheduled_at:
                continue
            try:
                item_dt = datetime.fromisoformat(item.scheduled_at)
                if item_dt.tzinfo is None:
                    item_dt = item_dt.replace(tzinfo=get_budapest_timezone())
                if item_dt >= eval_dt:
                    count += 1
            except Exception:
                count += 1  # count as valid if parsing fails but status is READY
        return count

    @classmethod
    def find_next_available_slot(
        cls,
        queue: PublishingQueue,
        current_dt: Optional[datetime] = None
    ) -> Dict[str, Any]:
        """
        Determine the next concrete available (date, slot, scheduled_at) in Europe/Budapest.
        Preserves all existing slots and advances consecutively:
        - If latest slot in queue is (Date D, Slot 1) -> next is (Date D, Slot 2 at 19:00).
        - If latest slot in queue is (Date D, Slot 2) -> next is (Date D+1, Slot 1 at 12:00).
        - If queue has no scheduled items or all slots are in the past -> starts next future slot relative to current_dt.
        """
        tz = get_budapest_timezone()
        eval_dt = current_dt or datetime.now(tz)
        today = eval_dt.date()

        scheduled_slots: List[Tuple[date, int]] = []
        for item in queue.items:
            if item.scheduled_date and item.daily_slot:
                try:
                    d = datetime.strptime(item.scheduled_date, "%Y-%m-%d").date()
                    scheduled_slots.append((d, item.daily_slot))
                except Exception:
                    pass

        if scheduled_slots:
            scheduled_slots.sort(key=lambda x: (x[0], x[1]))
            latest_date, latest_slot = scheduled_slots[-1]
            latest_time = SLOT_1_TIME if latest_slot == 1 else SLOT_2_TIME
            latest_dt = datetime(latest_date.year, latest_date.month, latest_date.day, latest_time.hour, latest_time.minute, latest_time.second, tzinfo=tz)

            if latest_dt >= eval_dt:
                if latest_slot == 1:
                    next_date = latest_date
                    next_slot = 2
                    next_time = SLOT_2_TIME
                else:
                    next_date = latest_date + timedelta(days=1)
                    next_slot = 1
                    next_time = SLOT_1_TIME
            else:
                # Latest slot in queue is in the past: schedule starting from next future slot relative to eval_dt
                if eval_dt.time() < SLOT_1_TIME:
                    next_date = today
                    next_slot = 1
                    next_time = SLOT_1_TIME
                elif eval_dt.time() < SLOT_2_TIME:
                    next_date = today
                    next_slot = 2
                    next_time = SLOT_2_TIME
                else:
                    next_date = today + timedelta(days=1)
                    next_slot = 1
                    next_time = SLOT_1_TIME
        else:
            # Empty queue: start today or tomorrow
            if eval_dt.time() < SLOT_1_TIME:
                next_date = today
                next_slot = 1
                next_time = SLOT_1_TIME
            elif eval_dt.time() < SLOT_2_TIME:
                next_date = today
                next_slot = 2
                next_time = SLOT_2_TIME
            else:
                next_date = today + timedelta(days=1)
                next_slot = 1
                next_time = SLOT_1_TIME

        dt_slot = datetime(next_date.year, next_date.month, next_date.day, next_time.hour, next_time.minute, next_time.second, tzinfo=tz)
        return {
            "date": next_date.strftime("%Y-%m-%d"),
            "slot": next_slot,
            "scheduled_at": dt_slot.isoformat(),
            "timezone": DEFAULT_TIMEZONE
        }

    @classmethod
    def schedule_existing_products(
        cls,
        queue: PublishingQueue,
        current_dt: Optional[datetime] = None,
        target_capacity: int = DEFAULT_TARGET_FUTURE_CAPACITY,
        log_file: Optional[Union[str, Path]] = None,
        products_dir: Optional[Union[str, Path]] = None,
        generated_dir: Optional[Union[str, Path]] = None,
    ) -> List[Dict[str, Any]]:
        """
        Scan existing persisted products and content packages.
        If a product is valid (status == READY_FOR_REVIEW) and not yet in the queue,
        allocate the next available future daily slot and schedule it.
        """
        pdir = Path(products_dir) if products_dir else Path("data/products")
        gdir = Path(generated_dir) if generated_dir else Path("data/generated")
        target_log = Path(log_file) if log_file else DEFAULT_MAINTENANCE_LOG_FILE

        existing_asins = {item.asin.upper() for item in queue.items if item.asin}
        existing_urls = {item.original_url for item in queue.items if item.original_url}

        test_prefixes = ("TESTADD", "EMPTY", "VALID", "IDEMP", "NEW", "ONLY", "EXIST", "BADVAL")
        items_added = []

        if not pdir.exists() or not gdir.exists():
            return items_added

        # If running against the default storage, restrict to authoritative URLs so we never schedule unsubmitted or legacy products
        auth_urls = None
        if products_dir is None:
            from publishing.queue import get_authoritative_urls_file, parse_affiliate_urls
            auth_file = get_authoritative_urls_file()
            if auth_file.exists():
                auth_urls = set(parse_affiliate_urls(auth_file))

        for pfile in sorted(pdir.glob("asin_*.json")):
            if cls.get_future_valid_count(queue, current_dt) >= target_capacity:
                break

            asin = pfile.stem.split("_", 1)[1].upper()
            if any(asin.startswith(pfx) for pfx in test_prefixes) or asin == "UNKNOWN":
                continue

            if asin in existing_asins:
                continue

            pkg_file = gdir / f"asin_{asin}_content_package.json"
            if not pkg_file.exists():
                continue

            try:
                with open(pkg_file, "r", encoding="utf-8") as f:
                    pkg_data = json.load(f)
                if pkg_data.get("status") != STATUS_READY_FOR_REVIEW:
                    continue

                with open(pfile, "r", encoding="utf-8") as f:
                    prod_data = json.load(f)
                affiliate_url = prod_data.get("affiliate_url") or prod_data.get("source_url") or ""
                title = prod_data.get("title") or pkg_data.get("source", {}).get("title") or f"Product {asin}"

                if auth_urls is not None and affiliate_url not in auth_urls:
                    continue

                if affiliate_url and affiliate_url in existing_urls:
                    continue

                next_slot_info = cls.find_next_available_slot(queue, current_dt)
                item_idx = len(queue.items) + 1
                item_id = f"item_{item_idx:03d}_{asin}"
                pkg_path_str = str(pkg_file).replace("\\", "/")

                new_queue_item = QueueItem(
                    queue_item_id=item_id,
                    original_url=affiliate_url,
                    asin=asin,
                    product_name=title,
                    scheduled_date=next_slot_info["date"],
                    daily_slot=next_slot_info["slot"],
                    scheduled_at=next_slot_info["scheduled_at"],
                    timezone=next_slot_info["timezone"],
                    package_reference=pkg_path_str,
                    status=STATUS_READY,
                    validation_status=STATUS_READY_FOR_REVIEW,
                    errors=[]
                )

                queue.items.append(new_queue_item)
                queue.recalculate_counts()

                existing_asins.add(asin)
                if affiliate_url:
                    existing_urls.add(affiliate_url)

                cls.log_event(
                    f"Scheduled Existing Product: ASIN {asin} -> Day {next_slot_info['date']} "
                    f"Slot {next_slot_info['slot']} ({next_slot_info['scheduled_at']})",
                    log_file=target_log
                )

                items_added.append({
                    "item_id": item_id,
                    "asin": asin,
                    "title": title,
                    "scheduled_date": next_slot_info["date"],
                    "daily_slot": next_slot_info["slot"],
                    "scheduled_at": next_slot_info["scheduled_at"],
                    "source": "EXISTING_STORAGE"
                })

            except Exception as e:
                cls.log_event(f"Error scheduling existing product {asin}: {e}", log_file=target_log, level="WARNING")

        return items_added

    @classmethod
    def maintain_queue(
        cls,
        queue_file: Optional[Union[str, Path]] = None,
        config: Optional[Dict[str, Any]] = None,
        target_capacity: int = DEFAULT_TARGET_FUTURE_CAPACITY,
        discovery_queries: Optional[List[str]] = None,
        candidate_provider: Optional[Callable[[str, int], List[DiscoveredCandidate]]] = None,
        product_loader: Optional[Callable[[str], Optional[Product]]] = None,
        generator: Optional[Any] = None,
        now_override: Optional[Union[str, datetime]] = None,
        log_file: Optional[Union[str, Path]] = None,
        history_file: Optional[Union[str, Path]] = None,
        dry_run: bool = True,
        force_discovery: bool = False,
        products_dir: Optional[Union[str, Path]] = None,
        generated_dir: Optional[Union[str, Path]] = None,
    ) -> Dict[str, Any]:
        """
        Execute automated queue maintenance:
        1. Load persistent queue.
        2. Ingest & schedule eligible existing stored products first.
        3. Evaluate future capacity.
        4. If discovery is enabled (or candidate_provider supplied) and needed, discover new products.
        5. Persist queue and return structured diagnostic report.
        """
        target_qfile = Path(queue_file) if queue_file else DEFAULT_QUEUE_FILE
        target_log = Path(log_file) if log_file else DEFAULT_MAINTENANCE_LOG_FILE
        target_history = Path(history_file) if history_file else None

        cls.log_event("=== QUEUE MAINTENANCE RUN START ===", log_file=target_log)

        # 1. Load or initialize queue
        if target_qfile.exists():
            queue = PublishingQueue.load(target_qfile)
        else:
            cls.log_event(f"Queue file not found at '{target_qfile}'. Creating new queue.", log_file=target_log)
            now_str = datetime.now(get_budapest_timezone()).strftime("%Y-%m-%d")
            queue = PublishingQueue(
                queue_id=f"queue_{now_str.replace('-', '')}_30d",
                created_at=datetime.now(get_budapest_timezone()).isoformat(),
                start_date=now_str,
                total_days=30,
                products_per_day=2,
                target_capacity=60
            )

        # Time evaluation
        tz = get_budapest_timezone()
        if now_override:
            if isinstance(now_override, datetime):
                current_dt = now_override.astimezone(tz) if now_override.tzinfo else now_override.replace(tzinfo=tz)
            else:
                current_dt = datetime.fromisoformat(str(now_override).strip())
                if current_dt.tzinfo is None:
                    current_dt = current_dt.replace(tzinfo=tz)
        else:
            current_dt = datetime.now(tz)

        # Configuration setup
        cfg = config or {}
        disc_cfg = cfg.get("discovery", {})
        queries = discovery_queries or disc_cfg.get("queries") or DEFAULT_CONFIG_QUERIES
        tracking_tag = disc_cfg.get("tracking_tag", DEFAULT_TRACKING_TAG)
        target_cap = target_capacity or disc_cfg.get("target_future_capacity", DEFAULT_TARGET_FUTURE_CAPACITY)

        # Initial future capacity before maintenance
        initial_future_capacity = cls.get_future_valid_count(queue, current_dt)

        # 2. Ingest & schedule eligible existing stored products first
        existing_items_added: List[Dict[str, Any]] = []
        is_default_queue = target_qfile.resolve() == DEFAULT_QUEUE_FILE.resolve()
        if (products_dir is not None or is_default_queue) and candidate_provider is None:
            existing_items_added = cls.schedule_existing_products(
                queue=queue,
                current_dt=current_dt,
                target_capacity=target_cap,
                log_file=target_log,
                products_dir=products_dir,
                generated_dir=generated_dir
            )

        items_added: List[Dict[str, Any]] = list(existing_items_added)

        # Evaluate updated future capacity
        future_valid_count = cls.get_future_valid_count(queue, current_dt)
        cls.log_event(f"Current Future Capacity: {future_valid_count} / Target: {target_cap}", log_file=target_log)

        # Determine if automatic discovery is allowed
        # Discovery is disabled by default unless explicitly enabled in config, forced, or candidate_provider supplied
        discovery_enabled = bool(candidate_provider is not None or disc_cfg.get("enabled", False) or force_discovery)

        if not discovery_enabled:
            cls.log_event("Automatic discovery is disabled (discovery.enabled=False). Queue maintained from existing products only.", log_file=target_log)
            if existing_items_added:
                queue.save(target_qfile)
            final_future_capacity = cls.get_future_valid_count(queue, current_dt)
            cls.log_event(
                f"Maintenance Finished: Evaluated=0, Added={len(items_added)}, Rejected=0, Final Future Capacity={final_future_capacity}/{target_cap}",
                log_file=target_log
            )
            cls.log_event("=== QUEUE MAINTENANCE RUN END (SUCCESS) ===", log_file=target_log)
            return {
                "replenished": len(items_added) > 0,
                "discovery_enabled": False,
                "initial_future_capacity": initial_future_capacity,
                "final_future_capacity": final_future_capacity,
                "target_capacity": target_cap,
                "needed_count": max(0, target_cap - final_future_capacity),
                "items_added_count": len(items_added),
                "items_added": items_added,
                "candidates_evaluated": 0,
                "candidates_rejected": 0,
                "rejection_summary": {},
                "queue_file": str(target_qfile)
            }

        if future_valid_count >= target_cap:
            msg = f"Queue capacity is healthy ({future_valid_count}/{target_cap} future slots filled). No replenishment required."
            cls.log_event(msg, log_file=target_log)
            if existing_items_added:
                queue.save(target_qfile)
            cls.log_event("=== QUEUE MAINTENANCE RUN END (NO ACTION NEEDED) ===", log_file=target_log)
            return {
                "replenished": len(items_added) > 0,
                "discovery_enabled": True,
                "reason": "CAPACITY_HEALTHY",
                "initial_future_capacity": initial_future_capacity,
                "final_future_capacity": future_valid_count,
                "target_capacity": target_cap,
                "needed_count": 0,
                "items_added_count": len(items_added),
                "items_added": items_added,
                "candidates_evaluated": 0,
                "candidates_rejected": 0,
                "rejection_summary": {},
                "queue_file": str(target_qfile)
            }

        needed_count = target_cap - future_valid_count
        cls.log_event(f"Replenishment Required: Need to discover and schedule {needed_count} new valid products.", log_file=target_log)

        # Setup Discovery Engine & History
        history = DiscoveryHistory(target_history)
        engine = AmazonDiscoveryEngine(tracking_tag=tracking_tag, history=history)

        existing_asins = {item.asin.upper() for item in queue.items if item.asin}
        existing_urls = {item.original_url for item in queue.items if item.original_url}
        batch_asins: Set[str] = set()

        # Ingestion dependencies
        if generator is None:
            ollama = OllamaClient(
                base_url=cfg.get("ollama", {}).get("base_url", "http://localhost:11434"),
                model=cfg.get("ollama", {}).get("model", "qwen2.5:7b"),
                timeout=cfg.get("ollama", {}).get("timeout", 300)
            )
            gen = ContentGenerator(ollama_client=ollama)
            model_name = ollama.model
        else:
            gen = generator
            model_name = getattr(generator, "model_name", "qwen2.5:7b")

        disclosure = cfg.get("affiliate", {}).get(
            "disclosure",
            "This post contains affiliate links. As an Amazon Associate I earn from qualifying purchases."
        )

        items_added: List[Dict[str, Any]] = []
        candidates_evaluated = 0
        candidates_rejected = 0
        rejection_counts: Dict[str, int] = {}

        for query in queries:
            if len(items_added) >= needed_count:
                break

            cls.log_event(f"Querying Amazon Discovery for: '{query}'...", log_file=target_log)

            if candidate_provider:
                candidates = candidate_provider(query, needed_count - len(items_added) + 5)
            else:
                candidates = engine.search_and_extract_candidates(query, max_candidates=10)

            cls.log_event(f"Discovered {len(candidates)} raw candidates for query '{query}'.", log_file=target_log)

            for cand in candidates:
                if len(items_added) >= needed_count:
                    break

                candidates_evaluated += 1

                # 1. Filter quality & deduplication
                is_valid_cand, reject_reason = engine.filter_candidate(
                    candidate=cand,
                    existing_asins=existing_asins,
                    existing_urls=existing_urls,
                    batch_asins=batch_asins
                )

                if not is_valid_cand:
                    candidates_rejected += 1
                    rejection_counts[reject_reason or "UNKNOWN"] = rejection_counts.get(reject_reason or "UNKNOWN", 0) + 1
                    history.record_candidate(cand, status="REJECTED", rejection_reason=reject_reason)
                    cls.log_event(f"Candidate rejected: ASIN={cand.asin} Title='{cand.title[:40]}' Reason={reject_reason}", log_file=target_log)
                    continue

                batch_asins.add(cand.asin.upper())

                # 2. Resolve full product data
                product: Optional[Product] = None
                if product_loader:
                    product = product_loader(cand.affiliate_url or cand.source_url)
                else:
                    from amazon.scraper import scrape_amazon_product
                    try:
                        product = scrape_amazon_product(cand.affiliate_url, headless=True)
                    except Exception as e:
                        candidates_rejected += 1
                        rejection_counts["SCRAPING_FAILED"] = rejection_counts.get("SCRAPING_FAILED", 0) + 1
                        history.record_candidate(cand, status="REJECTED", rejection_reason=f"SCRAPING_FAILED: {str(e)}")
                        cls.log_event(f"Candidate rejected: ASIN={cand.asin} Reason=SCRAPING_FAILED ({str(e)})", log_file=target_log)
                        continue

                if not product or not product.title:
                    candidates_rejected += 1
                    rejection_counts["MISSING_PRODUCT_DATA"] = rejection_counts.get("MISSING_PRODUCT_DATA", 0) + 1
                    history.record_candidate(cand, status="REJECTED", rejection_reason="MISSING_PRODUCT_DATA")
                    cls.log_event(f"Candidate rejected: ASIN={cand.asin} Reason=MISSING_PRODUCT_DATA", log_file=target_log)
                    continue

                # Ensure ASIN is attached
                if not product.asin:
                    product.asin = cand.asin
                if not product.affiliate_url:
                    product.affiliate_url = cand.affiliate_url

                # 3. Canonical Fact Extraction
                facts = FactExtractor.extract_canonical_facts(product)
                if not facts.is_sufficient():
                    candidates_rejected += 1
                    rejection_counts["INSUFFICIENT_FACTS"] = rejection_counts.get("INSUFFICIENT_FACTS", 0) + 1
                    history.record_candidate(cand, status="REJECTED", rejection_reason="INSUFFICIENT_FACTS")
                    cls.log_event(f"Candidate rejected: ASIN={cand.asin} Reason=INSUFFICIENT_FACTS", log_file=target_log)
                    continue

                # 4. Content Generation
                try:
                    social_data = gen.generate_all_platforms(product=facts, disclosure=disclosure)
                except Exception as e:
                    candidates_rejected += 1
                    rejection_counts["GENERATION_ERROR"] = rejection_counts.get("GENERATION_ERROR", 0) + 1
                    history.record_candidate(cand, status="REJECTED", rejection_reason=f"GENERATION_ERROR: {str(e)}")
                    cls.log_event(f"Candidate rejected: ASIN={cand.asin} Reason=GENERATION_ERROR ({str(e)})", log_file=target_log)
                    continue

                # 5. Build Content Package
                cache_key = f"asin_{product.asin.upper()}"
                pkg = ContentPackage.build_package(
                    product=product,
                    social_data=social_data,
                    cache_file_path=f"data/products/{cache_key}.json",
                    model_name=model_name
                )

                if pkg.get("status") != STATUS_READY_FOR_REVIEW:
                    candidates_rejected += 1
                    rejection_counts["VALIDATION_FAILED"] = rejection_counts.get("VALIDATION_FAILED", 0) + 1
                    history.record_candidate(cand, status="REJECTED", rejection_reason="VALIDATION_FAILED")
                    cls.log_event(f"Candidate rejected: ASIN={cand.asin} Reason=VALIDATION_FAILED", log_file=target_log)
                    continue

                # Save generated package to disk
                pkg_file = ContentPackage.save_content_package(pkg, cache_key)
                pkg_path_str = str(pkg_file).replace("\\", "/")

                # 6. Allocate Next Available Slot
                next_slot_info = cls.find_next_available_slot(queue, current_dt)
                item_idx = len(queue.items) + 1
                item_id = f"item_{item_idx:03d}_{product.asin.upper()}"

                new_queue_item = QueueItem(
                    queue_item_id=item_id,
                    original_url=product.affiliate_url,
                    asin=product.asin.upper(),
                    product_name=product.title,
                    scheduled_date=next_slot_info["date"],
                    daily_slot=next_slot_info["slot"],
                    scheduled_at=next_slot_info["scheduled_at"],
                    timezone=next_slot_info["timezone"],
                    package_reference=pkg_path_str,
                    status=STATUS_READY,
                    validation_status=STATUS_READY_FOR_REVIEW,
                    errors=[]
                )

                queue.items.append(new_queue_item)
                queue.total_processed += 1
                queue.total_ready += 1
                queue.scheduled_posts += 1
                queue.unfilled_slots = max(0, queue.target_capacity - queue.scheduled_posts)

                existing_asins.add(product.asin.upper())
                existing_urls.add(product.affiliate_url)
                history.record_candidate(cand, status="ACCEPTED")

                cls.log_event(
                    f"Accepted & Scheduled: ASIN {product.asin} -> Day {next_slot_info['date']} "
                    f"Slot {next_slot_info['slot']} ({next_slot_info['scheduled_at']})",
                    log_file=target_log
                )

                items_added.append({
                    "item_id": item_id,
                    "asin": product.asin.upper(),
                    "title": product.title,
                    "scheduled_date": next_slot_info["date"],
                    "daily_slot": next_slot_info["slot"],
                    "scheduled_at": next_slot_info["scheduled_at"],
                    "query": query
                })

        # Save Queue and History
        queue.save(target_qfile)
        history.save()

        final_future_capacity = cls.get_future_valid_count(queue, current_dt)

        cls.log_event(
            f"Maintenance Finished: Evaluated={candidates_evaluated}, Added={len(items_added)}, "
            f"Rejected={candidates_rejected}, Final Future Capacity={final_future_capacity}/{target_cap}",
            log_file=target_log
        )
        cls.log_event("=== QUEUE MAINTENANCE RUN END (SUCCESS) ===", log_file=target_log)

        return {
            "replenished": len(items_added) > 0,
            "initial_future_capacity": future_valid_count,
            "final_future_capacity": final_future_capacity,
            "target_capacity": target_cap,
            "needed_count": needed_count,
            "items_added_count": len(items_added),
            "items_added": items_added,
            "candidates_evaluated": candidates_evaluated,
            "candidates_rejected": candidates_rejected,
            "rejection_summary": rejection_counts,
            "queue_file": str(target_qfile)
        }
