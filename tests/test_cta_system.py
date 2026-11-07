"""
Unit tests for the AmazonAffiliateBot Call-to-Action (CTA) System.
Verifies approved CTA recognition, non-repetitive platform allocation,
rejection of commercial/unapproved hype, factual validation integrity,
and affiliate URL preservation.
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from amazon.scraper import Product
from content.facts import FactExtractor
from content.prompts import (
    APPROVED_CTA_TEMPLATES,
    APPROVED_CTA_REGEX,
    get_approved_cta_for_platform,
    build_facebook_prompt,
    build_pinterest_prompt,
    build_threads_prompt,
)
from content.validator import ClaimValidator
from content.generator import ContentGenerator


class TestCallToActionSystem(unittest.TestCase):
    def setUp(self):
        self.sample_product = Product(
            title="Adjustable Aluminum Laptop Stand Desk Riser with 7 Inches Height Adjustment",
            features=[
                "Crafted from lightweight aluminum with an 11 lbs load capacity",
                "Offers up to 7 inches of height adjustment for laptops from 10 to 17.3 inches",
                "Folds flat to 10 x 8.7 x 1.8 inches and weighs 1.7 lb"
            ],
            affiliate_url="https://link.amazon/B0hwa4Nkm",
            original_affiliate_url="https://link.amazon/B0hwa4Nkm",
            source_url="https://www.amazon.co.uk/dp/B0GKGBWRTR",
            price="HUF 12,850.72",
            asin="B0GKGBWRTR"
        )
        self.facts = FactExtractor.extract_canonical_facts(self.sample_product)
        self.disclosure = "This post contains affiliate links. As an Amazon Associate I earn from qualifying purchases."

    def test_all_approved_templates_match_regex(self):
        """Every single template in APPROVED_CTA_TEMPLATES must match APPROVED_CTA_REGEX."""
        self.assertGreaterEqual(len(APPROVED_CTA_TEMPLATES), 10)
        for template in APPROVED_CTA_TEMPLATES:
            self.assertTrue(
                ClaimValidator.is_approved_cta(template),
                f"Template failed approval check: '{template}'"
            )
            extracted = ClaimValidator.extract_approved_cta(template)
            self.assertIsNotNone(extracted, f"Failed to extract approved CTA: '{template}'")

    def test_non_repetitive_platform_allocation(self):
        """For any given product, Facebook, Pinterest, and Threads must receive distinct CTAs."""
        for asin in ["B0GKGBWRTR", "B0BW93676T", "B0CTJGJL2T", "B08BRJHJF9"]:
            fb_cta = get_approved_cta_for_platform("facebook", seed=asin)
            pin_cta = get_approved_cta_for_platform("pinterest", seed=asin)
            th_cta = get_approved_cta_for_platform("threads", seed=asin)

            self.assertNotEqual(fb_cta, pin_cta, f"Facebook and Pinterest CTAs duplicated for ASIN {asin}")
            self.assertNotEqual(pin_cta, th_cta, f"Pinterest and Threads CTAs duplicated for ASIN {asin}")
            self.assertNotEqual(fb_cta, th_cta, f"Facebook and Threads CTAs duplicated for ASIN {asin}")

    def test_approved_ctas_pass_validation_facebook(self):
        """Facebook post with approved CTA passes claim and platform validation."""
        for cta in APPROVED_CTA_TEMPLATES[:4]:
            post_text = (
                f"This laptop riser is crafted from lightweight aluminum with an 11 lbs load capacity.\n\n"
                f"It offers up to 7 inches of height adjustment and accommodates laptops from 10 to 17.3 inches. {cta}\n\n"
                f"Link: {self.sample_product.affiliate_url}\n\n"
                f"{self.disclosure}"
            )
            content = {"text": post_text}
            is_valid, issues = ClaimValidator.validate_platform_content(
                platform="facebook",
                content=content,
                product=self.facts,
                disclosure=self.disclosure
            )
            self.assertTrue(is_valid, f"Approved CTA '{cta}' failed Facebook validation: {issues}")
            self.assertEqual(len(issues), 0)

    def test_approved_ctas_pass_validation_threads(self):
        """Threads post with approved CTA passes claim and platform validation."""
        for cta in APPROVED_CTA_TEMPLATES[:4]:
            post_text = (
                f"This aluminum stand adjusts up to 7 inches and fits laptops from 10 to 17.3 inches. {cta}\n\n"
                f"Link: {self.sample_product.affiliate_url}\n\n"
                f"{self.disclosure}"
            )
            content = {"text": post_text}
            is_valid, issues = ClaimValidator.validate_platform_content(
                platform="threads",
                content=content,
                product=self.facts,
                disclosure=self.disclosure
            )
            self.assertTrue(is_valid, f"Approved CTA '{cta}' failed Threads validation: {issues}")
            self.assertEqual(len(issues), 0)

    def test_approved_ctas_pass_validation_pinterest(self):
        """Pinterest description with approved CTA passes claim and platform validation."""
        for cta in APPROVED_CTA_TEMPLATES[:4]:
            desc = (
                f"Adjustable height up to 7 inches and fits laptops from 10 to 17.3 inches. {cta} "
                f"{self.sample_product.affiliate_url}\n\n{self.disclosure}"
            )
            content = {
                "title": "Adjustable Aluminum Laptop Stand",
                "description": desc
            }
            is_valid, issues = ClaimValidator.validate_platform_content(
                platform="pinterest",
                content=content,
                product=self.facts,
                disclosure=self.disclosure
            )
            self.assertTrue(is_valid, f"Approved CTA '{cta}' failed Pinterest validation: {issues}")
            self.assertEqual(len(issues), 0)

    def test_unapproved_commercial_hype_strictly_rejected(self):
        """Aggressive, unapproved purchase commands and sales hype must fail validation."""
        forbidden_ctas = [
            "Buy now before stock runs out!",
            "Order today for 50% discount.",
            "Click here to purchase this amazing product.",
            "Get yours today with free shipping.",
            "Best price guaranteed!",
            "Hurry, limited time deal on Amazon.",
            "Don't miss out on this clearance sale.",
            "Shop now while supplies last.",
        ]
        for bad_cta in forbidden_ctas:
            post_text = (
                f"This aluminum laptop stand adjusts up to 7 inches. {bad_cta}\n\n"
                f"Link: {self.sample_product.affiliate_url}\n\n"
                f"{self.disclosure}"
            )
            content = {"text": post_text}
            is_valid, issues = ClaimValidator.validate_platform_content(
                platform="threads",
                content=content,
                product=self.facts,
                disclosure=self.disclosure
            )
            self.assertFalse(is_valid, f"Unapproved CTA '{bad_cta}' was incorrectly allowed to pass!")
            self.assertGreater(len(issues), 0)

    def test_multiple_ctas_in_single_post_rejected(self):
        """A post with more than 1 approved CTA must be rejected to prevent repetitive clutter."""
        post_text = (
            f"This aluminum laptop stand adjusts up to 7 inches.\n\n"
            f"Explore the product details and available options below. Check the specifications and current availability.\n\n"
            f"Link: {self.sample_product.affiliate_url}\n\n"
            f"{self.disclosure}"
        )
        content = {"text": post_text}
        is_valid, issues = ClaimValidator.validate_platform_content(
            platform="threads",
            content=content,
            product=self.facts,
            disclosure=self.disclosure
        )
        self.assertFalse(is_valid, "Multiple CTAs should be rejected")
        self.assertTrue(any("Too many Call-to-Action" in iss for iss in issues))

    def test_factual_claim_validation_not_weakened(self):
        """Unconfirmed product claims (e.g. false 99 inches) must still fail even when an approved CTA is present."""
        post_text = (
            f"This aluminum laptop stand adjusts up to 99 inches. Explore the product details and available options below.\n\n"
            f"Link: {self.sample_product.affiliate_url}\n\n"
            f"{self.disclosure}"
        )
        content = {"text": post_text}
        is_valid, issues = ClaimValidator.validate_platform_content(
            platform="threads",
            content=content,
            product=self.facts,
            disclosure=self.disclosure
        )
        self.assertFalse(is_valid, "Factual error (99 inches) must fail validation")
        self.assertTrue(any("99" in iss for iss in issues))

    def test_affiliate_url_preservation_character_for_character(self):
        """Affiliate URL must remain strictly unchanged throughout the pipeline."""
        gen = ContentGenerator()
        raw_content = {
            "text": "This aluminum stand adjusts up to 7 inches and fits laptops from 10 to 17.3 inches."
        }
        compliant = gen._ensure_compliance("threads", raw_content, self.facts, self.disclosure)

        self.assertIn(self.sample_product.affiliate_url, compliant["text"])
        self.assertIn(self.sample_product.original_affiliate_url, compliant["text"])

        # Check character-for-character match
        url = self.sample_product.affiliate_url
        start_idx = compliant["text"].find(url)
        self.assertNotEqual(start_idx, -1)
        extracted_url = compliant["text"][start_idx:start_idx + len(url)]
        self.assertEqual(extracted_url, self.sample_product.affiliate_url)

    def test_backward_compatibility_without_cta(self):
        """Existing posts without any CTA must continue to pass validation cleanly."""
        legacy_fb = {
            "text": (
                f"This laptop riser is crafted from lightweight aluminum with an 11 lbs capacity.\n\n"
                f"The stand offers up to 7 inches of height adjustment and accommodates laptops from 10 to 17.3 inches.\n\n"
                f"Link: {self.sample_product.affiliate_url}\n\n"
                f"{self.disclosure}"
            )
        }
        is_valid, issues = ClaimValidator.validate_platform_content(
            platform="facebook",
            content=legacy_fb,
            product=self.facts,
            disclosure=self.disclosure
        )
        self.assertTrue(is_valid, f"Legacy post without CTA should remain valid, got: {issues}")

    def test_prompts_instruct_approved_cta_usage(self):
        """Prompts must include guidelines for approved CTA usage without aggressive hype."""
        fb_prompt = build_facebook_prompt(self.sample_product)
        pin_prompt = build_pinterest_prompt(self.sample_product)
        th_prompt = build_threads_prompt(self.sample_product)

        for p in [fb_prompt, pin_prompt, th_prompt]:
            self.assertIn("APPROVED CALL TO ACTION", p)
            self.assertIn("Explore the product details", p)
            self.assertIn("NEVER write \"Buy now\"", p)

    def test_end_to_end_generation_preserves_model_cta(self):
        """When the LLM outputs an approved CTA, it is preserved without being duplicated."""
        from unittest.mock import MagicMock
        mock_ai = MagicMock()
        mock_ai.model = "qwen2.5:7b"
        mock_ai.generate_text.return_value = (
            "Adjustable aluminum laptop stand with up to 7 inches height adjustment and 11 lbs load capacity. "
            "Explore the product details and available options below."
        )
        gen = ContentGenerator(ollama_client=mock_ai)
        result = gen.generate_for_platform("threads", self.facts, self.disclosure)

        self.assertIn("Explore the product details and available options below.", result["text"])
        self.assertIn(self.sample_product.affiliate_url, result["text"])
        # Ensure it was not duplicated
        cta_count = result["text"].count("Explore the product details and available options below.")
        self.assertEqual(cta_count, 1)

        is_valid, issues = ClaimValidator.validate_platform_content(
            platform="threads",
            content=result,
            product=self.facts,
            disclosure=self.disclosure
        )
        self.assertTrue(is_valid, f"Generated content with model CTA failed validation: {issues}")

    def test_end_to_end_generation_attaches_cta_when_omitted_by_model(self):
        """When the LLM does not generate a CTA, _ensure_compliance attaches an approved non-repetitive CTA."""
        from unittest.mock import MagicMock
        mock_ai = MagicMock()
        mock_ai.model = "qwen2.5:7b"
        mock_ai.generate_text.return_value = (
            "Adjustable aluminum laptop stand with up to 7 inches height adjustment and 11 lbs load capacity."
        )
        gen = ContentGenerator(ollama_client=mock_ai)
        result = gen.generate_for_platform("threads", self.facts, self.disclosure)

        extracted_cta = ClaimValidator.extract_approved_cta(result["text"])
        self.assertIsNotNone(extracted_cta, "CTA should have been attached by _ensure_compliance")
        self.assertTrue(ClaimValidator.is_approved_cta(extracted_cta))
        self.assertIn(self.sample_product.affiliate_url, result["text"])

        is_valid, issues = ClaimValidator.validate_platform_content(
            platform="threads",
            content=result,
            product=self.facts,
            disclosure=self.disclosure
        )
        self.assertTrue(is_valid, f"Generated content with attached CTA failed validation: {issues}")


if __name__ == "__main__":
    unittest.main()
