"""
Unit tests for platform-specific prompts (X, Facebook, Pinterest, Threads).
Verifies active platforms, Instagram exclusion, strict prompt differentiation, and factual constraints.
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from amazon.scraper import Product
from content.prompts import (
    PLATFORM_PROMPTS,
    format_source_product_data,
    build_x_prompt,
    build_facebook_prompt,
    build_pinterest_prompt,
    build_threads_prompt
)


class TestPrompts(unittest.TestCase):
    def setUp(self):
        self.sample_product = Product(
            title="Laptop Stand Ergonomic Adjustable - Sturdy Portable Desk Riser for 17.3\" Laptops",
            features=[
                "Up to 7 inches height adjustment for improved posture",
                "Large hollow design for ventilation and heat dissipation",
                "Universal compatibility for 10 to 17.3 inches laptops and tablets",
                "Crafted from lightweight aluminum with 11 lbs capacity",
                "Folds flat to 10 x 8.7 x 1.8 inches and weighs 1.7 lb"
            ],
            affiliate_url="https://link.amazon/B0hwa4Nkm",
            source_url="https://www.amazon.co.uk/dp/B0GKGBWRTR",
            price="HUF 12,850.72",
            asin="B0GKGBWRTR"
        )

    def test_platforms_list_exact_four(self):
        """Active supported V3/V4 platforms must be exactly: X, Facebook, Pinterest, Threads."""
        expected = {"x", "facebook", "pinterest", "threads"}
        self.assertEqual(set(PLATFORM_PROMPTS.keys()), expected)
        self.assertNotIn("instagram", PLATFORM_PROMPTS)

    def test_source_product_data_formatting(self):
        """Verify SOURCE FACTS block strictly contains title and features without prices or links."""
        source_block = format_source_product_data(self.sample_product)
        self.assertIn("SOURCE FACTS:", source_block)
        self.assertIn(f"FACT 1: {self.sample_product.title}", source_block)
        self.assertIn("Up to 7 inches height adjustment", source_block)
        self.assertIn("11 lbs capacity", source_block)
        self.assertNotIn("https://link.amazon", source_block)
        self.assertNotIn("HUF 12,850.72", source_block)

    def test_each_platform_has_distinct_prompt(self):
        """Verify each platform prompt is unique and contains content rules."""
        prompts = {
            name: builder(self.sample_product)
            for name, builder in PLATFORM_PROMPTS.items()
        }
        prompt_list = list(prompts.values())
        self.assertEqual(len(prompt_list), len(set(prompt_list)))

        for name, prompt in prompts.items():
            self.assertIn("SOURCE FACTS:", prompt, f"{name} missing SOURCE FACTS")
            self.assertIn(self.sample_product.title, prompt, f"{name} missing title")
            self.assertIn("REPHRASING INSTRUCTIONS & CONSTRAINTS:", prompt, f"{name} missing REPHRASING INSTRUCTIONS")
            self.assertIn("REPHRASE, CONDENSE, and ORGANIZE", prompt)
            self.assertIn("DO NOT add marketing commentary", prompt)
            self.assertIn("DO NOT invent audiences", prompt)

    def test_pinterest_prompt_specifies_json(self):
        """Verify Pinterest prompt requires strict JSON output, title rules, and natural factual description."""
        prompt = build_pinterest_prompt(self.sample_product)
        self.assertIn("JSON", prompt)
        self.assertIn('"title"', prompt)
        self.assertIn('"description"', prompt)
        self.assertIn("MAXIMUM 100 characters", prompt)
        self.assertIn("Do NOT include hype", prompt)
        self.assertIn("natural, readable Pinterest-style product title", prompt)

    def test_threads_prompt_factual_rule(self):
        """Verify Threads prompt forbids invented pain points and rhetorical questions."""
        prompt = build_threads_prompt(self.sample_product)
        self.assertIn("Threads", prompt)
        self.assertIn('DO NOT use generalizations', prompt)

    def test_facebook_prompt_structure_rule(self):
        """Verify Facebook prompt specifies exactly 2 short paragraphs and no marketing hype."""
        prompt = build_facebook_prompt(self.sample_product)
        self.assertIn("EXACTLY 2 short paragraphs", prompt)
        self.assertIn("DO NOT invent audiences", prompt)

    def test_prompt_explicitly_defines_complete_factual_universe(self):
        """Verify generator prompts explicitly define canonical facts as the complete factual universe and mandate omission."""
        for name, builder in PLATFORM_PROMPTS.items():
            prompt = builder(self.sample_product)
            self.assertIn("COMPLETE FACTUAL UNIVERSE", prompt, f"{name} missing COMPLETE FACTUAL UNIVERSE constraint")
            self.assertIn("PREFER OMISSION", prompt, f"{name} missing PREFER OMISSION constraint")
            self.assertIn("STRICT PROPOSITION GROUNDING", prompt, f"{name} missing STRICT PROPOSITION GROUNDING constraint")


if __name__ == "__main__":
    unittest.main()
