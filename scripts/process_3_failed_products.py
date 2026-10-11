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


TARGET_3_ASINS = [
    "B0BJKRYCZF",
    "B0C235GNV9",
    "B0D6G5S35J",
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

    # Generate across all 4 platforms
    platforms_results = {}
    validation_metadata = {}

    for platform_key in ("x", "facebook", "pinterest", "threads"):
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
        model_name=generator.ollama.model
    )

    # Save to generated dir
    package_path = Path("data/generated") / f"asin_{asin}_content_package.json"
    with open(package_path, "w", encoding="utf-8") as f:
        json.dump(package, f, indent=2)

    social_path = Path("data/generated") / f"asin_{asin}_social.json"
    with open(social_path, "w", encoding="utf-8") as f:
        json.dump(social_data, f, indent=2)

    print(f"Package Status for {asin}: {package.get('status')}", flush=True)
    return package


def main():
    print(f"Targeting 3 Failed ASINs: {TARGET_3_ASINS}\n", flush=True)

    config = load_application_config()
    disclosure = config.get("disclosure", DEFAULT_AFFILIATE_DISCLOSURE)

    generator = ContentGenerator()
    print("Checking Ollama connection...", flush=True)
    generator.ollama.verify_ready()
    print("Ollama is ready.\n", flush=True)

    results = []
    start_time = time.time()

    for idx, asin in enumerate(TARGET_3_ASINS, 1):
        print(f"\n[{idx}/3] Starting {asin}...", flush=True)
        pkg_dict = process_product(asin, generator, disclosure)
        results.append(pkg_dict)

    duration = time.time() - start_time
    print(f"\n=======================================================", flush=True)
    print(f"3-PRODUCT RERUN COMPLETE in {duration:.1f}s", flush=True)
    print(f"=======================================================", flush=True)

    ready_count = sum(1 for p in results if p.get("status") == STATUS_READY_FOR_REVIEW)
    failed_count = sum(1 for p in results if p.get("status") == STATUS_FAILED_VALIDATION)

    print(f"Total Processed: {len(results)}", flush=True)
    print(f"READY_FOR_REVIEW: {ready_count}", flush=True)
    print(f"FAILED_VALIDATION: {failed_count}", flush=True)

    summary_out = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total_processed": len(results),
        "ready_count": ready_count,
        "failed_count": failed_count,
        "packages": results
    }

    summary_path = Path("data/generated") / "rerun_3_products_summary.json"
    with open(summary_path, "w", encoding="utf-8") as f:
        json.dump(summary_out, f, indent=2)
    print(f"Summary saved to {summary_path}", flush=True)


if __name__ == "__main__":
    main()
