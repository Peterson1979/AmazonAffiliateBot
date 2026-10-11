#!/usr/bin/env python3
"""
AmazonAffiliateBot - Main Runner (V2/V3/V4)
Scrapes Amazon affiliate links via Selenium & Firefox, normalizes product details,
caches data locally, and generates platform-specific social copy (Facebook,
Pinterest, Threads) using local Ollama (qwen2.5:7b).
"""

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, Any, Tuple, Optional

# Ensure UTF-8 output encoding across Windows consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from dotenv import load_dotenv
from amazon.scraper import Product, scrape_amazon_product, extract_asin_from_url
from ai.ollama import OllamaClient
from content.generator import ContentGenerator
from content.package import ContentPackage
from publishing import SocialPublisher, PublishingConfig


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent / "config.json"
DATA_DIR = Path(__file__).resolve().parent / "data"
PRODUCTS_CACHE_DIR = DATA_DIR / "products"
GENERATED_DIR = DATA_DIR / "generated"


def load_config(config_path: Path = DEFAULT_CONFIG_PATH) -> Dict[str, Any]:
    """Load configuration JSON or return safe default dictionary."""
    defaults = {
        "ollama": {
            "base_url": "http://localhost:11434",
            "model": "qwen2.5:7b",
            "timeout": 300,
            "num_predict": 120,
            "num_ctx": 1024
        },
        "affiliate": {
            "disclosure": "This post contains affiliate links. As an Amazon Associate I earn from qualifying purchases."
        },
        "browser": {
            "headless": False,
            "firefox_profile": ""
        }
    }

    if not config_path.exists():
        return defaults

    try:
        with open(config_path, "r", encoding="utf-8") as f:
            user_config = json.load(f)
            for section in ("ollama", "affiliate", "browser"):
                if section in user_config:
                    defaults[section].update(user_config[section])
            return defaults
    except Exception as e:
        print(f"[WARN] Failed to read config at {config_path}: {e}. Using defaults.", file=sys.stderr)
        return defaults


def get_cache_key(url: str, asin: str = None) -> str:
    """Generate a clean filesystem-friendly cache key based on ASIN or URL hash."""
    if asin and re.match(r'^[A-Z0-9]{10}$', asin, re.IGNORECASE):
        return f"asin_{asin.upper()}"
    
    url_asin = extract_asin_from_url(url)
    if url_asin:
        return f"asin_{url_asin.upper()}"

    # Fallback to deterministic URL hash
    url_hash = hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:12]
    return f"url_{url_hash}"


def load_cached_product(url: str, asin: str = None) -> Tuple[bool, Optional[Product], str]:
    """Check if product is already cached on disk by cache key or matching affiliate_url."""
    key = get_cache_key(url, asin)
    cache_file = PRODUCTS_CACHE_DIR / f"{key}.json"
    if cache_file.exists():
        try:
            with open(cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                cached_prod = Product.from_dict(data)
                if cached_prod.title:
                    # Verify ASIN consistency if key is asin-based
                    if key.startswith("asin_") and cached_prod.asin:
                        expected_asin = key.split("_", 1)[1].upper()
                        if cached_prod.asin.upper() != expected_asin:
                            return False, None, key
                    # Verify affiliate URL match if present in cache and caller provided url
                    if url and cached_prod.affiliate_url and cached_prod.affiliate_url != url:
                        if cached_prod.source_url != url:
                            return False, None, key
                    if not cached_prod.affiliate_url and url:
                        cached_prod.affiliate_url = url
                    return True, cached_prod, key
        except Exception:
            pass

    # Search cached product files for matching affiliate_url or source_url
    if PRODUCTS_CACHE_DIR.exists():
        for file in PRODUCTS_CACHE_DIR.glob("*.json"):
            try:
                with open(file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data.get("affiliate_url") == url or data.get("source_url") == url:
                        cached_prod = Product.from_dict(data)
                        if cached_prod.title:
                            if file.stem.startswith("asin_") and cached_prod.asin:
                                expected_asin = file.stem.split("_", 1)[1].upper()
                                if cached_prod.asin.upper() != expected_asin:
                                    continue
                            if not cached_prod.affiliate_url and url:
                                cached_prod.affiliate_url = url
                            return True, cached_prod, file.stem
            except Exception:
                continue

    return False, None, key


def save_product_cache(product: Product, cache_key: Optional[str] = None) -> Path:
    """Save normalized product dictionary as a JSON file."""
    PRODUCTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if product.asin:
        key = f"asin_{product.asin.upper()}"
    elif cache_key:
        key = cache_key
    else:
        key = get_cache_key(product.affiliate_url or product.source_url)
    cache_file = PRODUCTS_CACHE_DIR / f"{key}.json"
    with open(cache_file, "w", encoding="utf-8") as f:
        json.dump(product.to_dict(), f, indent=2, ensure_ascii=False)
    return cache_file


def print_v2_preview(
    social_data: Dict[str, Any],
    saved_file_path: Path,
    package_path: Optional[Path] = None,
    package_status: Optional[str] = None
) -> None:
    """Render structured terminal preview for active social platforms."""
    platforms = social_data.get("platforms", {})

    print("\n=== FACEBOOK ===", flush=True)
    print(platforms.get("facebook", {}).get("text", "(No content generated)"), flush=True)

    print("\n=== PINTEREST ===", flush=True)
    pin_title = platforms.get("pinterest", {}).get("title", "(No title)")
    pin_desc = platforms.get("pinterest", {}).get("description", "(No description)")
    print(f"Title:\n{pin_title}\n\nDescription:\n{pin_desc}", flush=True)

    print("\n=== THREADS ===", flush=True)
    print(platforms.get("threads", {}).get("text", "(No content generated)"), flush=True)

    print("\n------------------------------------------------------------", flush=True)
    print(f"Model: {social_data.get('generated_with', 'qwen2.5:7b')}", flush=True)
    print(f"Product: {social_data.get('product_title', '')}", flush=True)
    print(f"Saved: {saved_file_path}", flush=True)
    if package_path and package_status:
        try:
            display_pkg_path = package_path.relative_to(Path.cwd())
        except Exception:
            display_pkg_path = package_path
        print(f"Content package: {str(display_pkg_path).replace(chr(92), '/')}", flush=True)
        print(f"Status: {package_status}", flush=True)
    print("------------------------------------------------------------\n", flush=True)


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description="AmazonAffiliateBot - V2 Scraper & Multi-Platform AI Content Generator")
    parser.add_argument("url", nargs="?", default="", help="Amazon affiliate URL or product URL to process")
    parser.add_argument("--build-queue", action="store_true", help="Build 30-day publishing queue from affiliate URLs file")
    parser.add_argument("--publish-queue", action="store_true", help="Publish due items from the publishing queue")
    parser.add_argument("--now", default=None, help="Override current evaluation time (ISO format) for testing/simulation")
    parser.add_argument("--force-slot", type=int, choices=[1, 2], default=None, help="Optionally restrict to specific daily slot (1 or 2)")
    parser.add_argument("--input-urls", default=None, help="Path to affiliate URLs text file (default: data/input/affiliate_urls.txt)")
    parser.add_argument("--queue-file", default=None, help="Path to output queue JSON file (default: data/queue/publishing_queue.json)")
    parser.add_argument("--start-date", default=None, help="Start date for queue scheduling (YYYY-MM-DD, default: tomorrow)")
    parser.add_argument("--days", type=int, default=30, help="Number of days to schedule (default: 30)")
    parser.add_argument("--per-day", type=int, default=2, help="Number of products per day (default: 2)")
    parser.add_argument("--config", "-c", default=str(DEFAULT_CONFIG_PATH), help="Path to config.json")
    parser.add_argument("--headless", action="store_true", help="Force headless browser mode")
    parser.add_argument("--force-scrape", action="store_true", help="Bypass local cache and re-scrape from Amazon")
    parser.add_argument("--maintain-queue", action="store_true", help="Maintain and replenish publishing queue up to target capacity")
    parser.add_argument("--replenish-queue", action="store_true", help="Alias for --maintain-queue")
    parser.add_argument("--discovery", action="store_true", help="Explicitly enable automatic Amazon product discovery during queue maintenance")
    parser.add_argument("--target-capacity", type=int, default=28, help="Target future publishing capacity in products (default: 28 = 14 days)")
    parser.add_argument("--discovery-queries", default=None, help="Comma-separated custom Amazon search queries for discovery")
    parser.add_argument("--history-file", default=None, help="Path to discovery history JSON (default: data/discovery/discovery_history.json)")
    parser.add_argument("--log-file", default=None, help="Path to daily publisher log file (default: data/logs/daily_publisher.log)")
    parser.add_argument("--platform", choices=["all", "facebook", "pinterest", "threads"], default="all", help="Generate for specific platform (default: all)")
    parser.add_argument("--publish", action="store_true", help="Publish content package to target accounts in DRY_RUN mode")
    parser.add_argument("--live-publish", action="store_true", help="Publish content package live to target accounts (requires credentials)")
    parser.add_argument("--v1", action="store_true", help="Run legacy V1 single recommendation generator")
    parser.add_argument("--dashboard", action="store_true", help="Start local web-based Management Dashboard on localhost")
    parser.add_argument("--port", type=int, default=5000, help="Port for the Management Dashboard (default: 5000)")
    args = parser.parse_args()

    # 0. Handle Web Dashboard
    if args.dashboard:
        from web.app import create_app
        app = create_app()
        print(f"\n========================================================")
        print(f" AmazonAffiliateBot Management Dashboard")
        print(f" Address: http://127.0.0.1:{args.port}")
        print(f" Safety: DRY RUN ENABLED (No live social posts published)")
        print(f" Localhost only (Strictly binds to 127.0.0.1)")
        print(f"========================================================\n", flush=True)
        app.run(host="127.0.0.1", port=args.port, debug=False)
        return

    # 1. Load Configuration
    config = load_config(Path(args.config))
    headless_setting = args.headless or config.get("browser", {}).get("headless", False)
    firefox_profile = config.get("browser", {}).get("firefox_profile", "")
    ollama_base_url = config.get("ollama", {}).get("base_url", "http://localhost:11434")
    ollama_model = config.get("ollama", {}).get("model", "qwen2.5:7b")
    ollama_timeout = config.get("ollama", {}).get("timeout", 300)
    ollama_num_predict = config.get("ollama", {}).get("num_predict", 120)
    ollama_num_ctx = config.get("ollama", {}).get("num_ctx", 1024)
    disclosure_text = config.get("affiliate", {}).get(
        "disclosure",
        "This post contains affiliate links. As an Amazon Associate I earn from qualifying purchases."
    )

    # Ensure directories exist
    PRODUCTS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    Path("data/logs").mkdir(parents=True, exist_ok=True)
    Path("data/discovery").mkdir(parents=True, exist_ok=True)

    # Handle Queue Maintenance / Automatic Replenishment Flow
    if args.maintain_queue or args.replenish_queue:
        from publishing.replenisher import (
            QueueReplenisher,
            DEFAULT_MAINTENANCE_LOG_FILE
        )
        from publishing.queue import DEFAULT_QUEUE_FILE
        queue_file = Path(args.queue_file) if args.queue_file else DEFAULT_QUEUE_FILE
        queries_list = [q.strip() for q in args.discovery_queries.split(",")] if args.discovery_queries else None

        print(f"[INFO] Running automatic queue maintenance (Target: {args.target_capacity} products)...", flush=True)
        try:
            report = QueueReplenisher.maintain_queue(
                queue_file=queue_file,
                config=config,
                target_capacity=args.target_capacity,
                discovery_queries=queries_list,
                now_override=args.now,
                log_file=args.log_file or DEFAULT_MAINTENANCE_LOG_FILE,
                history_file=args.history_file,
                dry_run=not args.live_publish,
                force_discovery=args.discovery
            )
        except Exception as e:
            print(f"[ERROR] Queue maintenance failed: {e}", file=sys.stderr, flush=True)
            sys.exit(1)

        print("\n=== QUEUE MAINTENANCE REPORT ===", flush=True)
        print(f"Replenished: {report['replenished']}", flush=True)
        print(f"Initial Future Capacity: {report['initial_future_capacity']} / Target: {report['target_capacity']}", flush=True)
        print(f"New Products Added: {report['items_added_count']}", flush=True)
        print(f"Final Future Capacity: {report['final_future_capacity']} / Target: {report['target_capacity']}", flush=True)
        print(f"Candidates Evaluated: {report['candidates_evaluated']}", flush=True)
        print(f"Candidates Rejected: {report['candidates_rejected']}", flush=True)
        if report.get("rejection_summary"):
            print(f"Rejection Summary: {report['rejection_summary']}", flush=True)
        if report.get("items_added"):
            print("\nNewly Scheduled Products:", flush=True)
            for itm in report["items_added"]:
                print(f"- ASIN: {itm['asin']} | Day: {itm['scheduled_date']} Slot: {itm['daily_slot']} ({itm['scheduled_at']}) | Title: {itm['title'][:60]}...", flush=True)
        print(f"\nQueue updated: {queue_file}", flush=True)
        print("--------------------------------\n", flush=True)
        return

    # Handle Daily Queue Publishing Flow
    if args.publish_queue:
        from publishing.queue import (
            QueuePublisher,
            DEFAULT_QUEUE_FILE
        )
        queue_file = Path(args.queue_file) if args.queue_file else DEFAULT_QUEUE_FILE
        if not queue_file.exists():
            print(f"[ERROR] Publishing queue file not found at: {queue_file}", file=sys.stderr, flush=True)
            sys.exit(1)

        print(f"[INFO] Evaluating publishing queue for scheduled posts...", flush=True)
        try:
            report = QueuePublisher.publish_due_items(
                queue_file=queue_file,
                config=config,
                now_str=args.now,
                dry_run=not args.live_publish,
                slot_filter=args.force_slot,
                log_file=args.log_file
            )
        except Exception as e:
            print(f"[ERROR] Daily publishing failed: {e}", file=sys.stderr, flush=True)
            sys.exit(1)

        print("\n=== QUEUE PUBLISHING REPORT ===", flush=True)
        print(f"Mode: {report['mode']}", flush=True)
        print(f"Current Time (Europe/Budapest): {report['evaluated_at']}", flush=True)
        print(f"Total Queue Items: {report['total_items_in_queue']}", flush=True)
        print(f"Due Items Found: {report['due_items_count']}", flush=True)
        print(f"Successfully Processed: {report['published_count']}", flush=True)
        if report.get("partially_published_count", 0) > 0:
            print(f"Partially Processed: {report['partially_published_count']}", flush=True)
        if report.get("failed_count", 0) > 0:
            print(f"Failed: {report['failed_count']}", flush=True)

        if report.get("items_processed"):
            print("\nProcessed Items:", flush=True)
            for item_info in report["items_processed"]:
                print(f"- Item: {item_info['item_id']} | ASIN: {item_info['asin']} | Slot: {item_info['slot']} ({item_info['scheduled_at']}) | Status: {item_info['status']}", flush=True)

        print(f"\nQueue updated: {queue_file}", flush=True)
        print("-------------------------------\n", flush=True)
        return

    # Handle Multi-Product Publishing Queue Building Flow
    if args.build_queue:
        from publishing.queue import (
            QueueBuilder,
            parse_affiliate_urls,
            DEFAULT_AFFILIATE_URLS_FILE,
            DEFAULT_QUEUE_FILE,
            DEFAULT_QUEUE_DIR,
            DEFAULT_INPUT_DIR,
        )
        DEFAULT_QUEUE_DIR.mkdir(parents=True, exist_ok=True)
        DEFAULT_INPUT_DIR.mkdir(parents=True, exist_ok=True)

        input_file = Path(args.input_urls) if args.input_urls else DEFAULT_AFFILIATE_URLS_FILE
        queue_file = Path(args.queue_file) if args.queue_file else DEFAULT_QUEUE_FILE

        if not input_file.exists():
            if args.url and args.url.strip():
                urls = [args.url.strip()]
            else:
                print(f"[ERROR] Affiliate URLs input file not found at: {input_file}", file=sys.stderr, flush=True)
                sys.exit(1)
        else:
            urls = parse_affiliate_urls(input_file)
            if not urls and args.url and args.url.strip():
                urls = [args.url.strip()]

        if not urls:
            print(f"[ERROR] No valid Amazon affiliate URLs found in {input_file}.", file=sys.stderr, flush=True)
            sys.exit(1)

        print(f"[INFO] Building publishing queue for {len(urls)} products...", flush=True)
        queue = QueueBuilder.build_queue(
            urls=urls,
            start_date=args.start_date,
            total_days=args.days,
            products_per_day=args.per_day,
            output_file=queue_file
        )

        print("\n=== PUBLISHING QUEUE SUMMARY ===", flush=True)
        print(queue.format_summary(), flush=True)
        print(f"Queue file: {queue_file}", flush=True)
        print("--------------------------------\n", flush=True)
        return

    affiliate_url = args.url.strip()
    if not affiliate_url:
        print("[ERROR] No URL provided. Use --build-queue or --publish-queue to process publishing queues, or pass a product URL.", file=sys.stderr, flush=True)
        sys.exit(1)



    print("[INFO] Loading product", flush=True)

    # 2. Check Cache / Scrape
    product: Product = None
    is_cached, cached_product, found_key = load_cached_product(affiliate_url)

    if is_cached and not args.force_scrape:
        product = cached_product
        print(f"[OK] Product loaded from cache ({found_key}.json)", flush=True)
        print("[OK] Product data normalized", flush=True)
        cache_key = found_key
    else:
        # Scrape with Selenium + Firefox
        try:
            product = scrape_amazon_product(
                affiliate_url=affiliate_url,
                headless=headless_setting,
                firefox_profile=firefox_profile
            )
            print("[OK] Amazon product scraped", flush=True)
        except Exception as e:
            print(f"[ERROR] {e}", file=sys.stderr, flush=True)
            sys.exit(1)

        # Normalize product data
        print("[OK] Product data normalized", flush=True)

        # Determine definitive cache key with scraped ASIN
        cache_key = get_cache_key(affiliate_url, product.asin)
        save_product_cache(product, cache_key)
        print("[OK] Product cached", flush=True)

    # 3. Setup Ollama Client
    ollama = OllamaClient(
        base_url=ollama_base_url,
        model=ollama_model,
        timeout=ollama_timeout,
        num_predict=ollama_num_predict,
        num_ctx=ollama_num_ctx
    )

    # 4. Generate Content
    if args.v1:
        # Legacy V1 flow
        try:
            generated_content = ollama.generate_recommendation(
                product=product,
                disclosure=disclosure_text
            )
            print("[OK] Ollama response generated", flush=True)
            out_file = GENERATED_DIR / f"{cache_key}_recommendation.txt"
            with open(out_file, "w", encoding="utf-8") as f:
                f.write(generated_content)

            print("\n=== PRODUCT ===", flush=True)
            print(f"Title: {product.title}", flush=True)
            if product.price:
                print(f"Price: {product.price}", flush=True)
            if product.asin:
                print(f"ASIN: {product.asin}", flush=True)
            print("\nFeatures:", flush=True)
            for feat in product.features:
                print(f"- {feat}", flush=True)
            print("\n=== GENERATED CONTENT ===\n", flush=True)
            print(generated_content, flush=True)
            print("\n=== AFFILIATE DISCLOSURE ===\n", flush=True)
            print(disclosure_text, flush=True)
        except Exception as e:
            print(f"[ERROR] {e}", file=sys.stderr, flush=True)
            sys.exit(1)
    else:
        # Active multi-platform flow (Facebook, Pinterest, Threads)
        generator = ContentGenerator(ollama_client=ollama)
        try:
            if args.platform == "all":
                print("[INFO] Generating platform-specific content (Facebook, Pinterest, Threads)...", flush=True)
                social_data = generator.generate_all_platforms(
                    product=product,
                    disclosure=disclosure_text
                )
                print("[OK] All platform responses generated", flush=True)

                saved_path = ContentGenerator.save_social_content(
                    social_data=social_data,
                    cache_key=cache_key,
                    target_dir=GENERATED_DIR
                )
                print("[OK] Social content saved", flush=True)

                # V3 Content Package
                package = ContentPackage.build_package(
                    product=product,
                    social_data=social_data,
                    cache_file_path=f"data/products/{cache_key}.json",
                    model_name=ollama.model
                )
                package_path = ContentPackage.save_content_package(
                    package=package,
                    cache_key=cache_key,
                    target_dir=GENERATED_DIR
                )
                print(f"[OK] Content package saved ({package['status']})", flush=True)

                print_v2_preview(
                    social_data=social_data,
                    saved_file_path=saved_path,
                    package_path=package_path,
                    package_status=package["status"]
                )

                # V4 Publishing Execution (DRY_RUN by default)
                if args.publish or args.live_publish:
                    pub_config = PublishingConfig.from_dict(config, dry_run_override=not args.live_publish)
                    publisher = SocialPublisher(config=pub_config)
                    pub_report = publisher.publish_package(package)
                    print(f"=== PUBLISHING ROUTING (DRY_RUN: {pub_report['mode'] == 'DRY_RUN'}) ===", flush=True)
                    for p_name, p_res in pub_report.get("results", {}).items():
                        board_str = f" / Board: {p_res.get('board')}" if p_res.get("board") else ""
                        print(f"- {p_name.upper():<10} -> Account: {p_res.get('account')}{board_str} | Status: {p_res.get('status')}", flush=True)
                    print("------------------------------------------------------------\n", flush=True)

            else:
                platform_choice = args.platform
                print(f"[INFO] Generating content for {platform_choice}...", flush=True)
                content = generator.generate_for_platform(
                    platform=platform_choice,
                    product=product,
                    disclosure=disclosure_text
                )
                print(f"[OK] {platform_choice} content generated", flush=True)

                single_social_data = {
                    "asin": product.asin or "",
                    "product_title": product.title,
                    "generated_with": ollama.model,
                    "platforms": {platform_choice: content}
                }
                saved_path = ContentGenerator.save_social_content(
                    social_data=single_social_data,
                    cache_key=f"{cache_key}_{platform_choice}",
                    target_dir=GENERATED_DIR
                )
                print(f"\n=== {platform_choice.upper()} ===", flush=True)
                if platform_choice == "pinterest":
                    print(f"Title:\n{content.get('title')}\n\nDescription:\n{content.get('description')}", flush=True)
                else:
                    print(content.get("text"), flush=True)

                print(f"\nSaved: {saved_path}", flush=True)

        except Exception as e:
            print(f"[ERROR] {e}", file=sys.stderr, flush=True)
            sys.exit(1)


if __name__ == "__main__":
    main()
