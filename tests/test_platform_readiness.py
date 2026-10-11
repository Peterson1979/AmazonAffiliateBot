"""
Platform Configuration & Readiness Audit Script.
Safely checks credentials and routing configuration without printing secret values or calling live APIs.
Verifies DRY_RUN safety and Instagram exclusion.
"""

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from publishing.config import PublishingConfig, DEFAULT_TARGET_ACCOUNTS
from publishing.publisher import SocialPublisher, SUPPORTED_PLATFORMS


def audit_platform_readiness():
    print("=== PLATFORM CONFIGURATION READINESS AUDIT ===")

    config = PublishingConfig.from_dict({})
    publisher = SocialPublisher(config=config)

    print(f"\n[GLOBAL MODE]")
    print(f"  DRY_RUN Status: {config.dry_run} (Live publishing prevented: {config.dry_run is True})")
    assert config.dry_run is True, "DRY_RUN MUST BE TRUE FOR PRODUCTION READINESS PHASE!"

    print(f"\n[ACTIVE SUPPORTED PLATFORMS]")
    print(f"  Platforms: {list(SUPPORTED_PLATFORMS)}")
    assert "instagram" not in SUPPORTED_PLATFORMS, "Instagram must NOT be in supported platforms!"
    assert set(SUPPORTED_PLATFORMS) == {"x", "facebook", "pinterest", "threads"}

    print(f"\n[TARGET ACCOUNTS & ROUTING]")
    for plat in SUPPORTED_PLATFORMS:
        acc_info = config.accounts.get(plat, {})
        print(f"  -> {plat.upper():<10}: Account = {acc_info.get('account')}", end="")
        if "board" in acc_info:
            print(f" | Board = '{acc_info.get('board')}' (ID: {acc_info.get('board_id')})", end="")
        print()

    assert config.accounts.get("x", {}).get("account") == "@BitGoldone"
    assert config.accounts.get("facebook", {}).get("account") == "LifeModeHQ"
    assert config.accounts.get("pinterest", {}).get("account") == "dailyideasforyou"
    assert config.accounts.get("threads", {}).get("account") == "lifemodehq"

    print(f"\n[CREDENTIALS STATUS (SAFE AUDIT - NO SECRETS EXPOSED)]")
    for plat in SUPPORTED_PLATFORMS:
        adapter = publisher.adapters.get(plat)
        if adapter:
            has_creds, missing = adapter.check_credentials()
            status_str = "CONFIGURED" if has_creds else f"MISSING ({', '.join(missing)})"
            print(f"  -> {plat.upper():<10}: {status_str} [DRY_RUN Simulation Ready: YES]")

    print(f"\n[ADAPTER SIMULATION READINESS]")
    test_content = {
        "x": {"content": "Test post for X", "status": "VALID"},
        "facebook": {"content": "Test post for Facebook", "status": "VALID"},
        "pinterest": {"title": "Test Title", "description": "Test Desc", "image_url": "https://example.com/img.jpg", "link": "https://amzn.to/test", "status": "VALID"},
        "threads": {"content": "Test post for Threads", "status": "VALID"}
    }
    dummy_pkg = {
        "asin": "B0TESTREAD1",
        "product_name": "Readiness Test Product",
        "status": "READY_FOR_REVIEW",
        "platforms": test_content
    }
    res = publisher.publish_package(dummy_pkg, dry_run=True)
    print(f"  -> Simulation Status: {res.get('status')} (Mode: {res.get('mode')})")
    assert res.get("mode") == "DRY_RUN"
    assert res.get("status") == "WOULD_PUBLISH"

    print("\n[SUCCESS] Platform readiness audit passed cleanly with zero live calls!")
    return True


if __name__ == "__main__":
    success = audit_platform_readiness()
    sys.exit(0 if success else 1)
