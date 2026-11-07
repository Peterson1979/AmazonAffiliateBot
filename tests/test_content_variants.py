"""
Unit and integration tests for Step 2 Content Generation Improvements:
- Pinterest account-specific content (dailyideasforyou vs LifeModeHQ)
- Media-aware content generation (image vs video) across all platforms
- Content package variant packaging and backward compatibility
- Publisher slot copy selection across Windows 1, 2, 3, and 4
- CTA presence and affiliate URL preservation character-for-character
"""

import unittest
from unittest.mock import MagicMock, patch
import json

from amazon.scraper import Product
from content.facts import FactExtractor, CanonicalProductFacts
from content.prompts import (
    build_facebook_prompt,
    build_threads_prompt,
    build_pinterest_prompt,
    get_approved_cta_for_platform,
)
from content.generator import ContentGenerator
from content.package import (
    ContentPackage,
    SUPPORTED_PLATFORMS,
    STATUS_VALID,
    STATUS_READY_FOR_REVIEW,
)
from content.validator import ClaimValidator
from publishing.config import PublishingConfig
from publishing.publisher import SocialPublisher


class TestContentPromptsVariants(unittest.TestCase):
    """Test prompt builders for account-specific and media-aware copy."""

    def setUp(self):
        self.sample_product = Product(
            title="Laptop Stand Ergonomic Adjustable - Sturdy Portable Desk Riser for 17.3\" Laptops",
            features=[
                "Ergonomic Height Adjustment: Up to 7 inches of height adjustment",
                "Universal Compatibility: Accommodates tablets and laptops from 10 to 17.3 inches",
                "Loading Capacity: 11 lbs (5 kgs)",
                "Dimensions & Weight: Folded 10 x 8.7 x 1.8 inches, weighs 1.7 lb"
            ],
            affiliate_url="https://link.amazon/B0hwa4Nkm?tag=lifemode-20",
            source_url="https://www.amazon.co.uk/dp/B0GKGBWRTR",
            price="HUF 12,850.72",
            asin="B0GKGBWRTR"
        )
        self.facts = FactExtractor.extract_canonical_facts(self.sample_product)

    def test_pinterest_dailyideasforyou_vs_lifemodehq_prompts_differ(self):
        """Pinterest prompts must target different audience angles."""
        prompt_daily = build_pinterest_prompt(self.facts, account="dailyideasforyou", media_type="image")
        prompt_life = build_pinterest_prompt(self.facts, account="lifemodehq", media_type="image")

        self.assertIn("shopping discovery", prompt_daily.lower())
        self.assertIn("home, lifestyle", prompt_life.lower())
        self.assertNotEqual(prompt_daily, prompt_life)

    def test_image_vs_video_prompts_differ_all_platforms(self):
        """Image and video prompts must contain media-specific instructions."""
        # Facebook
        fb_img = build_facebook_prompt(self.facts, media_type="image")
        fb_vid = build_facebook_prompt(self.facts, media_type="video")
        self.assertIn("watch the video", fb_vid.lower())
        self.assertNotIn("watch the video", fb_img.lower())
        self.assertNotEqual(fb_img, fb_vid)

        # Threads
        th_img = build_threads_prompt(self.facts, media_type="image")
        th_vid = build_threads_prompt(self.facts, media_type="video")
        self.assertIn("video", th_vid.lower())
        self.assertNotEqual(th_img, th_vid)

        # Pinterest dailyideasforyou
        pin_img = build_pinterest_prompt(self.facts, account="dailyideasforyou", media_type="image")
        pin_vid = build_pinterest_prompt(self.facts, account="dailyideasforyou", media_type="video")
        self.assertIn("video", pin_vid.lower())
        self.assertNotEqual(pin_img, pin_vid)

        # Pinterest lifemodehq
        lm_img = build_pinterest_prompt(self.facts, account="lifemodehq", media_type="image")
        lm_vid = build_pinterest_prompt(self.facts, account="lifemodehq", media_type="video")
        self.assertIn("video", lm_vid.lower())
        self.assertNotEqual(lm_img, lm_vid)


class TestGeneratorVariants(unittest.TestCase):
    """Test ContentGenerator variant generation and compliance."""

    def setUp(self):
        self.sample_product = Product(
            title="Laptop Stand Ergonomic Adjustable - Sturdy Portable Desk Riser for 17.3\" Laptops",
            features=[
                "Material: Aluminum construction",
                "Ergonomic Height Adjustment: Up to 7 inches of height adjustment",
                "Universal Compatibility: Accommodates tablets and laptops from 10 to 17.3 inches",
                "Loading Capacity: 11 lbs (5 kgs)",
                "Dimensions & Weight: Folded 10 x 8.7 x 1.8 inches, weighs 1.7 lb"
            ],
            affiliate_url="https://link.amazon/B0hwa4Nkm?tag=lifemode-20",
            source_url="https://www.amazon.co.uk/dp/B0GKGBWRTR",
            price="HUF 12,850.72",
            asin="B0GKGBWRTR"
        )
        self.disclosure = "Paid link. As an Amazon Associate I earn from qualifying purchases."

    def test_generate_for_platform_media_types_and_accounts(self):
        """generate_for_platform handles media_type and account overrides."""
        mock_ollama = MagicMock()
        mock_ollama.generate_text.return_value = "Adjustable aluminum laptop stand supporting laptops from 10 to 17.3 inches with 11 lbs capacity."

        gen = ContentGenerator(ollama_client=mock_ollama)

        # 1. FB Video
        fb_res = gen.generate_for_platform("facebook", self.sample_product, self.disclosure, media_type="video")
        self.assertEqual(fb_res["media_type"], "video")
        self.assertIn(self.sample_product.affiliate_url, fb_res["text"])

        # 2. Pinterest dailyideasforyou Image
        mock_ollama.generate_text.return_value = json.dumps({
            "title": "Adjustable Aluminum Laptop Stand",
            "description": "7 inches height adjustment and 11 lbs capacity."
        })
        pin_daily = gen.generate_for_platform("pinterest", self.sample_product, self.disclosure, account="dailyideasforyou", media_type="image")
        self.assertEqual(pin_daily["media_type"], "image")
        self.assertEqual(pin_daily["account"], "dailyideasforyou")
        self.assertIn(self.sample_product.affiliate_url, pin_daily["description"])

        # 3. Pinterest lifemodehq Video
        mock_ollama.generate_text.return_value = json.dumps({
            "title": "Ergonomic Desk Setup Laptop Stand",
            "description": "Watch how this aluminum stand provides 7 inches of height adjustment for your workspace."
        })
        pin_life = gen.generate_for_platform("pinterest_lifemodehq", self.sample_product, self.disclosure, account="lifemodehq", media_type="video")
        self.assertEqual(pin_life["media_type"], "video")
        self.assertEqual(pin_life["account"], "lifemodehq")
        self.assertIn(self.sample_product.affiliate_url, pin_life["description"])

    def test_generate_all_platforms_with_variants(self):
        """generate_all_platforms(include_variants=True) creates full variant suite."""
        mock_ollama = MagicMock()
        # Return valid responses for all variant calls
        def mock_gen(prompt, **kwargs):
            if "JSON" in prompt:
                return json.dumps({
                    "title": "Adjustable Aluminum Laptop Stand",
                    "description": "7 inches height adjustment and 11 lbs loading capacity."
                })
            return "Adjustable aluminum laptop stand with 7 inches of height adjustment and 11 lbs capacity."

        mock_ollama.generate_text.side_effect = mock_gen
        gen = ContentGenerator(ollama_client=mock_ollama)

        result = gen.generate_all_platforms(self.sample_product, self.disclosure, include_variants=True)

        self.assertIn("facebook", result["platforms"])
        self.assertIn("pinterest", result["platforms"])
        self.assertIn("threads", result["platforms"])
        self.assertIn("pinterest_lifemodehq", result["platforms"])

        # Check variants on FB
        fb_variants = result["platforms"]["facebook"].get("variants", {})
        self.assertIn("image", fb_variants)
        self.assertIn("video", fb_variants)

        # Check variants on Threads
        th_variants = result["platforms"]["threads"].get("variants", {})
        self.assertIn("image", th_variants)
        self.assertIn("video", th_variants)

        # Check variants on Pinterest
        pin_variants = result["platforms"]["pinterest"].get("variants", {})
        self.assertIn("image", pin_variants)
        self.assertIn("video", pin_variants)

        # Check accounts on Pinterest
        pin_accounts = result["platforms"]["pinterest"].get("accounts", {})
        self.assertIn("dailyideasforyou", pin_accounts)
        self.assertIn("lifemodehq", pin_accounts)


class TestPackageVariants(unittest.TestCase):
    """Test ContentPackage with variants and backward compatibility."""

    def setUp(self):
        self.sample_product = Product(
            title="Laptop Stand Ergonomic Adjustable - Sturdy Portable Desk Riser for 17.3\" Laptops",
            features=[
                "Ergonomic Height Adjustment: Up to 7 inches of height adjustment",
                "Universal Compatibility: Accommodates tablets and laptops from 10 to 17.3 inches",
                "Loading Capacity: 11 lbs (5 kgs)",
                "Dimensions & Weight: Folded 10 x 8.7 x 1.8 inches, weighs 1.7 lb"
            ],
            affiliate_url="https://link.amazon/B0hwa4Nkm?tag=lifemode-20",
            source_url="https://www.amazon.co.uk/dp/B0GKGBWRTR",
            image_url="https://m.media-amazon.com/images/I/test.jpg",
            price="HUF 12,850.72",
            asin="B0GKGBWRTR"
        )
        self.disclosure = "Paid link. As an Amazon Associate I earn from qualifying purchases."

    def test_package_preserves_supported_platforms_constant(self):
        """SUPPORTED_PLATFORMS must strictly remain ('facebook', 'pinterest', 'threads')."""
        self.assertEqual(SUPPORTED_PLATFORMS, ("facebook", "pinterest", "threads"))

    def test_package_stores_variants_and_pinterest_accounts(self):
        """ContentPackage packages variants and accounts when present."""
        social_data = {
            "asin": "B0GKGBWRTR",
            "platforms": {
                "facebook": {
                    "text": "FB image text",
                    "variants": {
                        "image": {"content": "FB image text"},
                        "video": {"content": "FB video text"}
                    }
                },
                "threads": {
                    "text": "Threads image text",
                    "variants": {
                        "image": {"content": "Threads image text"},
                        "video": {"content": "Threads video text"}
                    }
                },
                "pinterest": {
                    "title": "Daily Pin Image Title",
                    "description": f"Daily Pin image description. {self.sample_product.affiliate_url} {self.disclosure}",
                    "image_url": self.sample_product.image_url,
                    "variants": {
                        "image": {"title": "Daily Pin Image Title", "description": "Daily Pin image description."},
                        "video": {"title": "Daily Pin Video Title", "description": "Daily Pin video description."}
                    },
                    "accounts": {
                        "dailyideasforyou": {
                            "title": "Daily Pin Image Title",
                            "description": "Daily Pin image description."
                        },
                        "lifemodehq": {
                            "title": "LifeModeHQ Pin Image Title",
                            "description": "LifeModeHQ Pin image description."
                        }
                    }
                },
                "pinterest_lifemodehq": {
                    "title": "LifeModeHQ Pin Image Title",
                    "description": f"LifeModeHQ Pin image description. {self.sample_product.affiliate_url} {self.disclosure}",
                    "image_url": self.sample_product.image_url,
                    "variants": {
                        "image": {"title": "LifeModeHQ Pin Image Title", "description": "LifeModeHQ Pin image description."},
                        "video": {"title": "LifeModeHQ Pin Video Title", "description": "LifeModeHQ Pin video description."}
                    }
                }
            },
            "validation_metadata": {
                "facebook": {"status": STATUS_VALID, "retry_count": 0, "errors": []},
                "pinterest": {"status": STATUS_VALID, "retry_count": 0, "errors": []},
                "threads": {"status": STATUS_VALID, "retry_count": 0, "errors": []},
                "pinterest_lifemodehq": {"status": STATUS_VALID, "retry_count": 0, "errors": []}
            }
        }

        pkg = ContentPackage.build_package(self.sample_product, social_data)
        self.assertEqual(pkg["status"], STATUS_READY_FOR_REVIEW)
        self.assertIn("pinterest_lifemodehq", pkg["platforms"])
        self.assertIn("variants", pkg["platforms"]["facebook"])
        self.assertIn("variants", pkg["platforms"]["pinterest_lifemodehq"])

    def test_legacy_package_without_variants_remains_valid(self):
        """Legacy package without variants builds cleanly and remains valid."""
        legacy_social = {
            "asin": "B0GKGBWRTR",
            "platforms": {
                "facebook": {"text": "Legacy FB"},
                "pinterest": {"title": "Legacy Pin", "description": "Legacy pin description.", "image_url": self.sample_product.image_url},
                "threads": {"text": "Legacy Threads"}
            },
            "validation_metadata": {
                "facebook": {"status": STATUS_VALID, "retry_count": 0, "errors": []},
                "pinterest": {"status": STATUS_VALID, "retry_count": 0, "errors": []},
                "threads": {"status": STATUS_VALID, "retry_count": 0, "errors": []}
            }
        }
        pkg = ContentPackage.build_package(self.sample_product, legacy_social)
        self.assertEqual(pkg["status"], STATUS_READY_FOR_REVIEW)
        self.assertEqual(set(pkg["platforms"].keys()), {"facebook", "pinterest", "threads"})


class TestPublisherWindowCopySelection(unittest.TestCase):
    """Test publisher copy selection across the four publishing windows."""

    def setUp(self):
        self.config = PublishingConfig.from_dict({"dry_run": True})
        self.publisher = SocialPublisher(config=self.config)

        self.product = Product(
            title="Laptop Stand Ergonomic Adjustable - Sturdy Portable Desk Riser for 17.3\" Laptops",
            features=[
                "Ergonomic Height Adjustment: Up to 7 inches of height adjustment",
                "Universal Compatibility: Accommodates tablets and laptops from 10 to 17.3 inches",
                "Loading Capacity: 11 lbs (5 kgs)",
                "Dimensions & Weight: Folded 10 x 8.7 x 1.8 inches, weighs 1.7 lb"
            ],
            affiliate_url="https://link.amazon/B0hwa4Nkm?tag=lifemode-20",
            source_url="https://www.amazon.co.uk/dp/B0GKGBWRTR",
            image_url="https://m.media-amazon.com/images/I/test.jpg",
            price="HUF 12,850.72",
            asin="B0GKGBWRTR"
        )

        self.package_with_variants = {
            "asin": "B0GKGBWRTR",
            "status": "READY_FOR_REVIEW",
            "image_url": "https://m.media-amazon.com/images/I/test.jpg",
            "video_url": "https://pub-r2.example.com/videos/B0GKGBWRTR.mp4",
            "source": {"affiliate_url": "https://link.amazon/B0hwa4Nkm?tag=lifemode-20"},
            "platforms": {
                "facebook": {
                    "status": "VALID",
                    "content": "FB default image text",
                    "image_url": "https://m.media-amazon.com/images/I/test.jpg",
                    "variants": {
                        "image": {"content": "FB custom IMAGE copy"},
                        "video": {"content": "FB custom VIDEO hook copy"}
                    }
                },
                "threads": {
                    "status": "VALID",
                    "content": "Threads default image text",
                    "image_url": "https://m.media-amazon.com/images/I/test.jpg",
                    "variants": {
                        "image": {"content": "Threads custom IMAGE copy"},
                        "video": {"content": "Threads custom VIDEO hook copy"}
                    }
                },
                "pinterest": {
                    "status": "VALID",
                    "title": "Daily Pin default title",
                    "description": "Daily Pin default description.",
                    "image_url": "https://m.media-amazon.com/images/I/test.jpg",
                    "link": "https://link.amazon/B0hwa4Nkm?tag=lifemode-20",
                    "variants": {
                        "image": {"title": "DailyIdeas IMAGE Title", "description": "DailyIdeas IMAGE description."},
                        "video": {"title": "DailyIdeas VIDEO Title", "description": "DailyIdeas VIDEO description."}
                    }
                },
                "pinterest_lifemodehq": {
                    "status": "VALID",
                    "title": "LifeMode default title",
                    "description": "LifeMode default description.",
                    "image_url": "https://m.media-amazon.com/images/I/test.jpg",
                    "link": "https://link.amazon/B0hwa4Nkm?tag=lifemode-20",
                    "variants": {
                        "image": {"title": "LifeMode IMAGE Title", "description": "LifeMode IMAGE description."},
                        "video": {"title": "LifeMode VIDEO Title", "description": "LifeMode VIDEO description."}
                    }
                }
            }
        }

    def test_window1_image_slot_selection(self):
        """Window 1 (15:00): Facebook Image, Threads Image, Pinterest dailyideasforyou Image."""
        # For image slot, pkg has no video_url (or detach_video)
        pkg = dict(self.package_with_variants)
        pkg.pop("video_url", None)

        mock_fb = MagicMock()
        mock_fb.publish.return_value = {"status": "WOULD_PUBLISH", "mode": "DRY_RUN"}
        mock_pin = MagicMock()
        mock_pin.publish.return_value = {"status": "WOULD_PUBLISH", "mode": "DRY_RUN"}
        mock_th = MagicMock()
        mock_th.publish.return_value = {"status": "WOULD_PUBLISH", "mode": "DRY_RUN"}

        self.publisher.adapters["facebook"] = mock_fb
        self.publisher.pinterest_adapters["dailyideasforyou"] = mock_pin
        self.publisher.adapters["threads"] = mock_th

        # Target destinations for Window 1
        w1_targets = ["facebook", "threads", "pinterest"]
        res = self.publisher.publish_package(pkg, dry_run=True, target_destinations=w1_targets)

        # Check FB received image variant
        fb_call_content = mock_fb.publish.call_args[1]["content"]
        self.assertEqual(fb_call_content["content"], "FB custom IMAGE copy")

        # Check Threads received image variant
        th_call_content = mock_th.publish.call_args[1]["content"]
        self.assertEqual(th_call_content["content"], "Threads custom IMAGE copy")

        # Check Pinterest dailyideasforyou received image variant
        pin_call_content = mock_pin.publish.call_args[1]["content"]
        self.assertEqual(pin_call_content["title"], "DailyIdeas IMAGE Title")
        self.assertEqual(pin_call_content["description"], "DailyIdeas IMAGE description.")

    def test_window2_lifemodehq_image_selection(self):
        """Window 2 (18:00): Pinterest LifeModeHQ Image."""
        pkg = dict(self.package_with_variants)
        pkg.pop("video_url", None)

        mock_pin_lm = MagicMock()
        mock_pin_lm.publish.return_value = {"status": "WOULD_PUBLISH", "mode": "DRY_RUN"}
        self.publisher.pinterest_adapters["lifemodehq"] = mock_pin_lm

        # Target destination for Window 2
        w2_targets = ["pinterest_lifemodehq"]
        res = self.publisher.publish_package(pkg, dry_run=True, target_destinations=w2_targets)

        pin_lm_content = mock_pin_lm.publish.call_args[1]["content"]
        self.assertEqual(pin_lm_content["title"], "LifeMode IMAGE Title")
        self.assertEqual(pin_lm_content["description"], "LifeMode IMAGE description.")

    def test_window3_video_slot_selection(self):
        """Window 3 (21:00): Facebook Video, Threads Video, Pinterest dailyideasforyou Video."""
        pkg = dict(self.package_with_variants)
        # Video is attached
        pkg["video_url"] = "https://pub-r2.example.com/videos/B0GKGBWRTR.mp4"

        mock_fb = MagicMock()
        mock_fb.publish.return_value = {"status": "WOULD_PUBLISH", "mode": "DRY_RUN"}
        mock_pin = MagicMock()
        mock_pin.publish.return_value = {"status": "WOULD_PUBLISH", "mode": "DRY_RUN"}
        mock_th = MagicMock()
        mock_th.publish.return_value = {"status": "WOULD_PUBLISH", "mode": "DRY_RUN"}

        self.publisher.adapters["facebook"] = mock_fb
        self.publisher.pinterest_adapters["dailyideasforyou"] = mock_pin
        self.publisher.adapters["threads"] = mock_th

        # Target destinations for Window 3
        w3_targets = ["facebook", "threads", "pinterest"]
        res = self.publisher.publish_package(pkg, dry_run=True, target_destinations=w3_targets)

        # Check FB received video variant
        fb_call_content = mock_fb.publish.call_args[1]["content"]
        self.assertEqual(fb_call_content["content"], "FB custom VIDEO hook copy")

        # Check Threads received video variant
        th_call_content = mock_th.publish.call_args[1]["content"]
        self.assertEqual(th_call_content["content"], "Threads custom VIDEO hook copy")

        # Check Pinterest dailyideasforyou received video variant
        pin_call_content = mock_pin.publish.call_args[1]["content"]
        self.assertEqual(pin_call_content["title"], "DailyIdeas VIDEO Title")
        self.assertEqual(pin_call_content["description"], "DailyIdeas VIDEO description.")

    def test_window4_lifemodehq_video_selection(self):
        """Window 4 (23:00): Pinterest LifeModeHQ Video."""
        pkg = dict(self.package_with_variants)
        pkg["video_url"] = "https://pub-r2.example.com/videos/B0GKGBWRTR.mp4"

        mock_pin_lm = MagicMock()
        mock_pin_lm.publish.return_value = {"status": "WOULD_PUBLISH", "mode": "DRY_RUN"}
        self.publisher.pinterest_adapters["lifemodehq"] = mock_pin_lm

        # Target destination for Window 4
        w4_targets = ["pinterest_lifemodehq"]
        res = self.publisher.publish_package(pkg, dry_run=True, target_destinations=w4_targets)

        pin_lm_content = mock_pin_lm.publish.call_args[1]["content"]
        self.assertEqual(pin_lm_content["title"], "LifeMode VIDEO Title")
        self.assertEqual(pin_lm_content["description"], "LifeMode VIDEO description.")

    def test_publisher_fallback_for_legacy_package(self):
        """Publisher gracefully falls back to default content if variants are missing."""
        legacy_pkg = {
            "asin": "B0GKGBWRTR",
            "status": "READY_FOR_REVIEW",
            "image_url": "https://m.media-amazon.com/images/I/test.jpg",
            "source": {"affiliate_url": "https://link.amazon/B0hwa4Nkm?tag=lifemode-20"},
            "platforms": {
                "facebook": {
                    "status": "VALID",
                    "content": "Legacy FB post body",
                    "image_url": "https://m.media-amazon.com/images/I/test.jpg"
                },
                "pinterest": {
                    "status": "VALID",
                    "title": "Legacy Pin Title",
                    "description": "Legacy Pin description.",
                    "image_url": "https://m.media-amazon.com/images/I/test.jpg",
                    "link": "https://link.amazon/B0hwa4Nkm?tag=lifemode-20"
                },
                "threads": {
                    "status": "VALID",
                    "content": "Legacy Threads post body",
                    "image_url": "https://m.media-amazon.com/images/I/test.jpg"
                }
            }
        }

        mock_pin_lm = MagicMock()
        mock_pin_lm.publish.return_value = {"status": "WOULD_PUBLISH", "mode": "DRY_RUN"}
        self.publisher.pinterest_adapters["lifemodehq"] = mock_pin_lm

        # LifeModeHQ targets pinterest_lifemodehq which is missing in legacy package -> falls back to "pinterest"
        res = self.publisher.publish_package(legacy_pkg, dry_run=True, target_destinations=["pinterest_lifemodehq"])
        self.assertEqual(res["results"]["pinterest_lifemodehq"]["status"], "WOULD_PUBLISH")

        called_content = mock_pin_lm.publish.call_args[1]["content"]
        self.assertEqual(called_content["title"], "Legacy Pin Title")
        self.assertEqual(called_content["description"], "Legacy Pin description.")


class TestContentQualityBodySentenceEnforcement(unittest.TestCase):
    """
    Focused regression tests for body sentence enforcement before CTA:
    - CTA-only content rejection across all platforms
    - Exact reproduction and resolution of Pinterest dailyideasforyou video failure
    - Fallback canonical sentence injection when sentence filtering removes all body content
    - Verification that all 8 platform/media variants contain a grounded body sentence before CTA
    """

    def setUp(self):
        self.sample_product = Product(
            title="Laptop Stand Ergonomic Adjustable - Sturdy Portable Desk Riser for 17.3\" Laptops",
            features=[
                "Material: Aluminum construction",
                "Ergonomic Height Adjustment: Up to 7 inches of height adjustment",
                "Universal Compatibility: Accommodates tablets and laptops from 10 to 17.3 inches",
                "Loading Capacity: 11 lbs (5 kgs)",
                "Dimensions & Weight: Folded 10 x 8.7 x 1.8 inches, weighs 1.7 lb"
            ],
            affiliate_url="https://link.amazon/B0hwa4Nkm?tag=lifemode-20",
            source_url="https://www.amazon.co.uk/dp/B0GKGBWRTR",
            price="HUF 12,850.72",
            asin="B0GKGBWRTR"
        )
        self.facts = FactExtractor.extract_canonical_facts(self.sample_product)
        self.disclosure = "Paid link. As an Amazon Associate I earn from qualifying purchases."

    def test_cta_only_content_rejected_across_all_platforms(self):
        """Content consisting only of CTA, URL and disclosure must fail validation for all platforms."""
        cta = "Explore the product details and available options below."

        # 1. Pinterest
        cta_only_pin = {
            "title": "Adjustable Aluminum Laptop Stand",
            "description": f"{cta} {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_val, issues = ClaimValidator.validate_platform_content("pinterest", cta_only_pin, self.facts, self.disclosure)
        self.assertFalse(is_val)
        self.assertTrue(any("Pinterest description must contain at least one factually grounded product description sentence before the CTA." in iss for iss in issues))

        # 2. Facebook
        cta_only_fb = {
            "text": f"{cta}\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_val, issues = ClaimValidator.validate_platform_content("facebook", cta_only_fb, self.facts, self.disclosure)
        self.assertFalse(is_val)
        self.assertTrue(any("FACEBOOK post must contain at least one factually grounded product description sentence before the CTA." in iss for iss in issues))

        # 3. Threads
        cta_only_th = {
            "text": f"{cta}\n\nLink: {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_val, issues = ClaimValidator.validate_platform_content("threads", cta_only_th, self.facts, self.disclosure)
        self.assertFalse(is_val)
        self.assertTrue(any("THREADS post must contain at least one factually grounded product description sentence before the CTA." in iss for iss in issues))

    def test_cta_placed_before_body_sentence_rejected(self):
        """CTA placed at the beginning before any product body sentence must be rejected."""
        cta = "Explore the product details and available options below."
        body = "Folded 10 x 8.7 x 1.8 inches, weighs 1.7 lb."

        inverted_pin = {
            "title": "Adjustable Aluminum Laptop Stand",
            "description": f"{cta} {body} {self.sample_product.affiliate_url}\n\n{self.disclosure}"
        }
        is_val, issues = ClaimValidator.validate_platform_content("pinterest", inverted_pin, self.facts, self.disclosure)
        self.assertFalse(is_val)
        self.assertTrue(any("at least one factually grounded product description sentence before the CTA" in iss for iss in issues))

    def test_pinterest_dailyideasforyou_video_reproduces_failure_and_triggers_regeneration(self):
        """
        Reproduce exact Pinterest dailyideasforyou video failure:
        Attempt 1 has an ungrounded opening hook + approved CTA, leaving 0 factual body sentences.
        Must trigger regeneration; when Attempt 2 provides grounded body copy, it succeeds.
        """
        mock_ollama = MagicMock()
        mock_ollama.generate_text.side_effect = [
            # Attempt 1: Opening hook has ungrounded causal/outcome claims that get filtered out, leaving ONLY CTA
            json.dumps({
                "title": "Adjustable Aluminum Laptop Stand",
                "description": "Watch the video demonstration to see how this wonderful stand improves your desk life! Explore the product details and available options below."
            }),
            # Attempt 2 (Regeneration): Model corrects itself with confirmed physical facts
            json.dumps({
                "title": "Adjustable Aluminum Laptop Stand",
                "description": "Watch the video to see this aluminum stand with up to 7 inches of height adjustment. Explore the product details and available options below."
            })
        ]

        gen = ContentGenerator(ollama_client=mock_ollama)
        result = gen.generate_for_platform(
            platform="pinterest",
            product=self.sample_product,
            disclosure=self.disclosure,
            account="dailyideasforyou",
            media_type="video"
        )

        # Regeneration must have been triggered
        self.assertEqual(mock_ollama.generate_text.call_count, 2)
        self.assertEqual(gen.last_validation_metadata["pinterest"]["retry_count"], 1)

        # Result must be valid
        is_val, issues = ClaimValidator.validate_platform_content("pinterest", result, self.facts, self.disclosure)
        self.assertTrue(is_val, f"Validation failed with: {issues}")

        # Result must contain grounded body sentence before CTA
        cta_match = ClaimValidator.extract_approved_cta(result["description"])
        self.assertIsNotNone(cta_match)
        cta_pos = result["description"].find(cta_match)
        pre_cta = result["description"][:cta_pos]
        body_sentences = ClaimValidator.extract_body_sentences(pre_cta, self.facts, self.disclosure)
        self.assertGreaterEqual(len(body_sentences), 1)
        self.assertIn("7 inches", body_sentences[0])

    def test_pinterest_dailyideasforyou_video_canonical_fallback_when_retry_also_filtered(self):
        """
        When both Attempt 1 and Attempt 2 have their body sentences filtered out (leaving only CTA),
        canonical fallback sentence construction must supply a confirmed fact before the CTA.
        """
        mock_ollama = MagicMock()
        # Both attempts contain only ungrounded outcome claims that get filtered out
        mock_ollama.generate_text.side_effect = [
            json.dumps({
                "title": "Adjustable Aluminum Laptop Stand",
                "description": "Unbelievable results for you! Explore the product details and available options below."
            }),
            json.dumps({
                "title": "Adjustable Aluminum Laptop Stand",
                "description": "Amazing experience guaranteed! Explore the product details and available options below."
            })
        ]

        gen = ContentGenerator(ollama_client=mock_ollama)
        result = gen.generate_for_platform(
            platform="pinterest",
            product=self.sample_product,
            disclosure=self.disclosure,
            account="dailyideasforyou",
            media_type="video"
        )

        self.assertEqual(mock_ollama.generate_text.call_count, 2)

        # Validation must pass with 0 issues
        is_val, issues = ClaimValidator.validate_platform_content("pinterest", result, self.facts, self.disclosure)
        self.assertTrue(is_val, f"Validation failed with: {issues}")

        # The description must NOT be only CTA + URL + disclosure
        cta_match = ClaimValidator.extract_approved_cta(result["description"])
        self.assertIsNotNone(cta_match)
        cta_pos = result["description"].find(cta_match)
        pre_cta = result["description"][:cta_pos]
        body_sentences = ClaimValidator.extract_body_sentences(pre_cta, self.facts, self.disclosure)
        self.assertGreaterEqual(len(body_sentences), 1)
        # Verify fallback sentence is grounded
        body_issues = ClaimValidator.validate_content_claims(body_sentences[0], self.facts, self.disclosure)
        self.assertEqual(body_issues, [])

    def test_all_8_variants_contain_grounded_body_sentence_before_cta(self):
        """Verify that every one of the 8 variants always contains at least one grounded body sentence before CTA."""
        mock_ollama = MagicMock()
        def mock_generate(prompt, **kwargs):
            if "JSON" in prompt:
                return json.dumps({
                    "title": "Adjustable Aluminum Laptop Stand",
                    "description": "Adjustable aluminum stand with 7 inches of height adjustment and 11 lbs capacity. Explore the product details and available options below."
                })
            return "Adjustable aluminum stand with 7 inches of height adjustment.\n\nUniversal compatibility for laptops from 10 to 17.3 inches. Explore the product details and available options below."

        mock_ollama.generate_text.side_effect = mock_generate
        gen = ContentGenerator(ollama_client=mock_ollama)

        variants_to_test = [
            ("facebook", None, "image"),
            ("facebook", None, "video"),
            ("threads", None, "image"),
            ("threads", None, "video"),
            ("pinterest", "dailyideasforyou", "image"),
            ("pinterest", "dailyideasforyou", "video"),
            ("pinterest_lifemodehq", "lifemodehq", "image"),
            ("pinterest_lifemodehq", "lifemodehq", "video"),
        ]

        for plat, acct, mtype in variants_to_test:
            with self.subTest(platform=plat, account=acct, media_type=mtype):
                res = gen.generate_for_platform(
                    platform=plat,
                    product=self.sample_product,
                    disclosure=self.disclosure,
                    account=acct,
                    media_type=mtype
                )
                text_or_desc = res.get("description", res.get("text", ""))

                # 1. Validation passes
                is_val, issues = ClaimValidator.validate_platform_content(plat, res, self.facts, self.disclosure)
                self.assertTrue(is_val, f"{plat}/{acct}/{mtype} failed validation: {issues}")

                # 2. Approved CTA present
                cta = ClaimValidator.extract_approved_cta(text_or_desc)
                self.assertIsNotNone(cta, f"{plat}/{acct}/{mtype} missing approved CTA")

                # 3. Grounded body sentence before CTA
                cta_pos = text_or_desc.find(cta)
                pre_cta = text_or_desc[:cta_pos]
                body_sentences = ClaimValidator.extract_body_sentences(pre_cta, self.facts, self.disclosure)
                self.assertGreaterEqual(len(body_sentences), 1, f"{plat}/{acct}/{mtype} has no body sentence before CTA")

                # 4. Body sentence strictly grounded
                for bs in body_sentences:
                    c_issues = ClaimValidator.validate_content_claims(bs, self.facts, self.disclosure)
                    self.assertEqual(c_issues, [], f"{plat}/{acct}/{mtype} body sentence ungrounded: {c_issues}")

                # 5. Affiliate URL preserved
                self.assertIn(self.sample_product.affiliate_url, text_or_desc)


if __name__ == "__main__":
    unittest.main()
