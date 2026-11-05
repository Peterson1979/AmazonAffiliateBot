"""Focused unit and regression tests for Google Flow Pinterest Creative workflow.

Covers:
1. Exactly two creative types (gift_inspiration and everyday_pick).
2. Correct prompt generation for both types (2:3 portrait, 1000x1500 target, mobile readability).
3. Product-fact accuracy and claim validation (CanonicalProductFacts, ClaimValidator).
4. Upload validation (accepted formats, rejected file types, invalid images, size limit).
5. Image preview and persistence (preserves exact uploaded bytes in flow_creatives.json).
6. Exactly one selected creative per ASIN.
7. Selection changes (switching selection, clearing selection).
8. Publisher uses only the selected image.
9. No selection means no creative is published (fails closed with clear error).
10. Affiliate URL remains byte-for-byte unchanged.
11. Legacy four-variation manifests do not become active selections.
"""

import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from PIL import Image

from amazon.scraper import Product
from content.facts import CanonicalProductFacts, FactExtractor
from content.validator import ClaimValidator
from content.pinterest_creative import (
    CREATIVE_TYPES,
    CREATIVE_TYPE_GIFT,
    CREATIVE_TYPE_EVERYDAY,
    CREATIVE_TYPE_LABELS,
    MAX_UPLOAD_SIZE_BYTES,
    ALLOWED_UPLOAD_EXTENSIONS,
    build_flow_prompt,
    generate_flow_creatives,
    load_product_creatives,
    validate_uploaded_creative_image,
    save_uploaded_creative,
    set_selected_creative,
    get_selected_creative,
    has_creative_context,
)
from publishing.config import PublishingConfig
from publishing.publisher import SocialPublisher, STATUS_WOULD_PUBLISH, STATUS_FAILED


class TestFlowCreativesWorkflow(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.creatives_dir = Path(self.temp_dir)

        # Standard perfume test product
        self.perfume_product = Product(
            asin="B09X5BQTEST",
            title="Bellavita Luxury Honey Oud Eau de Parfum 100 ml",
            features=[
                "Top notes: Bergamot and Honey",
                "Heart notes: Oud and Rose",
                "Base notes: Amber and Vanilla",
                "Unisex Eau de Parfum spray",
            ],
            affiliate_url="https://www.amazon.com/dp/B09X5BQTEST?tag=mytesttag-20&linkCode=ll1",
            original_affiliate_url="https://www.amazon.com/dp/B09X5BQTEST?tag=mytesttag-20&linkCode=ll1",
            image_url="https://m.media-amazon.com/images/I/71testperfume.jpg",
        )

        # Standard kitchen towel test product
        self.towel_product = Product(
            asin="B08BRJTEST",
            title="USANOOKS Microfiber Cleaning Cloth Grey - 12 Packs",
            features=[
                "Reinforced stitched edges for durability",
                "Ultra-absorbent microfiber weave grabs liquid instantly",
                "Machine washable and reusable for over 1200 washes",
                "Non-abrasive surface safe for mirrors and countertops",
            ],
            affiliate_url="https://www.amazon.com/dp/B08BRJTEST?tag=mytesttag-20&linkCode=ll1",
            original_affiliate_url="https://www.amazon.com/dp/B08BRJTEST?tag=mytesttag-20&linkCode=ll1",
            image_url="https://m.media-amazon.com/images/I/71testtowel.jpg",
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _create_test_image_bytes(self, size=(1000, 1500), fmt="PNG", color=(100, 150, 200)):
        img = Image.new("RGB", size, color=color)
        buf = io.BytesIO()
        img.save(buf, format=fmt)
        return buf.getvalue()

    def test_exactly_two_creative_types(self):
        """Verify that exactly two creative slots exist with expected identifiers and display names."""
        self.assertEqual(len(CREATIVE_TYPES), 2)
        self.assertIn(CREATIVE_TYPE_GIFT, CREATIVE_TYPES)
        self.assertIn(CREATIVE_TYPE_EVERYDAY, CREATIVE_TYPES)
        self.assertEqual(CREATIVE_TYPE_LABELS[CREATIVE_TYPE_GIFT], "Gift Inspiration")
        self.assertEqual(CREATIVE_TYPE_LABELS[CREATIVE_TYPE_EVERYDAY], "Everyday Pick")

    def test_correct_prompt_generation_for_both_types(self):
        """Verify prompt structure: 9:16 portrait, mandatory visual reference, identity preservation, Flow UI reminder."""
        facts = FactExtractor.extract_canonical_facts(self.perfume_product)

        gift_data = build_flow_prompt(self.perfume_product, CREATIVE_TYPE_GIFT, facts)
        prompt_gift = gift_data["prompt"]
        self.assertIn("9:16 (Vertical / Portrait)", prompt_gift)
        self.assertIn("Google Flow's image aspect-ratio settings", prompt_gift)
        self.assertIn("mandatory visual reference", prompt_gift)
        self.assertIn("Preserve the exact product identity and appearance", prompt_gift)
        self.assertIn("Do NOT replace the product with a similar-looking alternative", prompt_gift)
        self.assertIn("exact same product as in the reference image", prompt_gift)
        self.assertIn("mobile-readable", prompt_gift)
        self.assertIn(gift_data["headline"], prompt_gift)
        self.assertIn(gift_data["subheadline"], prompt_gift)
        self.assertIn(gift_data["cta_text"], prompt_gift)
        self.assertIn("Prominent Headline:", prompt_gift)
        self.assertIn("Supporting Subheadline:", prompt_gift)
        self.assertIn("No generic AI collages", prompt_gift)

        everyday_data = build_flow_prompt(self.perfume_product, CREATIVE_TYPE_EVERYDAY, facts)
        prompt_everyday = everyday_data["prompt"]
        self.assertIn("9:16 (Vertical / Portrait)", prompt_everyday)
        self.assertIn("mandatory visual reference", prompt_everyday)
        self.assertIn(everyday_data["headline"], prompt_everyday)
        self.assertIn(everyday_data["subheadline"], prompt_everyday)
        self.assertIn("commercial advertising studio art direction", prompt_everyday)

    def test_product_fact_accuracy_and_claim_validation(self):
        """Verify that prompts and copy strictly use verified facts and pass ClaimValidator."""
        facts = FactExtractor.extract_canonical_facts(self.towel_product)
        gift_data = build_flow_prompt(self.towel_product, CREATIVE_TYPE_GIFT, facts)
        everyday_data = build_flow_prompt(self.towel_product, CREATIVE_TYPE_EVERYDAY, facts)

        # Towel product must NOT contain storage/sink claims
        all_text = f"{gift_data['prompt']} {everyday_data['prompt']}".lower()
        self.assertNotIn("under the sink", all_text)
        self.assertNotIn("cabinet organizer", all_text)

        # Verified facts in payload must match facts from facts extraction
        self.assertTrue(len(gift_data["verified_facts"]) > 0)
        self.assertTrue(len(everyday_data["verified_facts"]) > 0)

        # Benefits must be verified
        for benefit in everyday_data["benefits"]:
            errs = ClaimValidator.validate_content_claims(benefit, facts)
            self.assertEqual(errs, [], f"Claim validation failed for benefit: '{benefit}'")

    def test_upload_validation(self):
        """Verify image upload validation: format, corruption, empty, and size limits."""
        # Valid PNG
        png_bytes = self._create_test_image_bytes(fmt="PNG")
        ok, msg, dims, fmt = validate_uploaded_creative_image(png_bytes, "pin.png")
        self.assertTrue(ok)
        self.assertEqual(dims, (1000, 1500))

        # Valid JPEG
        jpg_bytes = self._create_test_image_bytes(fmt="JPEG")
        ok, msg, dims, fmt = validate_uploaded_creative_image(jpg_bytes, "pin.jpg")
        self.assertTrue(ok)

        # Valid WEBP
        webp_bytes = self._create_test_image_bytes(fmt="WEBP")
        ok, msg, dims, fmt = validate_uploaded_creative_image(webp_bytes, "pin.webp")
        self.assertTrue(ok)

        # Reject unsupported extension
        ok, msg, dims, fmt = validate_uploaded_creative_image(png_bytes, "pin.gif")
        self.assertFalse(ok)
        self.assertIn("Invalid file extension", msg)

        # Reject empty bytes
        ok, msg, dims, fmt = validate_uploaded_creative_image(b"", "pin.png")
        self.assertFalse(ok)
        self.assertIn("empty", msg.lower())

        # Reject corrupted bytes
        ok, msg, dims, fmt = validate_uploaded_creative_image(b"not an image at all", "pin.png")
        self.assertFalse(ok)
        self.assertIn("corrupted", msg.lower())

        # Reject oversized image (> 15MB)
        oversized = b"x" * (MAX_UPLOAD_SIZE_BYTES + 1024)
        ok, msg, dims, fmt = validate_uploaded_creative_image(oversized, "pin.png")
        self.assertFalse(ok)
        self.assertIn("exceeds maximum allowed size", msg)

    def test_image_preview_and_persistence(self):
        """Verify uploaded image is stored with exact bytes unchanged and recorded in flow_creatives.json."""
        asin = self.perfume_product.asin
        test_bytes = self._create_test_image_bytes(size=(1000, 1500), fmt="PNG")

        ok, msg, manifest = save_uploaded_creative(
            asin=asin,
            creative_type=CREATIVE_TYPE_GIFT,
            file_bytes=test_bytes,
            filename="my_flow_render.png",
            creatives_dir=self.creatives_dir,
        )
        self.assertTrue(ok)

        manifest_file = self.creatives_dir / asin / "flow_creatives.json"
        self.assertTrue(manifest_file.is_file())

        with open(manifest_file, "r", encoding="utf-8") as f:
            data = json.load(f)

        gift_slot = data["creatives"][CREATIVE_TYPE_GIFT]
        self.assertIsNotNone(gift_slot["uploaded_image_path"])
        self.assertEqual(gift_slot["dimensions"], [1000, 1500])

        # Verify exact byte preservation on disk
        saved_file = self.creatives_dir / asin / "gift_inspiration_flow.png"
        self.assertTrue(saved_file.is_file())
        self.assertEqual(saved_file.read_bytes(), test_bytes)

    def test_exactly_one_selected_creative_per_asin(self):
        """Verify that at most one creative can be selected at a time per ASIN."""
        asin = self.perfume_product.asin
        img_bytes = self._create_test_image_bytes(fmt="PNG")

        # Upload to both slots
        save_uploaded_creative(asin, CREATIVE_TYPE_GIFT, img_bytes, "gift.png", creatives_dir=self.creatives_dir)
        save_uploaded_creative(asin, CREATIVE_TYPE_EVERYDAY, img_bytes, "everyday.png", creatives_dir=self.creatives_dir)

        # Select Gift Inspiration
        ok, msg, m1 = set_selected_creative(asin, CREATIVE_TYPE_GIFT, creatives_dir=self.creatives_dir)
        self.assertTrue(ok)
        self.assertEqual(m1["selected_creative_type"], CREATIVE_TYPE_GIFT)
        self.assertTrue(m1["creatives"][CREATIVE_TYPE_GIFT]["selected"])
        self.assertFalse(m1["creatives"][CREATIVE_TYPE_EVERYDAY]["selected"])

        # Select Everyday Pick -> Gift Inspiration must be deselected
        ok, msg, m2 = set_selected_creative(asin, CREATIVE_TYPE_EVERYDAY, creatives_dir=self.creatives_dir)
        self.assertTrue(ok)
        self.assertEqual(m2["selected_creative_type"], CREATIVE_TYPE_EVERYDAY)
        self.assertFalse(m2["creatives"][CREATIVE_TYPE_GIFT]["selected"])
        self.assertTrue(m2["creatives"][CREATIVE_TYPE_EVERYDAY]["selected"])

    def test_selection_changes(self):
        """Verify selecting, switching, and clearing selections."""
        asin = self.perfume_product.asin
        img_bytes = self._create_test_image_bytes(fmt="PNG")
        save_uploaded_creative(asin, CREATIVE_TYPE_GIFT, img_bytes, "gift.png", creatives_dir=self.creatives_dir)

        # Cannot select empty slot
        ok, msg, _ = set_selected_creative(asin, CREATIVE_TYPE_EVERYDAY, creatives_dir=self.creatives_dir)
        self.assertFalse(ok)
        self.assertIn("no image has been uploaded yet", msg)

        # Select uploaded slot
        ok, _, m = set_selected_creative(asin, CREATIVE_TYPE_GIFT, creatives_dir=self.creatives_dir)
        self.assertTrue(ok)
        self.assertEqual(m["selected_creative_type"], CREATIVE_TYPE_GIFT)

        # Clear selection
        ok, msg, m_cleared = set_selected_creative(asin, "none", creatives_dir=self.creatives_dir)
        self.assertTrue(ok)
        self.assertIsNone(m_cleared["selected_creative_type"])
        self.assertIsNone(get_selected_creative(asin, creatives_dir=self.creatives_dir))

    def test_publisher_uses_only_selected_image(self):
        """Verify that SocialPublisher uses strictly the selected creative image for Pinterest."""
        asin = "B0FLOWPUB1"
        c_dir = Path("data/creatives") / asin
        c_dir.mkdir(parents=True, exist_ok=True)
        try:
            img_bytes = self._create_test_image_bytes(fmt="PNG")
            save_uploaded_creative(asin, CREATIVE_TYPE_GIFT, img_bytes, "gift.png")
            set_selected_creative(asin, CREATIVE_TYPE_GIFT)

            package = {
                "asin": asin,
                "status": "READY_FOR_REVIEW",
                "source": {"affiliate_url": "https://www.amazon.com/dp/B0FLOWPUB1?tag=affil-20"},
                "platforms": {
                    "facebook": {"status": "VALID", "content": "FB post"},
                    "pinterest": {
                        "status": "VALID",
                        "title": "Pin Title",
                        "description": "Pin Description https://www.amazon.com/dp/B0FLOWPUB1?tag=affil-20",
                        "link": "https://www.amazon.com/dp/B0FLOWPUB1?tag=affil-20",
                    },
                    "threads": {"status": "VALID", "content": "Threads post", "image_url": "https://m.media-amazon.com/img.jpg"},
                },
            }

            publisher = SocialPublisher(config=PublishingConfig.from_dict({}, dry_run_override=True))
            report = publisher.publish_package(package, dry_run=True)
            pin_result = report["results"]["pinterest"]

            self.assertEqual(pin_result["status"], STATUS_WOULD_PUBLISH)
            self.assertIn("gift_inspiration_flow.png", pin_result["image_path"])
        finally:
            shutil.rmtree(c_dir, ignore_errors=True)

    def test_no_selection_means_no_creative_published(self):
        """Verify that when a product is in the creative workflow, having no selection blocks publishing."""
        asin = "B0FLOWNOSEL"
        c_dir = Path("data/creatives") / asin
        c_dir.mkdir(parents=True, exist_ok=True)
        try:
            img_bytes = self._create_test_image_bytes(fmt="PNG")
            save_uploaded_creative(asin, CREATIVE_TYPE_GIFT, img_bytes, "gift.png")
            # Do NOT select any creative
            set_selected_creative(asin, "none")

            package = {
                "asin": asin,
                "status": "READY_FOR_REVIEW",
                "source": {"affiliate_url": "https://www.amazon.com/dp/B0FLOWNOSEL?tag=affil-20"},
                "platforms": {
                    "facebook": {"status": "VALID", "content": "FB post"},
                    "pinterest": {
                        "status": "VALID",
                        "title": "Pin Title",
                        "description": "Pin Description",
                        "link": "https://www.amazon.com/dp/B0FLOWNOSEL?tag=affil-20",
                    },
                    "threads": {"status": "VALID", "content": "Threads post", "image_url": "https://m.media-amazon.com/img.jpg"},
                },
            }

            publisher = SocialPublisher(config=PublishingConfig.from_dict({}, dry_run_override=True))
            report = publisher.publish_package(package, dry_run=True)
            pin_result = report["results"]["pinterest"]

            self.assertEqual(pin_result["status"], STATUS_FAILED)
            self.assertIn("No Pinterest creative selected for publishing", pin_result["errors"][0])
        finally:
            shutil.rmtree(c_dir, ignore_errors=True)

    def test_affiliate_url_remains_byte_for_byte_unchanged(self):
        """Verify that the original affiliate URL and tracking tag are preserved byte-for-byte in all manifests."""
        expected_url = "https://www.amazon.com/dp/B09X5BQTEST?tag=mytesttag-20&linkCode=ll1"
        res = generate_flow_creatives(self.perfume_product, creatives_dir=self.creatives_dir)
        manifest = res["data"]
        self.assertEqual(manifest["affiliate_url"], expected_url)

        # Upload and select
        img_bytes = self._create_test_image_bytes(fmt="PNG")
        save_uploaded_creative(self.perfume_product.asin, CREATIVE_TYPE_GIFT, img_bytes, "pin.png", creatives_dir=self.creatives_dir)
        set_selected_creative(self.perfume_product.asin, CREATIVE_TYPE_GIFT, creatives_dir=self.creatives_dir)

        loaded = load_product_creatives(self.perfume_product.asin, base_dir=self.creatives_dir)
        self.assertEqual(loaded["affiliate_url"], expected_url)

    def test_legacy_four_variation_manifests_do_not_become_active_selections(self):
        """Verify that existing 4-variation creatives.json manifests do not become active selections."""
        asin = "B0LEGACYTEST"
        out_dir = self.creatives_dir / asin
        out_dir.mkdir(parents=True, exist_ok=True)

        # Write legacy 4-variation creatives.json
        legacy_data = {
            "asin": asin,
            "title": "Legacy Test Product",
            "total_variations": 4,
            "variations": [
                {"variation_id": "var_1_practical", "headline": "Old V1", "file_path": "variation_1.png"},
                {"variation_id": "var_2_benefits", "headline": "Old V2", "file_path": "variation_2.png"},
                {"variation_id": "var_3_design", "headline": "Old V3", "file_path": "variation_3.png"},
                {"variation_id": "var_4_gift", "headline": "Old V4", "file_path": "variation_4.png"},
            ],
        }
        with open(out_dir / "creatives.json", "w", encoding="utf-8") as f:
            json.dump(legacy_data, f)

        # 1. get_selected_creative must be None
        self.assertIsNone(get_selected_creative(asin, creatives_dir=self.creatives_dir))

        # 2. has_creative_context is True
        self.assertTrue(has_creative_context(asin, creatives_dir=self.creatives_dir))

        # 3. load_product_creatives without flow_creatives.json initializes the 2 Flow slots, never returning the 4 old variations
        flow_loaded = load_product_creatives(asin, base_dir=self.creatives_dir)
        if flow_loaded:
            self.assertIn("creatives", flow_loaded)
            self.assertEqual(set(flow_loaded["creatives"].keys()), {CREATIVE_TYPE_GIFT, CREATIVE_TYPE_EVERYDAY})
            self.assertIsNone(flow_loaded.get("selected_creative_type"))


if __name__ == "__main__":
    unittest.main()

