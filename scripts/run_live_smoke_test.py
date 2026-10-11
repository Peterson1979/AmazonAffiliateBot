import sys
import os
import json
from pathlib import Path

# Load dotenv if present
try:
    import dotenv
    dotenv.load_dotenv(Path(r"C:\Users\opeti\AmazonAffiliateBot\.env"))
except Exception as e:
    print(f"Dotenv loading note: {e}")

sys.path.insert(0, r"C:\Users\opeti\AmazonAffiliateBot")

from publishing.config import PublishingConfig
from publishing.publisher import SocialPublisher, STATUS_PUBLISHED, STATUS_FAILED, STATUS_WOULD_PUBLISH
from web.services import load_application_config


def run_smoke_test():
    target_url = "https://link.amazon/B0isaIph6"
    asin = "B0H1W18KGL"

    print("=======================================================")
    print("LIVE SMOKE TEST EXECUTION")
    print("=======================================================")
    print(f"Target Affiliate URL: {target_url}")
    print(f"Target ASIN: {asin}")

    # Load content package
    package_path = Path("data/generated") / f"asin_{asin}_content_package.json"
    if not package_path.exists():
        print(f"ERROR: Package file {package_path} not found.")
        sys.exit(1)

    with open(package_path, "r", encoding="utf-8") as f:
        package = json.load(f)

    print(f"Loaded Content Package Status: {package.get('status')}")
    if package.get("status") != "READY_FOR_REVIEW":
        print(f"ERROR: Package is not READY_FOR_REVIEW: status={package.get('status')}. ABORTING live smoke test.")
        sys.exit(1)

    # Verify all 4 platforms in package are VALID
    platforms = package.get("platforms", {})
    active_platforms = ("x", "facebook", "pinterest", "threads")
    for p in active_platforms:
        p_data = platforms.get(p, {})
        p_val_status = p_data.get("status")
        if p_val_status != "VALID":
            print(f"ERROR: Platform '{p}' status is '{p_val_status}', not 'VALID'. ABORTING live smoke test.")
            sys.exit(1)
        print(f"  Platform '{p}': VALID")

    # Load PublishingConfig with dry_run=False explicitly for this one run
    app_config = load_application_config()
    pub_config = PublishingConfig.from_dict(app_config, dry_run_override=False)

    print("\nInitializing SocialPublisher in LIVE mode...")
    publisher = SocialPublisher(config=pub_config, dry_run=False)

    results = {}
    stop_early = False

    for p_name in active_platforms:
        if stop_early:
            print(f"\nSkipping '{p_name}' due to previous failure.")
            results[p_name] = {
                "platform": p_name,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": "SKIPPED",
                "message": "Skipped because previous platform failed."
            }
            continue

        print(f"\n-------------------------------------------------------")
        print(f"Publishing to platform: {p_name.upper()}...")
        adapter = publisher.adapters.get(p_name)
        p_content = platforms.get(p_name, {})
        account_cfg = pub_config.accounts.get(p_name, {})

        try:
            pub_res = adapter.publish(
                content=p_content,
                account_config=account_cfg,
                dry_run=False
            )
            results[p_name] = pub_res
            status = pub_res.get("status")
            print(f"  Result: status={status}")
            print(f"  Details: {json.dumps(pub_res, indent=2)}")

            if status != STATUS_PUBLISHED:
                print(f"  WARNING: Platform '{p_name}' did not return STATUS_PUBLISHED (got '{status}').")
                # Check failure behavior requirement:
                # If the first live platform fails unexpectedly, STOP the remaining live publishing attempts.
                if p_name == "x" and "missing" in str(pub_res.get("errors", "")).lower() and "credentials" in str(pub_res.get("errors", "")).lower():
                    print("  (X lacks API keys in .env; proceeding to test remaining configured platforms.)")
                else:
                    print(f"  Stopping remaining platforms due to failure on {p_name}.")
                    stop_early = True

        except Exception as e:
            print(f"  EXCEPTION on {p_name}: {e}")
            results[p_name] = {
                "platform": p_name,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": [str(e)],
                "message": f"Exception during live publishing: {e}"
            }
            stop_early = True

    # Save live smoke test result
    smoke_result_path = Path("data/generated/live_smoke_test_result.json")
    with open(smoke_result_path, "w", encoding="utf-8") as f:
        json.dump({
            "asin": asin,
            "affiliate_url": target_url,
            "title": package.get("title") or package.get("platforms", {}).get("pinterest", {}).get("title", "5FT Halloween Swinging Ghost Outdoor Decor,Spooky Hanging Ghost Decorations"),
            "timestamp": package.get("generated_at"),
            "results": results
        }, f, indent=2)

    print("\n=======================================================")
    print("LIVE SMOKE TEST RUN FINISHED")
    print(f"Results saved to {smoke_result_path}")
    print("=======================================================")


if __name__ == "__main__":
    run_smoke_test()
