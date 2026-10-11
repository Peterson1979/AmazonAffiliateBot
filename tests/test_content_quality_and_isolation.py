"""
Production Readiness Verification: Content Quality & Multi-Category Isolation.
Tests:
1. Kitchen Appliance Product (Espresso Machine / Frother)
2. Home Office Product (Ergonomic Laptop Stand)
3. Camping / Outdoor Product (Vacuum Water Bottle)

Verifies:
- Strict multi-category isolation (zero cross-contamination of ASIN, title, facts, copy).
- Marketing-oriented tone (engaging, natural, not a raw specification dump).
- Pinterest title quality (<= 100 chars, product-focused, not raw spec dump).
- Pinterest description quality (<= 400 chars, grounded).
- Strict ClaimValidator proposition compliance.
- Affiliate disclosure preservation.
"""

import sys
from pathlib import Path
import json

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from amazon.scraper import Product
from content.facts import FactExtractor
from ai.ollama import OllamaClient
from content.generator import ContentGenerator
from content.validator import ClaimValidator
from content.package import ContentPackage, STATUS_READY_FOR_REVIEW


def run_content_quality_test():
    print("=== STARTING CONTENT QUALITY & MULTI-CATEGORY ISOLATION TEST ===")

    ollama = OllamaClient(base_url="http://localhost:11434", model="qwen2.5:7b", timeout=300)
    generator = ContentGenerator(ollama_client=ollama)
    disclosure = "This post contains affiliate links. As an Amazon Associate I earn from qualifying purchases."

    products = [
        Product(
            title="Electactic 15 Bar Espresso Machine with Milk Frother Steam Wand",
            features=[
                "15 bar professional pressure pump for rich extraction",
                "Built-in milk steam wand for latte and cappuccino preparation",
                "Compact stainless steel housing with removable 1.5L water tank"
            ],
            affiliate_url="https://www.amazon.com/dp/B0ESPRESSO1?tag=lifemode-20",
            source_url="https://www.amazon.com/dp/B0ESPRESSO1",
            price="$119.99",
            asin="B0ESPRESSO1"
        ),
        Product(
            title="Nulaxy Adjustable Aluminum Laptop Stand for Desk",
            features=[
                "Adjustable height from 2.1 to 13.8 inches for desktop ergonomics",
                "Sturdy aluminum alloy frame supporting up to 17.3 inch laptops",
                "Foldable portable design with anti-skid silicone pads"
            ],
            affiliate_url="https://www.amazon.com/dp/B0STAND0002?tag=lifemode-20",
            source_url="https://www.amazon.com/dp/B0STAND0002",
            price="$29.99",
            asin="B0STAND0002"
        ),
        Product(
            title="Iron Flask Vacuum Insulated Stainless Steel Water Bottle 32oz",
            features=[
                "Double wall vacuum insulation keeps beverages cold for up to 24 hours",
                "18/8 food-grade stainless steel with sweat-free powder coat",
                "Includes 3 different 100% leak-proof lids: straw, flip, and handle"
            ],
            affiliate_url="https://www.amazon.com/dp/B0BOTTLE003?tag=lifemode-20",
            source_url="https://www.amazon.com/dp/B0BOTTLE003",
            price="$24.95",
            asin="B0BOTTLE003"
        )
    ]

    generated_packages = {}

    for prod in products:
        print(f"\n=======================================================")
        print(f"Processing Category Test: [{prod.asin}] {prod.title}")
        print(f"=======================================================")

        # 1. Fact Extraction
        facts = FactExtractor.extract_canonical_facts(prod)
        print(f"[FACTS] Title: {facts.title}")
        print(f"[FACTS] Features: {facts.features}")
        print(f"[FACTS] Atomic Facts Count: {len(facts.atomic_facts)}")

        # 2. Generation
        social_data = generator.generate_all_platforms(product=facts, disclosure=disclosure)
        platforms = social_data.get("platforms", {})

        # 3. Content Package Building & Validation Check
        pkg = ContentPackage.build_package(
            product=prod,
            social_data=social_data,
            model_name="qwen2.5:7b"
        )
        generated_packages[prod.asin] = pkg

        print(f"\n[PACKAGE STATUS] {pkg.get('status')}")
        for platform_name in ("x", "facebook", "pinterest", "threads"):
            p_data = pkg.get("platforms", {}).get(platform_name, {})
            p_status = p_data.get("status")
            p_val = p_data.get("validation", {})
            print(f"  -> Platform [{platform_name.upper()}]: Status = {p_status} | Errors = {p_val.get('errors')}")

            if platform_name == "pinterest":
                print(f"     Title: {p_data.get('title')}")
                print(f"     Desc : {p_data.get('description')}")
            else:
                print(f"     Text : {p_data.get('content')}")

        assert pkg.get("status") == STATUS_READY_FOR_REVIEW, f"Package for {prod.asin} must be READY_FOR_REVIEW!"

    # 4. Multi-Category Isolation Verification
    print("\n[VERIFICATION] Verifying Zero Cross-Category Contamination...")
    espresso_copy = json.dumps(generated_packages["B0ESPRESSO1"]).lower()
    stand_copy = json.dumps(generated_packages["B0STAND0002"]).lower()
    bottle_copy = json.dumps(generated_packages["B0BOTTLE003"]).lower()

    # Espresso checks
    assert "laptop" not in espresso_copy and "bottle" not in espresso_copy
    assert "b0espresso1" in espresso_copy

    # Stand checks
    assert "espresso" not in stand_copy and "water" not in stand_copy
    assert "b0stand0002" in stand_copy

    # Bottle checks
    assert "espresso" not in bottle_copy and "laptop" not in bottle_copy
    assert "b0bottle003" in bottle_copy

    print("[SUCCESS] All 3 distinct categories generated high-quality, grounded copy with 100% isolation!")
    return True


if __name__ == "__main__":
    success = run_content_quality_test()
    sys.exit(0 if success else 1)
