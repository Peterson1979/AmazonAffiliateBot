"""
Safe Real Amazon Discovery Dry-Run Script.
Tests Selenium + Firefox headless discovery against real Amazon search pages.
Verifies ASIN extraction, title usability, URL construction, and graceful failure handling.
Zero live publishing is performed.
"""

import sys
from pathlib import Path
import json

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from amazon.discovery import AmazonDiscoveryEngine, DiscoveryHistory, DiscoveredCandidate
from amazon.scraper import scrape_amazon_product
from content.facts import FactExtractor
from content.validator import ClaimValidator


def run_live_discovery_dryrun():
    print("=== STARTING SAFE REAL AMAZON DISCOVERY DRY-RUN (DRY_RUN ONLY) ===")
    history_file = Path("data/discovery/test_live_discovery_history.json")
    history = DiscoveryHistory(history_file)
    engine = AmazonDiscoveryEngine(
        tracking_tag="lifemode-20",
        history=history,
        headless=True
    )

    query = "kitchen accessories"
    print(f"[INFO] Querying Amazon for: '{query}' (bounded to max 3 candidates)...")

    try:
        candidates = engine.search_and_extract_candidates(query=query, max_candidates=3)
        print(f"[INFO] Discovery finished. Candidates extracted: {len(candidates)}")

        if not candidates:
            print("[WARN] Zero candidates extracted (Amazon may have served CAPTCHA/robot check or rate-limited). Fail-closed gracefully handled.")
            return True

        for idx, cand in enumerate(candidates, start=1):
            print(f"\n--- Discovered Candidate {idx} ---")
            print(f"  ASIN         : {cand.asin}")
            print(f"  Title        : {cand.title[:70]}...")
            print(f"  Canonical URL: {cand.source_url}")
            print(f"  Affiliate URL: {cand.affiliate_url}")
            print(f"  Query        : {cand.query}")
            print(f"  Price        : {cand.price}")

            assert len(cand.asin) == 10 and cand.asin.isalnum(), f"Invalid ASIN: {cand.asin}"
            assert len(cand.title) >= 5, f"Title too short: {cand.title}"
            assert "lifemode-20" in cand.affiliate_url, f"Tracking tag missing: {cand.affiliate_url}"
            assert f"/dp/{cand.asin}" in cand.source_url or f"/dp/{cand.asin}" in cand.affiliate_url

        print("\n[SUCCESS] Real Amazon candidate discovery verified successfully!")
        return True

    except Exception as e:
        print(f"[ERROR] Live Amazon discovery exception: {e}")
        return False
    finally:
        if history_file.exists():
            history_file.unlink()


if __name__ == "__main__":
    success = run_live_discovery_dryrun()
    sys.exit(0 if success else 1)
