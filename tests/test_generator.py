"""
Unit tests for content generator validation, claim grounding, parsing, compliance, and regeneration limits.
Runs offline using unittest.mock without requiring live Ollama.
"""

import sys
import json
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from amazon.scraper import Product
from content.facts import FactExtractor
from content.generator import (
    ContentGenerator,
    extract_grounded_platform_content,
    parse_pinterest_response,
    clean_markdown_fences,
    sanitize_generated_text
)
from content.validator import ClaimValidator


class TestGeneratorAndValidator(unittest.TestCase):
    def setUp(self):
        self.sample_product = Product(
            title="Laptop Stand Ergonomic Adjustable - Sturdy Portable Desk Riser for 17.3\" Laptops & Notebooks",
            features=[
                "Ergonomic Height Adjustment: The laptop stand achieves personalized comfort with up to 7 inches of height adjustment, improving posture for extended laptop use at home or in the office",
                "Enhanced Ventilation: Large hollow design facilitates superior airflow and heat dissipation, keeping your laptop cool even during intensive tasks, ideal for maintaining optimal performance",
                "Universal Compatibility: The laptop riser accommodates tablets and laptops from 10 to 17.3 inches, including popular models.",
                "Sturdy and Durable Construction: Crafted from lightweight, rust-resistant aluminum with a loading capacity of 11 lbs (5 kgs), featuring non-slip silicone pads and protective hooks to securely hold your laptop in place",
                "Portable and Space-Saving: Folds flat for convenient storage and easy portability, fitting effortlessly into a laptop bag. When folded, the dimensions are 10 x 8.7 x 1.8 inches (LxWxH), and it weighs only 1.7 lb, perfect for on-the-go professionals."
            ],
            affiliate_url="https://link.amazon/B0hwa4Nkm",
            source_url="https://www.amazon.co.uk/dp/B0GKGBWRTR",
            price="HUF 12,850.72",
            asin="B0GKGBWRTR"
        )
        self.disclosure = "This post contains affiliate links. As an Amazon Associate I earn from qualifying purchases."

    def test_clean_markdown_fences(self):
        fenced_json = '```json\n{"title": "Test Title", "description": "Test Desc"}\n```'
        self.assertEqual(clean_markdown_fences(fenced_json), '{"title": "Test Title", "description": "Test Desc"}')

        plain = "Just plain text"
        self.assertEqual(clean_markdown_fences(plain), "Just plain text")

    def test_sanitize_generated_text(self):
        prefixed = "Post: This is a great aluminum stand."
        self.assertEqual(sanitize_generated_text(prefixed), "This is a great aluminum stand.")

        threads_prefixed = "Threads post: Lightweight folding laptop riser."
        self.assertEqual(sanitize_generated_text(threads_prefixed), "Lightweight folding laptop riser.")

    def test_parse_pinterest_response_valid_json(self):
        raw_json = json.dumps({
            "title": "Ergonomic Adjustable Laptop Stand",
            "description": "Adjustable height up to 7 inches and fits laptops from 10 to 17.3 inches."
        })
        parsed = parse_pinterest_response(raw_json, self.sample_product, self.disclosure)
        self.assertEqual(parsed["title"], "Ergonomic Adjustable Laptop Stand")
        self.assertIn("7 inches", parsed["description"])

    def test_parse_pinterest_response_fallback_regex(self):
        raw_text = 'Title: Portable Aluminum Laptop Stand\nDescription: Folds flat to 10 x 8.7 x 1.8 inches and weighs 1.7 lb.'
        parsed = parse_pinterest_response(raw_text, self.sample_product, self.disclosure)
        self.assertEqual(parsed["title"], "Portable Aluminum Laptop Stand")
        self.assertIn("1.7 lb", parsed["description"])

    def test_valid_grounded_content_passes(self):
        gen = ContentGenerator()
        
        # Valid Threads post
        valid_text = {
            "text": f"This aluminum laptop stand adjusts up to 7 inches and fits laptops from 10 to 17.3 inches. It supports up to 11 lbs.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("threads", valid_text, self.sample_product, self.disclosure)
        self.assertTrue(is_valid, f"Expected valid, got issues: {issues}")
        self.assertEqual(len(issues), 0)

        # Valid Facebook post (2-3 paragraphs)
        valid_fb = {
            "text": (
                f"This laptop riser is crafted from lightweight aluminum with a loading capacity of 11 lbs (5 kgs). It features non-slip silicone pads and protective hooks to securely hold laptops in place.\n\n"
                f"The stand offers up to 7 inches of height adjustment and accommodates laptops from 10 to 17.3 inches. A large hollow design provides airflow for heat dissipation.\n\n"
                f"When folded, it measures 10 x 8.7 x 1.8 inches and weighs 1.7 lb for easy portability.\n\n"
                f"Link: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
            )
        }
        is_valid, issues = gen.validate_platform_content("facebook", valid_fb, self.sample_product, self.disclosure)
        self.assertTrue(is_valid, f"Expected valid, got issues: {issues}")

        # Valid Pinterest post
        valid_pin = {
            "title": "Adjustable Aluminum Laptop Stand",
            "description": f"Portable desk riser supporting laptops from 10 to 17.3 inches with up to 7 inches height adjustment. {self.sample_product.affiliate_url} {self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("pinterest", valid_pin, self.sample_product, self.disclosure)
        self.assertTrue(is_valid, f"Expected valid, got issues: {issues}")

        # Valid Threads post
        valid_threads = {
            "text": f"A foldable aluminum laptop stand with up to 7 inches of height adjustment. Supports up to 11 lbs and fits 10 to 17.3 inches laptops.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("threads", valid_threads, self.sample_product, self.disclosure)
        self.assertTrue(is_valid, f"Expected valid, got issues: {issues}")

    # ==================== PLATFORM COMPLIANCE TESTS ====================

    def test_text_platform_compliance(self):
        """Threads: output exists, affiliate URL present, disclosure present, no unsupported claim."""
        gen = ContentGenerator()
        content = {
            "text": f"Lightweight aluminum laptop stand with up to 7 inches height adjustment and 11 lbs capacity.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("threads", content, self.sample_product, self.disclosure)
        self.assertTrue(is_valid, f"Threads post validation failed: {issues}")
        self.assertIn(self.sample_product.affiliate_url, content["text"])
        self.assertIn(self.disclosure, content["text"])
        self.assertLessEqual(len(content["text"]), 350)

    def test_facebook_platform_compliance(self):
        """Facebook: output exists, 2-4 paragraph structure, affiliate URL present, disclosure present, no unsupported claim."""
        gen = ContentGenerator()
        content = {
            "text": (
                f"This laptop stand is crafted from lightweight aluminum with a loading capacity of 11 lbs (5 kgs).\n\n"
                f"It offers up to 7 inches of height adjustment and accommodates laptops from 10 to 17.3 inches.\n\n"
                f"When folded, the dimensions are 10 x 8.7 x 1.8 inches and it weighs 1.7 lb.\n\n"
                f"Link: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
            )
        }
        is_valid, issues = gen.validate_platform_content("facebook", content, self.sample_product, self.disclosure)
        self.assertTrue(is_valid, f"Facebook post validation failed: {issues}")
        self.assertIn(self.sample_product.affiliate_url, content["text"])
        self.assertIn(self.disclosure, content["text"])
        paragraphs = [
            p.strip() for p in content["text"].split("\n\n")
            if p.strip() and not p.strip().startswith("Link:") and self.disclosure not in p
        ]
        self.assertGreaterEqual(len(paragraphs), 2)
        self.assertLessEqual(len(paragraphs), 4)

    def test_pinterest_platform_compliance(self):
        """Pinterest: valid structured output, natural title, natural description, affiliate URL, disclosure, grounded facts."""
        gen = ContentGenerator()
        content = {
            "title": "Adjustable Aluminum Laptop Stand for 10 to 17.3 Inch Laptops",
            "description": f"This aluminum laptop stand offers up to 7 inches of height adjustment and supports devices from 10 to 17.3 inches with an 11 lbs loading capacity. When folded, it measures 10 x 8.7 x 1.8 inches and weighs 1.7 lb. {self.sample_product.affiliate_url} {self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("pinterest", content, self.sample_product, self.disclosure)
        self.assertTrue(is_valid, f"Pinterest validation failed: {issues}")
        self.assertIn("title", content)
        self.assertIn("description", content)
        self.assertLessEqual(len(content["title"]), 100)
        self.assertLessEqual(len(content["description"]), 800)
        self.assertIn(self.sample_product.affiliate_url, content["description"])
        self.assertIn(self.disclosure, content["description"])

    def test_pinterest_rejects_title_exceeding_100_chars(self):
        """Pinterest validator must reject titles exceeding 100 characters."""
        gen = ContentGenerator()
        content = {
            "title": "Adjustable Aluminum Laptop Stand for 10 to 17.3 Inch Laptops with Extra Long Title Exceeding 100 Characters Limit",
            "description": f"Valid description. {self.sample_product.affiliate_url} {self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("pinterest", content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("exceeds 100 characters" in iss for iss in issues))

    def test_pinterest_rejects_description_exceeding_800_chars(self):
        """Pinterest validator must reject descriptions exceeding 800 characters."""
        gen = ContentGenerator()
        long_desc = ("This laptop stand is made of aluminum. " * 30) + f"{self.sample_product.affiliate_url} {self.disclosure}"
        content = {
            "title": "Adjustable Aluminum Laptop Stand",
            "description": long_desc
        }
        is_valid, issues = gen.validate_platform_content("pinterest", content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("exceeds 800 characters" in iss for iss in issues))

    def test_pinterest_rejects_unsupported_marketing_hype(self):
        """Pinterest validator must reject ungrounded hype and invented claims in title or description."""
        gen = ContentGenerator()
        
        # Hype in title
        hype_title = {
            "title": "Best Ultimate Must-Have Laptop Stand",
            "description": f"Aluminum stand with 7 inches adjustment. {self.sample_product.affiliate_url} {self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("pinterest", hype_title, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("Pinterest Title:" in iss for iss in issues))

        # Invented posture / health claim in description
        health_desc = {
            "title": "Adjustable Aluminum Laptop Stand",
            "description": f"Adjustable laptop stand for improving posture and relieving neck pain. {self.sample_product.affiliate_url} {self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("pinterest", health_desc, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("Pinterest Description:" in iss for iss in issues))

    def test_threads_platform_compliance(self):
        """Threads: output exists, conversational format, affiliate URL present, disclosure present, no unsupported claim, no invented user experience."""
        gen = ContentGenerator()
        content = {
            "text": f"A lightweight aluminum laptop riser with up to 7 inches height adjustment. Folds flat to 10 x 8.7 x 1.8 inches.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("threads", content, self.sample_product, self.disclosure)
        self.assertTrue(is_valid, f"Threads validation failed: {issues}")
        self.assertIn(self.sample_product.affiliate_url, content["text"])
        self.assertIn(self.disclosure, content["text"])

    def test_instagram_platform_compliance(self):
        """Instagram is NOT an active platform: validate_platform_content must report unsupported platform."""
        gen = ContentGenerator()
        content = {
            "text": f"17.3-inch Laptop Stand with 7 inches of height adjustment, 11 lbs capacity, and 10 x 8.7 x 1.8 inches folded dimensions.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("instagram", content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("unsupported platform" in iss.lower() for iss in issues))

    def test_negative_rejects_unsupported_price(self):
        """Negative test: AI must not invent prices (e.g. '$29.99')."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"Great aluminum laptop stand for only $29.99! {self.sample_product.affiliate_url} {self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("price" in iss.lower() or "$29.99" in iss for iss in issues))

    def test_negative_rejects_unsupported_discount(self):
        """Negative test: AI must not invent discounts or sales (e.g. '50% off')."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"Get this 7 inches laptop stand with 50% off today! {self.sample_product.affiliate_url} {self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("discount" in iss.lower() or "50%" in iss for iss in issues))

    def test_negative_rejects_unsupported_rating(self):
        """Negative test: AI must not invent ratings or social proof (e.g. '5-star rating')."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"This 5-star rating laptop stand is a bestseller! {self.sample_product.affiliate_url} {self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("rating" in iss.lower() or "5-star" in iss for iss in issues))

    def test_negative_rejects_unsupported_shipping(self):
        """Negative test: AI must not invent shipping claims (e.g. 'free shipping')."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"Order now with free shipping and prime delivery. {self.sample_product.affiliate_url} {self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("shipping" in iss.lower() or "commercial" in iss.lower() for iss in issues))

    def test_negative_rejects_unsupported_health_claim(self):
        """Negative test: AI must not invent medical/health benefits (e.g. 'reduces back pain')."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"This laptop stand reduces back pain and relieves neck strain. {self.sample_product.affiliate_url} {self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("health" in iss.lower() or "back pain" in iss for iss in issues))

    def test_negative_rejects_unsupported_use_case(self):
        """Negative test: AI must not invent specialized use cases (e.g. 'perfect for gaming')."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"This adjustable riser is perfect for gaming and comfortable coding. {self.sample_product.affiliate_url} {self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("use-case" in iss.lower() or "gaming" in iss for iss in issues))

    def test_negative_rejects_unsupported_compatibility(self):
        """Negative test: AI must not claim 'compatible with all laptops' or unsupported brands."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"This stand is compatible with all laptops and fits every MacBook. {self.sample_product.affiliate_url} {self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("compatibility" in iss.lower() or "brand" in iss.lower() for iss in issues))

    def test_negative_rejects_first_person_testimonials(self):
        """Negative test: AI must not claim personal ownership or experience."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"I bought this laptop stand last week and I love using it every day. {self.sample_product.affiliate_url} {self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("personal experience" in iss.lower() for iss in issues))

    def test_negative_rejects_invented_user_problems(self):
        """Negative test: AI must not create imaginary user struggles or rhetorical pain points."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"Ever had that nagging back pain from long hours at your desk? {self.sample_product.affiliate_url} {self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("threads", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("invented user problem" in iss.lower() or "health" in iss.lower() for iss in issues))

    def test_negative_rejects_unsupported_numbers(self):
        """Negative test: AI must not invent numbers not present in product data (e.g. '25 lbs', '15.6 inches')."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"Heavy-duty aluminum stand supports up to 25 lbs and raises 12 inches. {self.sample_product.affiliate_url} {self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("numerical specification" in iss.lower() for iss in issues))

    # ==================== POSITIVE REPHRASING TESTS ====================

    def test_positive_rephrasing_folds_flat(self):
        """Positive test: legitimate rephrasing of storage feature is allowed."""
        gen = ContentGenerator()
        content = {
            "text": f"Folds flat for storage.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", content, self.sample_product, self.disclosure)
        self.assertTrue(is_valid, f"Expected valid, got: {issues}")

    def test_positive_rephrasing_airflow(self):
        """Positive test: legitimate rephrasing of airflow feature is allowed."""
        gen = ContentGenerator()
        content = {
            "text": f"The large hollow design facilitates airflow.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", content, self.sample_product, self.disclosure)
        self.assertTrue(is_valid, f"Expected valid, got: {issues}")

    def test_positive_rephrasing_height_adjust(self):
        """Positive test: legitimate rephrasing of height adjustment is allowed."""
        gen = ContentGenerator()
        content = {
            "text": f"7 inches of height adjustment.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", content, self.sample_product, self.disclosure)
        self.assertTrue(is_valid, f"Expected valid, got: {issues}")

    def test_positive_rephrasing_materials(self):
        """Positive test: legitimate rephrasing of aluminum build and capacity is allowed."""
        gen = ContentGenerator()
        content = {
            "text": f"Crafted from lightweight aluminum with an 11 lbs loading capacity.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", content, self.sample_product, self.disclosure)
        self.assertTrue(is_valid, f"Expected valid, got: {issues}")

    def test_positive_size_range(self):
        """Positive test: concrete size range numbers are allowed."""
        gen = ContentGenerator()
        content = {
            "text": f"Supports devices from 10 to 17.3 inches.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", content, self.sample_product, self.disclosure)
        self.assertTrue(is_valid, f"Expected valid, got: {issues}")

    def test_positive_portable(self):
        """Positive test: simple 'portable' descriptor is allowed."""
        gen = ContentGenerator()
        content = {
            "text": f"Portable aluminum laptop stand.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", content, self.sample_product, self.disclosure)
        self.assertTrue(is_valid, f"Expected valid, got: {issues}")

    def test_positive_non_slip_pads(self):
        """Positive test: non-slip silicone pads feature is allowed."""
        gen = ContentGenerator()
        content = {
            "text": f"Includes non-slip silicone pads and protective hooks.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", content, self.sample_product, self.disclosure)
        self.assertTrue(is_valid, f"Expected valid, got: {issues}")

    # ==================== ATTRIBUTE-LEVEL NUMERIC GROUNDING TESTS ====================

    def test_attribute_accept_correct_folded_dimensions(self):
        """Attribute test 1: Accept correct folded dimensions (10 x 8.7 x 1.8 inches)."""
        gen = ContentGenerator()
        content = {
            "text": f"Folds to 10 x 8.7 x 1.8 inches for easy storage.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", content, self.sample_product, self.disclosure)
        self.assertTrue(is_valid, f"Expected valid, got issues: {issues}")

    def test_attribute_reject_incorrect_folded_dimensions(self):
        """Attribute test 2: Reject incorrect folded dimensions (10 x 8.7 x 1.7 inches)."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"Folds to 10 x 8.7 x 1.7 inches folded.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("dimension mismatch" in iss.lower() or "attribute/unit mismatch" in iss.lower() for iss in issues))

    def test_attribute_accept_correct_weight(self):
        """Attribute test 3: Accept correct weight (1.7 lb or 1.7 lbs)."""
        gen = ContentGenerator()
        content = {
            "text": f"Lightweight stand weighs only 1.7 lb.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", content, self.sample_product, self.disclosure)
        self.assertTrue(is_valid, f"Expected valid, got issues: {issues}")

        content_lbs = {
            "text": f"Weighs 1.7 lbs when folded.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", content_lbs, self.sample_product, self.disclosure)
        self.assertTrue(is_valid, f"Expected valid, got issues: {issues}")

    def test_attribute_reject_incorrect_weight(self):
        """Attribute test 4: Reject incorrect weight (1.8 lb)."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"Lightweight stand weighs only 1.8 lb.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("attribute/unit mismatch" in iss.lower() or "weight" in iss.lower() for iss in issues))

    def test_attribute_accept_correct_loading_capacity(self):
        """Attribute test 5: Accept correct loading capacity (11 lbs)."""
        gen = ContentGenerator()
        content = {
            "text": f"Supports up to 11 lbs loading capacity.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", content, self.sample_product, self.disclosure)
        self.assertTrue(is_valid, f"Expected valid, got issues: {issues}")

    def test_attribute_reject_mismatched_loading_capacity(self):
        """Attribute test 6: Reject number/attribute mismatch (7 lbs loading capacity)."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"Supports up to 7 lbs loading capacity.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("loading capacity mismatch" in iss.lower() or "attribute/unit mismatch" in iss.lower() for iss in issues))

    def test_attribute_accept_correct_height_adjustment(self):
        """Attribute test 7: Accept correct height adjustment (7 inches of height adjustment)."""
        gen = ContentGenerator()
        content = {
            "text": f"Offers 7 inches of height adjustment.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", content, self.sample_product, self.disclosure)
        self.assertTrue(is_valid, f"Expected valid, got issues: {issues}")

    def test_attribute_accept_correct_device_range(self):
        """Attribute test 8: Accept correct device range (10 to 17.3 inches)."""
        gen = ContentGenerator()
        content = {
            "text": f"Accommodates devices from 10 to 17.3 inches.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", content, self.sample_product, self.disclosure)
        self.assertTrue(is_valid, f"Expected valid, got issues: {issues}")

    def test_attribute_reject_dimension_weight_unit(self):
        """Attribute test 9: Reject dimensions expressed in weight units (10 x 8.7 x 1.8 lbs)."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"Folds to 10 x 8.7 x 1.8 lbs.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("weight units" in iss.lower() or "attribute/unit mismatch" in iss.lower() for iss in issues))

    def test_attribute_reject_weight_number_with_inch_unit(self):
        """Attribute test 10: Reject weight number used with length unit (1.7 inches)."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"Features 1.7 inches thickness.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("attribute/unit mismatch" in iss.lower() for iss in issues))

    # ==================== NEGATIVE SEMANTIC MARKETING ADDITION TESTS ====================

    def test_negative_rejects_improving_posture(self):
        """Negative test: AI must not infer or output posture improvement claims."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"Achieves up to 7 inches of height adjustment, improving posture for extended use.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("posture" in iss.lower() for iss in issues))

    def test_negative_rejects_better_posture(self):
        """Negative test: AI must not claim better posture."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"Adjustable laptop stand designed for better posture.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("posture" in iss.lower() for iss in issues))

    def test_negative_rejects_healthier_sitting(self):
        """Negative test: AI must not claim healthier sitting."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"Laptop riser designed for healthier sitting at work.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("health" in iss.lower() or "healthier" in iss.lower() for iss in issues))

    def test_negative_rejects_unsourced_sturdy_title(self):
        """Negative test: AI must not add 'Sturdy Desk Riser' when not present in source facts."""
        simple_product = Product(
            title="17.3-inch Laptop Stand",
            features=["Aluminum construction", "Folds flat to 1.7 lb"],
            affiliate_url="https://link.amazon/test",
            source_url="https://amazon.com/dp/test",
            price="$20",
            asin="B0TEST"
        )
        gen = ContentGenerator()
        bad_content = {
            "text": f"Sturdy Portable Desk Riser with 1.7 lb weight.\n\nLink: {simple_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, simple_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("sturdy" in iss.lower() or "desk riser" in iss.lower() for iss in issues))

    def test_negative_rejects_unsourced_enhanced_ventilation(self):
        """Negative test: AI must not add 'enhanced' or 'enhanced airflow' when not present in source facts."""
        simple_product = Product(
            title="Laptop Stand",
            features=["Hollow cutout design for airflow"],
            affiliate_url="https://link.amazon/test",
            source_url="https://amazon.com/dp/test",
            price="$20",
            asin="B0TEST"
        )
        gen = ContentGenerator()
        bad_content = {
            "text": f"Features enhanced airflow for ventilation.\n\nLink: {simple_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, simple_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("enhanced" in iss.lower() for iss in issues))

    def test_negative_rejects_unsourced_laptop_riser(self):
        """Negative test: AI must not substitute 'laptop stand' with 'laptop riser' or 'desk riser' if unsourced."""
        simple_product = Product(
            title="17.3-inch Laptop Stand",
            features=["Adjustable height up to 7 inches", "Aluminum build"],
            affiliate_url="https://link.amazon/test",
            source_url="https://amazon.com/dp/test",
            price="$20",
            asin="B0TEST"
        )
        gen = ContentGenerator()
        bad_content = {
            "text": f"This laptop riser offers 7 inches height adjustment.\n\nLink: {simple_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, simple_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("laptop riser" in iss.lower() or "riser" in iss.lower() for iss in issues))

    def test_negative_rejects_unsourced_ergonomic_height_adjustment(self):
        """Negative test: AI must not add 'ergonomic height adjustment' if source only says 'height adjustment'."""
        simple_product = Product(
            title="17.3-inch Laptop Stand",
            features=["Up to 7 inches height adjustment", "Aluminum build"],
            affiliate_url="https://link.amazon/test",
            source_url="https://amazon.com/dp/test",
            price="$20",
            asin="B0TEST"
        )
        gen = ContentGenerator()
        bad_content = {
            "text": f"Features ergonomic height adjustment up to 7 inches.\n\nLink: {simple_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, simple_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("ergonomic" in iss.lower() for iss in issues))

    def test_negative_rejects_universal_compatibility(self):
        """Negative test: AI must not use 'universal compatibility' generalization."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"Features universal compatibility for laptops.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("universal compatibility" in iss.lower() for iss in issues))

    def test_negative_rejects_popular_models(self):
        """Negative test: AI must not use marketing fluff like 'including popular models'."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"Fits laptops from 10 to 17.3 inches, including popular models.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("popular models" in iss.lower() for iss in issues))

    def test_negative_rejects_effortlessly(self):
        """Negative test: AI must not escalate to 'effortlessly'."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"Folds flat and fits effortlessly into a laptop bag.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("effortlessly" in iss.lower() or "effortless" in iss.lower() for iss in issues))

    def test_negative_rejects_fits_easily_into(self):
        """Negative test: AI must not escalate to 'fits easily into'."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"Folds flat and fits easily into a laptop bag.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("fits easily into" in iss.lower() for iss in issues))

    def test_negative_rejects_perfect_for_travel(self):
        """Negative test: AI must not infer 'perfect for travel'."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"Folds flat, perfect for travel.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("perfect for travel" in iss.lower() for iss in issues))

    def test_negative_rejects_wide_range_of_devices(self):
        """Negative test: AI must not use 'wide range of devices' generalization."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"Accommodates a wide range of devices from 10 to 17.3 inches.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("wide range" in iss.lower() for iss in issues))

    def test_negative_rejects_ideal_companion(self):
        """Negative test: AI must not infer audience or companionship (e.g. 'ideal companion for professionals')."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"An ideal companion for professionals who need to work in various settings.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("companion" in iss.lower() or "inferred audience" in iss.lower() for iss in issues))

    def test_negative_rejects_ensuring_safety(self):
        """Negative test: AI must not infer safety or stability outcomes not stated in source."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"Features protective hooks ensuring stability and safety.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("safety" in iss.lower() or "stability" in iss.lower() or "inferred outcome" in iss.lower() or "ensures" in iss.lower() for iss in issues))

    def test_negative_rejects_working_remotely(self):
        """Negative test: AI must not infer remote work scenarios not present in source."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"Great for working remotely and traveling abroad.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("remotely" in iss.lower() or "use-case" in iss.lower() or "inferred audience" in iss.lower() for iss in issues))

    def test_negative_rejects_ideal_position(self):
        """Negative test: AI must not infer ideal position comfort benefits."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"Offers height adjustment, perfect for finding the ideal position.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("ideal position" in iss.lower() or "hook" in iss.lower() or "ideal" in iss.lower() for iss in issues))

    def test_negative_rejects_upgrade_workspace(self):
        """Negative test: AI must not use marketing verbs like 'upgrade your workspace'."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"Upgrade your workspace with this 7 inches laptop stand.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("workspace" in iss.lower() or "upgrade" in iss.lower() or "hook" in iss.lower() for iss in issues))

    def test_negative_rejects_work_efficiently(self):
        """Negative test: AI must not infer productivity or efficiency outcomes."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"This laptop riser helps you work more efficiently all day.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("efficient" in iss.lower() or "inferred outcome" in iss.lower() or "helps you" in iss.lower() for iss in issues))

    def test_negative_rejects_rhetorical_question(self):
        """Negative test: AI must not use rhetorical opening sales questions."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"Looking for a way to make your laptop use more comfortable?\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("threads", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("rhetorical" in iss.lower() or "looking for" in iss.lower() for iss in issues))

    def test_negative_rejects_elevate_laptop(self):
        """Negative test: AI must not use marketing verbs like 'elevate your laptop'."""
        gen = ContentGenerator()
        bad_content = {
            "text": f"Elevate your laptop for better posture.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("x", bad_content, self.sample_product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("elevate" in iss.lower() or "hook" in iss.lower() or "posture" in iss.lower() for iss in issues))

    # ==================== REGENERATION LIMIT TESTS ====================

    def test_regeneration_exactly_once_on_failure_and_success(self):
        """If attempt 1 fails validation, it regenerates exactly once and returns if attempt 2 succeeds."""
        mock_ollama = MagicMock()
        mock_ollama.model = "qwen2.5:7b"
        # 1st call: contains hallucinated price '$29.99'
        # 2nd call: corrected valid factual text
        mock_ollama.generate_text.side_effect = [
            "Great laptop stand for only $29.99 with 7 inches adjustment.",
            "Lightweight aluminum laptop stand with up to 7 inches height adjustment and 11 lbs capacity."
        ]

        gen = ContentGenerator(ollama_client=mock_ollama)
        result = gen.generate_for_platform("threads", self.sample_product, self.disclosure)

        self.assertEqual(mock_ollama.generate_text.call_count, 2)
        self.assertIn("7 inches", result["text"])
        self.assertIn(self.sample_product.affiliate_url, result["text"])

    def test_regeneration_fails_clearly_after_max_one_retry(self):
        """If attempt 1 AND attempt 2 fail validation, it fails clearly with RuntimeError after 2 calls."""
        mock_ollama = MagicMock()
        mock_ollama.model = "qwen2.5:7b"
        # Both attempts contain unsupported claims
        mock_ollama.generate_text.side_effect = [
            "Great laptop stand for $29.99.",
            "Still $29.99 with 50% off."
        ]

        gen = ContentGenerator(ollama_client=mock_ollama)
        with self.assertRaises(RuntimeError) as ctx:
            gen.generate_for_platform("threads", self.sample_product, self.disclosure)

        self.assertEqual(mock_ollama.generate_text.call_count, 2)
        self.assertIn("failed validation after 1 regeneration attempt", str(ctx.exception))

    def test_single_call_when_first_attempt_valid(self):
        """When first attempt is valid, no regeneration attempt is made (exactly 1 call)."""
        mock_ollama = MagicMock()
        mock_ollama.model = "qwen2.5:7b"
        mock_ollama.generate_text.return_value = "Adjustable aluminum laptop stand with up to 7 inches height adjustment."

        gen = ContentGenerator(ollama_client=mock_ollama)
        result = gen.generate_for_platform("threads", self.sample_product, self.disclosure)

        self.assertEqual(mock_ollama.generate_text.call_count, 1)
        self.assertIn("7 inches", result["text"])

    def test_facebook_fallback_never_splices_keyword_stuffed_facts(self):
        """The second-paragraph fallback must skip SEO keyword lists and use a genuine fact."""
        keyword_blob = (
            "Great laptop stand, best laptop stand, amazing laptop stand, "
            "wonderful laptop stand, superb laptop stand, top laptop stand"
        )
        product = Product(
            title="Foldable Laptop Stand for 17.3 Inch Laptops",
            features=[
                keyword_blob,
                "Folds flat to 10 x 8.7 x 1.8 inches for easy storage",
            ],
            affiliate_url="https://link.amazon/B0KEYWORDTEST",
            source_url="https://www.amazon.co.uk/dp/B0KEYWORDTEST",
            asin="B0KEYWORDTEST",
        )
        facts = FactExtractor.extract_canonical_facts(product)

        # Exactly one valid sentence forces the single-paragraph fallback path.
        result = extract_grounded_platform_content(
            "facebook", "Foldable laptop stand for 17.3 inch laptops.", facts, ""
        )

        self.assertIsNotNone(result)
        text = result["text"].lower()
        self.assertNotIn("great laptop stand", text)
        self.assertNotIn("best laptop stand", text)
        self.assertNotIn("superb laptop stand", text)
        self.assertIn("10 x 8.7 x 1.8", result["text"])

    def test_facebook_fallback_returns_single_sentence_without_duplication(self):
        """When only one grounded sentence exists, the fallback must not duplicate it."""
        product = Product(
            title="Womens Cotton Long-Sleeved Shirt",
            features=[
                "Great shirt, best shirt, amazing shirt, wonderful shirt, superb shirt, top shirt"
            ],
            affiliate_url="https://link.amazon/B0NODUP001",
            source_url="https://www.amazon.co.uk/dp/B0NODUP001",
            asin="B0NODUP001",
        )
        facts = FactExtractor.extract_canonical_facts(product)

        result = extract_grounded_platform_content(
            "facebook", "Cotton long-sleeved shirt for women.", facts, ""
        )

        self.assertIsNotNone(result)
        self.assertNotIn("\n\n", result["text"], "single grounded sentence must not be repeated")
        self.assertEqual(result["text"].count("."), 1)

    def test_sleeve_and_sleeved_are_recognized_as_related_forms(self):
        """A generated 'sleeve' is grounded when the source says 'Long-Sleeved',
        while words absent from the source stay rejected."""
        product = Product(
            title="Womens Long-Sleeved Cotton Shirt",
            features=[],
            affiliate_url="https://link.amazon/B0SLEEVE01",
            source_url="https://www.amazon.co.uk/dp/B0SLEEVE01",
            asin="B0SLEEVE01",
        )
        facts = FactExtractor.extract_canonical_facts(product)

        grounded_issues = ClaimValidator.validate_content_claims(
            "Cotton shirt with a long sleeve.", facts, disclosure=""
        )
        self.assertEqual(grounded_issues, [], grounded_issues)

        unsupported_issues = ClaimValidator.validate_content_claims(
            "Waterproof fleece lining.", facts, disclosure=""
        )
        self.assertTrue(unsupported_issues, "unsupported claim must still be rejected")

    def test_keyword_lists_do_not_invent_entity_bindings(self):
        """SEO keyword text must not bind modifiers to invented entities (e.g. 'sleeved top'),
        which would reject copy grounded in the real title."""
        product = Product(
            title="Womens Cotton Long-Sleeved Shirt",
            features=[
                "women's long-sleeved top, women's tops, women's long-sleeved top, "
                "women's tops, women's long-sleeved top, women's tops"
            ],
            affiliate_url="https://link.amazon/B0BIND0001",
            source_url="https://www.amazon.co.uk/dp/B0BIND0001",
            asin="B0BIND0001",
        )
        facts = FactExtractor.extract_canonical_facts(product)

        self.assertNotIn("sleeved top", facts.entity_modifiers)
        issues = ClaimValidator.validate_content_claims(
            "The shirt is a cotton long-sleeved top.", facts, disclosure=""
        )
        self.assertEqual(issues, [], issues)

    def test_generate_all_platforms_mocked(self, tmp_path=None):
        """Mocked full test verifying all 3 active platforms are generated."""
        mock_ollama = MagicMock()
        mock_ollama.model = "qwen2.5:7b"
        mock_ollama.generate_text.side_effect = [
            # Facebook
            "This laptop stand is made from lightweight aluminum with a loading capacity of 11 lbs.\n\nIt offers up to 7 inches of height adjustment and fits laptops from 10 to 17.3 inches.\n\nFolds flat to 10 x 8.7 x 1.8 inches for easy storage.",
            # Pinterest (JSON)
            json.dumps({"title": "Adjustable Aluminum Laptop Stand", "description": "Features 7 inches height adjustment and fits 10 to 17.3 inches laptops."}),
            # Threads
            "Foldable aluminum laptop stand with up to 7 inches height adjustment. Fits laptops from 10 to 17.3 inches."
        ]

        gen = ContentGenerator(ollama_client=mock_ollama)
        result = gen.generate_all_platforms(self.sample_product, self.disclosure)

        self.assertEqual(result["asin"], "B0GKGBWRTR")
        self.assertEqual(set(result["platforms"].keys()), {"facebook", "pinterest", "threads"})
        self.assertNotIn("instagram", result["platforms"])

    # ==================== REGRESSION TESTS: PRODUCT-DATA MISMATCH ====================

    def test_espresso_machine_pinterest_validation_and_grounding(self):
        """Verify espresso machine content passes and laptop stand content is strictly rejected for espresso machine."""
        espresso_product = Product(
            title="Electactic - Espresso machine with 15 bars with grinder, espresso maker with milk steam/frother/brewing tool/espresso machine with grinder",
            features=[
                "Enhanced grinding without squeezing and without clogging - say goodbye to grinding paste. Our improved system features a 20% wider polished channel and a reinforced spiral drill that directly removes floors even from oily dark toasts. Enjoy interference-free function and perfect photos.",
                "Easy coffee quality - With this intuitive espresso machine, you can easily prepare delicious espresso, lattes and cappuccino at home. The precise eyebrows provide a brave, full taste and velvety microfoam for a truly professional finish.",
                "15 bar pressure: Designed to extract the maximum aroma and depth of each grind, the integrated 20 bar pump offers rich taste and real cream, just like your favorite coffee.",
                "Silky Microfoam: The powerful steam bar produces luxurious soft milk for lattes, dense foam for cappuccino and a velvety texture for flat white. Perfect your milk-based drinks with ease.",
                "Easy to clean: The removable drip tray and removable 2.3 litre water tank make cleaning easy. With the ETL certification, you can enjoy the peace with every beer."
            ],
            affiliate_url="https://link.amazon/B0bXBNrkK",
            source_url="https://www.amazon.co.uk/dp/B0GNGTN7TV",
            asin="B0GNGTN7TV"
        )
        gen = ContentGenerator()

        # 1. Valid espresso machine Pinterest content
        valid_espresso_pin = {
            "title": "Electactic Espresso Machine With Grinder 15 Bars",
            "description": f"Espresso maker featuring 15 bar pressure, built-in grinder, steam wand, removable drip tray, and 2.3 litre water tank. {espresso_product.affiliate_url} {self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("pinterest", valid_espresso_pin, espresso_product, self.disclosure)
        self.assertTrue(is_valid, f"Expected valid espresso content, got issues: {issues}")
        self.assertEqual(len(issues), 0)

        # 2. Mismatched laptop stand content passed against espresso machine product MUST fail
        mismatched_laptop_pin = {
            "title": "Adjustable Aluminum Laptop Stand",
            "description": f"Portable desk riser supporting laptops from 10 to 17.3 inches with up to 7 inches height adjustment. {espresso_product.affiliate_url} {self.disclosure}"
        }
        is_valid, issues = gen.validate_platform_content("pinterest", mismatched_laptop_pin, espresso_product, self.disclosure)
        self.assertFalse(is_valid, "Mismatched laptop stand content must be rejected for espresso machine")
        self.assertTrue(any("unsupported" in iss.lower() or "hallucinated" in iss.lower() or "claim" in iss.lower() or "number" in iss.lower() for iss in issues))

    def test_espresso_machine_generation_flow_with_mock(self):
        """Verify generator creates espresso machine content without laptop stand data."""
        espresso_product = Product(
            title="Electactic - Espresso machine with 15 bars with grinder, espresso maker with milk steam/frother/brewing tool/espresso machine with grinder",
            features=[
                "15 bar pressure: Designed to extract the maximum aroma and depth of each grind, the integrated 20 bar pump offers rich taste and real cream.",
                "Easy to clean: The removable drip tray and removable 2.3 litre water tank make cleaning easy."
            ],
            affiliate_url="https://link.amazon/B0bXBNrkK",
            source_url="https://www.amazon.co.uk/dp/B0GNGTN7TV",
            asin="B0GNGTN7TV"
        )
        mock_ollama = MagicMock()
        mock_ollama.model = "qwen2.5:7b"
        mock_ollama.generate_text.return_value = json.dumps({
            "title": "Electactic Espresso Machine 15 Bars With Grinder",
            "description": "Espresso machine with 15 bar pressure, built-in grinder, and removable 2.3 litre water tank."
        })

        gen = ContentGenerator(ollama_client=mock_ollama)
        content = gen.generate_for_platform("pinterest", espresso_product, self.disclosure)

        self.assertEqual(content["title"], "Electactic Espresso Machine 15 Bars With Grinder")
        self.assertIn("15 bar pressure", content["description"])
        self.assertIn("2.3 litre", content["description"])
        self.assertIn("https://link.amazon/B0bXBNrkK", content["description"])

        is_valid, issues = gen.validate_platform_content("pinterest", content, espresso_product, self.disclosure)
        self.assertTrue(is_valid, f"Generated espresso content failed validation: {issues}")


if __name__ == "__main__":
    unittest.main()
