"""
Comprehensive Universal Architecture Tests for AmazonAffiliateBot Source-Grounded Content Generation.
Tests multi-category canonical product facts extraction, strict claim grounding, benefit escalation rejection,
legitimate paraphrasing acceptance, exact numeric/unit consistency, affiliate URL preservation, and platform formatting.
Covers 6 categories: Appliance, Electronics, Furniture, Clothing/Accessory, Camping/Outdoor, and Novel Unseen Category.
"""

import sys
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from amazon.scraper import Product
from content.facts import FactExtractor, CanonicalProductFacts
from content.validator import ClaimValidator
from content.generator import ContentGenerator
from content.package import ContentPackage


class TestUniversalGroundingArchitecture(unittest.TestCase):
    def setUp(self):
        self.disclosure = "This post contains affiliate links. As an Amazon Associate I earn from qualifying purchases."
        self.gen = ContentGenerator()

    # ==================== 1. APPLIANCE CATEGORY ====================
    def test_appliance_valid_paraphrases(self):
        """Appliance: Valid factual paraphrasing preserving canonical facts."""
        product = Product(
            title="Espresso Machine with 15 bar pressure, integrated grinder, and milk frother",
            features=[
                "15 bar extraction pressure",
                "Includes coffee grinder",
                "Equipped with milk frother steam wand",
                "Removable water tank and drip tray",
                "Prepare espresso, lattes and cappuccino at home"
            ],
            affiliate_url="https://link.amazon/applianceA",
            asin="B0APPLIAN1"
        )
        valid_claims = [
            "The machine has 15 bar pressure.",
            "A grinder is included.",
            "It includes a milk frother.",
            "The water tank is removable.",
            "For at-home brewing of espresso, lattes and cappuccino."
        ]
        for claim in valid_claims:
            post = {"text": f"{claim}\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
            is_valid, issues = self.gen.validate_platform_content("x", post, product, self.disclosure)
            self.assertTrue(is_valid, f"Expected '{claim}' to be valid, got issues: {issues}")

    def test_appliance_exact_unit_mismatch(self):
        """Appliance: Rejects 15 psi or 15 lbs when source is 15 bar."""
        product = Product(
            title="Espresso Machine with 15 bar pressure and 2.3 litre water tank",
            features=["15 bar extraction pressure", "2.3 litre water reservoir"],
            affiliate_url="https://link.amazon/applianceA",
            asin="B0APPLIAN1"
        )
        bad_post = {"text": f"Espresso machine with 15 psi pressure and 2.3 gallons tank.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_valid, issues = self.gen.validate_platform_content("x", bad_post, product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("unit mismatch" in iss.lower() or "psi" in iss.lower() or "gallons" in iss.lower() for iss in issues))

    def test_appliance_entity_modifier_misbinding(self):
        """Appliance: Rejects associating removable with boiler or durable with drip tray when swapped."""
        product = Product(
            title="Espresso Machine with Durable Boiler and Removable Drip Tray",
            features=["Durable stainless steel boiler", "Removable plastic drip tray"],
            affiliate_url="https://link.amazon/applianceA",
            asin="B0APPLIAN1"
        )
        bad_post = {"text": f"Espresso machine with a removable boiler and durable drip tray.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_valid, issues = self.gen.validate_platform_content("x", bad_post, product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("modifier mismatch" in iss.lower() or "removable" in iss.lower() for iss in issues))

    def test_appliance_rejects_unsupported_outcomes_and_causal_claims(self):
        """Appliance: Rejects unsupported outcome, causal, and benefit claims."""
        product = Product(
            title="Espresso Machine with 15 bar pressure, integrated grinder, and milk frother",
            features=[
                "15 bar extraction pressure",
                "Includes coffee grinder",
                "Equipped with milk frother steam wand",
                "Removable water tank and drip tray"
            ],
            affiliate_url="https://link.amazon/applianceA",
            asin="B0APPLIAN1"
        )
        invalid_claims = [
            ("It delivers maximum extraction.", "maximum extraction"),
            ("The powerful steam system produces silky microfoam.", "silky"),
            ("The removable drip tray makes cleaning faster.", "cleaning"),
            ("It produces professional espresso.", "professional"),
            ("15 bar pressure produces richer coffee.", "richer"),
            ("The machine provides better brewing.", "better")
        ]
        for claim, expected_kw in invalid_claims:
            post = {"text": f"{claim}\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
            is_valid, issues = self.gen.validate_platform_content("x", post, product, self.disclosure)
            self.assertFalse(is_valid, f"Expected '{claim}' to be rejected, but it passed!")

    # ==================== 2. ELECTRONICS CATEGORY ====================
    def test_electronics_valid_paraphrases(self):
        """Electronics: Valid factual summary of confirmed display and stand specifications."""
        product = Product(
            title="Computer Monitor 27-Inch IPS Display with USB-C",
            features=[
                "27-inch IPS screen",
                "Includes USB-C port",
                "Adjustable monitor stand"
            ],
            affiliate_url="https://link.amazon/elecB",
            asin="B0ELECTR02"
        )
        valid_claims = [
            "It has a 27-inch display.",
            "The monitor includes a USB-C port.",
            "The stand is adjustable."
        ]
        for claim in valid_claims:
            post = {"text": f"{claim}\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
            is_valid, issues = self.gen.validate_platform_content("x", post, product, self.disclosure)
            self.assertTrue(is_valid, f"Expected '{claim}' to be valid, got issues: {issues}")

    def test_electronics_exact_unit_mismatch(self):
        """Electronics: Rejects 27 cm when source specifies 27-inch."""
        product = Product(
            title="Computer Monitor 27-Inch IPS Display with USB-C",
            features=["27-inch IPS screen", "USB-C port"],
            affiliate_url="https://link.amazon/elecB",
            asin="B0ELECTR02"
        )
        bad_post = {"text": f"Computer monitor with a 27 cm IPS screen.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_valid, issues = self.gen.validate_platform_content("x", bad_post, product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("unit mismatch" in iss.lower() or "cm" in iss.lower() for iss in issues))

    def test_electronics_entity_modifier_misbinding(self):
        """Electronics: Rejects attributing IPS to stand or adjustable to screen when swapped."""
        product = Product(
            title="Computer Monitor 27-Inch IPS Display with Adjustable Stand",
            features=["27-inch IPS screen", "Adjustable monitor stand"],
            affiliate_url="https://link.amazon/elecB",
            asin="B0ELECTR02"
        )
        bad_post = {"text": f"Monitor with an adjustable screen and IPS stand.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_valid, issues = self.gen.validate_platform_content("x", bad_post, product, self.disclosure)
        self.assertFalse(is_valid)

    def test_electronics_rejects_unsupported_benefits_and_outcomes(self):
        """Electronics: Rejects immersive experience, fast charging, posture improvements, and superior refresh."""
        product = Product(
            title="Computer Monitor 27-Inch IPS Display with USB-C",
            features=[
                "27-inch IPS screen",
                "Includes USB-C port",
                "Adjustable monitor stand"
            ],
            affiliate_url="https://link.amazon/elecB",
            asin="B0ELECTR02"
        )
        invalid_claims = [
            "The display provides an immersive experience.",
            "The USB-C port enables fast charging.",
            "The stand improves posture.",
            "The monitor provides superior refresh rates.",
            "The product improves performance."
        ]
        for claim in invalid_claims:
            post = {"text": f"{claim}\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
            is_valid, issues = self.gen.validate_platform_content("x", post, product, self.disclosure)
            self.assertFalse(is_valid, f"Expected '{claim}' to be rejected, but it passed!")

    # ==================== 3. FURNITURE CATEGORY ====================
    def test_furniture_valid_paraphrases(self):
        """Furniture: Valid factual summary of desk dimensions and materials."""
        product = Product(
            title="Writing Desk Solid Wood Workstation with 2 Drawers",
            features=[
                "Made from solid wood frame",
                "120 cm width and 60 cm depth",
                "Includes 2 drawers with handles"
            ],
            affiliate_url="https://link.amazon/furnC",
            asin="B0FURNIT03"
        )
        valid_claims = [
            "The piece is made from solid wood.",
            "It is 120 cm wide.",
            "It has 2 drawers."
        ]
        for claim in valid_claims:
            post = {"text": f"{claim}\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
            is_valid, issues = self.gen.validate_platform_content("x", post, product, self.disclosure)
            self.assertTrue(is_valid, f"Expected '{claim}' to be valid, got issues: {issues}")

    def test_furniture_exact_unit_mismatch(self):
        """Furniture: Rejects 120 inches width when source is 120 cm."""
        product = Product(
            title="Writing Desk Solid Wood Workstation",
            features=["120 cm width", "Solid wood frame"],
            affiliate_url="https://link.amazon/furnC",
            asin="B0FURNIT03"
        )
        bad_post = {"text": f"Writing desk with 120 inches width.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_valid, issues = self.gen.validate_platform_content("x", bad_post, product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("unit mismatch" in iss.lower() or "inches" in iss.lower() for iss in issues))

    def test_furniture_entity_modifier_misbinding(self):
        """Furniture: Rejects attaching metal to frame or solid wood to handles when swapped."""
        product = Product(
            title="Writing Desk with Solid Wood Frame and Metal Handles",
            features=["Solid wood frame", "Metal handles on 2 drawers"],
            affiliate_url="https://link.amazon/furnC",
            asin="B0FURNIT03"
        )
        bad_post = {"text": f"Desk with a metal frame and solid wood handles.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_valid, issues = self.gen.validate_platform_content("x", bad_post, product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("modifier mismatch" in iss.lower() or "metal" in iss.lower() or "wood" in iss.lower() for iss in issues))

    def test_furniture_rejects_unsupported_claims_and_comparatives(self):
        """Furniture: Rejects spacious design, built to last, superior stability, and ergonomic posture claims."""
        product = Product(
            title="Writing Desk Solid Wood Workstation with 2 Drawers",
            features=[
                "Solid wood frame",
                "120 cm width",
                "2 drawers"
            ],
            affiliate_url="https://link.amazon/furnC",
            asin="B0FURNIT03"
        )
        invalid_claims = [
            "The spacious design is ideal for any room.",
            "The premium construction is built to last.",
            "The steel frame provides superior stability.",
            "The design reduces effort.",
            "The solid wood provides stronger construction."
        ]
        for claim in invalid_claims:
            post = {"text": f"{claim}\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
            is_valid, issues = self.gen.validate_platform_content("x", post, product, self.disclosure)
            self.assertFalse(is_valid, f"Expected '{claim}' to be rejected, but it passed!")

    # ==================== 4. CLOTHING / ACCESSORY CATEGORY ====================
    def test_clothing_accessory_valid_paraphrases(self):
        """Clothing: Valid summary of materials, zipper, and hood."""
        product = Product(
            title="Waterproof Breathable Rain Jacket with YKK Zipper and Adjustable Hood",
            features=[
                "Waterproof breathable nylon shell",
                "Includes YKK zipper front closure",
                "Equipped with adjustable hood and zippered pockets"
            ],
            affiliate_url="https://link.amazon/jacket",
            asin="B0CLOTH001"
        )
        valid_post = {
            "text": f"Rain jacket with a waterproof breathable nylon shell, YKK zipper, and adjustable hood.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = self.gen.validate_platform_content("x", valid_post, product, self.disclosure)
        self.assertTrue(is_valid, f"Expected valid, got issues: {issues}")

    def test_clothing_exact_unit_mismatch(self):
        """Clothing: Rejects 3000 inches waterproof rating when source is 3000 mm."""
        product = Product(
            title="Rain Jacket with 3000mm Waterproof Nylon Shell",
            features=["3000mm waterproof polyurethane coating", "YKK zipper"],
            affiliate_url="https://link.amazon/jacket",
            asin="B0CLOTH001"
        )
        bad_post = {"text": f"Rain jacket with 3000 inches waterproof coating.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_valid, issues = self.gen.validate_platform_content("x", bad_post, product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("unit mismatch" in iss.lower() or "inches" in iss.lower() for iss in issues))

    def test_clothing_entity_modifier_misbinding(self):
        """Clothing: Rejects breathable shell and waterproof mesh lining when source is waterproof shell and breathable mesh."""
        product = Product(
            title="Rain Jacket with Waterproof Nylon Shell and Breathable Mesh Lining",
            features=["Waterproof nylon shell", "Breathable mesh lining with YKK zipper"],
            affiliate_url="https://link.amazon/jacket",
            asin="B0CLOTH001"
        )
        bad_post = {"text": f"Rain jacket featuring a breathable nylon shell and waterproof mesh lining.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_valid, issues = self.gen.validate_platform_content("x", bad_post, product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("modifier mismatch" in iss.lower() or "breathable" in iss.lower() or "waterproof" in iss.lower() for iss in issues))

    def test_clothing_rejects_unsupported_outcomes_and_benefits(self):
        """Clothing: Rejects 'keeps you warmer', 'effortless style', 'faster sweat evaporation'."""
        product = Product(
            title="Waterproof Breathable Rain Jacket with YKK Zipper and Adjustable Hood",
            features=[
                "Waterproof breathable nylon shell",
                "Includes YKK zipper front closure",
                "Equipped with adjustable hood"
            ],
            affiliate_url="https://link.amazon/jacket",
            asin="B0CLOTH001"
        )
        invalid_claims = [
            "The fabric keeps you warmer.",
            "The rain jacket provides effortless style.",
            "The breathable shell enables faster sweat evaporation.",
            "The jacket is lighter than standard rainwear."
        ]
        for claim in invalid_claims:
            post = {"text": f"{claim}\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
            is_valid, issues = self.gen.validate_platform_content("x", post, product, self.disclosure)
            self.assertFalse(is_valid, f"Expected '{claim}' to be rejected, but it passed!")

    # ==================== 5. CAMPING / OUTDOOR CATEGORY ====================
    def test_camping_outdoor_valid_paraphrases(self):
        """Camping: Valid summary of tent capacity, rainfly, and aluminum poles."""
        product = Product(
            title="4-Person Camping Tent with Waterproof 3000mm Rainfly and Aluminum Poles",
            features=[
                "Accommodates up to 4 persons",
                "3000mm waterproof polyurethane rainfly",
                "Includes lightweight aluminum alloy poles",
                "Features 2 zippered doors and mesh ventilation windows"
            ],
            affiliate_url="https://link.amazon/tentD",
            asin="B0TENT0004"
        )
        valid_post = {
            "text": f"Camping tent for 4 persons with a 3000mm waterproof rainfly and aluminum poles. Includes 2 zippered doors.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = self.gen.validate_platform_content("x", valid_post, product, self.disclosure)
        self.assertTrue(is_valid, f"Expected valid, got issues: {issues}")

    def test_camping_exact_unit_mismatch(self):
        """Camping: Rejects unit swap (e.g. 7001 kg poles when source is 7001 aluminum alloy)."""
        product = Product(
            title="Backpacking Tent with 7001 Aluminum Poles",
            features=["7001 aluminum alloy poles", "20D ripstop nylon rainfly"],
            affiliate_url="https://link.amazon/tent",
            asin="B0CAMP001"
        )
        bad_post = {"text": f"Backpacking tent with 7001 kg aluminum poles.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_valid, issues = self.gen.validate_platform_content("x", bad_post, product, self.disclosure)
        self.assertFalse(is_valid)

    def test_camping_entity_modifier_misbinding(self):
        """Camping: Rejects ripstop nylon poles and aluminum rainfly when swapped."""
        product = Product(
            title="Backpacking Tent with Aluminum Poles and Ripstop Nylon Rainfly",
            features=["Aluminum poles", "Ripstop nylon rainfly"],
            affiliate_url="https://link.amazon/tent",
            asin="B0CAMP001"
        )
        bad_post = {"text": f"Backpacking tent with ripstop nylon poles and an aluminum rainfly.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_valid, issues = self.gen.validate_platform_content("x", bad_post, product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("modifier mismatch" in iss.lower() or "nylon" in iss.lower() or "aluminum" in iss.lower() for iss in issues))

    def test_camping_rejects_unsupported_outcomes_and_causal_claims(self):
        """Camping: Rejects easy setup, peaceful mountain shelter, eliminating wind turbulence."""
        product = Product(
            title="4-Person Camping Tent with Waterproof 3000mm Rainfly and Aluminum Poles",
            features=[
                "4 persons capacity",
                "3000mm waterproof rainfly",
                "Aluminum poles"
            ],
            affiliate_url="https://link.amazon/tentD",
            asin="B0TENT0004"
        )
        invalid_claims = [
            "The tent enables easy setup in minutes.",
            "Provides a peaceful mountain shelter.",
            "Aerodynamic dome shape eliminates wind turbulence.",
            "The rainfly provides tougher protection."
        ]
        for claim in invalid_claims:
            post = {"text": f"{claim}\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
            is_valid, issues = self.gen.validate_platform_content("x", post, product, self.disclosure)
            self.assertFalse(is_valid, f"Expected '{claim}' to be rejected, but it passed!")

    # ==================== 6. NOVEL / UNSEEN CATEGORY ====================
    def test_novel_category_valid_paraphrases(self):
        """Novel Category (Astronomical Telescope): Valid summary of aperture, focal length, and tripod."""
        product = Product(
            title="Astronomical Telescope 70mm Aperture with 400mm Focal Length",
            features=[
                "70mm optical aperture",
                "400mm focal length with aluminum tripod",
                "Includes 2 eyepieces (10mm and 25mm)"
            ],
            affiliate_url="https://link.amazon/telescope",
            asin="B0SCOPE01"
        )
        valid_post = {
            "text": f"Astronomical telescope featuring a 70mm aperture and 400mm focal length with an aluminum tripod.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = self.gen.validate_platform_content("x", valid_post, product, self.disclosure)
        self.assertTrue(is_valid, f"Expected valid, got: {issues}")

    def test_novel_category_exact_unit_mismatch(self):
        """Novel Category: Rejects 70 inches aperture when source specifies 70 mm."""
        product = Product(
            title="Astronomical Telescope 70mm Aperture with 400mm Focal Length",
            features=["70mm optical aperture", "400mm focal length"],
            affiliate_url="https://link.amazon/telescope",
            asin="B0SCOPE01"
        )
        bad_post = {"text": f"Astronomical telescope with a 70 inches optical aperture.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_valid, issues = self.gen.validate_platform_content("x", bad_post, product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("unit mismatch" in iss.lower() or "inches" in iss.lower() for iss in issues))

    def test_novel_category_entity_modifier_misbinding(self):
        """Novel Category: Rejects optical glass tripod and aluminum eyepieces when swapped."""
        product = Product(
            title="Astronomical Telescope with Aluminum Tripod and Optical Glass Eyepieces",
            features=["Aluminum tripod", "Optical glass eyepieces"],
            affiliate_url="https://link.amazon/telescope",
            asin="B0SCOPE01"
        )
        bad_post = {"text": f"Telescope featuring an optical glass tripod and aluminum eyepieces.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_valid, issues = self.gen.validate_platform_content("x", bad_post, product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("modifier mismatch" in iss.lower() or "glass" in iss.lower() or "aluminum" in iss.lower() for iss in issues))

    def test_novel_category_rejects_unsupported_outcomes_and_causal_claims(self):
        """Novel Category: Rejects crystal clear planetary viewing, galactic exploration, brighter constellations."""
        product = Product(
            title="Astronomical Telescope 70mm Aperture with 400mm Focal Length",
            features=[
                "70mm optical aperture",
                "400mm focal length with aluminum tripod"
            ],
            affiliate_url="https://link.amazon/telescope",
            asin="B0SCOPE01"
        )
        invalid_claims = [
            "The telescope delivers crystal clear planetary viewing.",
            "Enables galactic exploration from your backyard.",
            "Multicoated lenses produce brighter constellations.",
            "Provides sharper celestial focus."
        ]
        for claim in invalid_claims:
            post = {"text": f"{claim}\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
            is_valid, issues = self.gen.validate_platform_content("x", post, product, self.disclosure)
            self.assertFalse(is_valid, f"Expected '{claim}' to be rejected, but it passed!")

    def test_novel_hallucination_without_blacklist(self):
        """
        Genuinely novel hallucination with completely unseen vocabulary that never appears in any blacklist or test.
        Must fail strictly because there is no canonical proposition mapping.
        """
        product = Product(
            title="Astronomical Telescope 70mm Aperture with 400mm Focal Length",
            features=[
                "70mm optical aperture",
                "400mm focal length with aluminum tripod",
                "Includes 2 eyepieces"
            ],
            affiliate_url="https://link.amazon/telescope",
            asin="B0SCOPE01"
        )
        novel_hallucinated_post = {
            "text": f"Astronomical telescope featuring quantum optical refractive alignment for celestial observation.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = self.gen.validate_platform_content("x", novel_hallucinated_post, product, self.disclosure)
        self.assertFalse(is_valid, "Novel hallucination MUST be rejected via absence of canonical proposition mapping!")
        self.assertTrue(any("untraceable" in iss.lower() or "quantum" in iss.lower() or "celestial" in iss.lower() for iss in issues))

    # ==================== 7. REGRESSION TEST: ELIMINATE RAW SOURCE TEXT AS EVIDENCE ====================
    def test_regression_raw_source_word_cannot_ground_unsupported_proposition(self):
        """
        Regression Test for Requirement 1:
        A raw source listing contains a vocabulary word, but NO canonical fact establishes
        the generated proposition. The generated claim using that vocabulary MUST still be rejected.
        Raw source text in the listing does NOT grant grounding immunity!
        """
        product = Product(
            title="Winter Jacket with 20D Ripstop Nylon Shell and YKK Zipper",
            features=[
                "20D ripstop nylon shell",
                "Includes YKK zipper",
                "Inspired by mountain blizzards and high-altitude polar expeditions"
            ],
            affiliate_url="https://link.amazon/winterJacket",
            asin="B0REGRESS01"
        )
        # Even though "blizzards" and "expeditions" occur in the raw listing marketing fluff,
        # the proposition "protects you during severe mountain blizzards" is an unsupported outcome claim.
        bad_post = {
            "text": f"Winter jacket with 20D ripstop nylon shell that protects you during severe mountain blizzards.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = self.gen.validate_platform_content("x", bad_post, product, self.disclosure)
        self.assertFalse(is_valid, "Raw listing vocabulary MUST NOT bypass proposition-level validation!")
        self.assertTrue(len(issues) > 0)

    # ==================== 8. ADVERSARIAL ENTITY-AWARE MODIFIER BINDING ====================
    def test_entity_aware_modifier_binding_adversarial(self):
        """
        Adversarial Modifier-Entity Binding:
        Source: durable steel frame, fragile glass cup.
        Valid: durable steel frame, fragile glass cup.
        Reject: durable glass cup.
        Reject: fragile steel frame.
        """
        product = Product(
            title="Desk Organizer with Durable Steel Frame and Fragile Glass Cup",
            features=[
                "Features a durable steel frame",
                "Includes a fragile glass cup for stationery"
            ],
            affiliate_url="https://link.amazon/organizer",
            asin="B0MOD0001"
        )
        valid_post = {
            "text": f"Desk organizer with a durable steel frame and a fragile glass cup.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = self.gen.validate_platform_content("x", valid_post, product, self.disclosure)
        self.assertTrue(is_valid, f"Expected valid for grounded modifier bindings, got: {issues}")

        bad_post_1 = {
            "text": f"Desk organizer with a durable glass cup and steel frame.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid_1, issues_1 = self.gen.validate_platform_content("x", bad_post_1, product, self.disclosure)
        self.assertFalse(is_valid_1, "'durable glass cup' must be rejected!")
        self.assertTrue(any("modifier mismatch" in iss.lower() or "durable" in iss.lower() for iss in issues_1))

        bad_post_2 = {
            "text": f"Desk organizer with a fragile steel frame and glass cup.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid_2, issues_2 = self.gen.validate_platform_content("x", bad_post_2, product, self.disclosure)
        self.assertFalse(is_valid_2, "'fragile steel frame' must be rejected!")
        self.assertTrue(any("modifier mismatch" in iss.lower() or "fragile" in iss.lower() for iss in issues_2))

    # ==================== 9. GENERAL VALIDATION & COMPLIANCE ====================
    def test_insufficient_source_facts_fails_closed(self):
        """Empty product title returns INSUFFICIENT_SOURCE_FACTS."""
        empty_product = Product(
            title="",
            features=[],
            affiliate_url="https://link.amazon/empty",
            asin="B0EMPTY001"
        )
        with self.assertRaises(RuntimeError) as ctx:
            self.gen.generate_for_platform("pinterest", empty_product, self.disclosure)
        self.assertIn("INSUFFICIENT_SOURCE_FACTS", str(ctx.exception))

    def test_affiliate_url_strictly_preserved(self):
        """Affiliate URL is strictly preserved exactly as supplied."""
        affiliate_url = "https://link.amazon/customAffiliateToken123"
        product = Product(
            title="Cordless Drill 20V Max with 2 Batteries and Charger",
            features=["20V motor", "Includes 2 lithium-ion batteries and charger case"],
            affiliate_url=affiliate_url,
            asin="B0DRILL001"
        )
        post_content = {
            "text": f"Cordless drill with 20V motor and 2 batteries.\n\nLink: {affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = self.gen.validate_platform_content("x", post_content, product, self.disclosure)
        self.assertTrue(is_valid)

        wrong_url_post = {
            "text": f"Cordless drill with 20V motor and 2 batteries.\n\nLink: https://amazon.com/dp/B0DRILL001\n\n{self.disclosure}"
        }
        is_valid_wrong, issues_wrong = self.gen.validate_platform_content("x", wrong_url_post, product, self.disclosure)
        self.assertFalse(is_valid_wrong)
        self.assertTrue(any("affiliate url" in iss.lower() for iss in issues_wrong))

    def test_pinterest_title_and_description_grounding(self):
        """Pinterest output enforces title <= 100 chars, description <= 800 chars, and factual claims."""
        product = Product(
            title="Wireless Noise Cancelling Over-Ear Headphones with 40-Hour Battery",
            features=[
                "Active noise cancellation with dual microphones",
                "40-hour battery life with fast USB-C charging",
                "40 mm dynamic drivers with bluetooth 5.3"
            ],
            affiliate_url="https://link.amazon/headphones",
            asin="B0HEADPH01"
        )
        valid_pin = {
            "title": "Wireless Noise Cancelling Headphones - 40-Hour Battery",
            "description": (
                f"Over-ear headphones featuring active noise cancellation and 40 mm dynamic drivers. "
                f"40-hour battery life with bluetooth 5.3 connectivity. {product.affiliate_url}\n\n{self.disclosure}"
            )
        }
        is_valid, issues = self.gen.validate_platform_content("pinterest", valid_pin, product, self.disclosure)
        self.assertTrue(is_valid, f"Expected valid, got issues: {issues}")

    # ==================== 10. UNIVERSAL PROPOSITION GROUNDING CONTRACT ====================
    def test_feature_only_declarative_statement_passes(self):
        """1. Feature-only declarative statement strictly grounded in canonical facts → PASS."""
        product = Product(
            title="Blender with 1200W Motor and 2L Glass Jar",
            features=["1200W motor", "2L glass jar", "Includes stainless steel 6-point blade"],
            affiliate_url="https://link.amazon/blender",
            asin="B0BLEND01"
        )
        post = {"text": f"Blender with 1200W motor and 2L glass jar. Includes stainless steel 6-point blade.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_valid, issues = self.gen.validate_platform_content("x", post, product, self.disclosure)
        self.assertTrue(is_valid, f"Expected clean declarative statement to pass, got: {issues}")

    def test_feature_plus_invented_benefit_fails(self):
        """2. Feature + invented benefit (e.g. 'for easy cleaning', 'for effortless maintenance') → FAIL."""
        product = Product(
            title="Blender with 1200W Motor and 2L Glass Jar",
            features=["1200W motor", "2L glass jar", "Removable blade assembly"],
            affiliate_url="https://link.amazon/blender",
            asin="B0BLEND01"
        )
        post = {"text": f"Blender with removable blade assembly for easy cleaning.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_valid, issues = self.gen.validate_platform_content("x", post, product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("benefit" in iss.lower() or "cleaning" in iss.lower() for iss in issues))

    def test_feature_plus_invented_outcome_fails(self):
        """3. Feature + invented outcome (e.g. 'for rich taste and dense cream') → FAIL."""
        product = Product(
            title="Espresso Machine with 15 bar pressure",
            features=["15 bar extraction pressure", "2.3 litre water tank"],
            affiliate_url="https://link.amazon/espresso",
            asin="B0ESPRESS1"
        )
        post = {"text": f"Espresso machine with 15 bar pressure for rich taste and dense cream.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_valid, issues = self.gen.validate_platform_content("x", post, product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("outcome" in iss.lower() or "rich" in iss.lower() or "cream" in iss.lower() for iss in issues))

    def test_feature_plus_invented_causal_relationship_fails(self):
        """4. Feature + invented causal relationship (e.g. 'enables interference-free function') → FAIL."""
        product = Product(
            title="Electric Coffee Grinder with Stainless Steel Conical Burr",
            features=["Stainless steel conical burr", "200W motor"],
            affiliate_url="https://link.amazon/grinder",
            asin="B0GRIND01"
        )
        post = {"text": f"Coffee grinder with conical burr enables interference-free function.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_valid, issues = self.gen.validate_platform_content("x", post, product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("capability" in iss.lower() or "outcome" in iss.lower() or "interference-free" in iss.lower() for iss in issues))

    def test_unsupported_comparative_fails(self):
        """5. Unsupported comparative (e.g. 'produces smoother results than other blenders') → FAIL."""
        product = Product(
            title="Blender with 1200W Motor and 2L Glass Jar",
            features=["1200W motor", "2L glass jar"],
            affiliate_url="https://link.amazon/blender",
            asin="B0BLEND01"
        )
        post = {"text": f"Blender with 1200W motor delivers smoother results.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_valid, issues = self.gen.validate_platform_content("x", post, product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("comparative" in iss.lower() or "outcome" in iss.lower() for iss in issues))

    def test_unsupported_modifier_entity_combination_fails(self):
        """6. Unsupported modifier/entity combination (e.g. assigning 'glass' to motor or 'stainless steel' to jar) → FAIL."""
        product = Product(
            title="Blender with Stainless Steel Motor Housing and Glass Jar",
            features=["Durable stainless steel motor housing", "Fragile glass jar"],
            affiliate_url="https://link.amazon/blender",
            asin="B0BLEND01"
        )
        post = {"text": f"Blender featuring a glass motor housing and stainless steel jar.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_valid, issues = self.gen.validate_platform_content("x", post, product, self.disclosure)
        self.assertFalse(is_valid)

    def test_exact_unit_mismatch_fails(self):
        """7. Exact unit mismatch (e.g. '1200 kW' instead of '1200W') → FAIL."""
        product = Product(
            title="Blender with 1200W Motor and 2L Glass Jar",
            features=["1200W motor", "2L glass jar"],
            affiliate_url="https://link.amazon/blender",
            asin="B0BLEND01"
        )
        post = {"text": f"Blender with 1200 kw motor.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_valid, issues = self.gen.validate_platform_content("x", post, product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("unit mismatch" in iss.lower() or "kw" in iss.lower() for iss in issues))

    def test_novel_unsupported_proposition_with_novel_vocabulary_fails(self):
        """8. Novel unsupported proposition with completely unseen vocabulary → FAIL."""
        product = Product(
            title="Blender with 1200W Motor and 2L Glass Jar",
            features=["1200W motor", "2L glass jar"],
            affiliate_url="https://link.amazon/blender",
            asin="B0BLEND01"
        )
        post = {"text": f"Blender with piezoelectric resonance technology.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_valid, issues = self.gen.validate_platform_content("x", post, product, self.disclosure)
        self.assertFalse(is_valid)
        self.assertTrue(any("untraceable" in iss.lower() or "piezoelectric" in iss.lower() for iss in issues))

    def test_legitimate_paraphrase_passes(self):
        """9. Legitimate natural language paraphrase of source facts → PASS."""
        product = Product(
            title="Espresso Machine with 15 bar pressure, integrated grinder, and milk frother",
            features=[
                "15 bar extraction pressure",
                "Includes coffee grinder",
                "Equipped with milk frother steam wand",
                "Removable water tank and drip tray",
                "Prepare espresso, lattes and cappuccino at home"
            ],
            affiliate_url="https://link.amazon/applianceA",
            asin="B0APPLIAN1"
        )
        post = {"text": f"Espresso machine equipped with 15 bar pressure, coffee grinder, and milk frother. Removable water tank.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_valid, issues = self.gen.validate_platform_content("x", post, product, self.disclosure)
        self.assertTrue(is_valid, f"Expected legitimate paraphrase to pass, got: {issues}")

    def test_threads_unsupported_marketing_sentence_fails(self):
        """10. Threads-style unsupported marketing sentence with rich taste / dense cream / perfect for / interference-free → FAIL."""
        product = Product(
            title="Espresso Machine with 15 bar pressure, grinder, and milk frother",
            features=[
                "15 bar pressure",
                "Includes coffee grinder",
                "Removable water tank"
            ],
            affiliate_url="https://link.amazon/B0bXBNrkK",
            asin="B0GNGTN7TV"
        )
        bad_threads_text = (
            "This espresso machine offers 15 bar pressure for rich taste and dense cream, "
            "perfect for espresso, lattes, and cappuccino. The enhanced grinding system features "
            "a 20% wider polished channel and a reinforced spiral drill for interference-free function."
        )
        post = {"text": f"{bad_threads_text}\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
        is_valid, issues = self.gen.validate_platform_content("threads", post, product, self.disclosure)
        self.assertFalse(is_valid, "Threads-style ungrounded marketing claim MUST be rejected by ClaimValidator!")
        self.assertTrue(any("outcome" in iss.lower() or "taste" in iss.lower() or "cream" in iss.lower() or "perfect" in iss.lower() for iss in issues))

    # ==================== 11. UNIVERSAL REGRESSION TESTS: WORD OVERLAP IMMUNITY ====================
    def test_untraceable_verb_concept_fails_closed_despite_word_overlap(self):
        """
        Universal Regression Test: Novel, previously unseen verb concept 'operates'
        cannot pass merely because other words in the sentence (15 bar, extraction, pressure, grinder)
        overlap with canonical source facts.
        """
        product = Product(
            title="Espresso Machine with 15 bar pressure and integrated grinder",
            features=["15 bar extraction pressure", "Includes coffee grinder", "Removable water tank"],
            affiliate_url="https://link.amazon/applianceX",
            asin="B0OVERLAP1"
        )
        bad_post = {
            "text": f"This machine operates at 15 bar extraction pressure with an integrated coffee grinder.\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"
        }
        is_valid, issues = self.gen.validate_platform_content("threads", bad_post, product, self.disclosure)
        self.assertFalse(is_valid, "Sentence with untraceable concept 'operates' MUST fail closed!")
        self.assertTrue(any("untraceable" in iss.lower() or "operates" in iss.lower() for iss in issues))

    def test_unsupported_psychological_and_safety_claims_fail_closed(self):
        """
        Universal Regression Test: 'peace of mind', 'safety', 'ensures', 'efficiently'
        cannot pass merely because 'stainless steel boiler' or '15 bar' match canonical facts.
        """
        product = Product(
            title="Espresso Machine with 15 bar pressure and stainless steel boiler",
            features=["15 bar extraction pressure", "Stainless steel boiler"],
            affiliate_url="https://link.amazon/applianceY",
            asin="B0OVERLAP2"
        )
        test_cases = [
            "Stainless steel boiler ensures peace of mind.",
            "The 15 bar extraction pressure ensures maximum safety.",
            "The machine operates efficiently for your peace of mind."
        ]
        for bad_text in test_cases:
            post = {"text": f"{bad_text}\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
            is_valid, issues = self.gen.validate_platform_content("facebook", post, product, self.disclosure)
            self.assertFalse(is_valid, f"Expected '{bad_text}' to fail closed, but it passed!")
            self.assertTrue(len(issues) > 0)

    def test_unsupported_sensory_and_benefit_claims_fail_closed(self):
        """
        Universal Regression Test: 'produces silky microfoam', 'brave taste'
        cannot pass merely because 'milk frother steam wand' or '15 bar' are in source facts.
        """
        product = Product(
            title="Espresso Machine with 15 bar pressure and milk frother steam wand",
            features=["15 bar extraction pressure", "Equipped with milk frother steam wand"],
            affiliate_url="https://link.amazon/applianceZ",
            asin="B0OVERLAP3"
        )
        test_cases = [
            "The milk frother steam wand produces silky microfoam.",
            "15 bar extraction pressure delivers brave taste."
        ]
        for bad_text in test_cases:
            post = {"text": f"{bad_text}\n\nLink: {product.affiliate_url}\n\n{self.disclosure}"}
            is_valid, issues = self.gen.validate_platform_content("x", post, product, self.disclosure)
            self.assertFalse(is_valid, f"Expected '{bad_text}' to fail closed, but it passed!")
            self.assertTrue(len(issues) > 0)

    def test_multi_category_novel_unsupported_vocabulary_fails_closed(self):
        """
        Universal Regression Test: Across multiple categories (Electronics, Camping, Furniture),
        unsupported propositions mixed with valid product features must fail closed.
        """
        # 1. Electronics
        elec_product = Product(
            title="Wireless Headphones with 40-Hour Battery",
            features=["40-hour battery life", "Active noise cancellation"],
            affiliate_url="https://link.amazon/elecReg",
            asin="B0ELEC001"
        )
        bad_elec_post = {"text": f"Headphones operate with quantum acoustic clarity.\n\nLink: {elec_product.affiliate_url}\n\n{self.disclosure}"}
        is_valid, issues = self.gen.validate_platform_content("x", bad_elec_post, elec_product, self.disclosure)
        self.assertFalse(is_valid)

        # 2. Camping
        camp_product = Product(
            title="Camping Tent with Waterproof Rainfly",
            features=["3000mm waterproof rainfly", "Aluminum poles"],
            affiliate_url="https://link.amazon/campReg",
            asin="B0CAMP001"
        )
        bad_camp_post = {"text": f"Waterproof rainfly ensures total tranquility and peace of mind during storms.\n\nLink: {camp_product.affiliate_url}\n\n{self.disclosure}"}
        is_valid, issues = self.gen.validate_platform_content("x", bad_camp_post, camp_product, self.disclosure)
        self.assertFalse(is_valid)

        # 3. Furniture
        furn_product = Product(
            title="Solid Wood Desk with 2 Drawers",
            features=["Solid wood frame", "Includes 2 drawers"],
            affiliate_url="https://link.amazon/furnReg",
            asin="B0FURN001"
        )
        bad_furn_post = {"text": f"Solid wood desk operates as an ergonomic productivity workstation.\n\nLink: {furn_product.affiliate_url}\n\n{self.disclosure}"}
        is_valid, issues = self.gen.validate_platform_content("x", bad_furn_post, furn_product, self.disclosure)
        self.assertFalse(is_valid)


if __name__ == "__main__":
    unittest.main()

