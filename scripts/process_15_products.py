import sys
import os
import json
import time
from pathlib import Path
from typing import Dict, Any, List

sys.path.insert(0, r"C:\Users\opeti\AmazonAffiliateBot")

from amazon.scraper import Product
from content.facts import FactExtractor
from content.generator import ContentGenerator
from content.package import ContentPackage, STATUS_READY_FOR_REVIEW, STATUS_FAILED_VALIDATION
from web.services import load_application_config, DEFAULT_AFFILIATE_DISCLOSURE


TARGET_ASINS = [
    "B01NBJEKX2",
    "B08CRPWQHB",
    "B08GH41N87",
    "B09SH38JRR",
    "B09ZV2TX28",
    "B0BJKRYCZF",
    "B0C235GNV9",
    "B0C51CZJ83",
    "B0C65T7727",
    "B0CNCL35CH",
    "B0D6G5S35J",
    "B0D9TCFRJ5",
    "B0DHKSF81K",
    "B0DTHHK2S1",
    "B0H1W18KGL",
]


def process_product(asin: str, generator: ContentGenerator, disclosure: str) -> Dict[str, Any]:
    product_path = Path(f"data/products/asin_{asin}.json")
    if not product_path.exists():
        print(f"[{asin}] ERROR: Product cache file does not exist at {product_path}", flush=True)
        return {
            "asin": asin,
            "status": STATUS_FAILED_VALIDATION,
            "errors": [f"Product file {product_path} missing."]
        }

    with open(product_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    product = Product(
        title=data.get("title", ""),
        features=data.get("features", []),
        affiliate_url=data.get("affiliate_url", ""),
        source_url=data.get("source_url", ""),
        price=data.get("price", ""),
        asin=data.get("asin", asin),
        image_url=data.get("image_url")
    )

    print(f"\n=======================================================", flush=True)
    print(f"Processing ASIN: {product.asin}", flush=True)
    print(f"Title: {product.title[:80]}...", flush=True)
    print(f"Affiliate URL: {product.affiliate_url}", flush=True)

    facts = FactExtractor.extract_canonical_facts(product)
    print(f"Canonical Facts Extracted: {len(facts.features)} features, {len(facts.source_numbers)} numbers, {len(facts.atomic_facts)} atomic facts", flush=True)

    # Generate across active platforms
    platforms_results = {}
    validation_metadata = {}

    for platform_key in ("facebook", "pinterest", "threads"):
        print(f"  -> Generating {platform_key.upper()}...", flush=True)
        try:
            res = generator.generate_for_platform(platform_key, facts, disclosure)
            platforms_results[platform_key] = res
            validation_metadata[platform_key] = generator.last_validation_metadata.get(platform_key, {
                "status": "VALID",
                "retry_count": 0,
                "errors": []
            })
            print(f"     Status: {validation_metadata[platform_key].get('status')}", flush=True)
        except Exception as e:
            err_msg = str(e)
            print(f"     FAILED: {err_msg}", flush=True)
            platforms_results[platform_key] = {"error": err_msg}
            validation_metadata[platform_key] = generator.last_validation_metadata.get(platform_key, {
                "status": STATUS_FAILED_VALIDATION,
                "retry_count": 1,
                "errors": [err_msg]
            })

    social_data = {
        "asin": facts.asin or asin,
        "affiliate_url": facts.affiliate_url or product.affiliate_url,
        "platforms": platforms_results,
        "validation_metadata": validation_metadata,
        "generated_with": generator.ollama.model
    }

    package = ContentPackage.build_package(
        product=product,
        social_data=social_data,
        cache_file_path=str(product_path)
    )

    # Save to generated directory
    ContentPackage.save_content_package(package, f"asin_{asin}")
    ContentGenerator.save_social_content(social_data, f"asin_{asin}")

    pkg_status = package.get("status")
    print(f"Package Status for {asin}: {pkg_status}", flush=True)
    return package


def main():
    cfg = load_application_config()
    disclosure = cfg.get("affiliate", {}).get("disclosure", DEFAULT_AFFILIATE_DISCLOSURE)
    print(f"Starting 15-Product Batch Validation Rerun...", flush=True)
    print(f"Authoritative Disclosure: {disclosure}", flush=True)
    print(f"Target Products ({len(TARGET_ASINS)}): {TARGET_ASINS}\n", flush=True)

    generator = ContentGenerator()
    results: List[Dict[str, Any]] = []

    start_time = time.time()
    for idx, asin in enumerate(TARGET_ASINS, 1):
        print(f"\n[{idx}/{len(TARGET_ASINS)}] Starting {asin}...", flush=True)
        pkg = process_product(asin, generator, disclosure)
        results.append(pkg)

    elapsed = time.time() - start_time
    print(f"\n=======================================================", flush=True)
    print(f"BATCH PROCESSING COMPLETE in {elapsed:.1f}s", flush=True)
    print(f"=======================================================", flush=True)

    ready_count = sum(1 for r in results if r.get("status") == STATUS_READY_FOR_REVIEW)
    failed_count = sum(1 for r in results if r.get("status") == STATUS_FAILED_VALIDATION)

    print(f"Total Processed: {len(results)}", flush=True)
    print(f"READY_FOR_REVIEW: {ready_count}", flush=True)
    print(f"FAILED_VALIDATION: {failed_count}", flush=True)

    # Save summary report JSON
    summary_path = Path("data/generated/batch_15_validation_summary.json")
    summary_data = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_processed": len(results),
        "ready_count": ready_count,
        "failed_count": failed_count,
        "packages": results
    }
    summary_path.write_text(json.dumps(summary_data, indent=2), encoding="utf-8")
    print(f"Summary saved to {summary_path}", flush=True)


if __name__ == "__main__":
    main()
