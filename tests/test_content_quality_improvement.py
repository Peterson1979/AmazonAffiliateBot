"""
Regression and Quality Tests for Generator Content-Quality Improvements (V4).
Verifies:
- Malformed punctuation cleanup (no ',.', '..', ', ,', dangling commas, spacing glitches, or bullet dashes)
- Natural platform-specific copy structure (X, Facebook 2 paragraphs, Pinterest, Threads)
- Pinterest title quality and natural product naming
- Authoritative Amazon affiliate disclosure configuration
- Exact affiliate URL preservation and provenance
- Fail-closed claim validation for unsupported claims and hallucinations
"""

import unittest
import json
from pathlib import Path

from amazon.scraper import Product
from content.facts import FactExtractor, CanonicalProductFacts
from content.generator import (
    ContentGenerator,
    clean_formatting,
    sanitize_generated_text,
    sanitize_outcome_clauses,
    parse_pinterest_response,
    extract_grounded_platform_content
)
from content.validator import ClaimValidator
from content.package import ContentPackage, STATUS_READY_FOR_REVIEW, STATUS_FAILED_VALIDATION
from web.services import (
    load_application_config,
    DEFAULT_AFFILIATE_DISCLOSURE,
    CONFIG_PATH
)


class TestContentQualityImprovement(unittest.TestCase):
    """Test suite for deterministic formatting cleanup and content quality."""

    def setUp(self):
        self.disclosure = DEFAULT_AFFILIATE_DISCLOSURE
        self.sample_product = Product(
            title="Electactic - Espresso machine with 15 bars with grinder, espresso maker with milk steam/frother/brewing tool/espresso machine with grinder",
            features=[
                "Enhanced grinding without squeezing and without clogging - say goodbye to grinding paste. Our improved system features a 20% wider polished channel and a reinforced spiral drill that directly removes floors even from oily dark toasts. Enjoy interference-free function and perfect photos.",
                "Easy coffee quality - With this intuitive espresso machine, you can easily prepare delicious espresso, lattes and cappuccino at home. The precise eyebrows provide a brave, full taste and velvety microfoam for a truly professional finish.",
                "15 bar pressure: Designed to extract the maximum aroma and depth of each grind, the integrated 20 bar pump offers rich taste and real cream, just like your favorite coffee.",
                "Silky Microfoam: The powerful steam bar produces luxurious soft milk for lattes, dense foam for cappuccino and a velvety texture for flat white. Perfect your milk-based drinks with ease.",
                "Easy to clean: The removable drip tray and removable 2.3 litre water tank make cleaning easy. With the ETL certification, you can enjoy the peace with every beer."
            ],
            affiliate_url="https://link.amazon/B0bXBNrkK",
            price="HUF304,217.58",
            asin="B0GNGTN7TV"
        )
        self.facts = FactExtractor.extract_canonical_facts(self.sample_product)
        self.generator = ContentGenerator()

    # -------------------------------------------------------------------------
    # 1. Deterministic Punctuation and Formatting Cleanup
    # -------------------------------------------------------------------------
    def test_clean_formatting_removes_awkward_punctuation(self):
        """clean_formatting deterministically fixes comma-period, double periods, and dangling commas."""
        raw_cases = [
            ("made of 100% microfiber,.", "made of 100% microfiber."),
            ("Available in solid colors, these sheets are easy to mix and match,.", "Available in solid colors, these sheets are easy to mix and match."),
            ("wrinkle resistant,.", "wrinkle resistant."),
            ("Machine washable..", "Machine washable."),
            ("Includes 15 bar pump , 2.3 litre tank .", "Includes 15 bar pump, 2.3 litre tank."),
            ("Features grinder, , milk frother", "Features grinder, milk frother"),
            ("Removable drip tray,", "Removable drip tray."),
            ("Integrated grinder;.", "Integrated grinder."),
            ("Stainless steel frame -.", "Stainless steel frame.")
        ]
        for raw, expected in raw_cases:
            cleaned = clean_formatting(raw)
            self.assertEqual(cleaned, expected, f"Failed for raw: '{raw}' -> got '{cleaned}'")

    def test_clean_formatting_removes_bullet_markers_and_dashes(self):
        """clean_formatting strips leading bullet markers, dashes, and mid-sentence list dashes."""
        raw = "- It features a 20 bar pump,.\n\n- The machine includes a grinder. - Enhanced grinding system with a 20% wider polished channel."
        expected = "It features a 20 bar pump.\n\nThe machine includes a grinder. Enhanced grinding system with a 20% wider polished channel."
        cleaned = clean_formatting(raw)
        self.assertEqual(cleaned, expected)

    def test_sanitize_outcome_clauses_preserves_clean_punctuation(self):
        """sanitize_outcome_clauses removes outcome clauses without leaving dangling commas or ',.'."""
        raw = "The fitted sheet features a 13-inch deep pocket and is wrinkle resistant, for easy cleaning."
        cleaned = sanitize_outcome_clauses(raw)
        self.assertNotIn(",.", cleaned)
        self.assertNotIn("easy cleaning", cleaned)
        self.assertTrue(cleaned.endswith("."))
        self.assertEqual(cleaned, "The fitted sheet features a 13-inch deep pocket and is wrinkle resistant.")

    # -------------------------------------------------------------------------
    # 2. Pinterest Title Quality and Product Naming
    # -------------------------------------------------------------------------
    def test_pinterest_title_parsing_and_quality(self):
        """Pinterest titles are parsed cleanly, stay <= 100 chars, and avoid spec dumps."""
        raw_json = json.dumps({
            "title": "Electactic 15-Bar Espresso Machine with Integrated Grinder",
            "description": "The Electactic espresso machine features a 15 bar pump and integrated grinder."
        })
        parsed = parse_pinterest_response(raw_json, self.facts, self.disclosure)
        self.assertEqual(parsed["title"], "Electactic 15-Bar Espresso Machine with Integrated Grinder")
        self.assertLessEqual(len(parsed["title"]), 100)
        self.assertNotIn(",.", parsed["title"])
        self.assertNotIn(",.", parsed["description"])

    def test_pinterest_fallback_title_clean(self):
        """When LLM title is empty or malformed, fallback title is clean and <= 100 chars."""
        parsed = parse_pinterest_response("invalid non json text", self.sample_product, self.disclosure)
        self.assertTrue(len(parsed["title"]) <= 100)
        self.assertNotIn(",.", parsed["title"])
        self.assertNotIn("-.", parsed["title"])

    # -------------------------------------------------------------------------
    # 3. Platform-Specific Copy Structure & Extraction
    # -------------------------------------------------------------------------
    def test_facebook_two_paragraph_structure_clean(self):
        """Facebook copy extraction enforces exactly 2 paragraphs without bullet dashes or ',.'."""
        raw_fb = (
            "- Electactic espresso machine with 15 bars and integrated grinder.\n\n"
            "- Includes removable drip tray and 2.3 litre water tank,."
        )
        extracted = extract_grounded_platform_content("facebook", raw_fb, self.facts, self.disclosure)
        self.assertIsNotNone(extracted)
        text = extracted["text"]
        self.assertNotIn(",.", text)
        self.assertFalse(text.startswith("-"))
        paragraphs = text.split("\n\n")
        self.assertEqual(len(paragraphs), 2)
        self.assertNotIn("- ", paragraphs[1])

    def test_x_and_threads_natural_sentence_extraction(self):
        """X and Threads extract fluent grounded sentences without list dumps."""
        raw_x = "The Electactic espresso machine features a 15 bar pressure pump and includes a grinder, espresso maker, milk frother, and brewing tool."
        extracted = extract_grounded_platform_content("x", raw_x, self.facts, self.disclosure)
        self.assertIsNotNone(extracted)
        self.assertNotIn(",.", extracted["text"])
        self.assertTrue(extracted["text"].endswith("."))

    # -------------------------------------------------------------------------
    # 4. Authoritative Disclosure Configuration & Persistence
    # -------------------------------------------------------------------------
    def test_authoritative_disclosure_string(self):
        """Default authoritative disclosure is the official Amazon Associate text."""
        expected = "This post contains affiliate links. As an Amazon Associate I earn from qualifying purchases."
        self.assertEqual(DEFAULT_AFFILIATE_DISCLOSURE, expected)

        cfg = load_application_config()
        self.assertEqual(cfg["affiliate"]["disclosure"], expected)
        self.assertNotEqual(cfg["affiliate"]["disclosure"], "Custom test disclosure string.")

    def test_config_json_on_disk_contains_real_disclosure(self):
        """config.json on disk must NOT contain the temporary test disclosure."""
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            self.assertEqual(
                data.get("affiliate", {}).get("disclosure"),
                "This post contains affiliate links. As an Amazon Associate I earn from qualifying purchases."
            )

    # -------------------------------------------------------------------------
    # 5. Affiliate URL Preservation
    # -------------------------------------------------------------------------
    def test_affiliate_url_preservation_in_all_platforms(self):
        """_ensure_compliance guarantees affiliate URL is strictly appended and unaltered."""
        content_map = {
            "x": {"text": "Electactic espresso machine with 15 bars."},
            "facebook": {"text": "Electactic espresso machine.\n\nIncludes 2.3 litre tank."},
            "pinterest": {"title": "Electactic Espresso Machine", "description": "Features 15 bars."},
            "threads": {"text": "Electactic espresso machine with 15 bars."}
        }
        for plat, c in content_map.items():
            compliant = self.generator._ensure_compliance(plat, c, self.facts, self.disclosure)
            if plat == "pinterest":
                self.assertIn(self.sample_product.affiliate_url, compliant["description"])
                self.assertIn(self.disclosure, compliant["description"])
            else:
                self.assertIn(self.sample_product.affiliate_url, compliant["text"])
                self.assertIn(self.disclosure, compliant["text"])

    # -------------------------------------------------------------------------
    # 6. Strict Claim Validation & Section 9 Regression Tests
    # -------------------------------------------------------------------------
    def test_validator_still_rejects_unsupported_benefits_and_outcomes(self):
        """Validator strictness is uncompromised and continues to reject hallucinations."""
        bad_claims = [
            f"Improves posture and reduces back pain.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}",
            f"Comes with a 5-star top-rated guarantee.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}",
            f"Now only $49.99 with 20% off coupon.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}",
            f"Delivers richer taste and ensures consistent experience.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}",
            f"Universal compatibility with all devices.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}",
            f"Equipped with a 25 bar pump and 500 watt motor.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}",
        ]
        for bad_text in bad_claims:
            is_val, issues = ClaimValidator.validate_platform_content(
                platform="threads",
                content={"text": bad_text},
                product=self.facts,
                disclosure=self.disclosure
            )
            self.assertFalse(is_val, f"Bad claim should have failed validation: '{bad_text}'")
            self.assertTrue(len(issues) > 0)

    # -------------------------------------------------------------------------
    # 6. Section 11 Required Architectural Regression Tests
    # -------------------------------------------------------------------------
    # MUST FAIL 1: Source 15 bar -> Generated 20 bar
    def test_must_fail_01_numeric_substitution_15_to_20_bar(self):
        """Must FAIL: Source '15 bar' -> Generated '20 bar'."""
        product = Product(
            title="Espresso Machine with 15 bar pump and portafilter",
            features=["15 bar pressure", "Includes stainless steel portafilter"],
            affiliate_url="https://link.amazon/espresso-test",
            asin="B0TESTGEN01"
        )
        facts = FactExtractor.extract_canonical_facts(product)
        bad_post = {"text": f"This espresso machine offers a 20 bar pump.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_val, issues = ClaimValidator.validate_platform_content("threads", bad_post, facts, self.disclosure)
        self.assertFalse(is_val, "Must FAIL: 20 bar is not supported when source specifies 15 bar.")
        self.assertTrue(any("20" in iss or "unit mismatch" in iss.lower() or "unsupported" in iss.lower() for iss in issues))

    # MUST FAIL 2: Source 2.3 litre -> Generated 3 litre
    def test_must_fail_02_numeric_substitution_2_3_to_3_litre(self):
        """Must FAIL: Source '2.3 litre' -> Generated '3 litre'."""
        product = Product(
            title="Espresso Machine with 2.3 litre water tank",
            features=["Removable 2.3 litre water tank", "15 bar pump"],
            affiliate_url="https://link.amazon/espresso-test",
            asin="B0TESTGEN02"
        )
        facts = FactExtractor.extract_canonical_facts(product)
        bad_post = {"text": f"Features a removable 3 litre water tank.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_val, issues = ClaimValidator.validate_platform_content("threads", bad_post, facts, self.disclosure)
        self.assertFalse(is_val, "Must FAIL: 3 litre is not supported when source specifies 2.3 litre.")
        self.assertTrue(any("3" in iss for iss in issues))

    # MUST FAIL 3: Source 20% wider -> Generated 30% wider
    def test_must_fail_03_numeric_substitution_20_percent_to_30_percent(self):
        """Must FAIL: Source '20% wider' -> Generated '30% wider'."""
        product = Product(
            title="Espresso Machine with Grinder",
            features=["Features a 20% wider polished channel and reinforced spiral drill"],
            affiliate_url="https://link.amazon/espresso-test",
            asin="B0TESTGEN03"
        )
        facts = FactExtractor.extract_canonical_facts(product)
        bad_post = {"text": f"Includes a 30% wider polished channel.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_val, issues = ClaimValidator.validate_platform_content("threads", bad_post, facts, self.disclosure)
        self.assertFalse(is_val, "Must FAIL: 30% is not supported when source specifies 20%.")
        self.assertTrue(any("30" in iss for iss in issues))

    # MUST FAIL 4: Source 15 bar pressure -> Generated 15 bar pressure for better taste (unsupported outcome)
    def test_must_fail_04_unsupported_outcome_better_taste(self):
        """Must FAIL: Generated '15 bar pressure for better taste' when 'better taste' is not source-grounded."""
        product = Product(
            title="Espresso Machine with 15 bar pressure",
            features=["15 bar extraction pressure", "Stainless steel boiler"],
            affiliate_url="https://link.amazon/espresso-test",
            asin="B0TESTGEN04"
        )
        facts = FactExtractor.extract_canonical_facts(product)
        bad_post = {"text": f"Features 15 bar pressure for better taste.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_val, issues = ClaimValidator.validate_platform_content("threads", bad_post, facts, self.disclosure)
        self.assertFalse(is_val, "Must FAIL: 'for better taste' is an unsupported outcome claim.")
        self.assertTrue(any("better" in iss.lower() or "outcome" in iss.lower() or "taste" in iss.lower() for iss in issues))

    # MUST FAIL 5: Generated extracts the maximum aroma (bypassed in old raw-text whitelist, must fail now)
    def test_must_fail_05_unsupported_claim_extract_maximum_aroma(self):
        """Must FAIL: 'extracts the maximum aroma' must fail even if raw bullet text contained seller puffery."""
        product = Product(
            title="Electactic Espresso Machine with 15 bars with grinder",
            features=["15 bar pressure: Designed to extract the maximum aroma and depth of each grind, the integrated 20 bar pump offers rich taste"],
            affiliate_url="https://link.amazon/espresso-test",
            asin="B0TESTGEN05"
        )
        facts = FactExtractor.extract_canonical_facts(product)
        bad_post = {"text": f"Features 15 bar pressure to extract the maximum aroma.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_val, issues = ClaimValidator.validate_platform_content("threads", bad_post, facts, self.disclosure)
        self.assertFalse(is_val, "Must FAIL: 'extract the maximum aroma' is an unsupported sensory outcome.")
        self.assertTrue(len(issues) > 0)

    # MUST FAIL 6: Generated perfect photos for an espresso machine
    def test_must_fail_06_irrelevant_claim_perfect_photos(self):
        """Must FAIL: 'perfect photos' for an espresso machine."""
        product = Product(
            title="Espresso Machine with 15 bar pressure and steam wand",
            features=["15 bar pressure", "Steam wand for milk frothing"],
            affiliate_url="https://link.amazon/espresso-test",
            asin="B0TESTGEN06"
        )
        facts = FactExtractor.extract_canonical_facts(product)
        bad_post = {"text": f"Enjoy interference-free function and perfect photos.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_val, issues = ClaimValidator.validate_platform_content("threads", bad_post, facts, self.disclosure)
        self.assertFalse(is_val, "Must FAIL: 'perfect photos' is completely untraceable for an espresso machine.")
        self.assertTrue(len(issues) > 0)

    # MUST FAIL 7: Generated interference-free function
    def test_must_fail_07_unsupported_operational_claim_interference_free(self):
        """Must FAIL: 'interference-free function' unless explicitly permitted as a canonical fact."""
        product = Product(
            title="Espresso Machine with reinforced spiral drill",
            features=["Reinforced spiral drill and polished channel"],
            affiliate_url="https://link.amazon/espresso-test",
            asin="B0TESTGEN07"
        )
        facts = FactExtractor.extract_canonical_facts(product)
        bad_post = {"text": f"The machine includes a reinforced spiral drill for interference-free function.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_val, issues = ClaimValidator.validate_platform_content("threads", bad_post, facts, self.disclosure)
        self.assertFalse(is_val, "Must FAIL: 'interference-free function' is an unsupported operational claim.")
        self.assertTrue(len(issues) > 0)

    # MUST FAIL 8: Generated powerful pump when not source-grounded
    def test_must_fail_08_unsupported_adjective_powerful_pump(self):
        """Must FAIL: 'powerful pump' when 'powerful' is not grounded in canonical source facts."""
        product = Product(
            title="Espresso Machine with 15 bar pump and steam wand",
            features=["15 bar pressure", "Steam wand"],
            affiliate_url="https://link.amazon/espresso-test",
            asin="B0TESTGEN08"
        )
        facts = FactExtractor.extract_canonical_facts(product)
        bad_post = {"text": f"This espresso machine features a powerful pump and 15 bar pressure.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_val, issues = ClaimValidator.validate_platform_content("threads", bad_post, facts, self.disclosure)
        self.assertFalse(is_val, "Must FAIL: 'powerful' is an ungrounded modifier.")
        self.assertTrue(any("powerful" in iss.lower() for iss in issues))

    # MUST FAIL 9: Generated professional results when not source-grounded
    def test_must_fail_09_unsupported_outcome_professional_results(self):
        """Must FAIL: 'professional results' when not source-grounded."""
        product = Product(
            title="Espresso Machine with 15 bar pressure",
            features=["15 bar extraction pressure", "Removable drip tray"],
            affiliate_url="https://link.amazon/espresso-test",
            asin="B0TESTGEN09"
        )
        facts = FactExtractor.extract_canonical_facts(product)
        bad_post = {"text": f"The espresso machine delivers professional results.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_val, issues = ClaimValidator.validate_platform_content("threads", bad_post, facts, self.disclosure)
        self.assertFalse(is_val, "Must FAIL: 'professional results' is an unsupported outcome claim.")
        self.assertTrue(len(issues) > 0)

    # MUST FAIL 10: Cross-entity fact mixing
    def test_must_fail_10_cross_entity_fact_mixing(self):
        """Must FAIL: Cross-entity modifier binding: 'durable glass cup' or 'fragile steel frame'."""
        product = Product(
            title="Coffee Set with durable steel frame and fragile glass cup",
            features=["Durable steel frame", "Fragile glass cup"],
            affiliate_url="https://link.amazon/coffee-set",
            asin="B0TESTGEN10"
        )
        facts = FactExtractor.extract_canonical_facts(product)
        
        # Test case A: durable glass cup
        bad_post_a = {"text": f"Includes a durable glass cup.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_val_a, issues_a = ClaimValidator.validate_platform_content("threads", bad_post_a, facts, self.disclosure)
        self.assertFalse(is_val_a, "Must FAIL: 'durable' was bound to steel frame in source, not glass cup.")
        self.assertTrue(any("modifier mismatch" in iss.lower() or "durable" in iss.lower() for iss in issues_a))

        # Test case B: fragile steel frame
        bad_post_b = {"text": f"Features a fragile steel frame.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_val_b, issues_b = ClaimValidator.validate_platform_content("threads", bad_post_b, facts, self.disclosure)
        self.assertFalse(is_val_b, "Must FAIL: 'fragile' was bound to glass cup in source, not steel frame.")
        self.assertTrue(any("modifier mismatch" in iss.lower() or "fragile" in iss.lower() for iss in issues_b))

    # MUST PASS 1: The machine features 15 bar pressure.
    def test_must_pass_01_exact_source_spec_15_bar_pressure(self):
        """Must PASS: 'The machine features 15 bar pressure.'"""
        product = Product(
            title="Espresso Machine with 15 bar pressure and grinder",
            features=["15 bar extraction pressure", "Integrated grinder"],
            affiliate_url="https://link.amazon/espresso-test",
            asin="B0TESTPASS01"
        )
        facts = FactExtractor.extract_canonical_facts(product)
        valid_post = {"text": f"The machine features 15 bar pressure.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_val, issues = ClaimValidator.validate_platform_content("threads", valid_post, facts, self.disclosure)
        self.assertTrue(is_val, f"Must PASS: '15 bar pressure' is fully grounded. Issues: {issues}")
        self.assertEqual(len(issues), 0)

    # MUST PASS 2: The machine features 15-bar pressure.
    def test_must_pass_02_hyphenated_unit_15_bar_pressure(self):
        """Must PASS: 'The machine features 15-bar pressure.'"""
        product = Product(
            title="Espresso Machine with 15 bar pressure and grinder",
            features=["15 bar extraction pressure", "Integrated grinder"],
            affiliate_url="https://link.amazon/espresso-test",
            asin="B0TESTPASS02"
        )
        facts = FactExtractor.extract_canonical_facts(product)
        valid_post = {"text": f"The machine features 15-bar pressure.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_val, issues = ClaimValidator.validate_platform_content("threads", valid_post, facts, self.disclosure)
        self.assertTrue(is_val, f"Must PASS: '15-bar pressure' is fully grounded. Issues: {issues}")
        self.assertEqual(len(issues), 0)

    # MUST PASS 3: Natural paraphrase preserving exact source-grounded fact
    def test_must_pass_03_natural_paraphrase_preserving_exact_fact(self):
        """Must PASS: A natural paraphrase preserving the exact source-grounded fact."""
        product = Product(
            title="Espresso Machine with 15 bar pressure and integrated grinder",
            features=["15 bar extraction pressure", "Integrated grinder", "Steam frother"],
            affiliate_url="https://link.amazon/espresso-test",
            asin="B0TESTPASS03"
        )
        facts = FactExtractor.extract_canonical_facts(product)
        valid_post = {
            "text": f"The espresso machine features an integrated grinder with 15-bar extraction pressure and a steam frother.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"
        }
        is_val, issues = ClaimValidator.validate_platform_content("threads", valid_post, facts, self.disclosure)
        self.assertTrue(is_val, f"Must PASS: Paraphrase preserves verified facts. Issues: {issues}")
        self.assertEqual(len(issues), 0)

    # MUST PASS 4: Natural marketing copy combining multiple independently verified source facts
    def test_must_pass_04_natural_marketing_copy_combining_verified_facts(self):
        """Must PASS: Natural marketing copy combining multiple independently verified source facts."""
        product = Product(
            title="Electactic Espresso Machine with 15 bars, integrated grinder, and steam frother",
            features=[
                "15 bar extraction pressure",
                "Enhanced grinding system with 20% wider polished channel and reinforced spiral drill",
                "Removable drip tray and 2.3 litre water tank",
                "ETL certification"
            ],
            affiliate_url="https://link.amazon/espresso-test",
            asin="B0TESTPASS04"
        )
        facts = FactExtractor.extract_canonical_facts(product)
        valid_fb = {
            "text": (
                f"This Electactic espresso machine features 15-bar pressure with an integrated grinder and steam frother.\n\n"
                f"It includes a 20% wider polished channel, reinforced spiral drill, removable drip tray, and 2.3 litre water tank with ETL certification.\n\n"
                f"Link: {product.affiliate_url}\n\n{self.disclosure}"
            )
        }
        is_val, issues = ClaimValidator.validate_platform_content("facebook", valid_fb, facts, self.disclosure)
        self.assertTrue(is_val, f"Must PASS: Facebook natural copy combining verified facts is valid. Issues: {issues}")
        self.assertEqual(len(issues), 0)

    # MUST PASS 5: Shortened platform-specific version that simply omits some source facts
    def test_must_pass_05_shortened_platform_version_omitting_facts(self):
        """Must PASS: A shortened platform-specific version that simply omits some source facts."""
        product = Product(
            title="Electactic Espresso Machine with 15 bars, integrated grinder, and steam frother",
            features=[
                "15 bar extraction pressure",
                "20% wider polished channel",
                "2.3 litre water tank",
                "Removable drip tray",
                "ETL certification"
            ],
            affiliate_url="https://link.amazon/espresso-test",
            asin="B0TESTPASS05"
        )
        facts = FactExtractor.extract_canonical_facts(product)
        valid_x = {
            "text": f"The Electactic espresso machine features 15 bar pressure and an integrated grinder.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"
        }
        is_val, issues = ClaimValidator.validate_platform_content("threads", valid_x, facts, self.disclosure)
        self.assertTrue(is_val, f"Must PASS: Concise version omitting other facts is valid. Issues: {issues}")
        self.assertEqual(len(issues), 0)


if __name__ == "__main__":
    unittest.main()

