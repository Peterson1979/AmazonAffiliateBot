"""Unit and regression tests for Pinterest Multi-Angle Marketing Creative Generator.

Covers:
1. Exact pixel preservation of original product image.
2. Original image file preservation (source file is never modified or retouched).
3. Exact 2:3 aspect ratio and 1000x1500 dimensions.
4. Dynamic selection of 3-5 distinct marketing angles tailored to product category.
5. Product-specific, grounded marketing copy without hallucinations.
6. Mobile-first CTA button rendering with valid CTA choices.
7. Immutable affiliate URL preservation.
8. Safe failure fallback when source image is missing or corrupted.
9. Backward compatibility with existing publishing adapters.
10. Duplicate generation prevention when existing creative set is valid.
"""

import json
import os
import random
import tempfile
import unittest
from pathlib import Path
from PIL import Image

from amazon.scraper import Product
from content.facts import CanonicalProductFacts, SourceFact, FactExtractor
from content.pinterest_creative import (
    CANVAS_WIDTH,
    CANVAS_HEIGHT,
    CANVAS_SIZE,
    ALLOWED_CTAS,
    ALL_MARKETING_ANGLES,
    CAT_FRAGRANCE,
    CAT_ELECTRONICS,
    CAT_HOME_KITCHEN,
    CAT_GENERAL,
    ANGLE_PRACTICAL_SOLUTION,
    ANGLE_SMART_TECH_UPGRADE,
    ANGLE_SMART_HOME_UPGRADE,
    ANGLE_COMPACT_CONVENIENCE,
    ANGLE_SPACE_SAVING_IDEA,
    ANGLE_GIFT_INSPIRATION,
    ANGLE_KEY_PRODUCT_BENEFITS,
    ANGLE_LUXURY_FRAGRANCE,
    ANGLE_FRAGRANCE_NOTES,
    ANGLE_EVERYDAY_ELEGANCE,
    ANGLE_UNISEX_APPEAL,
    THEMES,
    LAYOUT_HERO_SHOWCASE,
    LAYOUT_EDITORIAL_SPLIT,
    LAYOUT_FEATURE_FOCUS,
    LAYOUT_MINIMALIST_FRAME,
    LAYOUT_TEMPLATES,
    CreativeCopy,
    detect_product_category,
    select_marketing_angles,
    generate_creative_copy,
    validate_creative_copy,
    compose_pinterest_creative,
    generate_pinterest_creatives,
    load_product_creatives,
    get_creatives_dir,
    is_disclaimer_or_warning_text,
    is_isolated_spec_fragment,
)
from publishing.adapters.pinterest_adapter import PinterestAdapter


class TestPinterestMultiAngleCreative(unittest.TestCase):
    """Test suite for Pinterest marketing creative generation."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.test_root = Path(self.temp_dir.name)

        # Build synthetic products with realistic grounded facts
        self.tech_product = Product(
            title="Anker Magnetic Wireless Charging Station 3-in-1 Foldable Stand",
            description="High-speed wireless charger for iPhone, Apple Watch and AirPods with compact travel fold design.",
            features=[
                "Compact 3-in-1 foldable design saves desk and nightstand space",
                "Fast 15W wireless charging with certified safety protection",
                "Premium aluminum alloy construction with sleek matte finish",
            ],
            affiliate_url="https://amzn.to/3XTech1",
            original_affiliate_url="https://amzn.to/3XTech1",
            source_url="https://www.amazon.com/dp/B07TESTECH",
            price="$39.99",
            asin="B07TESTECH",
            image_url="https://m.media-amazon.com/images/I/synthetic_tech.jpg",
        )

        self.home_product = Product(
            title="Under Sink Organizer 2-Tier Sliding Storage Drawer for Kitchen Bathroom",
            description="Heavy-duty pull-out cabinet organizer maximizing under-sink storage space.",
            features=[
                "Sliding bottom drawer for effortless access in tight spaces",
                "Durable ABS plastic with rust-proof stainless steel support pipes",
                "Tool-free 2-minute assembly with clean non-slip rubber feet",
            ],
            affiliate_url="https://amzn.to/3YHome1",
            original_affiliate_url="https://amzn.to/3YHome1",
            source_url="https://www.amazon.com/dp/B08TESHOME",
            price="$24.99",
            asin="B08TESHOME",
            image_url="https://m.media-amazon.com/images/I/synthetic_home.jpg",
        )

        self.perfume_product = Product(
            title="Bellavita Luxury Honey Oud Eau de Parfum Perfume Unisex, 3.4 fl oz (100 ml)",
            features=[
                "Sweet Honey Perfume with Warm Oud – Honey Oud is a unisex Eau de Parfum loved as a honey perfume for women and a warm cologne for men. Golden honey and bergamot open, rose and patchouli follow, and oud, amber and vanilla close.",
                "Notes Pyramid – Top: sparkling bergamot and golden honey. Heart: soft rose, earthy patchouli and smoky oud. Base: warm amber, creamy vanilla and smooth musk for a sweet, woody and gourmand dry-down on skin and clothes.",
                "Eau de Parfum Concentration – One to two sprays are enough. Apply to pulse points such as the wrists, neck and behind the ears, and layer over unscented lotion in cold weather for a fuller scent trail. Patch test before first use.",
                "Christmas Gift for Her or Him – A cozy holiday present for wives, husbands, moms, dads, girlfriends and boyfriends who love sweet, warm scents. Made for winter evenings, date nights and festive dinners.",
                "What's Included – One 3.4 fluid ounce (100 milliliter) spray bottle of Honey Oud Eau de Parfum. Also available in a 0.67 fluid ounce (20 milliliter) travel size and inside the Bellavita Luxury unisex gift set."
            ],
            affiliate_url="https://link.amazon/B0g3KMMBy",
            original_affiliate_url="https://link.amazon/B0g3KMMBy",
            source_url="https://www.amazon.co.uk/dp/B09X5BQ969",
            price="$29.99",
            asin="B09X5BQ969",
            image_url="https://m.media-amazon.com/images/I/synthetic_perfume.jpg",
        )

        self.unknown_product = Product(
            title="Stainless Steel Ergonomic Multi-Grip Utility Wrench Tool 8-in-1",
            features=[
                "Multi-angle 360-degree rotating head for tight mechanical spaces",
                "Constructed from high-carbon alloy steel for heavy torque resistance",
                "Ergonomic rubberized grip handle prevents hand fatigue during extended use",
            ],
            affiliate_url="https://amzn.to/3Unknown",
            original_affiliate_url="https://amzn.to/3Unknown",
            source_url="https://www.amazon.com/dp/B0UNKNOWN0",
            price="$18.50",
            asin="B0UNKNOWN0",
            image_url="https://m.media-amazon.com/images/I/synthetic_unknown.jpg",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_original_image_pixel_preservation(self):
        """
        Verify that original image pixels inside the showcase area are 100% identical.
        No filters, inpainting, blur, or pixel modification allowed.
        """
        random.seed(12345)
        w, h = 300, 300
        test_img = Image.new("RGB", (w, h))
        random_pixels = [
            (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
            for _ in range(w * h)
        ]
        test_img.putdata(random_pixels)

        copy = generate_creative_copy(self.tech_product, ANGLE_PRACTICAL_SOLUTION)
        theme = THEMES["warm_minimalist"]

        exact_box = []
        canvas = compose_pinterest_creative(
            test_img,
            copy,
            theme,
            exact_box_out=exact_box,
            allow_upscale=False,
        )

        paste_x, paste_y, target_w, target_h = exact_box
        self.assertEqual(target_w, w, "Width should match original image")
        self.assertEqual(target_h, h, "Height should match original image")

        # Crop out exactly the pasted region from the canvas
        canvas_slice = canvas.crop((paste_x, paste_y, paste_x + w, paste_y + h))

        # Bit-level verification: every pixel must match verbatim
        self.assertEqual(test_img.tobytes(), canvas_slice.tobytes(), "Pasted product pixels must match original bit-for-bit")

    def test_original_image_file_preservation(self):
        """Verify that the original source image file on disk is never modified or overwritten."""
        src_path = self.test_root / "original_product_image.jpg"
        orig_img = Image.new("RGB", (400, 400), (34, 139, 34))
        orig_img.save(src_path, "JPEG")
        orig_bytes = src_path.read_bytes()
        orig_mtime = os.path.getmtime(src_path)

        # Run creative generator referencing this image
        res = generate_pinterest_creatives(
            self.tech_product,
            count=3,
            source_image=Image.open(src_path),
            creatives_dir=self.test_root / "creatives",
            overwrite=True,
        )
        self.assertTrue(res["success"])

        # Check source file bytes and mtime
        self.assertTrue(src_path.is_file(), "Original file must still exist")
        self.assertEqual(src_path.read_bytes(), orig_bytes, "Source file bytes must remain untouched")
        self.assertEqual(os.path.getmtime(src_path), orig_mtime, "Source file mtime must remain unchanged")

    def test_output_dimensions_and_aspect_ratio(self):
        """Verify that all generated creative outputs strictly conform to 1000x1500 (2:3 ratio)."""
        test_img = Image.new("RGB", (500, 500), (100, 150, 200))
        res = generate_pinterest_creatives(
            self.tech_product,
            count=4,
            source_image=test_img,
            creatives_dir=self.test_root / "creatives",
            overwrite=True,
        )
        self.assertTrue(res["success"])
        variations = res["data"]["variations"]
        self.assertGreaterEqual(len(variations), 3)

        for var in variations:
            self.assertEqual(var["dimensions"], [CANVAS_WIDTH, CANVAS_HEIGHT])
            self.assertEqual(var["aspect_ratio"], "2:3")
            img_path = var["file_path"]
            self.assertTrue(os.path.isfile(img_path), f"File {img_path} must exist on disk")
            with Image.open(img_path) as loaded:
                self.assertEqual(loaded.size, (CANVAS_WIDTH, CANVAS_HEIGHT))
                # Ratio check
                ratio = loaded.size[0] / loaded.size[1]
                self.assertAlmostEqual(ratio, 2.0 / 3.0, places=4)

    def test_dynamic_marketing_angle_selection(self):
        """Verify that marketing angles are dynamically selected based on product keywords and category."""
        # 1. Perfume product
        perfume_cat = detect_product_category(self.perfume_product)
        self.assertEqual(perfume_cat, CAT_FRAGRANCE)
        perfume_angles = select_marketing_angles(self.perfume_product, count=4)
        self.assertIn(ANGLE_LUXURY_FRAGRANCE, perfume_angles)
        self.assertIn(ANGLE_FRAGRANCE_NOTES, perfume_angles)
        self.assertIn(ANGLE_GIFT_INSPIRATION, perfume_angles)
        self.assertIn(ANGLE_EVERYDAY_ELEGANCE, perfume_angles)
        # Ensure no inappropriate tech or kitchen angles in perfume
        self.assertNotIn(ANGLE_SMART_TECH_UPGRADE, perfume_angles)
        self.assertNotIn(ANGLE_SPACE_SAVING_IDEA, perfume_angles)
        self.assertEqual(len(set(perfume_angles)), len(perfume_angles), "Selected angles must be distinct")

        # 2. Tech product with wireless charging and compact keywords
        tech_cat = detect_product_category(self.tech_product)
        self.assertEqual(tech_cat, CAT_ELECTRONICS)
        tech_angles = select_marketing_angles(self.tech_product, count=4)
        self.assertIn(ANGLE_SMART_TECH_UPGRADE, tech_angles)
        self.assertIn(ANGLE_COMPACT_CONVENIENCE, tech_angles)
        self.assertNotIn(ANGLE_LUXURY_FRAGRANCE, tech_angles)
        self.assertEqual(len(set(tech_angles)), len(tech_angles), "Selected angles must be distinct")
        self.assertGreaterEqual(len(tech_angles), 3)
        self.assertLessEqual(len(tech_angles), 5)

        # 3. Home organizer product with storage and under-sink keywords
        home_cat = detect_product_category(self.home_product)
        self.assertEqual(home_cat, CAT_HOME_KITCHEN)
        home_angles = select_marketing_angles(self.home_product, count=4)
        self.assertIn(ANGLE_SPACE_SAVING_IDEA, home_angles)
        self.assertIn(ANGLE_PRACTICAL_SOLUTION, home_angles)
        self.assertNotIn(ANGLE_LUXURY_FRAGRANCE, home_angles)
        self.assertNotIn(ANGLE_SMART_TECH_UPGRADE, home_angles)
        self.assertEqual(len(set(home_angles)), len(home_angles), "Selected angles must be distinct")

        # 4. Unknown / general product safe fallback
        unknown_cat = detect_product_category(self.unknown_product)
        self.assertEqual(unknown_cat, CAT_GENERAL)
        unknown_angles = select_marketing_angles(self.unknown_product, count=4)
        self.assertIn(ANGLE_PRACTICAL_SOLUTION, unknown_angles)
        self.assertEqual(len(set(unknown_angles)), len(unknown_angles), "Selected angles must be distinct")

    def test_perfume_category_product_aware_marketing(self):
        """
        Verify product-aware marketing for perfumes (ASIN B09X5BQ969):
        - Factual scent notes, no hallucinations
        - Scent pyramid extraction (Top, Heart, Base)
        - Zero generic templates ('Engineered for Reliable Performance', 'Durable, quality build', 'Smart Home Upgrade')
        - Distinct headlines and non-identical bullet sets per variation
        """
        facts = FactExtractor.extract_canonical_facts(self.perfume_product)
        angles = select_marketing_angles(self.perfume_product, facts, count=4)
        self.assertEqual(len(angles), 4)

        copies = [generate_creative_copy(self.perfume_product, angle, facts) for angle in angles]

        # Verify distinct headlines
        headlines = [c.headline for c in copies]
        self.assertEqual(len(set(headlines)), len(headlines), "All creative headlines must be distinct")

        # Verify fragrance notes headline and pyramid
        notes_copy = next(c for c in copies if c.angle == ANGLE_FRAGRANCE_NOTES)
        self.assertIn("Golden Honey", notes_copy.headline)
        self.assertIn("Smoky Oud", notes_copy.headline)
        self.assertTrue(any(b.startswith("Top:") for b in notes_copy.benefits))
        self.assertTrue(any(b.startswith("Heart:") for b in notes_copy.benefits))
        self.assertTrue(any(b.startswith("Base:") for b in notes_copy.benefits))

        # Verify luxury fragrance headline and bullets
        lux_copy = next(c for c in copies if c.angle == ANGLE_LUXURY_FRAGRANCE)
        self.assertIn("Honey", lux_copy.headline)
        self.assertIn("Smoky Oud", lux_copy.headline)
        self.assertTrue(any("Eau de Parfum" in b for b in lux_copy.benefits))

        # Verify everyday elegance bullets contain pulse points or volume
        elegance_copy = next(c for c in copies if c.angle == ANGLE_EVERYDAY_ELEGANCE)
        self.assertTrue(any("pulse points" in b.lower() for b in elegance_copy.benefits))

        # Verify gift inspiration copy
        gift_copy = next(c for c in copies if c.angle == ANGLE_GIFT_INSPIRATION)
        self.assertIn("GIFT", gift_copy.badge.upper())
        self.assertTrue(any("holiday" in b.lower() or "present" in b.lower() for b in gift_copy.benefits))

        # Verify no generic boilerplate phrases anywhere in copy
        forbidden_boilerplate = [
            "durable, quality build",
            "engineered for reliable performance",
            "easy everyday use",
            "smart home upgrade",
            "reliable performance",
            "built to impress",
        ]
        for c in copies:
            corpus = f"{c.badge} {c.headline} {c.subheadline} {' '.join(c.benefits)}".lower()
            for phrase in forbidden_boilerplate:
                self.assertNotIn(phrase, corpus, f"Forbidden generic phrase '{phrase}' found in copy for angle {c.angle}")

        # Verify non-identical bullet sets across variations
        bullet_sets = [tuple(sorted(c.benefits)) for c in copies]
        self.assertEqual(len(set(bullet_sets)), len(bullet_sets), "Each variation must feature non-identical bullet points")

    def test_product_specific_grounded_copy(self):
        """Verify that copy generation produces angle-appropriate, grounded headlines and bullets."""
        facts = FactExtractor.extract_canonical_facts(self.tech_product)
        copy_smart = generate_creative_copy(self.tech_product, ANGLE_SMART_TECH_UPGRADE, facts)
        copy_space = generate_creative_copy(self.home_product, ANGLE_SPACE_SAVING_IDEA, facts)
        copy_gift = generate_creative_copy(self.tech_product, ANGLE_GIFT_INSPIRATION, facts)

        # Distinct headlines and badges
        self.assertNotEqual(copy_smart.headline, copy_space.headline)
        self.assertNotEqual(copy_smart.badge, copy_gift.badge)
        self.assertEqual(copy_smart.badge, "SMART TECH UPGRADE")
        self.assertEqual(copy_space.badge, "SPACE-SAVING IDEA")

        # Grounded benefits
        self.assertGreaterEqual(len(copy_smart.benefits), 2)
        for b in copy_smart.benefits:
            self.assertLessEqual(len(b), 48, "Benefit bullets must be mobile-friendly and concise")

    def test_unknown_category_safe_fallback(self):
        """Verify that unclassified products use conservative, verified clauses with zero invented claims."""
        facts = FactExtractor.extract_canonical_facts(self.unknown_product)
        copy_fallback = generate_creative_copy(self.unknown_product, ANGLE_PRACTICAL_SOLUTION, facts)
        self.assertEqual(copy_fallback.badge, "PRACTICAL SOLUTION")
        full_text = f"{copy_fallback.headline} {copy_fallback.subheadline}"
        self.assertIn("Wrench", full_text)
        # Bullets must come from product features without hallucinating perfumes or tech
        self.assertGreaterEqual(len(copy_fallback.benefits), 1)
        for b in copy_fallback.benefits:
            self.assertNotIn("fragrance", b.lower())
            self.assertNotIn("wireless", b.lower())

    def test_cta_selection_and_rendering(self):
        """Verify that CTAs come from allowed set and render without errors."""
        for angle in ALL_MARKETING_ANGLES:
            copy = generate_creative_copy(self.tech_product, angle)
            self.assertIn(copy.cta_text, ALLOWED_CTAS, f"CTA {copy.cta_text} must be in allowed list")

        # Render test
        test_img = Image.new("RGB", (200, 200), (200, 200, 200))
        for angle in ALL_MARKETING_ANGLES:
            copy = generate_creative_copy(self.tech_product, angle)
            canvas = compose_pinterest_creative(test_img, copy, THEMES["warm_minimalist"])
            self.assertEqual(canvas.size, (1000, 1500))

    def test_affiliate_url_preservation(self):
        """Verify that product affiliate URLs are strictly preserved byte-for-byte in metadata."""
        test_img = Image.new("RGB", (300, 300), (50, 50, 50))
        res = generate_pinterest_creatives(
            self.tech_product,
            count=3,
            source_image=test_img,
            creatives_dir=self.test_root / "creatives",
            overwrite=True,
        )
        self.assertTrue(res["success"])
        self.assertEqual(
            res["data"]["affiliate_url"],
            self.tech_product.original_affiliate_url,
            "Affiliate URL must match byte-for-byte"
        )
        self.assertEqual(
            res["data"]["original_image_url"],
            self.tech_product.image_url,
            "Original image URL must match byte-for-byte"
        )

    def test_failure_fallback_on_missing_image(self):
        """Verify that creative generator fails closed safely when no product image is available."""
        product_no_image = Product(
            title="Item Without Image",
            asin="B0NOIMAGE1",
            affiliate_url="https://amzn.to/noimg",
            image_url=None,
        )
        res = generate_pinterest_creatives(
            product_no_image,
            count=3,
            creatives_dir=self.test_root / "creatives",
        )
        self.assertFalse(res["success"])
        self.assertIn("missing", res["message"].lower())
        self.assertFalse(res["reused"])

    def test_prevent_duplicate_generation_when_valid(self):
        """Verify that existing creative variations are reused if still valid, unless overwrite=True."""
        test_img = Image.new("RGB", (300, 300), (80, 80, 80))
        creatives_dir = self.test_root / "creatives"

        # First run: generates
        res1 = generate_pinterest_creatives(
            self.tech_product,
            count=3,
            source_image=test_img,
            creatives_dir=creatives_dir,
            overwrite=False,
        )
        self.assertTrue(res1["success"])
        self.assertFalse(res1["reused"])

        # Second run: reuses existing
        res2 = generate_pinterest_creatives(
            self.tech_product,
            count=3,
            source_image=test_img,
            creatives_dir=creatives_dir,
            overwrite=False,
        )
        self.assertTrue(res2["success"])
        self.assertTrue(res2["reused"])
        self.assertEqual(len(res2["data"]["variations"]), len(res1["data"]["variations"]))

        # Third run with overwrite=True: regenerates
        res3 = generate_pinterest_creatives(
            self.tech_product,
            count=3,
            source_image=test_img,
            creatives_dir=creatives_dir,
            overwrite=True,
        )
        self.assertTrue(res3["success"])
        self.assertFalse(res3["reused"])

    def test_publishing_adapter_compatibility(self):
        """
        Verify that existing Pinterest publishing adapter continues to work with
        original image URL unchanged, and handles creatives metadata without regression.
        """
        adapter = PinterestAdapter(credentials={"access_token": "dummy_token"})
        content = {
            "title": "Anker 3-in-1 Charging Stand",
            "description": "Keep your nightstand tidy https://amzn.to/3XTech1",
            "image_url": self.tech_product.image_url,
            "link": self.tech_product.affiliate_url,
            "pinterest_creatives": {
                "total_variations": 3,
                "variations": [
                    {"angle": "Smart Home Upgrade", "file_path": "data/creatives/B07TESTECH/var_1.png"}
                ]
            }
        }
        account_config = {
            "account": "dailyideasforyou",
            "board": "Shopping ideas",
            "board_id": "1033646620665866548",
        }

        # Dry run simulation must succeed using the original image_url
        res = adapter.publish(content, account_config, dry_run=True)
        self.assertEqual(res["status"], "WOULD_PUBLISH")
        self.assertEqual(res["platform"], "pinterest")
        self.assertEqual(res["account"], "dailyideasforyou")
        self.assertEqual(res["board"], "Shopping ideas")

    def test_towel_no_undersink_storage_regression(self):
        """
        Regression Test: A towel or kitchen textile product must NEVER receive
        under-sink storage, sliding drawer, or cabinet organization copy.
        """
        towel_product = Product(
            title="Utopia Towels 8-Piece Kitchen Dish Towels 100% Ring Spun Cotton",
            description="Soft and highly absorbent cotton dish towels for kitchen cleaning and drying dishes.",
            features=[
                "100% Ring Spun Cotton for superior absorbency and soft hand feel",
                "Durable reinforced hems prevent unraveling after multiple machine washes",
                "Versatile cleaning cloths ideal for wiping counters, drying hands and dishes",
            ],
            affiliate_url="https://amzn.to/3Towel1",
            original_affiliate_url="https://amzn.to/3Towel1",
            source_url="https://www.amazon.com/dp/B08TOWEL01",
            price="$19.99",
            asin="B08TOWEL01",
            image_url="https://m.media-amazon.com/images/I/synthetic_towel.jpg",
        )
        facts = FactExtractor.extract_canonical_facts(towel_product)
        angles = select_marketing_angles(towel_product, facts, count=4)

        # Space-saving idea should NOT be selected for a towel without storage features
        self.assertNotIn(ANGLE_SPACE_SAVING_IDEA, angles)

        copies = [generate_creative_copy(towel_product, angle, facts) for angle in angles]
        prohibited_storage_terms = [
            "under the sink",
            "sliding drawer",
            "cabinet organization",
            "pull-out",
            "organizer",
            "sliding bottom drawer",
            "sliding tiers",
        ]

        for copy in copies:
            corpus = f"{copy.badge} {copy.headline} {copy.subheadline} {' '.join(copy.benefits)}".lower()
            for term in prohibited_storage_terms:
                self.assertNotIn(term, corpus, f"Towel received storage term '{term}' for angle '{copy.angle}'")

            # Must mention absorbent, soft, or cotton features
            self.assertTrue(
                any(k in corpus for k in ["absorb", "cotton", "soft", "clean", "towel", "cloth", "kitchen"]),
                f"Towel copy must ground in textile features, got: {corpus}"
            )

            # Validate copy through validation engine
            errors = validate_creative_copy(copy, facts, towel_product)
            self.assertEqual(errors, [], f"Towel creative copy failed validation: {errors}")

    def test_warning_and_disclaimer_exclusion_regression(self):
        """
        Regression Test: Warning labels, disclaimers, or negative care notices
        must be completely excluded from creative headlines, subheadlines, and benefits.
        """
        product_with_warnings = Product(
            title="Organic Tea Tree Essential Oil Face Serum 1 fl oz",
            features=[
                "Pure organic tea tree leaf extract for soothing facial skin care",
                "WARNING: For external use only. Keep out of reach of children. Patch test before first use.",
                "Caution: Avoid direct contact with eyes and open wounds.",
                "Rich in natural antioxidants that hydrate and nourish dry skin",
            ],
            asin="B0WARNING01",
            affiliate_url="https://amzn.to/3Warn1",
            image_url="https://m.media-amazon.com/images/I/synthetic_oil.jpg",
        )
        facts = FactExtractor.extract_canonical_facts(product_with_warnings)
        angles = select_marketing_angles(product_with_warnings, facts, count=4)

        for angle in angles:
            copy = generate_creative_copy(product_with_warnings, angle, facts)
            corpus = f"{copy.badge} {copy.headline} {copy.subheadline} {' '.join(copy.benefits)}".lower()

            self.assertNotIn("warning", corpus)
            self.assertNotIn("caution", corpus)
            self.assertNotIn("patch test", corpus)
            self.assertNotIn("external use only", corpus)
            self.assertNotIn("keep out of reach", corpus)

            # Benefits must not contain warning clauses
            for b in copy.benefits:
                self.assertFalse(is_disclaimer_or_warning_text(b), f"Warning detected in benefit bullet: '{b}'")

            errors = validate_creative_copy(copy, facts, product_with_warnings)
            self.assertEqual(errors, [], f"Copy with warnings should be cleanly filtered, but errors found: {errors}")

    def test_isolated_spec_exclusion_regression(self):
        """
        Regression Test: Isolated raw dimensions or specifications (e.g. '1200 W', '15W')
        must never be rendered as standalone bullet points without descriptive context.
        """
        product_with_specs = Product(
            title="Compact Electric Travel Kettle 0.8L",
            features=[
                "Food grade 304 stainless steel interior with cool-touch exterior",
                "1200 W",
                "120V / 60Hz",
                "Fast boiling in under 5 minutes with automatic shut-off safety protection",
            ],
            asin="B0SPEC0001",
            affiliate_url="https://amzn.to/3Spec1",
            image_url="https://m.media-amazon.com/images/I/synthetic_kettle.jpg",
        )
        facts = FactExtractor.extract_canonical_facts(product_with_specs)
        angles = select_marketing_angles(product_with_specs, facts, count=4)

        for angle in angles:
            copy = generate_creative_copy(product_with_specs, angle, facts)
            for b in copy.benefits:
                self.assertNotEqual(b.strip().lower(), "1200 w")
                self.assertNotEqual(b.strip().lower(), "size: 1200 w")
                self.assertNotEqual(b.strip().lower(), "120v / 60hz")
                self.assertFalse(is_isolated_spec_fragment(b), f"Isolated spec fragment in benefit: '{b}'")

    def test_canonical_facts_and_claim_validator_enforcement(self):
        """
        Verify that validate_creative_copy enforces ClaimValidator rules,
        rejects ungrounded commercial claims, and detects cross-ASIN mismatches.
        """
        facts = FactExtractor.extract_canonical_facts(self.tech_product)
        facts.asin = self.tech_product.asin

        # Valid grounded copy passes
        valid_copy = generate_creative_copy(self.tech_product, ANGLE_SMART_TECH_UPGRADE, facts)
        errors = validate_creative_copy(valid_copy, facts, self.tech_product)
        self.assertEqual(errors, [])

        # Cross-ASIN mismatch is caught
        foreign_product = Product(
            title="Foreign Product",
            asin="B0FOREIGN1",
            affiliate_url="https://amzn.to/foreign",
        )
        mismatch_errors = validate_creative_copy(valid_copy, facts, foreign_product)
        self.assertTrue(any("Cross-ASIN mismatch" in e for e in mismatch_errors))

        # Hallucinated / prohibited filler copy is caught
        hallucinated_copy = CreativeCopy(
            angle="Key Benefits",
            badge="BENEFITS",
            headline="Engineered for Reliable Performance",
            subheadline="Durable, quality build with guaranteed satisfaction.",
            benefits=["100% genuine titanium casing", "Patch test before first use"],
            cta_text="Shop Now",
        )
        val_errors = validate_creative_copy(hallucinated_copy, facts, self.tech_product)
        self.assertTrue(any("Prohibited generic filler" in e for e in val_errors))
        self.assertTrue(any("Warning or disclaimer" in e for e in val_errors))

    def test_multi_template_rendering_and_pixel_preservation(self):
        """
        Verify that all 4 distinct layout templates render cleanly on 1000x1500 canvas
        and preserve original product image pixels bit-for-bit.
        """
        random.seed(98765)
        w, h = 280, 280
        test_img = Image.new("RGB", (w, h))
        random_pixels = [
            (random.randint(0, 255), random.randint(0, 255), random.randint(0, 255))
            for _ in range(w * h)
        ]
        test_img.putdata(random_pixels)

        copy = generate_creative_copy(self.tech_product, ANGLE_SMART_TECH_UPGRADE)
        theme = THEMES["modern_tech"]

        for template in LAYOUT_TEMPLATES:
            exact_box = []
            canvas = compose_pinterest_creative(
                test_img,
                copy,
                theme,
                exact_box_out=exact_box,
                allow_upscale=False,
                layout_template=template,
            )
            self.assertEqual(canvas.size, (CANVAS_WIDTH, CANVAS_HEIGHT), f"Template {template} size must be 1000x1500")

            paste_x, paste_y, target_w, target_h = exact_box
            self.assertEqual(target_w, w, f"Template {template} width must match original")
            self.assertEqual(target_h, h, f"Template {template} height must match original")

            # Slice and compare exact bytes
            canvas_slice = canvas.crop((paste_x, paste_y, paste_x + w, paste_y + h))
            self.assertEqual(
                test_img.tobytes(),
                canvas_slice.tobytes(),
                f"Template {template} must preserve product pixels bit-for-bit"
            )

        # Full generation across multiple variations rotates templates
        res = generate_pinterest_creatives(
            self.tech_product,
            count=4,
            source_image=test_img,
            creatives_dir=self.test_root / "creatives",
            overwrite=True,
        )
        self.assertTrue(res["success"])
        variations = res["data"]["variations"]
        templates_used = [v.get("layout_template") for v in variations]
        self.assertEqual(len(set(templates_used)), 4, "All 4 variations should use distinct layout templates")

    def test_insufficient_verified_facts_safe_failure(self):
        """Verify that creative generation fails closed safely when minimal product facts are absent."""
        empty_product = Product(
            title="",
            asin="B0EMPTY0001",
            affiliate_url="https://amzn.to/empty",
            image_url="https://m.media-amazon.com/images/I/empty.jpg",
        )
        test_img = Image.new("RGB", (200, 200), (255, 255, 255))
        res = generate_pinterest_creatives(
            empty_product,
            source_image=test_img,
            creatives_dir=self.test_root / "creatives",
        )
        self.assertFalse(res["success"])
        self.assertIn("insufficient", res["message"].lower())


class TestDashboardCreativeEndpoints(unittest.TestCase):
    """Test web dashboard routes for Pinterest marketing creative generation."""

    def setUp(self):
        from web.app import create_app
        self.app = create_app()
        self.client = self.app.test_client()

    def test_generate_creatives_api_endpoint(self):
        """Verify POST /api/generate-creatives/<asin> returns structured JSON."""
        # Use existing product in data/products/ if available, or synthetic
        res = self.client.post(
            "/api/generate-creatives/B08CRPWQHB",
            headers={"X-Requested-With": "XMLHttpRequest"}
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertIn("success", data)
        self.assertIn("asin", data)
        self.assertEqual(data["asin"], "B08CRPWQHB")

    def test_serve_creative_file_endpoint_and_404(self):
        """Verify GET /creatives/<asin>/<filename> correctly serves files or returns 404."""
        # Non-existent file must 404
        res_404 = self.client.get("/creatives/B08CRPWQHB/non_existent_creative.png")
        self.assertEqual(res_404.status_code, 404)


if __name__ == "__main__":
    unittest.main()
