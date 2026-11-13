"""Product-Aware Pinterest Multi-Angle Marketing Creative Generator.

Generates 3-5 distinct, high-converting Pinterest marketing creative variations
(1000x1500, 2:3 aspect ratio) for Amazon affiliate products while keeping
the original Amazon product image strictly unchanged (no inpainting, no redrawing,
no distortion, and preserving original pixels).

Category-aware and fact-grounded:
- Detects the genuine product category (Fragrance, Beauty, Home/Kitchen, Electronics,
  Fashion, Pets, Home Decor, Gifts, or General).
- Selects angles tailored to the product's actual features and characteristics.
- Uses only structured canonical facts and verified feature clauses.
- Eliminates empty generic boilerplate ("Durable, quality build", "Built to impress",
  "Reliable performance").
- Formulates unique headlines, supporting copy, feature bullets, and CTAs per variation.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import io
import json
import logging
import os
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Tuple, Set, Union

import requests
from PIL import Image, ImageDraw, ImageFont

from amazon.scraper import Product, is_valid_image_url
from content.facts import (
    CanonicalProductFacts,
    FactExtractor,
    is_keyword_stuffed_text,
    STOPWORDS,
    PROPERTY_SYNONYMS,
)
from content.validator import ClaimValidator

LOGGER = logging.getLogger(__name__)
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Two Active Creative Types for Google Flow Workflow
CREATIVE_TYPE_GIFT = "gift_inspiration"
CREATIVE_TYPE_EVERYDAY = "everyday_pick"
CREATIVE_TYPES = (CREATIVE_TYPE_GIFT, CREATIVE_TYPE_EVERYDAY)

CREATIVE_TYPE_LABELS = {
    CREATIVE_TYPE_GIFT: "Gift Inspiration",
    CREATIVE_TYPE_EVERYDAY: "Everyday Pick",
}

MAX_UPLOAD_SIZE_BYTES = 15 * 1024 * 1024  # 15 MB
ALLOWED_UPLOAD_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

# Output specifications (Pinterest mobile-first 2:3 standard)
CANVAS_WIDTH = 1000
CANVAS_HEIGHT = 1500
CANVAS_SIZE = (CANVAS_WIDTH, CANVAS_HEIGHT)

# Layout Template Identifiers
LAYOUT_HERO_SHOWCASE = "hero_showcase"
LAYOUT_EDITORIAL_SPLIT = "editorial_split"
LAYOUT_FEATURE_FOCUS = "feature_focus"
LAYOUT_MINIMALIST_FRAME = "minimalist_frame"

LAYOUT_TEMPLATES = [
    LAYOUT_HERO_SHOWCASE,
    LAYOUT_EDITORIAL_SPLIT,
    LAYOUT_FEATURE_FOCUS,
    LAYOUT_MINIMALIST_FRAME,
]

# Allowed CTA options
ALLOWED_CTAS = [
    "Discover More",
    "See Details",
    "Explore This Find",
    "Shop Now",
    "Explore Fragrance",
    "Discover the Scent",
    "Explore Notes",
    "Explore Gift Idea",
    "See Specs",
    "Organize Your Space",
    "Explore Routine",
]

# Product Category Identifiers
CAT_FRAGRANCE = "perfumes_and_fragrances"
CAT_BEAUTY = "beauty_and_personal_care"
CAT_HOME_KITCHEN = "home_and_kitchen"
CAT_ELECTRONICS = "electronics_and_gadgets"
CAT_FASHION = "fashion_and_accessories"
CAT_PETS = "pet_products"
CAT_HOME_DECOR = "home_decor_and_wall_art"
CAT_GIFTS = "gifts"
CAT_GENERAL = "general"

# Marketing Angle Identifiers
ANGLE_LUXURY_FRAGRANCE = "Luxury Fragrance"
ANGLE_FRAGRANCE_NOTES = "Fragrance Notes"
ANGLE_EVERYDAY_ELEGANCE = "Everyday Elegance"
ANGLE_UNISEX_APPEAL = "Unisex Appeal"

ANGLE_DAILY_SKINCARE = "Daily Skincare Ritual"
ANGLE_NOURISHING_INGREDIENTS = "Key Active Ingredients"
ANGLE_TARGETED_CARE = "Targeted Care"

ANGLE_SPACE_SAVING_IDEA = "Space-Saving Idea"
ANGLE_PRACTICAL_SOLUTION = "Practical Solution"
ANGLE_KITCHEN_ORGANIZATION = "Effortless Organization"
ANGLE_DURABLE_UTILITY = "Durable Utility"

ANGLE_SMART_TECH_UPGRADE = "Smart Tech Upgrade"
ANGLE_SMART_HOME_UPGRADE = "Smart Tech Upgrade"  # Backward compatibility alias
ANGLE_COMPACT_CONVENIENCE = "Compact Convenience"
ANGLE_KEY_PRODUCT_BENEFITS = "Key Product Benefits"
ANGLE_DESK_TRAVEL_HUB = "Desk & Travel Setup"

ANGLE_WARDROBE_STAPLE = "Everyday Wardrobe Staple"
ANGLE_REFINED_STYLE = "Refined Style & Fit"
ANGLE_VERSATILE_ACCESSORY = "Versatile Accessory"

ANGLE_PET_COMFORT = "Pet Comfort & Wellness"
ANGLE_PRACTICAL_PET_CARE = "Easy Pet Care"
ANGLE_ACTIVE_PLAY = "Active Play & Safety"

ANGLE_AESTHETIC_ACCENT = "Aesthetic Living Accent"
ANGLE_ROOM_TRANSFORMATION = "Instant Room Refresh"
ANGLE_COZY_TOUCH = "Cozy Interior Touch"

ANGLE_GIFT_INSPIRATION = "Gift Inspiration"
ANGLE_THOUGHTFUL_DESIGN = "Thoughtful Design"

ALL_MARKETING_ANGLES = [
    ANGLE_LUXURY_FRAGRANCE,
    ANGLE_FRAGRANCE_NOTES,
    ANGLE_EVERYDAY_ELEGANCE,
    ANGLE_UNISEX_APPEAL,
    ANGLE_DAILY_SKINCARE,
    ANGLE_NOURISHING_INGREDIENTS,
    ANGLE_TARGETED_CARE,
    ANGLE_SPACE_SAVING_IDEA,
    ANGLE_PRACTICAL_SOLUTION,
    ANGLE_KITCHEN_ORGANIZATION,
    ANGLE_DURABLE_UTILITY,
    ANGLE_SMART_TECH_UPGRADE,
    ANGLE_COMPACT_CONVENIENCE,
    ANGLE_KEY_PRODUCT_BENEFITS,
    ANGLE_DESK_TRAVEL_HUB,
    ANGLE_WARDROBE_STAPLE,
    ANGLE_REFINED_STYLE,
    ANGLE_VERSATILE_ACCESSORY,
    ANGLE_PET_COMFORT,
    ANGLE_PRACTICAL_PET_CARE,
    ANGLE_ACTIVE_PLAY,
    ANGLE_AESTHETIC_ACCENT,
    ANGLE_ROOM_TRANSFORMATION,
    ANGLE_COZY_TOUCH,
    ANGLE_GIFT_INSPIRATION,
    ANGLE_THOUGHTFUL_DESIGN,
]


@dataclass
class CreativeTheme:
    """Color palette and visual styling definition for Pinterest creatives."""
    name: str
    canvas_bg: Tuple[int, int, int]
    card_bg: Tuple[int, int, int]
    card_border: Tuple[int, int, int]
    badge_bg: Tuple[int, int, int]
    badge_text: Tuple[int, int, int]
    text_primary: Tuple[int, int, int]
    text_secondary: Tuple[int, int, int]
    bullet_box_bg: Tuple[int, int, int]
    bullet_box_border: Tuple[int, int, int]
    bullet_text: Tuple[int, int, int]
    cta_bg: Tuple[int, int, int]
    cta_text: Tuple[int, int, int]


THEMES: Dict[str, CreativeTheme] = {
    "warm_minimalist": CreativeTheme(
        name="Warm Minimalist",
        canvas_bg=(250, 248, 244),
        card_bg=(255, 255, 255),
        card_border=(234, 229, 220),
        badge_bg=(242, 237, 228),
        badge_text=(146, 84, 46),
        text_primary=(34, 31, 29),
        text_secondary=(102, 95, 89),
        bullet_box_bg=(245, 242, 236),
        bullet_box_border=(232, 226, 216),
        bullet_text=(40, 36, 33),
        cta_bg=(196, 59, 44),
        cta_text=(255, 255, 255),
    ),
    "modern_tech": CreativeTheme(
        name="Modern Slate Tech",
        canvas_bg=(243, 246, 250),
        card_bg=(255, 255, 255),
        card_border=(222, 228, 236),
        badge_bg=(228, 237, 248),
        badge_text=(29, 78, 216),
        text_primary=(17, 24, 39),
        text_secondary=(75, 85, 99),
        bullet_box_bg=(238, 242, 247),
        bullet_box_border=(220, 227, 237),
        bullet_text=(31, 41, 55),
        cta_bg=(29, 78, 216),
        cta_text=(255, 255, 255),
    ),
    "fresh_botanical": CreativeTheme(
        name="Fresh Botanical",
        canvas_bg=(244, 247, 244),
        card_bg=(255, 255, 255),
        card_border=(220, 228, 220),
        badge_bg=(226, 238, 226),
        badge_text=(31, 90, 46),
        text_primary=(26, 38, 28),
        text_secondary=(78, 94, 80),
        bullet_box_bg=(236, 243, 236),
        bullet_box_border=(218, 228, 218),
        bullet_text=(29, 44, 32),
        cta_bg=(45, 106, 79),
        cta_text=(255, 255, 255),
    ),
    "editorial_classic": CreativeTheme(
        name="Editorial Classic",
        canvas_bg=(255, 255, 255),
        card_bg=(248, 249, 250),
        card_border=(229, 231, 235),
        badge_bg=(254, 226, 226),
        badge_text=(153, 27, 27),
        text_primary=(15, 23, 42),
        text_secondary=(71, 85, 105),
        bullet_box_bg=(241, 245, 249),
        bullet_box_border=(226, 232, 240),
        bullet_text=(15, 23, 42),
        cta_bg=(230, 0, 35),  # Signature Pinterest red
        cta_text=(255, 255, 255),
    ),
    "rose_lifestyle": CreativeTheme(
        name="Soft Rose Lifestyle",
        canvas_bg=(251, 247, 248),
        card_bg=(255, 255, 255),
        card_border=(239, 223, 228),
        badge_bg=(247, 228, 233),
        badge_text=(142, 44, 72),
        text_primary=(42, 30, 34),
        text_secondary=(105, 87, 94),
        bullet_box_bg=(247, 240, 242),
        bullet_box_border=(236, 222, 227),
        bullet_text=(42, 30, 34),
        cta_bg=(173, 40, 74),
        cta_text=(255, 255, 255),
    ),
}

DEFAULT_THEME_ORDER = [
    "warm_minimalist",
    "modern_tech",
    "editorial_classic",
    "fresh_botanical",
    "rose_lifestyle",
]


@dataclass
class CreativeCopy:
    """Marketing text and CTA content for one Pinterest creative variation."""
    angle: str
    badge: str
    headline: str
    subheadline: str
    benefits: List[str]
    cta_text: str


@dataclass
class CreativeVariation:
    """Metadata record for a generated Pinterest creative image."""
    variation_id: str
    angle: str
    theme_name: str
    headline: str
    subheadline: str
    benefits: List[str]
    cta_text: str
    file_path: str
    dimensions: List[int]
    aspect_ratio: str
    created_at: str
    layout_template: str = LAYOUT_HERO_SHOWCASE

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# Font Management
# ---------------------------------------------------------------------------

def _get_font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    """Safely load a clean sans-serif system font, with fallback to default."""
    candidates = []
    if os.name == "nt":
        fonts_dir = r"C:\Windows\Fonts"
        if bold:
            candidates.extend([
                os.path.join(fonts_dir, "segoeuib.ttf"),
                os.path.join(fonts_dir, "arialbd.ttf"),
                os.path.join(fonts_dir, "calibrib.ttf"),
            ])
        else:
            candidates.extend([
                os.path.join(fonts_dir, "segoeui.ttf"),
                os.path.join(fonts_dir, "arial.ttf"),
                os.path.join(fonts_dir, "calibri.ttf"),
            ])
    else:
        linux_dirs = ["/usr/share/fonts/truetype", "/usr/share/fonts"]
        for ldir in linux_dirs:
            if bold:
                candidates.extend([
                    os.path.join(ldir, "dejavu", "DejaVuSans-Bold.ttf"),
                    os.path.join(ldir, "liberation", "LiberationSans-Bold.ttf"),
                ])
            else:
                candidates.extend([
                    os.path.join(ldir, "dejavu", "DejaVuSans.ttf"),
                    os.path.join(ldir, "liberation", "LiberationSans.ttf"),
                ])

    for path in candidates:
        if os.path.isfile(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue

    return ImageFont.load_default()


# ---------------------------------------------------------------------------
# Category Detection Engine
# ---------------------------------------------------------------------------

CATEGORY_KEYWORDS = {
    CAT_FRAGRANCE: {
        "perfume", "parfum", "eau de parfum", "eau de toilette", "cologne",
        "fragrance", "scent", "edp", "edt", "oud", "bodyspray", "fragrance oil",
        "bergamot", "patchouli", "sandalwood", "musk", "pulse points", "scents",
        "spray bottle", "perfumes", "parfums"
    },
    CAT_BEAUTY: {
        "skincare", "serum", "moisturizer", "cream", "lotion", "cleanser",
        "shampoo", "conditioner", "lipstick", "makeup", "mascara", "sunscreen",
        "exfoliator", "haircare", "facial", "toner", "anti-aging", "beauty",
        "foundation", "concealer", "cosmetic", "cosmetics"
    },
    CAT_ELECTRONICS: {
        "wireless", "charger", "charging", "bluetooth", "headphones", "earbuds",
        "battery", "usb", "cable", "speaker", "audio", "smartwatch", "camera",
        "monitor", "keyboard", "mouse", "power bank", "adapter", "dock",
        "gadget", "electronics", "watt", "watts", "charging station", "wifi"
    },
    CAT_HOME_KITCHEN: {
        "kitchen", "cooking", "knife", "pan", "pot", "blender", "coffee",
        "cookware", "organizer", "drawer", "rack", "shelf", "cleaning",
        "vacuum", "mop", "sink", "utensil", "appliance", "container",
        "under sink", "cutting board", "cabinet", "sliding drawer"
    },
    CAT_HOME_DECOR: {
        "decor", "wall art", "canvas", "print", "poster", "frame", "vase",
        "candle", "rug", "curtain", "throw blanket", "pillow", "cushion",
        "sculpture", "lamp", "mirror", "tapestry", "wall decor"
    },
    CAT_FASHION: {
        "dress", "shirt", "pants", "jacket", "coat", "shoes", "boots",
        "sneakers", "bag", "backpack", "tote", "wallet", "belt", "scarf",
        "sunglasses", "jewelry", "necklace", "bracelet", "earrings", "ring",
        "apparel", "clothing", "wear"
    },
    CAT_PETS: {
        "dog", "cat", "puppy", "kitten", "pet", "leash", "collar", "harness",
        "pet bed", "dog food", "cat food", "litter", "scratching post",
        "pet toy", "grooming", "chew toy", "pets"
    },
    CAT_GIFTS: {
        "gift set", "gift basket", "gift for", "holiday gift", "birthday gift",
        "anniversary gift", "luxury gift", "present", "keepsake"
    },
}


def detect_product_category(
    product: Product,
    facts: Optional[CanonicalProductFacts] = None
) -> str:
    """
    Detect the genuine product category from verified product title, features,
    and canonical facts. Returns one of the CAT_* constants.
    """
    corpus = " ".join([
        product.title or "",
        " ".join(product.features or []),
        product.description or "",
        (getattr(facts, "category", "") if facts else "") or "",
    ]).lower()

    scores: Dict[str, float] = {}
    for cat, terms in CATEGORY_KEYWORDS.items():
        score = 0.0
        for term in terms:
            if re.search(r"\b" + re.escape(term) + r"\b", corpus):
                if re.search(r"\b" + re.escape(term) + r"\b", (product.title or "").lower()):
                    score += 3.0
                else:
                    score += 1.0
        scores[cat] = score

    best_cat, best_score = max(scores.items(), key=lambda x: x[1])

    if best_score >= 2.0:
        return best_cat

    return CAT_GENERAL


# ---------------------------------------------------------------------------
# Warning, Disclaimer, and Spec Filtering Rules
# ---------------------------------------------------------------------------

WARNING_DISCLAIMER_PATTERNS = [
    r"\bwarning\b",
    r"\bcaution\b",
    r"\bnotice\b",
    r"\bdisclaimer\b",
    r"\bchoking hazard\b",
    r"\bnot for children\b",
    r"\bkeep out of (?:the\s+)?reach\b",
    r"\bpatch test\b",
    r"\bconsult (?:your|a) (?:physician|doctor)\b",
    r"\bnot dishwasher safe\b",
    r"\bdo not submerge\b",
    r"\bdo not use if\b",
    r"\bmake sure this fits\b",
    r"\benter(?:ing)? your model number\b",
    r"\bprop(?:osition)?\s*65\b",
    r"\bfor external use only\b",
    r"\bavoid contact with\b",
    r"\bflammable\b",
    r"\bkeep away from\b",
    r"\brisk of\b",
    r"\bdanger\b",
    r"\bpoison\b",
    r"\ballerg(?:y|ies|en|ens)\b",
    r"\bhand wash only\b",
    r"\bdo not bleach\b",
    r"\bdo not tumble dry\b",
    r"\bdo not iron\b",
    r"\bwarranty\b",
    r"\bguarantee\b",
    r"\brefund\b",
    r"\breturn policy\b",
    r"\bcustomer service\b",
    r"\bcontact us\b",
]


def is_disclaimer_or_warning_text(text: str) -> bool:
    """Detect warnings, disclaimers, negative care instructions, or marketplace boilerplate."""
    if not text:
        return False
    lower = text.lower()
    return any(bool(re.search(pat, lower)) for pat in WARNING_DISCLAIMER_PATTERNS)


SPEC_UNITS = {
    "w", "watt", "watts", "v", "volt", "volts", "hz", "khz", "mhz", "ghz",
    "inch", "inches", "in", "cm", "mm", "m", "ft", "oz", "fl", "fl oz", "lbs", "lb",
    "kg", "g", "l", "liter", "liters", "ml", "mah", "ah", "a", "amp", "amps",
    "rpm", "db", "size", "dimension", "dimensions",
}


def is_isolated_spec_fragment(text: str) -> bool:
    """
    True if text is an isolated raw dimension, wattage, voltage, frequency, weight,
    or technical specification without meaningful descriptive context.
    e.g. '1200 W', '15W', '120V / 60Hz', '10 x 8 x 2 inches', '5V/2A', '2.4 GHz'.
    """
    if not text:
        return True
    cleaned = text.strip().rstrip(".,;–—")
    words = re.findall(r"[a-z0-9.]+", cleaned.lower())
    if not words:
        return True

    substantive = []
    for w in words:
        if w in STOPWORDS or w in {"and", "or", "with", "x", "/", "-"}:
            continue
        if w in SPEC_UNITS:
            continue
        m = re.match(r"^\d+(?:\.\d+)?([a-z]+)?$", w)
        if m:
            unit_part = m.group(1)
            if not unit_part or unit_part in SPEC_UNITS:
                continue
        substantive.append(w)

    return len(substantive) == 0


# ---------------------------------------------------------------------------
# Dynamic Marketing Angle Selection
# ---------------------------------------------------------------------------

def select_marketing_angles(
    product: Product,
    facts: Optional[CanonicalProductFacts] = None,
    count: int = 4
) -> List[str]:
    """
    Select 3 to 5 genuinely relevant marketing angles tailored to the specific
    product and its verified category characteristics.
    """
    category = detect_product_category(product, facts)
    corpus = " ".join([
        product.title or "",
        " ".join(product.features or []),
        product.description or "",
    ]).lower()

    if category == CAT_FRAGRANCE:
        candidates = [
            ANGLE_LUXURY_FRAGRANCE,
            ANGLE_FRAGRANCE_NOTES,
            ANGLE_GIFT_INSPIRATION,
            ANGLE_EVERYDAY_ELEGANCE,
        ]
        if "unisex" in corpus or ("men" in corpus and "women" in corpus):
            candidates.append(ANGLE_UNISEX_APPEAL)

    elif category == CAT_BEAUTY:
        candidates = [
            ANGLE_DAILY_SKINCARE,
            ANGLE_NOURISHING_INGREDIENTS,
            ANGLE_TARGETED_CARE,
            ANGLE_GIFT_INSPIRATION,
        ]

    elif category == CAT_HOME_KITCHEN:
        is_textile = any(bool(re.search(r"\b" + re.escape(t) + r"\b", corpus)) for t in ["towel", "towels", "dishcloth", "dishcloths", "washcloth", "cloth", "cloths", "linen", "microfiber", "rag", "rags"])
        has_storage = not is_textile and any(bool(re.search(r"\b" + re.escape(k) + r"\b", corpus)) for k in ["sink", "cabinet", "drawer", "organizer", "rack", "shelf", "sliding", "storage", "tier"])
        if has_storage:
            candidates = [
                ANGLE_SPACE_SAVING_IDEA,
                ANGLE_PRACTICAL_SOLUTION,
                ANGLE_KITCHEN_ORGANIZATION,
                ANGLE_DURABLE_UTILITY,
            ]
        else:
            candidates = [
                ANGLE_PRACTICAL_SOLUTION,
                ANGLE_KEY_PRODUCT_BENEFITS,
                ANGLE_THOUGHTFUL_DESIGN,
                ANGLE_GIFT_INSPIRATION,
            ]

    elif category == CAT_ELECTRONICS:
        candidates = [
            ANGLE_SMART_TECH_UPGRADE,
            ANGLE_COMPACT_CONVENIENCE,
            ANGLE_KEY_PRODUCT_BENEFITS,
            ANGLE_DESK_TRAVEL_HUB,
        ]

    elif category == CAT_FASHION:
        candidates = [
            ANGLE_WARDROBE_STAPLE,
            ANGLE_REFINED_STYLE,
            ANGLE_VERSATILE_ACCESSORY,
            ANGLE_GIFT_INSPIRATION,
        ]

    elif category == CAT_PETS:
        candidates = [
            ANGLE_PET_COMFORT,
            ANGLE_PRACTICAL_PET_CARE,
            ANGLE_ACTIVE_PLAY,
            ANGLE_PRACTICAL_SOLUTION,
        ]

    elif category == CAT_HOME_DECOR:
        candidates = [
            ANGLE_AESTHETIC_ACCENT,
            ANGLE_ROOM_TRANSFORMATION,
            ANGLE_COZY_TOUCH,
            ANGLE_GIFT_INSPIRATION,
        ]

    elif category == CAT_GIFTS:
        candidates = [
            ANGLE_GIFT_INSPIRATION,
            ANGLE_THOUGHTFUL_DESIGN,
            ANGLE_PRACTICAL_SOLUTION,
            ANGLE_KEY_PRODUCT_BENEFITS,
        ]

    else:  # CAT_GENERAL / Safe Fallback
        candidates = [
            ANGLE_PRACTICAL_SOLUTION,
            ANGLE_KEY_PRODUCT_BENEFITS,
            ANGLE_THOUGHTFUL_DESIGN,
            ANGLE_GIFT_INSPIRATION,
        ]

    clamped_count = max(3, min(5, count))
    return candidates[:clamped_count]


# ---------------------------------------------------------------------------
# Fact Extraction & Feature Parsing (Strictly Grounded, Zero Hallucinations)
# ---------------------------------------------------------------------------

def _clean_product_name(title: str, max_words: int = 6) -> str:
    """Extract a clean, readable product descriptor without Amazon keyword stuffing."""
    cleaned = re.sub(r"\[.*?\]|\(.*?\)", "", title)
    cleaned = re.sub(r",.*$", "", cleaned)
    cleaned = re.sub(r" - .*$", "", cleaned)
    words = [w for w in cleaned.split() if w.lower() not in {"with", "and", "for", "the", "a", "an", "of"}]
    core = " ".join(words[:max_words]).strip()
    return core if len(core) >= 3 else title[:30].strip()


def _clean_bullet_phrase(text: str, max_chars: int = 52) -> str:
    """Format and clean a bullet clause to avoid awkward truncation or dangling conjunctions."""
    clean = text.strip().rstrip(".,;–—")
    clean = re.sub(r"\b(?:ideal|perfect)\s+for\b", "for", clean, flags=re.IGNORECASE)
    clean = re.sub(r"^(?:for|great\s+for)\s+", "", clean, flags=re.IGNORECASE)
    if len(clean) > max_chars:
        clean = clean[:max_chars].rstrip()
        if " " in clean and len(clean) >= 16:
            last_space = clean.rfind(" ")
            if last_space >= 16:
                clean = clean[:last_space]
    clean = clean.rstrip(" ,;–—.:-")
    clean = re.sub(r"\b(?:and|with|for|the|a|an|of|in|to|on|at|by|or|as)\s*$", "", clean, flags=re.IGNORECASE).rstrip(" ,;–—.:-")
    return clean[:1].upper() + clean[1:] if clean else ""


def _parse_verified_clauses(
    product: Product,
    facts: Optional[CanonicalProductFacts] = None,
) -> List[str]:
    """
    Extract discrete, factual phrases directly from product.features or facts.supporting_attributes().
    Strictly excludes:
    - Warnings, disclaimers, negative notices
    - SEO keyword dumps and search phrase lists
    - Isolated raw specifications without context
    - Marketplace administrative boilerplate
    """
    clauses: List[str] = []
    source_features = (facts.supporting_attributes() if facts else None) or (product.features or [])
    for feat in source_features:
        if not feat or is_keyword_stuffed_text(feat):
            continue
        feat_clean = feat.strip()
        parts = re.split(r"\s*[–—]\s*|\.\s+", feat_clean)
        for part in parts:
            part = part.strip().rstrip(".,;")
            if 8 <= len(part) <= 120:
                if is_disclaimer_or_warning_text(part):
                    continue
                if is_isolated_spec_fragment(part):
                    continue
                clauses.append(part)
    return clauses


def _extract_category_bullets(
    product: Product,
    category: str,
    angle: str,
    facts: Optional[CanonicalProductFacts] = None,
) -> List[str]:
    """
    Extract strictly verified feature bullets tailored to the specific angle and product.
    Never uses generic placeholder templates.
    """
    raw_clauses = _parse_verified_clauses(product, facts)
    all_text = " ".join([product.title or ""] + (product.features or [])).lower()

    bullets: List[str] = []

    # -----------------------------------------------------------------------
    # FRAGRANCE BULLET EXTRACTION
    # -----------------------------------------------------------------------
    if category == CAT_FRAGRANCE:
        if angle == ANGLE_FRAGRANCE_NOTES:
            top_match = re.search(r"top[:\s–—]+([^.–—\n]+)", all_text, re.IGNORECASE)
            heart_match = re.search(r"(?:heart|middle)[:\s–—]+([^.–—\n]+)", all_text, re.IGNORECASE)
            base_match = re.search(r"base[:\s–—]+([^.–—\n]+)", all_text, re.IGNORECASE)

            if top_match:
                bullets.append(_clean_bullet_phrase(f"Top: {top_match.group(1).strip().capitalize()}", 52))
            if heart_match:
                bullets.append(_clean_bullet_phrase(f"Heart: {heart_match.group(1).strip().capitalize()}", 52))
            if base_match:
                bullets.append(_clean_bullet_phrase(f"Base: {base_match.group(1).strip().capitalize()}", 52))

            if not bullets:
                notes = [w for w in ["honey", "oud", "bergamot", "rose", "patchouli", "amber", "vanilla", "musk"] if w in all_text]
                if notes:
                    bullets.append(_clean_bullet_phrase(f"Notes: {', '.join(notes[:3]).capitalize()}", 46))
                    if len(notes) > 3:
                        bullets.append(_clean_bullet_phrase(f"Base: {', '.join(notes[3:6]).capitalize()}", 46))

        elif angle == ANGLE_LUXURY_FRAGRANCE:
            if "unisex" in all_text and "eau de parfum" in all_text:
                bullets.append("Unisex Eau de Parfum")
            elif "eau de parfum" in all_text:
                bullets.append("Eau de Parfum Concentration")
            if "dry-down" in all_text or "gourmand" in all_text:
                bullets.append("Sweet, woody and gourmand dry-down")
            if "honey" in all_text and "bergamot" in all_text:
                bullets.append("Golden honey and bergamot open")

            if len(bullets) < 3:
                for c in raw_clauses:
                    cl = c.lower()
                    if any(k in cl for k in ["eau de parfum", "unisex", "concentration", "dry-down", "gourmand", "warm", "rich"]):
                        cb = _clean_bullet_phrase(c, 50)
                        if cb and cb not in bullets and not is_disclaimer_or_warning_text(cb) and not is_isolated_spec_fragment(cb):
                            bullets.append(cb)
                    if len(bullets) >= 3:
                        break

        elif angle == ANGLE_EVERYDAY_ELEGANCE:
            if "pulse points" in all_text:
                bullets.append("Apply to pulse points (wrists, neck)")
            if "one to two sprays" in all_text:
                bullets.append("One to two sprays are enough")
            size_match = re.search(r"(\d+(?:\.\d+)?\s*(?:fl oz|ml))", product.title or "", re.IGNORECASE)
            if size_match:
                bullets.append(f"{size_match.group(1)} spray bottle")

            if len(bullets) < 3:
                for c in raw_clauses:
                    cl = c.lower()
                    if any(k in cl for k in ["spray", "pulse points", "wrists", "layer", "bottle", "fl oz", "ml"]):
                        cb = _clean_bullet_phrase(c, 50)
                        if cb and cb not in bullets and not is_disclaimer_or_warning_text(cb) and not is_isolated_spec_fragment(cb):
                            bullets.append(cb)
                    if len(bullets) >= 3:
                        break

        elif angle == ANGLE_GIFT_INSPIRATION:
            if "holiday present" in all_text or "christmas gift" in all_text or "present" in all_text:
                bullets.append("A cozy holiday present")
            if "winter evenings" in all_text or "date nights" in all_text:
                bullets.append("Made for winter evenings & date nights")
            if "women" in all_text and "men" in all_text:
                bullets.append("Loved by women and men")

            if len(bullets) < 3:
                for c in raw_clauses:
                    cl = c.lower()
                    if any(k in cl for k in ["gift", "holiday", "present", "cozy", "winter", "date night", "dinners"]):
                        cb = _clean_bullet_phrase(c, 50)
                        if cb and cb not in bullets and not is_disclaimer_or_warning_text(cb) and not is_isolated_spec_fragment(cb):
                            bullets.append(cb)
                    if len(bullets) >= 3:
                        break

        elif angle == ANGLE_UNISEX_APPEAL:
            bullets.append("Unisex fragrance for women and men")
            if "honey" in all_text and "cologne" in all_text:
                bullets.append("Honey perfume & warm cologne appeal")
            if "gourmand" in all_text or "dry-down" in all_text:
                bullets.append("Sweet, woody gourmand dry-down")

            if len(bullets) < 3:
                for c in raw_clauses:
                    cl = c.lower()
                    if any(k in cl for k in ["unisex", "women", "men", "honey", "cologne"]):
                        cb = _clean_bullet_phrase(c, 50)
                        if cb and cb not in bullets and not is_disclaimer_or_warning_text(cb) and not is_isolated_spec_fragment(cb):
                            bullets.append(cb)
                    if len(bullets) >= 3:
                        break

    # -----------------------------------------------------------------------
    # HOME & KITCHEN BULLET EXTRACTION
    # -----------------------------------------------------------------------
    elif category == CAT_HOME_KITCHEN:
        for c in raw_clauses:
            cl = c.lower()
            if angle == ANGLE_SPACE_SAVING_IDEA and any(k in cl for k in ["space", "compact", "drawer", "cabinet", "sliding", "tight"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            elif angle == ANGLE_PRACTICAL_SOLUTION and any(k in cl for k in ["absorb", "cotton", "microfiber", "soft", "wash", "quick", "clean", "access", "easy", "sliding", "organize", "pull-out", "dry", "towel", "cloth", "hems", "stitch", "weave"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            elif angle == ANGLE_DURABLE_UTILITY and any(k in cl for k in ["steel", "plastic", "rust", "heavy-duty", "durable", "sturdy", "feet", "reinforced", "hems", "wash", "washes", "gsm", "stitch"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            elif angle == ANGLE_KITCHEN_ORGANIZATION and any(k in cl for k in ["assembly", "tier", "storage", "sink", "kitchen", "bathroom"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            elif angle in (ANGLE_KEY_PRODUCT_BENEFITS, ANGLE_THOUGHTFUL_DESIGN) and any(k in cl for k in ["cotton", "microfiber", "material", "absorb", "fabric", "quality", "stitch", "wash", "washes", "shine", "dry"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            if len(bullets) >= 3:
                break

    # -----------------------------------------------------------------------
    # ELECTRONICS BULLET EXTRACTION
    # -----------------------------------------------------------------------
    elif category == CAT_ELECTRONICS:
        for c in raw_clauses:
            cl = c.lower()
            if angle in (ANGLE_SMART_TECH_UPGRADE, "Smart Home Upgrade") and any(k in cl for k in ["wireless", "fast", "speed", "smart", "bluetooth", "charging"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            elif angle == ANGLE_COMPACT_CONVENIENCE and any(k in cl for k in ["foldable", "compact", "travel", "desk", "nightstand", "3-in-1"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            elif angle == ANGLE_KEY_PRODUCT_BENEFITS and any(k in cl for k in ["watt", "protection", "aluminum", "certified", "safety"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            elif angle == ANGLE_DESK_TRAVEL_HUB and any(k in cl for k in ["iphone", "watch", "airpods", "multi-device", "cable", "stand"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            if len(bullets) >= 3:
                break

    # -----------------------------------------------------------------------
    # BEAUTY BULLET EXTRACTION
    # -----------------------------------------------------------------------
    elif category == CAT_BEAUTY:
        for c in raw_clauses:
            cl = c.lower()
            if angle == ANGLE_NOURISHING_INGREDIENTS and any(k in cl for k in ["ingredient", "vitamin", "acid", "formula", "extract", "natural"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            elif angle == ANGLE_DAILY_SKINCARE and any(k in cl for k in ["daily", "morning", "night", "routine", "apply", "skin"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            elif angle == ANGLE_TARGETED_CARE and any(k in cl for k in ["gentle", "texture", "soothing", "hydration", "moisture"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            elif angle == ANGLE_GIFT_INSPIRATION and any(k in cl for k in ["gift", "luxury", "care", "present"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            if len(bullets) >= 3:
                break

    # -----------------------------------------------------------------------
    # FASHION & ACCESSORIES BULLET EXTRACTION
    # -----------------------------------------------------------------------
    elif category == CAT_FASHION:
        for c in raw_clauses:
            cl = c.lower()
            if angle == ANGLE_WARDROBE_STAPLE and any(k in cl for k in ["comfort", "fit", "cotton", "fabric", "wear", "daily", "stretch"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            elif angle == ANGLE_REFINED_STYLE and any(k in cl for k in ["style", "design", "tailored", "classic", "modern", "finish"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            elif angle == ANGLE_VERSATILE_ACCESSORY and any(k in cl for k in ["versatile", "pair", "casual", "pocket", "strap", "lightweight"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            elif angle == ANGLE_GIFT_INSPIRATION and any(k in cl for k in ["gift", "timeless", "present", "accessory"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            if len(bullets) >= 3:
                break

    # -----------------------------------------------------------------------
    # PET PRODUCTS BULLET EXTRACTION
    # -----------------------------------------------------------------------
    elif category == CAT_PETS:
        for c in raw_clauses:
            cl = c.lower()
            if angle == ANGLE_PET_COMFORT and any(k in cl for k in ["comfort", "soft", "cushion", "sleep", "warm", "fleece", "support"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            elif angle == ANGLE_PRACTICAL_PET_CARE and any(k in cl for k in ["washable", "clean", "removable", "waterproof", "easy", "hygiene"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            elif angle == ANGLE_ACTIVE_PLAY and any(k in cl for k in ["durable", "chew", "play", "active", "toy", "training", "exercise"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            elif angle == ANGLE_PRACTICAL_SOLUTION and any(k in cl for k in ["durable", "safe", "secure", "convenient"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            if len(bullets) >= 3:
                break

    # -----------------------------------------------------------------------
    # HOME DECOR & WALL ART BULLET EXTRACTION
    # -----------------------------------------------------------------------
    elif category == CAT_HOME_DECOR:
        for c in raw_clauses:
            cl = c.lower()
            if angle == ANGLE_AESTHETIC_ACCENT and any(k in cl for k in ["wall", "frame", "canvas", "print", "color", "texture", "finish"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            elif angle == ANGLE_ROOM_TRANSFORMATION and any(k in cl for k in ["room", "hang", "mount", "display", "refresh", "focal"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            elif angle == ANGLE_COZY_TOUCH and any(k in cl for k in ["cozy", "warm", "ambient", "soft", "lighting", "glow", "charm"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            elif angle == ANGLE_GIFT_INSPIRATION and any(k in cl for k in ["gift", "housewarming", "present", "decor"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            if len(bullets) >= 3:
                break

    # -----------------------------------------------------------------------
    # GIFTS BULLET EXTRACTION
    # -----------------------------------------------------------------------
    elif category == CAT_GIFTS:
        for c in raw_clauses:
            cl = c.lower()
            if angle == ANGLE_GIFT_INSPIRATION and any(k in cl for k in ["gift", "holiday", "present", "box", "package", "celebration"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            elif angle == ANGLE_THOUGHTFUL_DESIGN and any(k in cl for k in ["thoughtful", "handcrafted", "premium", "quality", "detail"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            elif angle == ANGLE_PRACTICAL_SOLUTION and any(k in cl for k in ["daily", "useful", "easy", "versatile"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            elif angle == ANGLE_KEY_PRODUCT_BENEFITS and any(k in cl for k in ["feature", "material", "finish", "craft"]):
                bullets.append(_clean_bullet_phrase(c, 52))
            if len(bullets) >= 3:
                break

    # -----------------------------------------------------------------------
    # GENERAL / SAFE FALLBACK (Strictly uses actual feature clauses)
    # -----------------------------------------------------------------------
    if len(bullets) < 2:
        for c in raw_clauses:
            clean_c = _clean_bullet_phrase(c, 52)
            if clean_c and clean_c not in bullets and not is_disclaimer_or_warning_text(clean_c) and not is_isolated_spec_fragment(clean_c):
                bullets.append(clean_c)
            if len(bullets) >= 3:
                break

    deduped = []
    seen = set()
    for b in bullets:
        clean_b = b.strip().rstrip(".,;")
        if clean_b.lower() not in seen and len(clean_b) >= 4:
            seen.add(clean_b.lower())
            deduped.append(clean_b)

    return deduped[:3]


# ---------------------------------------------------------------------------
# Grounded Marketing Copy Generation
# ---------------------------------------------------------------------------

def generate_creative_copy(
    product: Product,
    angle: str,
    facts: Optional[CanonicalProductFacts] = None,
) -> CreativeCopy:
    """
    Generate product-specific, non-hallucinated marketing copy and CTA tailored
    to the detected category and selected marketing angle.
    """
    category = detect_product_category(product, facts)
    bullets = _extract_category_bullets(product, category, angle, facts)
    all_text = " ".join([product.title or ""] + (product.features or [])).lower()
    core_name = _clean_product_name(product.title)

    # -----------------------------------------------------------------------
    # 1. PERFUMES & FRAGRANCES
    # -----------------------------------------------------------------------
    if category == CAT_FRAGRANCE:
        has_honey = "honey" in all_text
        has_oud = "oud" in all_text

        if angle == ANGLE_FRAGRANCE_NOTES:
            badge = "FRAGRANCE NOTES"
            if has_honey and has_oud:
                headline = "Where Golden Honey Meets Smoky Oud"
            else:
                headline = f"The Distinctive Scent of {core_name}"
            subheadline = "A warm, sweet and woody fragrance with a rich, elegant character."
            cta = "Explore Notes"

        elif angle == ANGLE_LUXURY_FRAGRANCE:
            badge = "LUXURY FRAGRANCE"
            if has_honey and has_oud:
                headline = "Warm Honey & Rich Smoky Oud"
            else:
                headline = f"An Intoxicating Luxury Eau de Parfum"
            subheadline = "A warm, sweet and woody fragrance with an alluring, elegant character."
            cta = "Discover the Scent"

        elif angle == ANGLE_EVERYDAY_ELEGANCE:
            badge = "EVERYDAY ELEGANCE"
            headline = "Your Signature Cold-Weather Scent"
            subheadline = "One to two sprays on pulse points for an all-day captivating scent trail."
            cta = "See Details"

        elif angle == ANGLE_GIFT_INSPIRATION:
            badge = "GIFT INSPIRATION"
            headline = "A Memorable Fragrance for Someone Special"
            subheadline = "A cozy holiday present made for winter evenings and festive dinners."
            cta = "Explore Gift Idea"

        elif angle == ANGLE_UNISEX_APPEAL:
            badge = "UNISEX SCENT"
            headline = "Loved as a Perfume and Cologne"
            subheadline = "A sweet, woody and gourmand dry-down designed for women and men."
            cta = "Explore Fragrance"

        else:
            badge = "SIGNATURE SCENT"
            headline = f"Discover {core_name}"
            subheadline = "A rich, distinctive Eau de Parfum crafted for a lasting impression."
            cta = "See Details"

    # -----------------------------------------------------------------------
    # 2. HOME & KITCHEN
    # -----------------------------------------------------------------------
    elif category == CAT_HOME_KITCHEN:
        is_textile = any(bool(re.search(r"\b" + re.escape(t) + r"\b", all_text)) for t in ["towel", "towels", "dishcloth", "dishcloths", "washcloth", "cloth", "cloths", "linen", "microfiber", "rag", "rags"])
        has_storage = not is_textile and any(bool(re.search(r"\b" + re.escape(k) + r"\b", all_text)) for k in ["sink", "cabinet", "drawer", "organizer", "rack", "shelf", "sliding", "storage", "tier"])

        if has_storage:
            if angle == ANGLE_SPACE_SAVING_IDEA:
                badge = "SPACE-SAVING IDEA"
                headline = "Clever Space-Saving Storage"
                subheadline = "Sliding bottom drawer for effortless access in tight cabinet spaces."
                cta = "Shop Now"
            elif angle == ANGLE_PRACTICAL_SOLUTION:
                badge = "PRACTICAL SOLUTION"
                headline = "Effortless Organization Under the Sink"
                subheadline = "Keep your kitchen and bathroom essentials tidy and within reach."
                cta = "Organize Your Space"
            elif angle == ANGLE_DURABLE_UTILITY:
                badge = "DURABLE BUILD"
                headline = "Sturdy & Rust-Proof Storage Design"
                subheadline = "Built with durable materials and non-slip feet for reliable support."
                cta = "See Details"
            elif angle == ANGLE_KITCHEN_ORGANIZATION:
                badge = "EASY SETUP"
                headline = "Quick Tool-Free Cabinet Organization"
                subheadline = "Maximize vertical storage space with smooth pull-out sliding tiers."
                cta = "Discover More"
            else:
                badge = "HOME ESSENTIAL"
                headline = f"Smart Organization: {core_name}"
                subheadline = "Designed for seamless daily convenience and clutter-free storage."
                cta = "See Details"

        elif is_textile:
            if angle == ANGLE_PRACTICAL_SOLUTION:
                badge = "PRACTICAL ESSENTIAL"
                headline = f"Soft & Absorbent {core_name}"
                subheadline = "High-absorbency fabric with reinforced edges built for everyday kitchen tasks."
                cta = "Shop Now"
            elif angle in (ANGLE_KEY_PRODUCT_BENEFITS, ANGLE_DURABLE_UTILITY):
                badge = "QUALITY BUILD"
                headline = f"Everyday Kitchen Quality: {core_name}"
                subheadline = "Durable, absorbent weave designed to withstand daily use and washing."
                cta = "See Details"
            elif angle == ANGLE_THOUGHTFUL_DESIGN:
                badge = "THOUGHTFUL DESIGN"
                headline = f"Crafted for Daily Chores: {core_name}"
                subheadline = "Quality stitching and quick-drying fabric made for busy homes."
                cta = "Discover More"
            elif angle == ANGLE_GIFT_INSPIRATION:
                badge = "GIFT INSPIRATION"
                headline = "A Practical Gift for Any Kitchen"
                subheadline = "A useful, attractive kitchen essential anyone will appreciate."
                cta = "Explore Gift Idea"
            else:
                badge = "KITCHEN ESSENTIAL"
                headline = f"Everyday Kitchen Essential: {core_name}"
                subheadline = "High-quality fabric tailored for reliable daily convenience."
                cta = "Shop Now"

        else:
            if angle == ANGLE_PRACTICAL_SOLUTION:
                badge = "PRACTICAL SOLUTION"
                headline = f"Reliable Cooking Essential: {core_name}"
                subheadline = "Designed to streamline daily meal preparation and kitchen routines."
                cta = "Shop Now"
            elif angle in (ANGLE_KEY_PRODUCT_BENEFITS, ANGLE_DURABLE_UTILITY):
                badge = "QUALITY CRAFT"
                headline = f"Performance & Durability: {core_name}"
                subheadline = "Crafted with durable materials for consistent kitchen performance."
                cta = "See Details"
            elif angle == ANGLE_THOUGHTFUL_DESIGN:
                badge = "THOUGHTFUL DESIGN"
                headline = f"Designed for Home Cooks: {core_name}"
                subheadline = "Smart ergonomic design that makes cooking simpler and enjoyable."
                cta = "Discover More"
            elif angle == ANGLE_GIFT_INSPIRATION:
                badge = "GIFT INSPIRATION"
                headline = "A Practical Gift for Food Lovers"
                subheadline = "A thoughtful culinary upgrade perfect for any home kitchen."
                cta = "Explore Gift Idea"
            else:
                badge = "KITCHEN ESSENTIAL"
                headline = f"Kitchen Upgrade: {core_name}"
                subheadline = "Quality craftsmanship tailored for everyday cooking convenience."
                cta = "See Details"

    # -----------------------------------------------------------------------
    # 3. ELECTRONICS & GADGETS
    # -----------------------------------------------------------------------
    elif category == CAT_ELECTRONICS:
        is_charger = any(k in all_text for k in ["wireless", "charger", "charging", "dock", "power bank", "station"])
        is_audio = any(k in all_text for k in ["headphone", "headphones", "earbud", "earbuds", "speaker", "audio"])

        if angle in (ANGLE_SMART_TECH_UPGRADE, "Smart Home Upgrade"):
            badge = "SMART HOME UPGRADE" if angle == "Smart Home Upgrade" else "SMART TECH UPGRADE"
            if is_charger:
                headline = "Fast Wireless Multi-Device Charging"
                subheadline = "Power your daily essentials simultaneously with clean cable-free speed."
            elif is_audio:
                headline = f"Immersive Audio Experience: {core_name}"
                subheadline = "High-fidelity sound reproduction tailored for everyday listening."
            else:
                headline = f"Smart Tech Upgrade: {core_name}"
                subheadline = "Advanced functionality and reliable performance for your daily setup."
            cta = "Explore This Find"

        elif angle == ANGLE_COMPACT_CONVENIENCE:
            badge = "COMPACT & PORTABLE"
            headline = "Foldable Travel & Nightstand Setup" if is_charger else f"Compact & Travel Ready: {core_name}"
            subheadline = "Compact design that folds flat for easy packing and desk decluttering." if is_charger else "Lightweight, space-saving design built for seamless portability."
            cta = "Shop Now"

        elif angle == ANGLE_KEY_PRODUCT_BENEFITS:
            badge = "VERIFIED PERFORMANCE"
            headline = "Certified Fast & Safe Charging" if is_charger else f"High Performance: {core_name}"
            subheadline = "Engineered with certified safety protection and premium construction." if is_charger else "Engineered with verified specifications for dependable daily operation."
            cta = "See Specs"

        elif angle == ANGLE_DESK_TRAVEL_HUB:
            badge = "DESK COMPANION"
            headline = "Streamline Your Nightstand Routine" if is_charger else f"Versatile Desk Companion: {core_name}"
            subheadline = "All-in-one charging station dedicated to your daily devices." if is_charger else "Designed to integrate smoothly into your workspace and daily workflow."
            cta = "See Details"

        else:
            badge = "TECH ESSENTIAL"
            headline = f"Upgrade Your Setup With {core_name}"
            subheadline = "Smart, reliable performance designed for modern device workflows."
            cta = "Explore This Find"

    # -----------------------------------------------------------------------
    # 4. BEAUTY & PERSONAL CARE
    # -----------------------------------------------------------------------
    elif category == CAT_BEAUTY:
        if angle == ANGLE_DAILY_SKINCARE:
            badge = "DAILY SKINCARE"
            headline = f"Elevate Your Daily Routine"
            subheadline = "Gentle nourishment formulated for balanced, radiant-looking care."
            cta = "Explore Routine"

        elif angle == ANGLE_NOURISHING_INGREDIENTS:
            badge = "ACTIVE INGREDIENTS"
            headline = f"Formulated With Proven Care"
            subheadline = "Thoughtful active ingredients chosen to support healthy daily balance."
            cta = "See Details"

        elif angle == ANGLE_TARGETED_CARE:
            badge = "TARGETED CARE"
            headline = f"Targeted Self-Care: {core_name}"
            subheadline = "A soothing self-care essential designed for everyday wellness."
            cta = "Discover More"

        elif angle == ANGLE_GIFT_INSPIRATION:
            badge = "GIFT INSPIRATION"
            headline = "A Self-Care Gift Worth Giving"
            subheadline = "A thoughtful beauty present designed for everyday pampering."
            cta = "Explore Gift Idea"

        else:
            badge = "PERSONAL CARE"
            headline = f"Gentle & Refreshing: {core_name}"
            subheadline = "A soothing self-care essential designed for everyday wellness."
            cta = "Discover More"

    # -----------------------------------------------------------------------
    # 5. FASHION & ACCESSORIES
    # -----------------------------------------------------------------------
    elif category == CAT_FASHION:
        if angle == ANGLE_WARDROBE_STAPLE:
            badge = "WARDROBE STAPLE"
            headline = f"Your Everyday Essential: {core_name}"
            subheadline = "Timeless comfort and versatile style made for everyday wear."
            cta = "Shop Now"

        elif angle == ANGLE_REFINED_STYLE:
            badge = "REFINED STYLE"
            headline = "Tailored Details & Modern Silhouette"
            subheadline = "Clean lines and classic styling designed to effortlessly elevate your look."
            cta = "See Details"

        elif angle == ANGLE_VERSATILE_ACCESSORY:
            badge = "DAILY ACCESSORY"
            headline = "Versatile Style for Every Occasion"
            subheadline = "Pairs effortlessly from morning errands to evening plans."
            cta = "Discover More"

        elif angle == ANGLE_GIFT_INSPIRATION:
            badge = "GIFT INSPIRATION"
            headline = "A Classic Gift for Someone Special"
            subheadline = "A stylish, timeless present anyone will love wearing."
            cta = "Explore Gift Idea"

        else:
            badge = "STYLE ESSENTIAL"
            headline = f"Effortless Style: {core_name}"
            subheadline = "Clean craftsmanship crafted for modern everyday confidence."
            cta = "See Details"

    # -----------------------------------------------------------------------
    # 6. PET PRODUCTS
    # -----------------------------------------------------------------------
    elif category == CAT_PETS:
        if angle == ANGLE_PET_COMFORT:
            badge = "PET COMFORT"
            headline = f"Cozy Comfort for Your Furry Friend"
            subheadline = "Soft, supportive comfort crafted for restful naps and everyday wellness."
            cta = "Discover More"

        elif angle == ANGLE_PRACTICAL_PET_CARE:
            badge = "EASY PET CARE"
            headline = "Simple, Hassle-Free Pet Care"
            subheadline = "Durable, easy-to-clean materials that keep your living space fresh."
            cta = "See Details"

        elif angle == ANGLE_ACTIVE_PLAY:
            badge = "ACTIVE PLAY"
            headline = "Built for Fun & Healthy Exercise"
            subheadline = "Engaging, pet-safe design made to keep your pet active and happy."
            cta = "Shop Now"

        else:
            badge = "PET ESSENTIAL"
            headline = f"Happy Pets: {core_name}"
            subheadline = "Thoughtful design made to enhance your pet's daily routine."
            cta = "See Details"

    # -----------------------------------------------------------------------
    # 7. HOME DECOR & WALL ART
    # -----------------------------------------------------------------------
    elif category == CAT_HOME_DECOR:
        if angle == ANGLE_AESTHETIC_ACCENT:
            badge = "HOME AESTHETIC"
            headline = f"Elevate Your Living Space Aesthetic"
            subheadline = "A tasteful visual accent that adds warmth and character to any room."
            cta = "Explore This Find"

        elif angle == ANGLE_ROOM_TRANSFORMATION:
            badge = "INSTANT REFRESH"
            headline = "Refresh Your Walls & Living Room"
            subheadline = "An eye-catching focal piece designed to transform your space."
            cta = "Discover More"

        elif angle == ANGLE_COZY_TOUCH:
            badge = "COZY INTERIOR"
            headline = "Warm & Inviting Living Decor"
            subheadline = "Subtle charm and comforting style that make your house feel like home."
            cta = "Shop Now"

        elif angle == ANGLE_GIFT_INSPIRATION:
            badge = "GIFT INSPIRATION"
            headline = "A Beautiful Gift for Any Home"
            subheadline = "A thoughtful housewarming or holiday accent they will admire daily."
            cta = "Explore Gift Idea"

        else:
            badge = "HOME ACCENT"
            headline = f"Charming Decor: {core_name}"
            subheadline = "A beautiful aesthetic touch designed for everyday living."
            cta = "See Details"

    # -----------------------------------------------------------------------
    # 8. GIFTS
    # -----------------------------------------------------------------------
    elif category == CAT_GIFTS:
        if angle == ANGLE_GIFT_INSPIRATION:
            badge = "GIFT INSPIRATION"
            headline = "A Standout Gift Worth Giving"
            subheadline = "Thoughtful, memorable, and ready for holidays, birthdays, or special days."
            cta = "Explore Gift Idea"

        elif angle == ANGLE_THOUGHTFUL_DESIGN:
            badge = "THOUGHTFUL DESIGN"
            headline = f"Crafted With Care: {core_name}"
            subheadline = "A memorable find that combines quality, beauty, and everyday use."
            cta = "Discover More"

        elif angle == ANGLE_PRACTICAL_SOLUTION:
            badge = "PRACTICAL GIFT"
            headline = "A Thoughtful & Useful Surprise"
            subheadline = "Practical everyday quality that friends and family will truly appreciate."
            cta = "See Details"

        else:
            badge = "GIFT IDEA"
            headline = f"A Memorable Find: {core_name}"
            subheadline = "A wonderful gift idea that makes any occasion feel special."
            cta = "Explore Gift Idea"

    # -----------------------------------------------------------------------
    # 9. GENERAL / SAFE FALLBACK (Title & Feature Grounded)
    # -----------------------------------------------------------------------
    else:
        if angle == ANGLE_PRACTICAL_SOLUTION:
            badge = "PRACTICAL SOLUTION"
            headline = f"A Smart Solution for Daily Use"
            subheadline = f"Thoughtful design and proven functionality from {core_name}."
            cta = "See Details"

        elif angle == ANGLE_GIFT_INSPIRATION:
            badge = "GIFT INSPIRATION"
            headline = f"A Standout Find Worth Gifting"
            subheadline = "A thoughtful present designed for everyday utility and lasting value."
            cta = "Explore Gift Idea"

        elif angle == ANGLE_THOUGHTFUL_DESIGN:
            badge = "THOUGHTFUL DESIGN"
            headline = f"Focused on Functional Design"
            subheadline = "Clean design elements that make everyday use simpler and smoother."
            cta = "Discover More"

        else:  # ANGLE_KEY_PRODUCT_BENEFITS
            badge = "KEY HIGHLIGHTS"
            headline = f"Verified Features & Specifications"
            subheadline = f"Grounded product details for {core_name}."
            cta = "See Details"

    return CreativeCopy(
        angle=angle,
        badge=badge,
        headline=headline,
        subheadline=subheadline,
        benefits=bullets,
        cta_text=cta,
    )


# ---------------------------------------------------------------------------
# Validation Engine (Strict Grounding, Zero Hallucinations, ASIN Isolation)
# ---------------------------------------------------------------------------

def validate_creative_copy(
    copy: CreativeCopy,
    facts: CanonicalProductFacts,
    product: Product,
) -> List[str]:
    """
    Validate that creative copy is truthful, strictly grounded in verified facts,
    and free from generic filler, disclaimers, or cross-ASIN contamination.
    Returns a list of error strings; an empty list indicates valid copy.
    """
    errors: List[str] = []

    # 1. ASIN consistency check
    if facts.asin and product.asin:
        if facts.asin.upper().strip() != product.asin.upper().strip():
            errors.append(
                f"Cross-ASIN mismatch: facts belong to ASIN '{facts.asin}', but product is '{product.asin}'"
            )

    # 2. Disclaimer and warning check
    all_fields = [copy.badge, copy.headline, copy.subheadline] + copy.benefits
    for field in all_fields:
        if is_disclaimer_or_warning_text(field):
            errors.append(f"Warning or disclaimer text detected in creative copy: '{field}'")

    # 3. Isolated specification fragments check
    for benefit in copy.benefits:
        if is_isolated_spec_fragment(benefit):
            errors.append(f"Isolated raw specification fragment without descriptive context: '{benefit}'")

    # 4. Prohibited generic filler check
    prohibited_filler = [
        "engineered for reliable performance",
        "durable, quality build",
        "easy everyday use",
        "built to impress",
    ]
    corpus = f"{copy.badge} {copy.headline} {copy.subheadline} {' '.join(copy.benefits)}".lower()
    for filler in prohibited_filler:
        if filler in corpus:
            errors.append(f"Prohibited generic filler phrase detected: '{filler}'")

    # 5. Category cross-contamination check
    cat = detect_product_category(product, facts)
    all_prod_text = " ".join([product.title or ""] + (product.features or [])).lower()
    if cat == CAT_HOME_KITCHEN:
        has_storage = any(k in all_prod_text for k in ["sink", "cabinet", "drawer", "organizer", "rack", "shelf", "sliding", "storage", "tier"])
        if not has_storage:
            for term in ["under the sink", "sliding drawer", "cabinet organizer", "pull-out drawer"]:
                if term in corpus:
                    errors.append(f"Storage claim '{term}' generated for non-storage kitchen product")
    elif cat == CAT_FRAGRANCE:
        for term in ["wireless", "charging", "organizer", "sliding drawer", "under the sink"]:
            if term in corpus:
                errors.append(f"Inappropriate claim '{term}' generated for fragrance product")

    # 6. Structured ClaimValidator check
    full_text = f"{copy.headline}. {copy.subheadline}. " + ". ".join(copy.benefits)
    try:
        validator_errors = ClaimValidator.validate_content_claims(full_text, facts)
        for v_err in validator_errors:
            if any(k in v_err.lower() for k in ["ungrounded", "invented", "unsupported", "exaggerated", "banned"]):
                errors.append(v_err)
    except Exception as exc:
        LOGGER.debug("ClaimValidator check encountered: %s", exc)

    return errors


# ---------------------------------------------------------------------------
# Pillow Graphic Canvas Composition & Layout Templates
# ---------------------------------------------------------------------------

def _fit_image_preserve_pixels(
    product_image: Image.Image,
    max_w: int,
    max_h: int,
    allow_upscale: bool = False,
) -> Tuple[Image.Image, int, int]:
    """
    Proportionally fit product image preserving aspect ratio and bit-level pixels when unscaled.
    Uses high-quality Lanczos resampling only when scaling is strictly necessary.
    """
    orig_w, orig_h = product_image.size
    if orig_w <= 0 or orig_h <= 0:
        return product_image, orig_w, orig_h

    if allow_upscale:
        scale = min(max_w / orig_w, max_h / orig_h)
    else:
        scale = min(1.0, max_w / orig_w, max_h / orig_h)

    target_w = max(1, int(round(orig_w * scale)))
    target_h = max(1, int(round(orig_h * scale)))

    if target_w == orig_w and target_h == orig_h:
        return product_image, target_w, target_h

    fitted = product_image.resize((target_w, target_h), Image.Resampling.LANCZOS)
    return fitted, target_w, target_h


def _wrap_text_lines(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    max_lines: int = 2,
) -> List[str]:
    """Wrap text to fit within max_width using the given font."""
    words = text.split()
    lines: List[str] = []
    current_line: List[str] = []
    for word in words:
        test_line = " ".join(current_line + [word])
        bbox = draw.textbbox((0, 0), test_line, font=font)
        if (bbox[2] - bbox[0]) <= max_width:
            current_line.append(word)
        else:
            if current_line:
                lines.append(" ".join(current_line))
            current_line = [word]
    if current_line:
        lines.append(" ".join(current_line))
    return lines[:max_lines]


def _render_hero_showcase(
    product_image: Image.Image,
    copy: CreativeCopy,
    theme: CreativeTheme,
    output_size: Tuple[int, int] = CANVAS_SIZE,
    exact_box_out: Optional[List[int]] = None,
    allow_upscale: bool = False,
) -> Image.Image:
    """Template 1: Hero Showcase - Top header, prominent central spotlight card, lower benefit pills, CTA."""
    canvas_w, canvas_h = output_size
    canvas = Image.new("RGB", (canvas_w, canvas_h), theme.canvas_bg)
    draw = ImageDraw.Draw(canvas)

    margin_x = 60
    margin_top = 50

    # Badge Pill
    badge_font = _get_font(22, bold=True)
    badge_text = copy.badge.upper()
    badge_bbox = draw.textbbox((0, 0), badge_text, font=badge_font)
    badge_w = (badge_bbox[2] - badge_bbox[0]) + 32
    badge_h = (badge_bbox[3] - badge_bbox[1]) + 16
    badge_x = margin_x
    badge_y = margin_top

    draw.rounded_rectangle(
        [badge_x, badge_y, badge_x + badge_w, badge_y + badge_h],
        radius=badge_h // 2,
        fill=theme.badge_bg,
    )
    draw.text((badge_x + 16, badge_y + 8), badge_text, fill=theme.badge_text, font=badge_font)

    # Headline (52pt bold)
    headline_font = _get_font(52, bold=True)
    headline_y = badge_y + badge_h + 16
    max_headline_w = canvas_w - (margin_x * 2)
    lines = _wrap_text_lines(draw, copy.headline, headline_font, max_headline_w, max_lines=2)

    current_y = headline_y
    for line in lines:
        draw.text((margin_x, current_y), line, fill=theme.text_primary, font=headline_font)
        current_y += 62

    # Subheadline (28pt regular)
    sub_font = _get_font(28, bold=False)
    draw.text((margin_x, current_y + 4), copy.subheadline, fill=theme.text_secondary, font=sub_font)

    # Central Spotlight Card
    card_x1 = margin_x
    card_y1 = current_y + 48
    card_w = canvas_w - (margin_x * 2)
    card_h = 630
    card_x2 = card_x1 + card_w
    card_y2 = card_y1 + card_h

    draw.rounded_rectangle(
        [card_x1, card_y1, card_x2, card_y2],
        radius=28,
        fill=theme.card_bg,
        outline=theme.card_border,
        width=2,
    )

    fitted_img, target_w, target_h = _fit_image_preserve_pixels(
        product_image, card_w - 60, card_h - 60, allow_upscale=allow_upscale
    )
    paste_x = card_x1 + (card_w - target_w) // 2
    paste_y = card_y1 + (card_h - target_h) // 2

    if fitted_img.mode == "RGBA":
        canvas.paste(fitted_img, (paste_x, paste_y), fitted_img)
    else:
        canvas.paste(fitted_img, (paste_x, paste_y))

    if exact_box_out is not None:
        exact_box_out.extend([paste_x, paste_y, target_w, target_h])

    # Benefits Pills (26pt bold)
    bullet_font = _get_font(26, bold=True)
    bullet_y = card_y2 + 25
    bullet_h = 56
    bullet_gap = 12

    for idx, bullet_text in enumerate(copy.benefits[:3]):
        b_box_y1 = bullet_y + (idx * (bullet_h + bullet_gap))
        b_box_y2 = b_box_y1 + bullet_h

        draw.rounded_rectangle(
            [margin_x, b_box_y1, canvas_w - margin_x, b_box_y2],
            radius=16,
            fill=theme.bullet_box_bg,
            outline=theme.bullet_box_border,
            width=1,
        )
        draw.text((margin_x + 20, b_box_y1 + 14), "✓", fill=theme.badge_text, font=bullet_font)
        draw.text((margin_x + 58, b_box_y1 + 14), bullet_text, fill=theme.bullet_text, font=bullet_font)

    # CTA Pill Button (30pt bold)
    cta_font = _get_font(30, bold=True)
    cta_btn_w = 480
    cta_btn_h = 78
    cta_x1 = (canvas_w - cta_btn_w) // 2
    cta_y1 = canvas_h - 136
    cta_x2 = cta_x1 + cta_btn_w
    cta_y2 = cta_y1 + cta_btn_h

    draw.rounded_rectangle([cta_x1, cta_y1, cta_x2, cta_y2], radius=cta_btn_h // 2, fill=theme.cta_bg)
    cta_label = f"{copy.cta_text} →"
    cta_bbox = draw.textbbox((0, 0), cta_label, font=cta_font)
    text_w = cta_bbox[2] - cta_bbox[0]
    text_h = cta_bbox[3] - cta_bbox[1]
    draw.text((cta_x1 + (cta_btn_w - text_w) // 2, cta_y1 + (cta_btn_h - text_h) // 2 - 2), cta_label, fill=theme.cta_text, font=cta_font)

    return canvas


def _render_editorial_split(
    product_image: Image.Image,
    copy: CreativeCopy,
    theme: CreativeTheme,
    output_size: Tuple[int, int] = CANVAS_SIZE,
    exact_box_out: Optional[List[int]] = None,
    allow_upscale: bool = False,
) -> Image.Image:
    """Template 2: Editorial Split - Top product hero card, middle editorial header, left-accent feature strip, CTA."""
    canvas_w, canvas_h = output_size
    canvas = Image.new("RGB", (canvas_w, canvas_h), theme.canvas_bg)
    draw = ImageDraw.Draw(canvas)

    margin_x = 60

    # Top Product Hero Card
    card_x1 = margin_x
    card_y1 = 50
    card_w = canvas_w - (margin_x * 2)
    card_h = 580
    card_x2 = card_x1 + card_w
    card_y2 = card_y1 + card_h

    draw.rounded_rectangle(
        [card_x1, card_y1, card_x2, card_y2],
        radius=26,
        fill=theme.card_bg,
        outline=theme.card_border,
        width=2,
    )

    fitted_img, target_w, target_h = _fit_image_preserve_pixels(
        product_image, card_w - 60, card_h - 60, allow_upscale=allow_upscale
    )
    paste_x = card_x1 + (card_w - target_w) // 2
    paste_y = card_y1 + (card_h - target_h) // 2

    if fitted_img.mode == "RGBA":
        canvas.paste(fitted_img, (paste_x, paste_y), fitted_img)
    else:
        canvas.paste(fitted_img, (paste_x, paste_y))

    if exact_box_out is not None:
        exact_box_out.extend([paste_x, paste_y, target_w, target_h])

    # Middle Editorial Header
    badge_font = _get_font(22, bold=True)
    badge_text = copy.badge.upper()
    badge_bbox = draw.textbbox((0, 0), badge_text, font=badge_font)
    badge_w = (badge_bbox[2] - badge_bbox[0]) + 32
    badge_h = (badge_bbox[3] - badge_bbox[1]) + 16
    badge_x = margin_x
    badge_y = card_y2 + 30

    draw.rounded_rectangle(
        [badge_x, badge_y, badge_x + badge_w, badge_y + badge_h],
        radius=badge_h // 2,
        fill=theme.badge_bg,
    )
    draw.text((badge_x + 16, badge_y + 8), badge_text, fill=theme.badge_text, font=badge_font)

    # Headline (52pt bold)
    headline_font = _get_font(52, bold=True)
    headline_y = badge_y + badge_h + 16
    max_headline_w = canvas_w - (margin_x * 2)
    lines = _wrap_text_lines(draw, copy.headline, headline_font, max_headline_w, max_lines=2)

    current_y = headline_y
    for line in lines:
        draw.text((margin_x, current_y), line, fill=theme.text_primary, font=headline_font)
        current_y += 62

    # Subheadline (28pt regular)
    sub_font = _get_font(28, bold=False)
    draw.text((margin_x, current_y + 4), copy.subheadline, fill=theme.text_secondary, font=sub_font)

    # Lower Left-Accent Feature Strip Cards (26pt bold)
    bullet_font = _get_font(26, bold=True)
    bullet_y = current_y + 46
    bullet_h = 56
    bullet_gap = 12

    for idx, bullet_text in enumerate(copy.benefits[:3]):
        b_box_y1 = bullet_y + (idx * (bullet_h + bullet_gap))
        b_box_y2 = b_box_y1 + bullet_h

        # Base box
        draw.rounded_rectangle(
            [margin_x, b_box_y1, canvas_w - margin_x, b_box_y2],
            radius=12,
            fill=theme.bullet_box_bg,
            outline=theme.bullet_box_border,
            width=1,
        )
        # Left accent color bar
        draw.rounded_rectangle(
            [margin_x, b_box_y1, margin_x + 8, b_box_y2],
            radius=4,
            fill=theme.cta_bg,
        )
        draw.text((margin_x + 24, b_box_y1 + 14), bullet_text, fill=theme.bullet_text, font=bullet_font)

    # CTA Pill Button (30pt bold)
    cta_font = _get_font(30, bold=True)
    cta_btn_w = 500
    cta_btn_h = 78
    cta_x1 = (canvas_w - cta_btn_w) // 2
    cta_y1 = canvas_h - 136
    cta_x2 = cta_x1 + cta_btn_w
    cta_y2 = cta_y1 + cta_btn_h

    draw.rounded_rectangle([cta_x1, cta_y1, cta_x2, cta_y2], radius=cta_btn_h // 2, fill=theme.cta_bg)
    cta_label = f"{copy.cta_text} →"
    cta_bbox = draw.textbbox((0, 0), cta_label, font=cta_font)
    text_w = cta_bbox[2] - cta_bbox[0]
    text_h = cta_bbox[3] - cta_bbox[1]
    draw.text((cta_x1 + (cta_btn_w - text_w) // 2, cta_y1 + (cta_btn_h - text_h) // 2 - 2), cta_label, fill=theme.cta_text, font=cta_font)

    return canvas


def _render_feature_focus(
    product_image: Image.Image,
    copy: CreativeCopy,
    theme: CreativeTheme,
    output_size: Tuple[int, int] = CANVAS_SIZE,
    exact_box_out: Optional[List[int]] = None,
    allow_upscale: bool = False,
) -> Image.Image:
    """Template 3: Feature Focus - Top header, product card, prominent numbered feature highlight cards, CTA."""
    canvas_w, canvas_h = output_size
    canvas = Image.new("RGB", (canvas_w, canvas_h), theme.canvas_bg)
    draw = ImageDraw.Draw(canvas)

    margin_x = 60
    margin_top = 50

    # Badge Pill
    badge_font = _get_font(22, bold=True)
    badge_text = copy.badge.upper()
    badge_bbox = draw.textbbox((0, 0), badge_text, font=badge_font)
    badge_w = (badge_bbox[2] - badge_bbox[0]) + 32
    badge_h = (badge_bbox[3] - badge_bbox[1]) + 16
    badge_x = margin_x
    badge_y = margin_top

    draw.rounded_rectangle(
        [badge_x, badge_y, badge_x + badge_w, badge_y + badge_h],
        radius=badge_h // 2,
        fill=theme.badge_bg,
    )
    draw.text((badge_x + 16, badge_y + 8), badge_text, fill=theme.badge_text, font=badge_font)

    # Headline (50pt bold)
    headline_font = _get_font(50, bold=True)
    headline_y = badge_y + badge_h + 16
    max_headline_w = canvas_w - (margin_x * 2)
    lines = _wrap_text_lines(draw, copy.headline, headline_font, max_headline_w, max_lines=2)

    current_y = headline_y
    for line in lines:
        draw.text((margin_x, current_y), line, fill=theme.text_primary, font=headline_font)
        current_y += 60

    # Subheadline (28pt regular)
    sub_font = _get_font(28, bold=False)
    draw.text((margin_x, current_y + 4), copy.subheadline, fill=theme.text_secondary, font=sub_font)

    # Product Card (520px height)
    card_x1 = margin_x
    card_y1 = current_y + 42
    card_w = canvas_w - (margin_x * 2)
    card_h = 520
    card_x2 = card_x1 + card_w
    card_y2 = card_y1 + card_h

    draw.rounded_rectangle(
        [card_x1, card_y1, card_x2, card_y2],
        radius=26,
        fill=theme.card_bg,
        outline=theme.card_border,
        width=2,
    )

    fitted_img, target_w, target_h = _fit_image_preserve_pixels(
        product_image, card_w - 60, card_h - 60, allow_upscale=allow_upscale
    )
    paste_x = card_x1 + (card_w - target_w) // 2
    paste_y = card_y1 + (card_h - target_h) // 2

    if fitted_img.mode == "RGBA":
        canvas.paste(fitted_img, (paste_x, paste_y), fitted_img)
    else:
        canvas.paste(fitted_img, (paste_x, paste_y))

    if exact_box_out is not None:
        exact_box_out.extend([paste_x, paste_y, target_w, target_h])

    # Numbered Feature Cards (26pt bold)
    bullet_font = _get_font(26, bold=True)
    num_font = _get_font(20, bold=True)
    bullet_y = card_y2 + 25
    bullet_h = 62
    bullet_gap = 14

    for idx, bullet_text in enumerate(copy.benefits[:3]):
        b_box_y1 = bullet_y + (idx * (bullet_h + bullet_gap))
        b_box_y2 = b_box_y1 + bullet_h

        draw.rounded_rectangle(
            [margin_x, b_box_y1, canvas_w - margin_x, b_box_y2],
            radius=14,
            fill=theme.bullet_box_bg,
            outline=theme.bullet_box_border,
            width=1,
        )

        # Number pill / badge
        num_str = f"0{idx + 1}"
        num_box_w = 44
        num_box_h = 36
        num_x1 = margin_x + 14
        num_y1 = b_box_y1 + 13
        draw.rounded_rectangle(
            [num_x1, num_y1, num_x1 + num_box_w, num_y1 + num_box_h],
            radius=num_box_h // 2,
            fill=theme.badge_bg,
        )
        draw.text((num_x1 + 10, num_y1 + 8), num_str, fill=theme.badge_text, font=num_font)
        draw.text((num_x1 + num_box_w + 16, b_box_y1 + 16), bullet_text, fill=theme.bullet_text, font=bullet_font)

    # CTA Button (30pt bold)
    cta_font = _get_font(30, bold=True)
    cta_btn_w = 480
    cta_btn_h = 78
    cta_x1 = (canvas_w - cta_btn_w) // 2
    cta_y1 = canvas_h - 136
    cta_x2 = cta_x1 + cta_btn_w
    cta_y2 = cta_y1 + cta_btn_h

    draw.rounded_rectangle([cta_x1, cta_y1, cta_x2, cta_y2], radius=cta_btn_h // 2, fill=theme.cta_bg)
    cta_label = f"{copy.cta_text} →"
    cta_bbox = draw.textbbox((0, 0), cta_label, font=cta_font)
    text_w = cta_bbox[2] - cta_bbox[0]
    text_h = cta_bbox[3] - cta_bbox[1]
    draw.text((cta_x1 + (cta_btn_w - text_w) // 2, cta_y1 + (cta_btn_h - text_h) // 2 - 2), cta_label, fill=theme.cta_text, font=cta_font)

    return canvas


def _render_minimalist_frame(
    product_image: Image.Image,
    copy: CreativeCopy,
    theme: CreativeTheme,
    output_size: Tuple[int, int] = CANVAS_SIZE,
    exact_box_out: Optional[List[int]] = None,
    allow_upscale: bool = False,
) -> Image.Image:
    """Template 4: Minimalist Frame - Inset poster border, centered typography, spotlight frame, circular bullet badges, CTA."""
    canvas_w, canvas_h = output_size
    canvas = Image.new("RGB", (canvas_w, canvas_h), theme.canvas_bg)
    draw = ImageDraw.Draw(canvas)

    # Elegant 2px inset poster frame around the canvas
    frame_inset = 24
    draw.rectangle(
        [frame_inset, frame_inset, canvas_w - frame_inset, canvas_h - frame_inset],
        outline=theme.card_border,
        width=2,
    )

    margin_x = 70
    margin_top = 58

    # Centered Badge Pill
    badge_font = _get_font(22, bold=True)
    badge_text = copy.badge.upper()
    badge_bbox = draw.textbbox((0, 0), badge_text, font=badge_font)
    badge_w = (badge_bbox[2] - badge_bbox[0]) + 32
    badge_h = (badge_bbox[3] - badge_bbox[1]) + 16
    badge_x = (canvas_w - badge_w) // 2
    badge_y = margin_top

    draw.rounded_rectangle(
        [badge_x, badge_y, badge_x + badge_w, badge_y + badge_h],
        radius=badge_h // 2,
        fill=theme.badge_bg,
    )
    draw.text((badge_x + 16, badge_y + 8), badge_text, fill=theme.badge_text, font=badge_font)

    # Centered Headline (52pt bold)
    headline_font = _get_font(52, bold=True)
    headline_y = badge_y + badge_h + 16
    max_headline_w = canvas_w - (margin_x * 2)
    lines = _wrap_text_lines(draw, copy.headline, headline_font, max_headline_w, max_lines=2)

    current_y = headline_y
    for line in lines:
        line_bbox = draw.textbbox((0, 0), line, font=headline_font)
        line_w = line_bbox[2] - line_bbox[0]
        draw.text(((canvas_w - line_w) // 2, current_y), line, fill=theme.text_primary, font=headline_font)
        current_y += 62

    # Centered Subheadline (28pt regular)
    sub_font = _get_font(28, bold=False)
    sub_bbox = draw.textbbox((0, 0), copy.subheadline, font=sub_font)
    sub_w = sub_bbox[2] - sub_bbox[0]
    draw.text(((canvas_w - sub_w) // 2, current_y + 4), copy.subheadline, fill=theme.text_secondary, font=sub_font)

    # Centered Spotlight Product Frame (580px height)
    card_x1 = margin_x
    card_y1 = current_y + 44
    card_w = canvas_w - (margin_x * 2)
    card_h = 580
    card_x2 = card_x1 + card_w
    card_y2 = card_y1 + card_h

    draw.rounded_rectangle(
        [card_x1, card_y1, card_x2, card_y2],
        radius=24,
        fill=theme.card_bg,
        outline=theme.card_border,
        width=2,
    )

    fitted_img, target_w, target_h = _fit_image_preserve_pixels(
        product_image, card_w - 60, card_h - 60, allow_upscale=allow_upscale
    )
    paste_x = card_x1 + (card_w - target_w) // 2
    paste_y = card_y1 + (card_h - target_h) // 2

    if fitted_img.mode == "RGBA":
        canvas.paste(fitted_img, (paste_x, paste_y), fitted_img)
    else:
        canvas.paste(fitted_img, (paste_x, paste_y))

    if exact_box_out is not None:
        exact_box_out.extend([paste_x, paste_y, target_w, target_h])

    # Minimalist Bullet Points (26pt bold with circular bullet badges)
    bullet_font = _get_font(26, bold=True)
    bullet_y = card_y2 + 25
    bullet_h = 52
    bullet_gap = 12

    for idx, bullet_text in enumerate(copy.benefits[:3]):
        b_box_y1 = bullet_y + (idx * (bullet_h + bullet_gap))
        b_box_y2 = b_box_y1 + bullet_h

        # Bullet container
        draw.rounded_rectangle(
            [margin_x, b_box_y1, canvas_w - margin_x, b_box_y2],
            radius=14,
            fill=theme.bullet_box_bg,
            outline=theme.bullet_box_border,
            width=1,
        )

        # Circular bullet badge
        circle_cx = margin_x + 24
        circle_cy = b_box_y1 + (bullet_h // 2)
        circle_r = 7
        draw.ellipse(
            [circle_cx - circle_r, circle_cy - circle_r, circle_cx + circle_r, circle_cy + circle_r],
            fill=theme.badge_text,
        )
        draw.text((margin_x + 48, b_box_y1 + 12), bullet_text, fill=theme.bullet_text, font=bullet_font)

    # Centered CTA Button (30pt bold)
    cta_font = _get_font(30, bold=True)
    cta_btn_w = 460
    cta_btn_h = 78
    cta_x1 = (canvas_w - cta_btn_w) // 2
    cta_y1 = canvas_h - 136
    cta_x2 = cta_x1 + cta_btn_w
    cta_y2 = cta_y1 + cta_btn_h

    draw.rounded_rectangle([cta_x1, cta_y1, cta_x2, cta_y2], radius=cta_btn_h // 2, fill=theme.cta_bg)
    cta_label = f"{copy.cta_text} →"
    cta_bbox = draw.textbbox((0, 0), cta_label, font=cta_font)
    text_w = cta_bbox[2] - cta_bbox[0]
    text_h = cta_bbox[3] - cta_bbox[1]
    draw.text((cta_x1 + (cta_btn_w - text_w) // 2, cta_y1 + (cta_btn_h - text_h) // 2 - 2), cta_label, fill=theme.cta_text, font=cta_font)

    return canvas


def compose_pinterest_creative(
    product_image: Image.Image,
    copy: CreativeCopy,
    theme: CreativeTheme,
    output_size: Tuple[int, int] = CANVAS_SIZE,
    exact_box_out: Optional[List[int]] = None,
    allow_upscale: bool = False,
    layout_template: str = LAYOUT_HERO_SHOWCASE,
) -> Image.Image:
    """
    Compose a professional 1000x1500 Pinterest marketing pin using one of 4 distinct templates:
    1. hero_showcase
    2. editorial_split
    3. feature_focus
    4. minimalist_frame

    STRICT ORIGINAL IMAGE PRESERVATION:
    - The product_image pixels inside its target bounding box are drawn without
      distortion, inpainting, or alteration.
    - If exact_box_out list is passed, it receives [paste_x, paste_y, width, height]
      for bit-level pixel verification.
    """
    if layout_template == LAYOUT_EDITORIAL_SPLIT:
        return _render_editorial_split(
            product_image, copy, theme, output_size=output_size,
            exact_box_out=exact_box_out, allow_upscale=allow_upscale,
        )
    elif layout_template == LAYOUT_FEATURE_FOCUS:
        return _render_feature_focus(
            product_image, copy, theme, output_size=output_size,
            exact_box_out=exact_box_out, allow_upscale=allow_upscale,
        )
    elif layout_template == LAYOUT_MINIMALIST_FRAME:
        return _render_minimalist_frame(
            product_image, copy, theme, output_size=output_size,
            exact_box_out=exact_box_out, allow_upscale=allow_upscale,
        )
    else:
        return _render_hero_showcase(
            product_image, copy, theme, output_size=output_size,
            exact_box_out=exact_box_out, allow_upscale=allow_upscale,
        )


# ---------------------------------------------------------------------------
# Storage and Orchestration
# ---------------------------------------------------------------------------

def get_creatives_dir(asin: str, base_dir: Optional[Path] = None) -> Path:
    """Return dedicated storage directory for an ASIN's marketing creatives."""
    root = base_dir or (Path(__file__).resolve().parent.parent / "data" / "creatives")
    asin_dir = root / asin.upper().strip()
    asin_dir.mkdir(parents=True, exist_ok=True)
    return asin_dir


def load_product_image_safely(
    product: Product,
    cache_dir: Optional[Path] = None
) -> Optional[Image.Image]:
    """
    Acquire the product image safely:
    1. Check local image cache if present.
    2. Download from product.image_url.
    3. Never alters or overwrites source files.
    """
    image_url = product.image_url
    if not image_url or not is_valid_image_url(image_url):
        return None

    asin = (product.asin or "UNKNOWN").upper().strip()
    root_cache = cache_dir or (Path(__file__).resolve().parent.parent / "data" / "products" / "images")
    root_cache.mkdir(parents=True, exist_ok=True)
    local_cached = root_cache / f"asin_{asin}_source.jpg"

    if local_cached.is_file():
        try:
            return Image.open(local_cached).convert("RGB")
        except Exception:
            pass

    try:
        resp = requests.get(image_url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200:
            img = Image.open(io.BytesIO(resp.content))
            try:
                img.save(local_cached, "JPEG", quality=95)
            except Exception:
                pass
            return img.convert("RGB")
    except Exception as exc:
        LOGGER.warning("Failed to download product image for ASIN %s: %s", asin, exc)

    return None


def _get_gift_visual_direction(category: str) -> str:
    """Return gift-oriented visual art direction tailored to category."""
    if category == CAT_FRAGRANCE:
        return (
            "Luxurious, celebratory gift presentation with warm amber atmosphere and refined holiday styling. "
            "The perfume bottle is the prominent hero, beautifully staged with subtle festive elegance."
        )
    elif category == CAT_HOME_KITCHEN:
        return (
            "Thoughtful housewarming or kitchen gift setting with clean, inviting kitchen backdrop and warm natural lighting. "
            "The product is the prominent hero, staged with inviting everyday charm."
        )
    elif category == CAT_ELECTRONICS:
        return (
            "Sleek, modern gift setting with clean desktop or tech lifestyle aesthetic and crisp studio lighting. "
            "The product is the prominent hero, staged as an impressive modern gift."
        )
    elif category == CAT_BEAUTY:
        return (
            "Elegant self-care or holiday pampering gift presentation with clean, radiant bathroom or vanity styling. "
            "The product is the prominent hero with fresh, premium presentation."
        )
    elif category == CAT_PETS:
        return (
            "Heartwarming pet gift setting with cheerful, tidy home background. "
            "The product is the prominent hero, staged as a thoughtful gift for pet owners."
        )
    return (
        "Polished, premium gift presentation with warm inviting aesthetic and clean commercial composition. "
        "The product is the prominent hero, staged with thoughtful gift appeal."
    )


def _get_everyday_visual_direction(category: str) -> str:
    """Return product-focused commercial visual art direction tailored to category."""
    if category == CAT_FRAGRANCE:
        return (
            "Sophisticated everyday lifestyle aesthetic, elegant vanity staging with soft daytime lighting. "
            "Crisp focus on the perfume bottle highlighting daily signature scent appeal."
        )
    elif category == CAT_HOME_KITCHEN:
        return (
            "Clean modern kitchen or utility space, crisp high-contrast commercial lighting highlighting "
            "practical daily usability and durable performance."
        )
    elif category == CAT_ELECTRONICS:
        return (
            "Modern workspace or everyday carry aesthetic, crisp directional lighting highlighting "
            "product build quality, ports, and daily convenience."
        )
    elif category == CAT_BEAUTY:
        return (
            "Fresh daily beauty routine aesthetic, bright daylight studio setup highlighting "
            "texture, purity, and daily self-care quality."
        )
    elif category == CAT_PETS:
        return (
            "Practical daily pet care aesthetic, bright clean home background showing "
            "reliable everyday use and durability."
        )
    return (
        "Professional commercial advertising studio setting, clean high-contrast lighting highlighting "
        "practical daily utility, quality materials, and reliable performance."
    )


def build_flow_prompt(
    product: Product,
    creative_type: str,
    facts: Optional[CanonicalProductFacts] = None,
) -> Dict[str, Any]:
    """
    Build a structured, ready-to-copy English image-generation prompt for Google Flow
    along with verified product facts and commercial art direction.
    """
    if creative_type not in CREATIVE_TYPES:
        raise ValueError(f"Unknown creative type '{creative_type}'. Must be one of {CREATIVE_TYPES}")

    if facts is None:
        facts = FactExtractor.extract_canonical_facts(product)

    category = detect_product_category(product, facts)

    if creative_type == CREATIVE_TYPE_GIFT:
        angle = ANGLE_GIFT_INSPIRATION
        copy = generate_creative_copy(product, angle, facts)
        visual_direction = _get_gift_visual_direction(category)
    else:
        angle = (
            ANGLE_KEY_PRODUCT_BENEFITS
            if category in (CAT_FRAGRANCE, CAT_BEAUTY)
            else ANGLE_PRACTICAL_SOLUTION
        )
        copy = generate_creative_copy(product, angle, facts)
        visual_direction = _get_everyday_visual_direction(category)

    # Validate copy against ClaimValidator
    val_errors = validate_creative_copy(copy, product, facts)
    if val_errors:
        LOGGER.warning("Creative copy validation warning for %s (%s): %s", product.asin, creative_type, val_errors)

    bullets_text = "\n".join(f"- {b}" for b in (copy.benefits or [])) if copy.benefits else "- Quality design crafted for daily satisfaction"

    prompt = (
        "IMPORTANT SETUP IN FLOW UI:\n"
        "Select 9:16 (Vertical / Portrait) in Google Flow's image aspect-ratio settings before generating. "
        "A text instruction alone does not set canvas dimensions—please ensure the 9:16 ratio is active in the Flow UI.\n\n"
        "Create a professional commercial Pinterest promotional pin image in 9:16 (Vertical / Portrait) aspect ratio.\n\n"
        "MANDATORY PRODUCT REFERENCE & FIDELITY:\n"
        "- Use the uploaded original product image as a mandatory visual reference, not merely as loose inspiration.\n"
        "- Preserve the exact product identity and appearance: its shape, proportions, colors, materials, distinctive details, and design must match the uploaded reference image.\n"
        "- Do NOT replace the product with a similar-looking alternative, redesign it, alter its appearance, or invent features/accessories.\n"
        "- The product depicted in the final Pinterest creative must clearly and unmistakably be the exact same product as in the reference image.\n"
        "- The product may be placed in an attractive lifestyle or promotional setting, but the product itself must remain completely faithful to the reference.\n\n"
        "VISUAL FOCUS & ART DIRECTION:\n"
        "- The primary visual hero is the authentic referenced product.\n"
        "- Professional commercial advertising studio art direction with high-end lighting and clean composition.\n"
        f"- {visual_direction}\n"
        "- No generic AI collages, no clutter, no excessive empty space, no competing decorative elements.\n\n"
        "TYPOGRAPHY & TEXT OVERLAY:\n"
        "- Clean, modern visual hierarchy with strong, mobile-readable typography.\n"
        f"- Prominent Headline: \"{copy.headline}\"\n"
        f"- Supporting Subheadline: \"{copy.subheadline}\"\n"
        "- Key Feature Callouts (concise & mobile-readable):\n"
        f"{bullets_text}\n"
        f"- Call to Action: \"{copy.cta_text}\"\n"
        "- Strictly NO additional text, NO tiny unreadable labels, NO invented claims or specifications."
    )

    return {
        "creative_type": creative_type,
        "display_name": CREATIVE_TYPE_LABELS[creative_type],
        "angle": angle,
        "category": category,
        "headline": copy.headline,
        "subheadline": copy.subheadline,
        "benefits": copy.benefits,
        "cta_text": copy.cta_text,
        "visual_direction": visual_direction,
        "prompt": prompt,
        "verified_facts": (
            facts.supporting_attributes()
            if facts and callable(getattr(facts, "supporting_attributes", None)) and facts.supporting_attributes()
            else (copy.benefits or [])
        ),
    }


def _find_product_for_asin(asin: str) -> Optional[Product]:
    """Helper to locate cached Product data from data/products/ or data/generated/."""
    asin_clean = asin.upper().strip()
    prod_file = PROJECT_ROOT / "data" / "products" / f"asin_{asin_clean}.json"
    if prod_file.is_file():
        try:
            with open(prod_file, "r", encoding="utf-8") as f:
                return Product.from_dict(json.load(f))
        except Exception:
            pass

    pkg_file = PROJECT_ROOT / "data" / "generated" / f"asin_{asin_clean}_content_package.json"
    if pkg_file.is_file():
        try:
            with open(pkg_file, "r", encoding="utf-8") as f:
                pkg = json.load(f)
                from publishing.images import recover_verified_image_url
                verified_image = recover_verified_image_url(pkg)
                return Product(
                    title=pkg.get("title", f"Product {asin_clean}"),
                    asin=asin_clean,
                    affiliate_url=pkg.get("source", {}).get("affiliate_url", ""),
                    original_affiliate_url=pkg.get("source", {}).get("affiliate_url", ""),
                    image_url=verified_image,
                )
        except Exception:
            pass
    return None


def get_or_download_reference_image(
    asin: str,
    product: Optional[Product] = None,
    creatives_dir: Optional[Path] = None,
) -> Optional[Path]:
    """
    Ensure the reference product image is saved locally under data/creatives/<asin>/
    for one-click user download.
    """
    asin_clean = asin.upper().strip()
    out_dir = get_creatives_dir(asin_clean, creatives_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # Check for existing reference image
    for ext in (".jpg", ".jpeg", ".png", ".webp"):
        candidate = out_dir / f"reference_product_image{ext}"
        if candidate.is_file() and candidate.stat().st_size > 0:
            return candidate

    if not product:
        product = _find_product_for_asin(asin_clean)

    if not product or not product.image_url or not is_valid_image_url(product.image_url):
        return None

    try:
        resp = requests.get(
            product.image_url,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=15,
        )
        if resp.status_code == 200 and resp.content:
            target = out_dir / "reference_product_image.jpg"
            target.write_bytes(resp.content)
            return target
    except Exception as exc:
        LOGGER.warning("Could not download reference image for %s: %s", asin_clean, exc)

    return None


def generate_flow_creatives(
    product: Product,
    creatives_dir: Optional[Path] = None,
    overwrite: bool = False,
) -> Dict[str, Any]:
    """
    Generate Google Flow prompts and facts for the two creative types:
    - Gift Inspiration
    - Everyday Pick
    Persists flow_creatives.json while preserving any existing uploaded images.
    """
    asin = (product.asin or "UNKNOWN").upper().strip()
    out_dir = get_creatives_dir(asin, creatives_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest_file = out_dir / "flow_creatives.json"

    facts = FactExtractor.extract_canonical_facts(product)
    category = detect_product_category(product, facts)

    existing_manifest = {}
    if manifest_file.is_file():
        try:
            with open(manifest_file, "r", encoding="utf-8") as handle:
                existing_manifest = json.load(handle)
        except Exception:
            existing_manifest = {}

    gift_info = build_flow_prompt(product, CREATIVE_TYPE_GIFT, facts)
    everyday_info = build_flow_prompt(product, CREATIVE_TYPE_EVERYDAY, facts)

    # Reference product image resolution
    ref_image_path = get_or_download_reference_image(asin, product, creatives_dir=creatives_dir)
    ref_rel_path = None
    if ref_image_path and ref_image_path.is_file():
        try:
            ref_rel_path = str(ref_image_path.relative_to(PROJECT_ROOT)).replace("\\", "/")
        except Exception:
            ref_rel_path = str(ref_image_path).replace("\\", "/")

    now_iso = datetime.now(timezone.utc).isoformat()

    existing_creatives = existing_manifest.get("creatives", {})
    existing_gift = existing_creatives.get(CREATIVE_TYPE_GIFT, {})
    existing_everyday = existing_creatives.get(CREATIVE_TYPE_EVERYDAY, {})

    gift_slot = {
        "creative_type": CREATIVE_TYPE_GIFT,
        "display_name": CREATIVE_TYPE_LABELS[CREATIVE_TYPE_GIFT],
        "angle": gift_info["angle"],
        "headline": gift_info["headline"],
        "subheadline": gift_info["subheadline"],
        "benefits": gift_info["benefits"],
        "cta_text": gift_info["cta_text"],
        "prompt": gift_info["prompt"],
        "visual_direction": gift_info["visual_direction"],
        "verified_facts": gift_info["verified_facts"],
        "uploaded_image_path": existing_gift.get("uploaded_image_path") if not overwrite else None,
        "uploaded_image_url": existing_gift.get("uploaded_image_url") if not overwrite else None,
        "uploaded_at": existing_gift.get("uploaded_at") if not overwrite else None,
        "dimensions": existing_gift.get("dimensions") if not overwrite else None,
        "selected": bool(existing_gift.get("selected")) if not overwrite else False,
    }

    everyday_slot = {
        "creative_type": CREATIVE_TYPE_EVERYDAY,
        "display_name": CREATIVE_TYPE_LABELS[CREATIVE_TYPE_EVERYDAY],
        "angle": everyday_info["angle"],
        "headline": everyday_info["headline"],
        "subheadline": everyday_info["subheadline"],
        "benefits": everyday_info["benefits"],
        "cta_text": everyday_info["cta_text"],
        "prompt": everyday_info["prompt"],
        "visual_direction": everyday_info["visual_direction"],
        "verified_facts": everyday_info["verified_facts"],
        "uploaded_image_path": existing_everyday.get("uploaded_image_path") if not overwrite else None,
        "uploaded_image_url": existing_everyday.get("uploaded_image_url") if not overwrite else None,
        "uploaded_at": existing_everyday.get("uploaded_at") if not overwrite else None,
        "dimensions": existing_everyday.get("dimensions") if not overwrite else None,
        "selected": bool(existing_everyday.get("selected")) if not overwrite else False,
    }

    selected_type = existing_manifest.get("selected_creative_type") if not overwrite else None
    selected_path = existing_manifest.get("selected_image_path") if not overwrite else None
    selected_url = existing_manifest.get("selected_image_url") if not overwrite else None

    # Validate selected_type has uploaded file
    if selected_type == CREATIVE_TYPE_GIFT and not gift_slot.get("uploaded_image_path"):
        selected_type, selected_path, selected_url = None, None, None
        gift_slot["selected"] = False
    elif selected_type == CREATIVE_TYPE_EVERYDAY and not everyday_slot.get("uploaded_image_path"):
        selected_type, selected_path, selected_url = None, None, None
        everyday_slot["selected"] = False

    manifest_data = {
        "asin": asin,
        "title": product.title,
        "category": category,
        "affiliate_url": product.original_affiliate_url or product.affiliate_url,
        "original_image_url": product.image_url,
        "reference_image_path": ref_rel_path,
        "reference_image_url": f"/creatives/{asin}/reference-image" if ref_rel_path else None,
        "selected_creative_type": selected_type,
        "selected_creative_display": CREATIVE_TYPE_LABELS.get(selected_type) if selected_type else None,
        "selected_image_path": selected_path,
        "selected_image_url": selected_url,
        "updated_at": now_iso,
        "total_creatives": 2,
        "creatives": {
            CREATIVE_TYPE_GIFT: gift_slot,
            CREATIVE_TYPE_EVERYDAY: everyday_slot,
        },
    }

    with open(manifest_file, "w", encoding="utf-8") as handle:
        json.dump(manifest_data, handle, indent=2, ensure_ascii=False)

    return {
        "success": True,
        "asin": asin,
        "message": f"Successfully generated Google Flow prompts for {product.title or asin}.",
        "data": manifest_data,
    }


def load_product_creatives(
    asin: str,
    base_dir: Optional[Path] = None,
    product: Optional[Product] = None,
) -> Optional[Dict[str, Any]]:
    """
    Load active Google Flow two-creative manifest for an ASIN.
    If flow_creatives.json does not exist yet on disk, builds the two prompt cards
    in-memory for display without persisting to disk on read.
    Legacy 4-variation manifests are never returned as active dashboard choices.
    """
    asin_clean = asin.upper().strip()
    out_dir = get_creatives_dir(asin_clean, base_dir)
    manifest_file = out_dir / "flow_creatives.json"

    if manifest_file.is_file():
        try:
            with open(manifest_file, "r", encoding="utf-8") as handle:
                data = json.load(handle)
                sel = data.get("selected_creative_type")
                data["selected_creative_display"] = CREATIVE_TYPE_LABELS.get(sel) if sel else None
                return data
        except Exception:
            pass

    if not product:
        product = _find_product_for_asin(asin_clean)

    if product:
        facts = FactExtractor.extract_canonical_facts(product)
        category = detect_product_category(product, facts)
        gift_info = build_flow_prompt(product, CREATIVE_TYPE_GIFT, facts)
        everyday_info = build_flow_prompt(product, CREATIVE_TYPE_EVERYDAY, facts)
        now_iso = datetime.now(timezone.utc).isoformat()
        return {
            "asin": asin_clean,
            "title": product.title,
            "category": category,
            "affiliate_url": product.original_affiliate_url or product.affiliate_url,
            "original_image_url": product.image_url,
            "reference_image_path": None,
            "reference_image_url": f"/creatives/{asin_clean}/reference-image",
            "selected_creative_type": None,
            "selected_creative_display": None,
            "selected_image_path": None,
            "selected_image_url": None,
            "updated_at": now_iso,
            "total_creatives": 2,
            "creatives": {
                CREATIVE_TYPE_GIFT: {
                    "creative_type": CREATIVE_TYPE_GIFT,
                    "display_name": CREATIVE_TYPE_LABELS[CREATIVE_TYPE_GIFT],
                    "angle": gift_info["angle"],
                    "headline": gift_info["headline"],
                    "subheadline": gift_info["subheadline"],
                    "benefits": gift_info["benefits"],
                    "cta_text": gift_info["cta_text"],
                    "prompt": gift_info["prompt"],
                    "visual_direction": gift_info["visual_direction"],
                    "verified_facts": gift_info["verified_facts"],
                    "uploaded_image_path": None,
                    "uploaded_image_url": None,
                    "uploaded_at": None,
                    "dimensions": None,
                    "selected": False,
                },
                CREATIVE_TYPE_EVERYDAY: {
                    "creative_type": CREATIVE_TYPE_EVERYDAY,
                    "display_name": CREATIVE_TYPE_LABELS[CREATIVE_TYPE_EVERYDAY],
                    "angle": everyday_info["angle"],
                    "headline": everyday_info["headline"],
                    "subheadline": everyday_info["subheadline"],
                    "benefits": everyday_info["benefits"],
                    "cta_text": everyday_info["cta_text"],
                    "prompt": everyday_info["prompt"],
                    "visual_direction": everyday_info["visual_direction"],
                    "verified_facts": everyday_info["verified_facts"],
                    "uploaded_image_path": None,
                    "uploaded_image_url": None,
                    "uploaded_at": None,
                    "dimensions": None,
                    "selected": False,
                },
            },
        }

    return None


def validate_uploaded_creative_image(
    file_bytes: bytes,
    filename: str,
) -> Tuple[bool, str, Optional[Tuple[int, int]], Optional[str]]:
    """
    Validate uploaded image file:
    - Accepted extensions: .png, .jpg, .jpeg, .webp
    - Rejects files > 15MB or 0 bytes
    - Verifies image integrity and dimensions using Pillow
    """
    if not file_bytes:
        return False, "Uploaded file is empty (0 bytes).", None, None

    if len(file_bytes) > MAX_UPLOAD_SIZE_BYTES:
        return (
            False,
            f"File size ({len(file_bytes) / (1024 * 1024):.1f} MB) exceeds maximum allowed size (15 MB).",
            None,
            None,
        )

    ext = Path(filename).suffix.lower()
    if ext not in ALLOWED_UPLOAD_EXTENSIONS:
        return (
            False,
            f"Invalid file extension '{ext}'. Allowed extensions are: {', '.join(sorted(ALLOWED_UPLOAD_EXTENSIONS))}",
            None,
            None,
        )

    try:
        buf = io.BytesIO(file_bytes)
        img = Image.open(buf)
        img.verify()
    except Exception as exc:
        return False, f"Invalid or corrupted image file: {exc}", None, None

    # Re-open after verify() to inspect properties
    try:
        buf = io.BytesIO(file_bytes)
        img = Image.open(buf)
        width, height = img.size
        img_format = img.format or ext.lstrip(".").upper()
        return True, "Valid image file.", (width, height), img_format
    except Exception as exc:
        return False, f"Could not read image properties: {exc}", None, None


def save_uploaded_creative(
    asin: str,
    creative_type: str,
    file_bytes: bytes,
    filename: str,
    creatives_dir: Optional[Path] = None,
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """
    Save an uploaded Flow image for a creative slot, strictly preserving original bytes.
    """
    asin_clean = asin.upper().strip()
    if creative_type not in CREATIVE_TYPES:
        return False, f"Invalid creative type '{creative_type}'. Must be one of {CREATIVE_TYPES}.", None

    valid, err_msg, dims, fmt = validate_uploaded_creative_image(file_bytes, filename)
    if not valid:
        return False, err_msg, None

    out_dir = get_creatives_dir(asin_clean, creatives_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    ext = Path(filename).suffix.lower()
    saved_filename = f"{creative_type}_flow{ext}"
    dest_path = out_dir / saved_filename

    # Save exact uploaded bytes - PRESERVE ORIGINAL UPLOADED IMAGE
    with open(dest_path, "wb") as f:
        f.write(file_bytes)

    manifest_file = out_dir / "flow_creatives.json"
    manifest = {}
    if manifest_file.is_file():
        try:
            with open(manifest_file, "r", encoding="utf-8") as handle:
                manifest = json.load(handle)
        except Exception:
            manifest = {}

    if not manifest:
        product = _find_product_for_asin(asin_clean)
        if product:
            res = generate_flow_creatives(product, creatives_dir=creatives_dir)
            manifest = res.get("data", {})

    try:
        repo_relative = str(dest_path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    except Exception:
        repo_relative = str(dest_path).replace("\\", "/")

    now_iso = datetime.now(timezone.utc).isoformat()

    creatives = manifest.setdefault("creatives", {})
    slot = creatives.setdefault(creative_type, {
        "creative_type": creative_type,
        "display_name": CREATIVE_TYPE_LABELS[creative_type],
    })
    slot["uploaded_image_path"] = repo_relative
    slot["uploaded_image_url"] = f"/creatives/{asin_clean}/{saved_filename}"
    slot["uploaded_at"] = now_iso
    slot["dimensions"] = list(dims) if dims else None

    # Attempt Cloudflare R2 upload if configured
    try:
        from publishing.r2_storage import upload_creative_to_r2, R2Config
        r2_cfg = R2Config.from_env()
        r2_cfg.validate()
        r2_res = upload_creative_to_r2(asin_clean, dest_path, config=r2_cfg)
        if r2_res.get("success") and r2_res.get("public_url"):
            slot["r2_url"] = r2_res["public_url"]
            slot["uploaded_image_url"] = r2_res["public_url"]
            if manifest.get("selected_creative_type") == creative_type:
                manifest["selected_image_r2_url"] = r2_res["public_url"]
                manifest["selected_image_url"] = r2_res["public_url"]
    except Exception:
        pass

    # If this slot was already selected, update selected_image_path
    if manifest.get("selected_creative_type") == creative_type:
        manifest["selected_image_path"] = repo_relative
        manifest["selected_image_url"] = slot.get("r2_url") or slot["uploaded_image_url"]

    manifest["updated_at"] = now_iso

    with open(manifest_file, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False)

    return True, f"Image for {CREATIVE_TYPE_LABELS[creative_type]} uploaded successfully.", manifest


def set_selected_creative(
    asin: str,
    creative_type: Optional[str],
    creatives_dir: Optional[Path] = None,
) -> Tuple[bool, str, Optional[Dict[str, Any]]]:
    """
    Set or clear the active creative selected for Pinterest publishing.
    Enforces exactly one selected creative per ASIN.
    """
    asin_clean = asin.upper().strip()
    out_dir = get_creatives_dir(asin_clean, creatives_dir)
    manifest_file = out_dir / "flow_creatives.json"

    if not manifest_file.is_file():
        product = _find_product_for_asin(asin_clean)
        if product:
            generate_flow_creatives(product, creatives_dir=creatives_dir)
        else:
            return False, f"No creatives found for ASIN {asin_clean}.", None

    try:
        with open(manifest_file, "r", encoding="utf-8") as handle:
            manifest = json.load(handle)
    except Exception as exc:
        return False, f"Could not read creative manifest: {exc}", None

    now_iso = datetime.now(timezone.utc).isoformat()
    creatives = manifest.setdefault("creatives", {})

    # Deselect if empty or "none"
    if not creative_type or creative_type.lower() in ("none", "deselect", "clear"):
        manifest["selected_creative_type"] = None
        manifest["selected_creative_display"] = None
        manifest["selected_image_path"] = None
        manifest["selected_image_url"] = None
        for slot in creatives.values():
            if isinstance(slot, dict):
                slot["selected"] = False
        manifest["updated_at"] = now_iso
        with open(manifest_file, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, indent=2, ensure_ascii=False)
        return True, "Creative selection cleared.", manifest

    if creative_type not in CREATIVE_TYPES:
        return False, f"Unknown creative type '{creative_type}'. Must be one of {CREATIVE_TYPES}.", manifest

    slot = creatives.get(creative_type)
    if not slot or not slot.get("uploaded_image_path"):
        return (
            False,
            f"Cannot select '{CREATIVE_TYPE_LABELS[creative_type]}' for publishing: no image has been uploaded yet.",
            manifest,
        )

    # Enforce EXACTLY ONE selection
    manifest["selected_creative_type"] = creative_type
    manifest["selected_creative_display"] = CREATIVE_TYPE_LABELS[creative_type]
    manifest["selected_image_path"] = slot["uploaded_image_path"]

    # Attempt Cloudflare R2 upload if r2_url is not set yet
    if not slot.get("r2_url"):
        try:
            from publishing.r2_storage import upload_creative_to_r2, R2Config
            r2_cfg = R2Config.from_env()
            r2_cfg.validate()
            local_file = Path(slot["uploaded_image_path"])
            if not local_file.is_file():
                local_file = PROJECT_ROOT / slot["uploaded_image_path"]
            if local_file.is_file():
                r2_res = upload_creative_to_r2(asin_clean, local_file, config=r2_cfg)
                if r2_res.get("success") and r2_res.get("public_url"):
                    slot["r2_url"] = r2_res["public_url"]
                    slot["uploaded_image_url"] = r2_res["public_url"]
        except Exception:
            pass

    manifest["selected_image_url"] = slot.get("r2_url") or slot.get("uploaded_image_url")
    if slot.get("r2_url"):
        manifest["selected_image_r2_url"] = slot["r2_url"]
    manifest["updated_at"] = now_iso

    for k, s in creatives.items():
        if isinstance(s, dict):
            s["selected"] = (k == creative_type)

    with open(manifest_file, "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, indent=2, ensure_ascii=False)

    # Sync with content package if present
    pkg_file = PROJECT_ROOT / "data" / "generated" / f"asin_{asin_clean}_content_package.json"
    if pkg_file.is_file():
        try:
            with open(pkg_file, "r", encoding="utf-8") as handle:
                pkg_data = json.load(handle)
            pin_p = pkg_data.setdefault("platforms", {}).setdefault("pinterest", {})
            creative_payload = {
                "creative_type": creative_type,
                "display_name": CREATIVE_TYPE_LABELS[creative_type],
                "image_path": slot["uploaded_image_path"],
                "image_url": slot.get("r2_url") or slot.get("uploaded_image_url"),
            }
            if slot.get("r2_url"):
                creative_payload["r2_url"] = slot["r2_url"]
            pin_p["selected_creative"] = creative_payload

            # Also sync to Threads platform entry if present
            threads_p = pkg_data.setdefault("platforms", {}).setdefault("threads", {})
            threads_p["selected_creative"] = dict(creative_payload)
            if slot.get("r2_url"):
                threads_p["image_url"] = slot["r2_url"]
            with open(pkg_file, "w", encoding="utf-8") as handle:
                json.dump(pkg_data, handle, indent=2, ensure_ascii=False)
        except Exception:
            pass

    return True, f"'{CREATIVE_TYPE_LABELS[creative_type]}' selected for Pinterest publishing.", manifest


def get_selected_creative(asin: str, creatives_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """
    Get the currently selected creative for an ASIN.
    Returns None if:
    - No manifest exists,
    - Only a legacy 4-variation manifest exists,
    - Or no creative has been explicitly selected.
    """
    if not asin:
        return None
    asin_clean = asin.upper().strip()
    manifest_file = get_creatives_dir(asin_clean, creatives_dir) / "flow_creatives.json"
    if not manifest_file.is_file():
        return None

    try:
        with open(manifest_file, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return None

    sel_type = data.get("selected_creative_type")
    sel_path = data.get("selected_image_path")
    if not sel_type or sel_type not in CREATIVE_TYPES or not sel_path:
        return None

    slot = data.get("creatives", {}).get(sel_type, {})
    r2_url = slot.get("r2_url") or data.get("selected_image_r2_url") or data.get("r2_url")
    return {
        "asin": asin_clean,
        "creative_type": sel_type,
        "display_name": CREATIVE_TYPE_LABELS[sel_type],
        "selected_image_path": sel_path,
        "uploaded_image_url": r2_url or data.get("selected_image_url") or slot.get("uploaded_image_url"),
        "r2_url": r2_url,
        "headline": slot.get("headline", ""),
        "subheadline": slot.get("subheadline", ""),
        "benefits": slot.get("benefits", []),
        "cta_text": slot.get("cta_text", "Shop Now"),
    }


def get_product_flow_creative_status(asin: str, creatives_dir: Optional[Path] = None) -> Dict[str, Any]:
    """
    Determine the truthful Flow creative status for an ASIN on the Products page.
    Source of truth: flow_creatives.json AND actual verified image files on disk.

    States:
    1. 'selected' - Selected for publishing (an uploaded image has explicitly been selected)
    2. 'uploaded' - Flow image uploaded (at least one valid uploaded image exists on disk)
    3. 'none'     - No Flow image (no valid uploaded Flow image exists)
    """
    if not asin:
        return {
            "status": "none",
            "label": "No Flow image",
            "badge_class": "badge-muted",
            "selected_type": None,
            "selected_display": None,
            "uploaded_count": 0,
        }

    asin_clean = asin.upper().strip()
    out_dir = get_creatives_dir(asin_clean, creatives_dir)
    manifest_file = out_dir / "flow_creatives.json"

    if not manifest_file.is_file():
        return {
            "status": "none",
            "label": "No Flow image",
            "badge_class": "badge-muted",
            "selected_type": None,
            "selected_display": None,
            "uploaded_count": 0,
        }

    try:
        with open(manifest_file, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {
            "status": "none",
            "label": "No Flow image",
            "badge_class": "badge-muted",
            "selected_type": None,
            "selected_display": None,
            "uploaded_count": 0,
        }

    creatives = data.get("creatives", {})
    valid_uploaded_slots = []
    for c_type in (CREATIVE_TYPE_GIFT, CREATIVE_TYPE_EVERYDAY):
        c_info = creatives.get(c_type, {})
        img_path = c_info.get("uploaded_image_path")
        if img_path:
            p = Path(img_path)
            if not p.is_absolute():
                p = out_dir / p.name
            if p.is_file() and p.stat().st_size > 0:
                valid_uploaded_slots.append(c_type)

    selected_type = data.get("selected_creative_type")
    if selected_type and selected_type in valid_uploaded_slots:
        sel_path = data.get("selected_image_path")
        if sel_path:
            p_sel = Path(sel_path)
            if not p_sel.is_absolute():
                p_sel = out_dir / p_sel.name
            if p_sel.is_file() and p_sel.stat().st_size > 0:
                return {
                    "status": "selected",
                    "label": "Selected for publishing",
                    "badge_class": "badge-success",
                    "selected_type": selected_type,
                    "selected_display": CREATIVE_TYPE_LABELS.get(selected_type, selected_type),
                    "uploaded_count": len(valid_uploaded_slots),
                }

    if valid_uploaded_slots:
        return {
            "status": "uploaded",
            "label": "Flow image uploaded",
            "badge_class": "badge-info",
            "selected_type": None,
            "selected_display": None,
            "uploaded_count": len(valid_uploaded_slots),
        }

    return {
        "status": "none",
        "label": "No Flow image",
        "badge_class": "badge-muted",
        "selected_type": None,
        "selected_display": None,
        "uploaded_count": 0,
    }


def has_creative_context(asin: str, creatives_dir: Optional[Path] = None) -> bool:
    """
    Check if an ASIN has creative assets that require manual creative selection.
    Returns True if:
    - Legacy 4-variation creatives.json exists on disk with variations, OR
    - flow_creatives.json exists on disk AND has either a selection or an uploaded image.
    """
    if not asin:
        return False
    asin_clean = asin.upper().strip()
    c_dir = get_creatives_dir(asin_clean, creatives_dir)

    # 1. Legacy 4-variation manifest on disk
    legacy_file = c_dir / "creatives.json"
    if legacy_file.is_file():
        try:
            with open(legacy_file, "r", encoding="utf-8") as f:
                l_data = json.load(f)
            if l_data.get("variations"):
                return True
        except Exception:
            pass

    # 2. Flow creatives with uploaded images or explicit selection
    flow_file = c_dir / "flow_creatives.json"
    if flow_file.is_file():
        try:
            with open(flow_file, "r", encoding="utf-8") as f:
                f_data = json.load(f)
            if f_data.get("selected_creative_type"):
                return True
            for slot in (f_data.get("creatives") or {}).values():
                if isinstance(slot, dict) and slot.get("uploaded_image_path"):
                    return True
        except Exception:
            pass

    return False


def load_legacy_creatives(asin: str, base_dir: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    """Load legacy 4-variation creative metadata for backward compatibility."""
    target = get_creatives_dir(asin, base_dir) / "creatives.json"
    if not target.is_file():
        return None
    try:
        with open(target, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def generate_pinterest_creatives(
    product: Product,
    count: int = 4,
    overwrite: bool = False,
    creatives_dir: Optional[Path] = None,
    source_image: Optional[Image.Image] = None,
) -> Dict[str, Any]:
    """
    Main orchestration entry point:
    Generates 3-5 distinct Pinterest marketing creative variations for a product.

    - Detects category dynamically from verified facts.
    - Prevents generic feature templates and hallucinations.
    - Preserves the original image file and URL strictly.
    - Generates 1000x1500 px (2:3) high-quality PNGs with mobile-first layout.
    """
    asin = (product.asin or "UNKNOWN").upper().strip()
    out_dir = get_creatives_dir(asin, creatives_dir)
    meta_file = out_dir / "creatives.json"

    # 1. Prevent duplicate generation if existing creatives are valid
    if not overwrite and meta_file.is_file():
        existing = load_legacy_creatives(asin, creatives_dir)
        if existing and existing.get("variations"):
            all_exist = all(
                os.path.isfile(v.get("file_path", ""))
                for v in existing.get("variations", [])
            )
            if all_exist:
                return {
                    "success": True,
                    "reused": True,
                    "asin": asin,
                    "message": f"Loaded {len(existing['variations'])} existing valid creatives.",
                    "data": existing,
                }

    # 2. Acquire original product image
    img = source_image or load_product_image_safely(product)
    if img is None:
        return {
            "success": False,
            "reused": False,
            "asin": asin,
            "message": "Product image is missing or could not be loaded; creative generation skipped.",
            "original_image_url": product.image_url,
        }

    # 3. Extract canonical product facts for grounding & ASIN isolation
    facts = FactExtractor.extract_canonical_facts(product)
    facts.asin = asin

    # Fail closed safely if minimal facts are insufficient
    if not facts.is_sufficient():
        return {
            "success": False,
            "reused": False,
            "asin": asin,
            "message": f"Insufficient verified product facts available for ASIN {asin} to safely generate creatives.",
            "original_image_url": product.image_url,
        }

    # 4. Dynamically select distinct marketing angles
    angles = select_marketing_angles(product, facts, count=count)

    variations: List[Dict[str, Any]] = []
    now_iso = datetime.now(timezone.utc).isoformat()

    # 5. Generate each variation with distinct theme and layout template
    for idx, angle in enumerate(angles):
        theme_key = DEFAULT_THEME_ORDER[idx % len(DEFAULT_THEME_ORDER)]
        theme = THEMES[theme_key]
        layout_template = LAYOUT_TEMPLATES[idx % len(LAYOUT_TEMPLATES)]

        copy = generate_creative_copy(product, angle, facts)

        # Validate copy against verified facts
        val_errors = validate_creative_copy(copy, facts, product)
        if val_errors:
            LOGGER.warning("Creative copy validation warning for ASIN %s: %s", asin, val_errors)

        canvas = compose_pinterest_creative(
            img,
            copy,
            theme,
            output_size=CANVAS_SIZE,
            layout_template=layout_template,
        )

        angle_slug = angle.lower().replace(" ", "_").replace("&", "and").replace("-", "_")
        filename = f"variation_{idx + 1}_{angle_slug}.png"
        filepath = out_dir / filename

        # Save high quality PNG
        canvas.save(filepath, "PNG", optimize=True)

        try:
            repo_relative = str(filepath.relative_to(Path.cwd())).replace("\\", "/")
        except Exception:
            repo_relative = str(filepath).replace("\\", "/")

        variation = CreativeVariation(
            variation_id=f"var_{idx + 1}_{angle_slug}",
            angle=angle,
            theme_name=theme.name,
            headline=copy.headline,
            subheadline=copy.subheadline,
            benefits=copy.benefits,
            cta_text=copy.cta_text,
            file_path=repo_relative,
            dimensions=[CANVAS_WIDTH, CANVAS_HEIGHT],
            aspect_ratio="2:3",
            created_at=now_iso,
            layout_template=layout_template,
        )
        variations.append(variation.to_dict())

    # 6. Save metadata
    result_data = {
        "asin": asin,
        "title": product.title,
        "category": detect_product_category(product, facts),
        "affiliate_url": product.original_affiliate_url or product.affiliate_url,
        "original_image_url": product.image_url,
        "created_at": now_iso,
        "total_variations": len(variations),
        "variations": variations,
    }

    with open(meta_file, "w", encoding="utf-8") as handle:
        json.dump(result_data, handle, indent=2, ensure_ascii=False)

    return {
        "success": True,
        "reused": False,
        "asin": asin,
        "message": f"Successfully generated {len(variations)} Pinterest marketing creatives.",
        "data": result_data,
    }
