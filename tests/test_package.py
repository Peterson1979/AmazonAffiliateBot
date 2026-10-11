"""
Unit tests for ContentPackage (V3/V4).
Verifies deterministic package construction, platform structures, validation statuses,
metadata integrity, and presence of all 4 active platforms (X, Facebook, Pinterest, Threads).
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from amazon.scraper import Product
from content.package import (
    ContentPackage,
    STATUS_READY_FOR_REVIEW,
    STATUS_FAILED_VALIDATION,
    STATUS_VALID,
    SUPPORTED_PLATFORMS
)


class TestContentPackage(unittest.TestCase):
    def setUp(self):
        self.sample_product = Product(
            title="Laptop Stand Ergonomic Adjustable - Sturdy Portable Desk Riser for 17.3\" Laptops & Notebooks",
            features=[
                "Ergonomic Height Adjustment: Up to 7 inches of height adjustment",
                "Universal Compatibility: Accommodates tablets and laptops from 10 to 17.3 inches",
                "Loading Capacity: 11 lbs (5 kgs)",
                "Dimensions & Weight: Folded 10 x 8.7 x 1.8 inches, weighs 1.7 lb"
            ],
            affiliate_url="https://link.amazon/B0hwa4Nkm",
            source_url="https://www.amazon.co.uk/dp/B0GKGBWRTR",
            price="HUF 12,850.72",
            asin="B0GKGBWRTR"
        )
        self.disclosure = "This post contains affiliate links. As an Amazon Associate I earn from qualifying purchases."

        self.valid_social_data = {
            "asin": "B0GKGBWRTR",
            "product_title": self.sample_product.title,
            "generated_with": "qwen2.5:7b",
            "platforms": {
                "x": {
                    "text": f"17.3-inch Laptop Stand adjusts up to 7 inches and fits 10 to 17.3 inches devices with 11 lbs capacity.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
                },
                "facebook": {
                    "text": f"17.3-inch Laptop Stand with 7 inches of height adjustment and 11 lbs capacity.\n\nSupports 10 to 17.3 inches laptops and folds to 10 x 8.7 x 1.8 inches.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
                },
                "pinterest": {
                    "title": "17.3-inch Laptop Stand",
                    "description": f"7 inches height adjustment, 10 to 17.3 inches device range, 11 lbs loading capacity. {self.sample_product.affiliate_url} {self.disclosure}"
                },
                "threads": {
                    "text": f"17.3-inch Laptop Stand with 7 inches height adjustment, supporting 10 to 17.3 inches laptops up to 11 lbs.\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
                }
            },
            "validation_metadata": {
                "x": {"status": STATUS_VALID, "retry_count": 0, "errors": []},
                "facebook": {"status": STATUS_VALID, "retry_count": 0, "errors": []},
                "pinterest": {"status": STATUS_VALID, "retry_count": 0, "errors": []},
                "threads": {"status": STATUS_VALID, "retry_count": 0, "errors": []}
            }
        }

    def test_all_platforms_valid(self):
        """Test 1: Given valid generated content for all 4 platforms, package status is READY_FOR_REVIEW."""
        package = ContentPackage.build_package(
            product=self.sample_product,
            social_data=self.valid_social_data,
            cache_file_path="data/products/asin_B0GKGBWRTR.json",
            model_name="qwen2.5:7b"
        )
        self.assertEqual(package["status"], STATUS_READY_FOR_REVIEW)
        self.assertEqual(package["asin"], "B0GKGBWRTR")
        for p in SUPPORTED_PLATFORMS:
            self.assertIn(p, package["platforms"])
            self.assertEqual(package["platforms"][p]["status"], STATUS_VALID)

    def test_one_platform_invalid(self):
        """Test 2: If one platform fails validation, status is FAILED_VALIDATION and not ready."""
        invalid_social_data = dict(self.valid_social_data)
        invalid_social_data["validation_metadata"] = {
            "x": {"status": STATUS_VALID, "retry_count": 0, "errors": []},
            "facebook": {"status": STATUS_FAILED_VALIDATION, "retry_count": 1, "errors": ["Unsupported generalization"]},
            "pinterest": {"status": STATUS_VALID, "retry_count": 0, "errors": []},
            "threads": {"status": STATUS_VALID, "retry_count": 0, "errors": []}
        }

        package = ContentPackage.build_package(
            product=self.sample_product,
            social_data=invalid_social_data
        )
        self.assertEqual(package["status"], STATUS_FAILED_VALIDATION)
        self.assertNotEqual(package["status"], STATUS_READY_FOR_REVIEW)
        self.assertEqual(package["platforms"]["facebook"]["status"], STATUS_FAILED_VALIDATION)
        self.assertIn("Unsupported generalization", package["platforms"]["facebook"]["validation"]["errors"])

    def test_pinterest_structure(self):
        """Test 3: Pinterest contains title and description and does not require a normal content field."""
        package = ContentPackage.build_package(
            product=self.sample_product,
            social_data=self.valid_social_data
        )
        pin_entry = package["platforms"]["pinterest"]
        self.assertIn("title", pin_entry)
        self.assertIn("description", pin_entry)
        self.assertNotIn("content", pin_entry)
        self.assertEqual(pin_entry["title"], "17.3-inch Laptop Stand")
        self.assertIn("7 inches height adjustment", pin_entry["description"])

    def test_all_four_active_platforms_present(self):
        """Test 4: Package platform set contains exactly x, facebook, pinterest, threads."""
        package = ContentPackage.build_package(
            product=self.sample_product,
            social_data=self.valid_social_data
        )
        platform_keys = set(package["platforms"].keys())
        expected_platforms = {"x", "facebook", "pinterest", "threads"}
        self.assertEqual(platform_keys, expected_platforms)
        self.assertNotIn("instagram", platform_keys)

    def test_metadata_fields(self):
        """Test 5: Verify ASIN, model, source path, and generation timestamp are present."""
        custom_time = "2026-10-02T19:00:00Z"
        package = ContentPackage.build_package(
            product=self.sample_product,
            social_data=self.valid_social_data,
            cache_file_path="data/products/asin_B0GKGBWRTR.json",
            model_name="qwen2.5:7b",
            timestamp=custom_time
        )
        self.assertEqual(package["asin"], "B0GKGBWRTR")
        self.assertEqual(package["model"], "qwen2.5:7b")
        self.assertEqual(package["generated_at"], custom_time)
        self.assertEqual(package["source"]["product_cache"], "data/products/asin_B0GKGBWRTR.json")

    def test_retry_metadata_representation(self):
        """Test 6: Verify successful generation after one retry can be represented with retry_count = 1."""
        retry_social_data = dict(self.valid_social_data)
        retry_social_data["validation_metadata"] = {
            "x": {"status": STATUS_VALID, "retry_count": 1, "errors": []},
            "facebook": {"status": STATUS_VALID, "retry_count": 0, "errors": []},
            "pinterest": {"status": STATUS_VALID, "retry_count": 0, "errors": []},
            "threads": {"status": STATUS_VALID, "retry_count": 0, "errors": []}
        }

        package = ContentPackage.build_package(
            product=self.sample_product,
            social_data=retry_social_data
        )
        self.assertEqual(package["status"], STATUS_READY_FOR_REVIEW)
        self.assertEqual(package["platforms"]["x"]["validation"]["retry_count"], 1)
        self.assertEqual(package["platforms"]["x"]["validation"]["status"], STATUS_VALID)
        self.assertEqual(package["platforms"]["facebook"]["validation"]["retry_count"], 0)

    def test_failed_package_cannot_be_ready(self):
        """Test 7: Verify a package cannot become READY_FOR_REVIEW when a platform has failed validation."""
        missing_platform_data = {
            "asin": "B0GKGBWRTR",
            "platforms": {
                "x": {"text": "Valid X content"},
                "facebook": {"text": "Valid FB content"},
                "pinterest": {"title": "Valid Pin", "description": "Valid Desc"}
                # threads is omitted/missing
            }
        }
        package = ContentPackage.build_package(
            product=self.sample_product,
            social_data=missing_platform_data
        )
        self.assertEqual(package["status"], STATUS_FAILED_VALIDATION)
        self.assertNotEqual(package["status"], STATUS_READY_FOR_REVIEW)
        self.assertEqual(package["platforms"]["threads"]["status"], STATUS_FAILED_VALIDATION)


if __name__ == "__main__":
    unittest.main()
