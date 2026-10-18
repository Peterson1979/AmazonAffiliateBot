import sys
import os
import json
from pathlib import Path
from datetime import datetime

# Load dotenv if present
try:
    from dotenv import load_dotenv
    load_dotenv(Path(r"C:\Users\opeti\AmazonAffiliateBot\.env"))
except Exception as e:
    print(f"Dotenv note: {e}")

sys.path.insert(0, r"C:\Users\opeti\AmazonAffiliateBot")

from publishing.config import PublishingConfig
from publishing.publisher import SocialPublisher, STATUS_PUBLISHED, STATUS_FAILED, STATUS_WOULD_PUBLISH
from publishing.queue import PublishingQueue, QueuePublisher, get_budapest_timezone, STATUS_READY, STATUS_PARTIALLY_PUBLISHED

def publish_first_eligible():
    queue_file = Path(r"C:\Users\opeti\AmazonAffiliateBot\data\queue\publishing_queue.json")
    if not queue_file.exists():
        print(f"ERROR: Queue file not found at {queue_file}")
        sys.exit(1)

    queue = PublishingQueue.load(queue_file)

    # Explicitly target product B01NBJEKX2 as instructed
    target_item = None
    for item in queue.items:
        if item.asin == "B01NBJEKX2":
            target_item = item
            break

    if not target_item:
        print("ERROR: Target item B01NBJEKX2 not found in queue.")
        sys.exit(1)

    print(f"Selected item: {target_item.queue_item_id}")
    print(f"ASIN: {target_item.asin}")
    print(f"Product Name: {target_item.product_name}")
    print(f"Original URL: {target_item.original_url}")
    print(f"Package Reference: {target_item.package_reference}")

    # Load content package
    pkg_path = Path(target_item.package_reference)
    if not pkg_path.exists():
        pkg_path = Path(r"C:\Users\opeti\AmazonAffiliateBot") / target_item.package_reference

    if not pkg_path.exists():
        print(f"ERROR: Package file not found: {pkg_path}")
        sys.exit(1)

    with open(pkg_path, "r", encoding="utf-8") as f:
        package = json.load(f)

    if package.get("status") != "READY_FOR_REVIEW":
        print(f"ERROR: Package status is {package.get('status')}, expected READY_FOR_REVIEW.")
        sys.exit(1)

    # Initialize SocialPublisher in LIVE mode
    pub_config = PublishingConfig.from_dict({}, dry_run_override=False)
    publisher = SocialPublisher(config=pub_config, dry_run=False)

    print("\n--- EXECUTING LIVE PUBLISHING ---")
    pub_report = publisher.publish_package(package, dry_run=False)
    platform_results = pub_report.get("results", {})

    print("\n--- PLATFORM RESULTS ---")
    for p_name, p_res in platform_results.items():
        print(f"Platform: {p_name.upper()} | Status: {p_res.get('status')}")
        if p_res.get("post_id"):
            print(f"  Post ID: {p_res.get('post_id')}")
        if p_res.get("photo_id"):
            print(f"  Photo ID: {p_res.get('photo_id')}")
        if p_res.get("pin_id"):
            print(f"  Pin ID: {p_res.get('pin_id')}")
        if p_res.get("pin_url"):
            print(f"  Pin URL: {p_res.get('pin_url')}")
        if p_res.get("permalink"):
            print(f"  Permalink: {p_res.get('permalink')}")
        if p_res.get("permalink_url"):
            print(f"  Permalink URL: {p_res.get('permalink_url')}")
        if p_res.get("image_url"):
            print(f"  Image URL: {p_res.get('image_url')}")
        if p_res.get("verified_image_read") is not None:
            print(f"  Verified Image Read: {p_res.get('verified_image_read')}")
        if p_res.get("errors"):
            print(f"  Errors: {p_res.get('errors')}")
        if p_res.get("message"):
            print(f"  Message: {p_res.get('message')}")

    # Update item in queue only from evidence-backed live responses.
    now_dt = datetime.now(get_budapest_timezone())
    def confirmed_live(result):
        return (
            result.get("status") == STATUS_PUBLISHED
            and result.get("mode") == "LIVE"
            and any(result.get(key) for key in ("post_id", "pin_id", "photo_id", "permalink", "permalink_url", "pin_url"))
        )

    target_item.publishing_results = platform_results

    required = {"facebook", "pinterest", "pinterest_lifemodehq", "threads"}
    active_results = {k: v for k, v in platform_results.items() if k in required}
    all_succeeded = set(active_results) == required and all(confirmed_live(r) for r in active_results.values())
    any_succeeded = any(confirmed_live(r) for r in active_results.values())

    if all_succeeded:
        target_item.status = STATUS_PUBLISHED
    elif any_succeeded:
        target_item.status = STATUS_PARTIALLY_PUBLISHED
    else:
        target_item.status = STATUS_FAILED
    if any_succeeded:
        target_item.published_at = now_dt.isoformat()

    queue.recalculate_counts()
    queue.save(queue_file)
    print(f"\nQueue updated successfully. Item status is now: {target_item.status}")

    # Log to daily_publisher.log
    log_file = Path(r"C:\Users\opeti\AmazonAffiliateBot\data\logs\daily_publisher.log")
    QueuePublisher.log_event("=== ONE-TIME LIVE PUBLISHING RUN ===", log_file=log_file)
    QueuePublisher.log_event(
        f"Item '{target_item.queue_item_id}' | ASIN: {target_item.asin} | URL: {target_item.original_url} | Status: {target_item.status}",
        log_file=log_file
    )
    for p_name, p_res in platform_results.items():
        QueuePublisher.log_event(
            f"  Platform '{p_name}': Status={p_res.get('status')} | Message={p_res.get('message', '')} | Errors={p_res.get('errors', [])}",
            log_file=log_file
        )

    # Output JSON summary for processing
    print("\n--- FINAL REPORT JSON ---")
    print(json.dumps({
        "item_id": target_item.queue_item_id,
        "asin": target_item.asin,
        "title": target_item.product_name,
        "affiliate_url": target_item.original_url,
        "item_status": target_item.status,
        "published_at": target_item.published_at,
        "results": platform_results
    }, indent=2))

if __name__ == "__main__":
    publish_first_eligible()
