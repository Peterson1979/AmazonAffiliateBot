"""
Canonical Source Fact Representation and Conservative Fact Extractor for AmazonAffiliateBot.
Defines the authoritative source data layer that guarantees all generated social media copy
originates strictly from verified Amazon product data without inference or embellishment.
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Set, Tuple, Optional, Union
import re

from amazon.scraper import Product


# Canonical unit definitions and alias mappings for exact unit verification
UNIT_ALIASES: Dict[str, Set[str]] = {
    # Pressure
    "bar": {"bar", "bars"},
    "psi": {"psi"},
    "kpa": {"kpa"},
    "atm": {"atm"},
    # Length / Distance / Screen Size
    "inch": {"inch", "inches", "in", '"', "''"},
    "cm": {"cm", "centimeter", "centimeters"},
    "mm": {"mm", "millimeter", "millimeters"},
    "m": {"m", "meter", "meters"},
    "ft": {"ft", "feet", "foot"},
    # Weight / Mass
    "lb": {"lb", "lbs", "pound", "pounds"},
    "kg": {"kg", "kgs", "kilogram", "kilograms"},
    "g": {"g", "gram", "grams"},
    "oz": {"oz", "ounce", "ounces"},
    # Volume / Liquid
    "litre": {"l", "litre", "litres", "liter", "liters"},
    "ml": {"ml", "millilitre", "millilitres", "milliliter", "milliliters"},
    "cup": {"cup", "cups"},
    "fl oz": {"fl oz", "fluid ounce", "fluid ounces"},
    "qt": {"qt", "quart", "quarts"},
    "gal": {"gal", "gallon", "gallons"},
    # Power / Electricity
    "w": {"w", "watt", "watts"},
    "kw": {"kw", "kilowatt", "kilowatts"},
    "v": {"v", "volt", "volts"},
    "mah": {"mah"},
    "ah": {"ah"},
    "a": {"a", "amp", "amps", "ampere", "amperes"},
    # Frequency / Data / Rates
    "hz": {"hz", "hertz"},
    "khz": {"khz"},
    "mhz": {"mhz"},
    "ghz": {"ghz"},
    "mb": {"mb", "megabyte", "megabytes"},
    "gb": {"gb", "gigabyte", "gigabytes"},
    "tb": {"tb", "terabyte", "terabytes"},
    "kb": {"kb", "kilobyte", "kilobytes"},
    "fps": {"fps"},
    "ms": {"ms", "millisecond", "milliseconds"},
    # Speed
    "rpm": {"rpm"},
    "mph": {"mph"},
    "km/h": {"km/h", "kmh"},
    # Ratio / Percentage
    "%": {"%", "percent", "percentage"},
}

CANONICAL_UNIT_TO_FAMILY: Dict[str, str] = {
    "bar": "pressure", "psi": "pressure", "kpa": "pressure", "atm": "pressure",
    "inch": "length", "cm": "length", "mm": "length", "m": "length", "ft": "length",
    "lb": "weight", "kg": "weight", "g": "weight", "oz": "weight",
    "litre": "volume", "ml": "volume", "cup": "volume", "fl oz": "volume", "qt": "volume", "gal": "volume",
    "w": "power", "kw": "power", "v": "power", "mah": "power", "ah": "power", "a": "power",
    "hz": "data_frequency", "khz": "data_frequency", "mhz": "data_frequency", "ghz": "data_frequency",
    "mb": "data_frequency", "gb": "data_frequency", "tb": "data_frequency", "kb": "data_frequency",
    "fps": "data_frequency", "ms": "data_frequency",
    "rpm": "speed", "mph": "speed", "km/h": "speed",
    "%": "ratio_percentage"
}

# Universal unit categories for cross-product unit family consistency checking
UNIT_FAMILIES: Dict[str, Set[str]] = {
    "pressure": {"bar", "bars", "psi", "kpa", "atm"},
    "length": {"inch", "inches", "in", '"', "''", "cm", "mm", "m", "meter", "meters", "ft", "feet"},
    "weight": {"lb", "lbs", "pound", "pounds", "kg", "kgs", "g", "gram", "grams", "oz", "ounce", "ounces"},
    "volume": {"l", "litre", "litres", "liter", "liters", "ml", "cup", "cups", "fl oz", "qt", "quart", "quarts", "gal", "gallon", "gallons"},
    "power": {"w", "watt", "watts", "kw", "kilowatt", "kilowatts", "v", "volt", "volts", "mah", "ah", "amp", "amps", "a"},
    "speed": {"rpm", "mph", "km/h", "kmh"},
    "data_frequency": {"hz", "khz", "mhz", "ghz", "mb", "gb", "tb", "kb", "fps", "ms"},
    "ratio_percentage": {"%", "percent", "percentage"},
}


def get_canonical_unit(token: str) -> Optional[str]:
    """Resolve a raw unit token or abbreviation into its canonical unit key."""
    cleaned = token.lower().strip()
    for canonical_key, aliases in UNIT_ALIASES.items():
        if cleaned in aliases or cleaned == canonical_key:
            return canonical_key
    return None


def get_unit_family(token_or_canonical: str) -> Optional[str]:
    """Resolve unit family for a unit token or canonical unit key."""
    canonical = get_canonical_unit(token_or_canonical) or token_or_canonical.lower().strip()
    return CANONICAL_UNIT_TO_FAMILY.get(canonical)


KNOWN_MATERIALS = {
    "aluminum", "aluminium", "stainless steel", "steel", "solid wood", "wood", "hardwood",
    "plastic", "glass", "tempered glass", "ceramic", "leather", "faux leather", "fabric",
    "bamboo", "silicone", "rubber", "copper", "brass", "carbon fiber", "iron", "cast iron",
    "cotton", "linen", "mesh", "acrylic", "resin", "metal", "polyurethane", "alloy", "nylon",
    "ripstop", "polyester"
}

KNOWN_CERTIFICATIONS = {
    "etl", "ce", "fcc", "ul", "rohs", "fda", "bpa-free", "bpa free",
    "energy star", "oeko-tex", "ansi", "iso", "greenguard"
}

COMMON_PHYSICAL_PROPERTIES = {
    "removable", "adjustable", "foldable", "rotatable", "tilting", "tilt-adjustable",
    "height-adjustable", "dual", "integrated", "built-in", "cordless", "wireless",
    "rechargeable", "solar-powered", "battery-powered", "automatic", "solid", "4k",
    "uhd", "ips", "led", "dynamic", "noise-cancelling", "noise cancelling", "over-ear",
    "in-ear", "portable", "waterproof", "water-resistant", "anti-clog", "non-slip",
    "zippered", "ergonomic", "lightweight", "heavy-duty", "compact", "sturdy", "durable",
    "fragile", "polished", "reinforced", "rust-resistant", "breathable", "ultralight"
}

PROPERTY_SYNONYMS: Dict[str, Set[str]] = {
    "built-in": {"built-in", "builtin", "integrated"},
    "integrated": {"built-in", "builtin", "integrated"},
    "cordless": {"cordless", "wireless"},
    "wireless": {"cordless", "wireless"},
    "noise-cancelling": {"noise-cancelling", "noise cancelling", "active noise cancellation"},
    "noise cancelling": {"noise-cancelling", "noise cancelling", "active noise cancellation"},
    "adjustable": {"adjustable", "height-adjustable", "tilt-adjustable"},
    "height-adjustable": {"adjustable", "height-adjustable"},
    "tilt-adjustable": {"adjustable", "tilt-adjustable"},
    "waterproof": {"waterproof", "water-resistant"},
    "water-resistant": {"waterproof", "water-resistant"},
    "foldable": {"foldable", "folding", "folds", "fold"},
    "folding": {"foldable", "folding", "folds", "fold"},
    "wide": {"wide", "width"},
    "width": {"wide", "width"},
    "deep": {"deep", "depth"},
    "depth": {"deep", "depth"},
    "high": {"high", "height", "tall"},
    "height": {"high", "height", "tall"},
    "tall": {"high", "height", "tall"},
    "long": {"long", "length"},
    "length": {"long", "length"},
    "thick": {"thick", "thickness"},
    "thickness": {"thick", "thickness"},
    # Component / technical synonyms for valid natural paraphrasing
    "tank": {"tank", "reservoir"},
    "reservoir": {"tank", "reservoir"},
    "tray": {"tray", "plate"},
    "display": {"display", "screen", "monitor"},
    "screen": {"display", "screen", "monitor"},
    "wand": {"wand", "bar", "tool", "nozzle"},
    "bar": {"bar", "wand", "tool", "nozzle"},
    "approx": {"approx", "approximately", "approximate"},
    "approximately": {"approx", "approximately", "approximate"},
    "approximate": {"approx", "approximately", "approximate"},
}

# Generic words and structural rephrasing connectors that do not carry product-specific claim weight
STOPWORDS = {
    # Pronouns & Articles & Prepositions & Conjunctions
    "a", "an", "the", "and", "or", "but", "if", "while", "with", "without",
    "at", "by", "for", "from", "in", "into", "of", "off", "on", "onto", "out",
    "over", "to", "up", "upon", "within", "is", "are", "was", "were",
    "be", "been", "being", "have", "has", "had", "do", "does", "did", "can",
    "could", "will", "would", "shall", "should", "may", "might", "must",
    "it", "its", "this", "that", "these", "those", "they", "them", "their",
    "which", "what", "who", "whom", "whose", "when", "where", "why", "how",
    "all", "any", "both", "each", "few", "more", "most", "other", "some",
    "such", "no", "nor", "not", "only", "own", "same", "so", "than", "too",
    "very", "just", "also", "as", "well",
    # Post formatting & structural packaging keywords
    "link", "post", "title", "description", "note", "pin", "check", "see",
    "find", "here", "available", "product", "products", "item", "items", "unit",
    "units", "model", "models", "piece", "pieces", "device", "devices",
    "machine", "machines", "system", "systems", "feature", "features", "featuring",
    "include", "includes", "included", "including",
    "equip", "equips", "equipped", "equipping",
    "come", "comes", "coming",
    "provide", "provides", "provided", "providing",
    "offer", "offers", "offered", "offering",
    "contain", "contains", "contained", "containing",
    "support", "supports", "supported", "supporting",
    "measure", "measures", "measured", "measuring",
    "weigh", "weighs", "weighed", "weighing",
    "adjust", "adjusts", "adjusted", "adjusting",
    "remove", "removes", "removed", "removing",
    "fit", "fits", "fitted", "fitting",
    "make", "makes", "made", "making",
    "craft", "crafts", "crafted", "crafting",
    "design", "designs", "designed", "designing",
    "connect", "connects", "connected", "connecting", "connectivity",
    "build", "builds", "built", "building",
    "construct", "constructs", "constructed", "constructing", "construction",
    "hour", "hours", "day", "days", "minute", "minutes",
    "set", "sets", "pack", "packs", "package", "packages",
    "size", "sizes", "height", "high", "tall", "width", "wide", "depth", "deep",
    "length", "long", "weight", "capacity", "thick", "thickness",
    "level", "levels", "mode", "modes", "speed", "speeds", "setting", "settings",
    "type", "types", "option", "options", "spec", "specs", "specification", "specifications",
    "dimension", "dimensions", "material", "materials", "color", "colors",
    "brew", "brews", "brewing", "home", "at-home", "serve", "serves", "serving",
    "use", "uses", "used", "using", "usage", "hold", "holds", "holding", "held",
    "store", "stores", "stored", "storing", "storage", "accommodate", "accommodates", "accommodated"
}


def stem_word(w: str) -> str:
    """Simple, conservative morphological stemmer for English vocabulary."""
    w = w.lower().strip()
    for suffix in ("ing", "tion", "able", "ible", "ment", "ness", "ies", "es", "ed", "ly", "s"):
        if len(w) > len(suffix) + 2 and w.endswith(suffix):
            return w[:-len(suffix)]
    # Align silent-e variants of the same word ("sleeve" vs "sleeved"/"sleeves").
    # Applied to source text and generated text alike, so a generated word still
    # has to stem to a word that is genuinely present in the source.
    if len(w) > 4 and w.endswith("e"):
        return w[:-1]
    return w


def is_keyword_stuffed_text(text: Optional[str]) -> bool:
    """
    Detect SEO keyword lists, search-phrase dumps, or repeated keyword text instead of
    real descriptive prose.

    Deterministic and category-independent. It is only used to decide whether an
    *existing* source field may be treated as a description or as a factual supporting
    attribute; it never creates, edits, or invents source content.

    Signals:
    - repeated comma-separated fragments (the same phrase pasted several times)
    - long chains of short fragments (6+ fragments where most are 8 words or fewer)
    """
    value = (text or "").strip()
    if not value:
        return False
    fragments = [p.strip() for p in re.split(r"[,;|/]+", value) if p.strip()]
    if len(fragments) < 2:
        return False
    lowered = [f.lower() for f in fragments]
    if len(set(lowered)) < len(lowered):
        return True
    if len(fragments) >= 6:
        short = sum(1 for f in fragments if len(f.split()) <= 8)
        if short >= max(3, int(len(fragments) * 0.75)):
            return True
    return False


@dataclass
class SourceFact:
    """
    Atomic, explicit factual statement from verified product source data.
    Maintains subject entity, predicate relation, object component/property,
    numeric measurements, exact unit tokens, and specifically bound modifiers.
    """
    text: str
    fact_type: str = "feature"  # "title", "component", "specification", "dimension", "material", "certification", "property", "feature"
    subject: str = "product"
    predicate: str = "has"
    object_name: str = ""
    value: Optional[str] = None
    unit: Optional[str] = None
    canonical_unit: Optional[str] = None
    unit_family: Optional[str] = None
    numbers: Set[str] = field(default_factory=set)
    modifiers: Set[str] = field(default_factory=set)
    concepts: Set[str] = field(default_factory=set)

    @property
    def supported_modifiers(self) -> Set[str]:
        return self.modifiers

    @property
    def supported_concepts(self) -> Set[str]:
        return self.concepts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "fact_type": self.fact_type,
            "subject": self.subject,
            "predicate": self.predicate,
            "object_name": self.object_name,
            "value": self.value,
            "unit": self.unit,
            "canonical_unit": self.canonical_unit,
            "unit_family": self.unit_family,
            "numbers": sorted(list(self.numbers)),
            "modifiers": sorted(list(self.modifiers)),
            "concepts": sorted(list(self.concepts)),
        }


@dataclass
class CanonicalProductFacts:
    """
    Canonical, structured product facts representation.
    Serves as the single authoritative source of truth for all content generators and validators.
    Maintains entity-modifier bindings, exact numeric units, and verified atomic facts.
    """
    title: str
    features: List[str] = field(default_factory=list)
    affiliate_url: str = ""
    asin: Optional[str] = None
    price: Optional[str] = None
    image_url: Optional[str] = None
    source_url: Optional[str] = None
    description: Optional[str] = None
    
    # Atomic facts list
    atomic_facts: List[SourceFact] = field(default_factory=list)
    
    # Entity to bound modifiers: entity_name -> set of modifiers specifically bound to it
    entity_modifiers: Dict[str, Set[str]] = field(default_factory=dict)
    
    # Entity to component mapping: entity_name -> set of component/part names
    entity_components: Dict[str, Set[str]] = field(default_factory=dict)
    
    # Exact unit mappings: number -> set of canonical unit keys (e.g. '2.3' -> {'litre'})
    number_exact_units: Dict[str, Set[str]] = field(default_factory=dict)
    
    # Number to entity bindings: number -> set of (entity, property_name)
    number_entity_bindings: Dict[str, Set[Tuple[str, str]]] = field(default_factory=dict)
    
    # Grounding metadata
    source_numbers: Set[str] = field(default_factory=set)
    number_units: Dict[str, Set[str]] = field(default_factory=dict)  # number -> set of unit tokens
    number_unit_families: Dict[str, Set[str]] = field(default_factory=dict)  # number -> set of unit families
    dimensions_3d: Set[Tuple[str, str, str]] = field(default_factory=set)
    explicit_materials: Set[str] = field(default_factory=set)
    explicit_certifications: Set[str] = field(default_factory=set)
    explicit_properties: Set[str] = field(default_factory=set)
    explicit_modifiers: Set[str] = field(default_factory=set)
    grounded_concepts: Set[str] = field(default_factory=set)
    stemmed_concepts: Set[str] = field(default_factory=set)
    explicit_causal_propositions: Set[str] = field(default_factory=set)
    
    def is_sufficient(self) -> bool:
        """Verify that minimal authoritative information is available for safe generation."""
        return bool(self.title and self.title.strip())

    def primary_description(self) -> str:
        """
        The actual product description, only when it really is descriptive prose.

        Returns an empty string when the source data has no description, or when the
        field is a keyword list / search-phrase dump. Keyword-stuffed text is never
        treated as the product description.
        """
        description = (self.description or "").strip()
        if not description or is_keyword_stuffed_text(description):
            return ""
        return description

    def supporting_attributes(self) -> List[str]:
        """Factual supporting attributes, excluding SEO keyword lists and search phrases."""
        return [
            f.strip()
            for f in self.features
            if f and f.strip() and not is_keyword_stuffed_text(f)
        ]

    def format_for_prompt(self) -> str:
        """
        Format canonical source facts into a clean, numbered, delimited block for prompt consumption.

        The actual product title and the actual product description are the primary
        generation inputs and are emitted first. Keyword lists and search-phrase dumps
        are excluded; remaining factual attributes are labelled as optional supporting
        attributes only.
        """
        lines = [
            "SOURCE FACTS:",
            f"FACT 1: {self.title}",
            "PRIMARY SOURCE (product title): summarize or rewrite the actual product "
            "title above into short, natural copy.",
        ]
        fact_index = 2

        description = self.primary_description()
        if description:
            lines.append(f"FACT {fact_index}: {description}")
            lines.append(
                "PRIMARY SOURCE (product description): summarize or rewrite the actual "
                "product description above into short, natural copy."
            )
            fact_index += 1
        else:
            lines.append(
                "PRODUCT DESCRIPTION: not available in the source data - do not invent one."
            )

        lines.append(
            "SUPPORTING ATTRIBUTES (optional - use only when genuinely needed; never "
            "treat keyword lists, keyword dumps, search phrases, or repeated keyword "
            "text as the product description):"
        )
        supporting = self.supporting_attributes()
        if supporting:
            for feat in supporting:
                lines.append(f"FACT {fact_index}: {feat}")
                fact_index += 1
        else:
            rely_on = "FACT 1 and FACT 2" if description else "FACT 1"
            lines.append(
                f"FACT {fact_index}: (No additional factual attributes; rely strictly on {rely_on})"
            )

        lines.append("-------------------")
        return "\n".join(lines)

    def to_dict(self) -> Dict[str, Any]:
        """Convert canonical facts to a serializable dictionary."""
        return {
            "title": self.title,
            "description": self.description,
            "features": self.features,
            "affiliate_url": self.affiliate_url,
            "asin": self.asin,
            "price": self.price,
            "image_url": self.image_url,
            "source_url": self.source_url,
            "source_numbers": sorted(list(self.source_numbers)),
            "number_units": {k: sorted(list(v)) for k, v in self.number_units.items()},
            "number_exact_units": {k: sorted(list(v)) for k, v in self.number_exact_units.items()},
            "entity_modifiers": {k: sorted(list(v)) for k, v in self.entity_modifiers.items()},
            "dimensions_3d": [list(d) for d in self.dimensions_3d],
            "explicit_materials": sorted(list(self.explicit_materials)),
            "explicit_certifications": sorted(list(self.explicit_certifications)),
            "explicit_properties": sorted(list(self.explicit_properties)),
            "explicit_modifiers": sorted(list(self.explicit_modifiers)),
            "grounded_concepts": sorted(list(self.grounded_concepts)),
            "atomic_facts": [f.to_dict() for f in self.atomic_facts]
        }


class FactExtractor:
    """
    Universal, Category-Independent Conservative Fact Extractor.
    Extracts atomic, explicit factual propositions, entity-modifier bindings,
    and exact measurement units from Amazon product data without domain-specific hardcoding.
    """

    @classmethod
    def _extract_numbers(cls, text: str) -> Set[str]:
        """Extract all integer and floating point numeric tokens from text."""
        return set(re.findall(r"\d+(?:\.\d+)?", text))

    @classmethod
    def _tokenize_concepts(cls, text: str) -> Set[str]:
        """Extract substantive concept words from text (excluding stopwords)."""
        words = re.findall(r"[a-z]+", text.lower())
        concepts = set()
        for w in words:
            if w not in STOPWORDS and len(w) > 1:
                concepts.add(w)
                if w in PROPERTY_SYNONYMS:
                    concepts.update(PROPERTY_SYNONYMS[w])
        return concepts

    @classmethod
    def _extract_modifier_bindings(cls, text: str) -> List[Tuple[str, str]]:
        """
        Universal syntactic modifier-entity binding extractor.
        Extracts (modifier, entity) pairs from noun phrases and predicate clauses across any product category.
        e.g., 'durable steel frame' -> [('durable', 'frame'), ('durable', 'steel frame'), ('steel', 'frame')]
        'fragile glass cup' -> [('fragile', 'cup'), ('fragile', 'glass cup'), ('glass', 'cup')]
        'waterproof polyurethane rainfly' -> [('waterproof', 'rainfly'), ('polyurethane', 'rainfly')]
        'removable water tank' -> [('removable', 'water tank'), ('removable', 'tank')]
        """
        bindings: List[Tuple[str, str]] = []
        text_lower = text.lower()

        # Preserve coordinate adjective lists e.g. "lightweight, rust-resistant aluminum"
        cleaned = re.sub(r'([a-z0-9-]+),\s*([a-z0-9-]+)\s+([a-z0-9-]+)', r'\1 \2 \3', text_lower)

        # Split clauses by delimiters
        chunks = re.split(r'[;:.!\n\r()|/]+|\bwith\b|\bfeaturing\b|\bincludes?\b|\bequipped with\b|\band\b|\bhas\b|\bfor\b', cleaned)
        for chunk in chunks:
            tokens = re.findall(r'[a-z0-9-]+', chunk)
            if not tokens:
                continue

            content_tokens = [
                t for t in tokens
                if t not in STOPWORDS and not re.match(r'^\d+(?:\.\d+)?$', t) and len(t) > 1
                and t not in {"litre", "liter", "l", "lb", "lbs", "kg", "inch", "inches", "mm", "cm", "bar", "bars", "w", "v", "oz", "ft", "m"}
            ]

            if len(content_tokens) >= 2:
                head_noun = content_tokens[-1]
                two_noun = f"{content_tokens[-2]} {content_tokens[-1]}" if len(content_tokens) >= 3 else head_noun
                
                # Attributive modifier bindings
                for i in range(len(content_tokens) - 1):
                    mod = content_tokens[i]
                    bindings.append((mod, head_noun))
                    if two_noun != head_noun:
                        bindings.append((mod, two_noun))
                
                if len(content_tokens) >= 3:
                    two_mod = f"{content_tokens[0]} {content_tokens[1]}"
                    bindings.append((two_mod, head_noun))
                    if len(content_tokens) >= 4:
                        bindings.append((two_mod, two_noun))

            # Predicate pattern: <noun> is/are <modifier>
            for m in re.finditer(r'\b([a-z]+)\s+(?:is|are|was|were)\s+([a-z-]+)\b', chunk):
                ent = m.group(1)
                mod = m.group(2)
                if ent not in STOPWORDS and mod not in STOPWORDS:
                    bindings.append((mod, ent))

            # Material pattern: <noun> made of/from <material>
            for m in re.finditer(r'\b([a-z]+)\s+(?:made|crafted|constructed)\s+(?:from|of|with)\s+([a-z\s-]+)\b', chunk):
                ent = m.group(1)
                mat = m.group(2).strip()
                if ent not in STOPWORDS and mat not in STOPWORDS:
                    bindings.append((mat, ent))
                    for mat_w in mat.split():
                        if mat_w not in STOPWORDS:
                            bindings.append((mat_w, ent))

        return bindings

    @classmethod
    def _sanitize_feature_text(cls, text: str) -> str:
        """
        Universal, category-independent feature bullet sanitizer.
        Strips subjective marketing puffery, imperative hooks, translation noise,
        and ungrounded outcome clauses from raw Amazon feature strings while preserving
        concrete physical components, materials, certifications, and specifications.
        """
        if not text or not text.strip():
            return ""

        cleaned = text.strip()

        # 1. Strip imperative promotional hooks & non-product noise sentences
        cleaned = re.sub(r"\b(?:say goodbye to|stop dealing with)\b[^.!?]*[.?!]?", " ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\b(?:enjoy|experience)\b[^.!?]*[.?!]?", " ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\b(?:perfect|elevate|upgrade)\s+your\b[^.!?]*[.?!]?", " ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\bjust like your favorite\b[^.!?]*[.?!]?", " ", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\bfor a truly\s+[a-z\s-]+(?:\.|$)", " ", cleaned, flags=re.IGNORECASE)

        # 2. Clean feature headers that are marketing slogans vs real physical specs
        # e.g. "Easy to clean: The removable..." -> "The removable..."
        # e.g. "Silky Microfoam: The powerful steam bar..." -> "The steam bar..."
        # but keep "15 bar pressure: ..." -> "15 bar pressure. ..."
        header_match = re.match(r"^([a-z0-9\s-]+):\s*([\s\S]*)$", cleaned, re.IGNORECASE)
        if header_match:
            hdr = header_match.group(1).strip()
            rest = header_match.group(2).strip()
            hdr_lower = hdr.lower()
            # If header contains an explicit unit, number, material, or property, keep header as fact
            has_spec = (
                any(re.search(rf"\b\d+\s*{re.escape(u)}\b", hdr_lower) for u in ["bar", "bars", "w", "v", "inch", "mm", "cm", "litre", "liter", "l", "oz", "lb", "lbs", "kg", "%"])
                or any(m in hdr_lower for m in KNOWN_MATERIALS)
                or any(c in hdr_lower for c in KNOWN_CERTIFICATIONS)
                or any(p in hdr_lower for p in COMMON_PHYSICAL_PROPERTIES)
            )
            is_puffery_hdr = any(p in hdr_lower for p in ["easy to clean", "silky microfoam", "easy coffee", "delicious", "perfect", "intuitive"])
            if is_puffery_hdr and not has_spec:
                cleaned = rest
            elif has_spec:
                cleaned = f"{hdr}. {rest}"

        # 3. Strip subordinate outcome clauses and subjective sensory puffery
        cleaned = re.sub(r"[,;\s]+(?:make|makes|making)\s+cleaning\s+(?:easy|easier|simple)[^.!?]*", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"[,;\s]+(?:designed to|to)\s+(?:extract|deliver|provide|produce|ensure)\s+(?:the\s+)?(?:maximum|rich|optimal|full|best)?\s*[a-z\s,-]+(?=[.!?]|$)", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"[,;\s]+(?:offers?|offering|delivers?|delivering|produces?|producing)\s+(?:a\s+)?(?:rich|brave|full|luxurious|silky|velvety|dense|perfect|professional|superior)\s+[a-z\s,-]+(?=[.!?]|$)", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"[,;\s]+(?:for\s+a\s+truly\s+professional\s+finish|for\s+a\s+professional\s+finish)", "", cleaned, flags=re.IGNORECASE)
        cleaned = re.sub(r"\bwith this intuitive\s+[a-z\s-]+,\s+you can easily\s+", "", cleaned, flags=re.IGNORECASE)

        # Normalize whitespace and punctuation
        cleaned = re.sub(r"\s+([,.])", r"\1", cleaned)
        cleaned = re.sub(r"[,;:\-–—\s]+\.", ".", cleaned)
        cleaned = re.sub(r"[ \t]{2,}", " ", cleaned).strip()
        return cleaned

    @classmethod
    def extract_canonical_facts(cls, product: Product) -> CanonicalProductFacts:
        """
        Convert a raw Product into a structured, canonical facts representation.
        Faithfully preserves explicit source statements, exact measurement units,
        and entity-modifier relationships without category-specific hardcoding.
        Enforces Title precedence for core unit measurements and sanitizes raw feature noise.
        """
        raw_title = (product.title or "").strip()
        raw_features = [f.strip() for f in product.features if f and f.strip()]
        
        # Sanitize feature bullets to isolate concrete physical propositions from marketing puffery
        sanitized_features: List[str] = []
        for rf in raw_features:
            sf = cls._sanitize_feature_text(rf)
            if sf and len(sf) > 3:
                sanitized_features.append(sf)

        # The actual product description, when present and not a keyword dump, is real
        # source text: it must reach the prompt and the grounding concept set.
        raw_description = (getattr(product, "description", "") or "").strip()
        description = (
            raw_description
            if raw_description and not is_keyword_stuffed_text(raw_description)
            else ""
        )

        all_text = [raw_title] + sanitized_features
        if description:
            all_text.append(description)
        combined_lower = " ".join(all_text).lower()

        # 1. Title measurements & unit extraction (Highest Authoritative Precedence)
        title_lower = raw_title.lower()
        title_numbers = cls._extract_numbers(title_lower)
        title_unit_families: Dict[str, str] = {}  # family -> canonical_key
        title_number_exact_units: Dict[str, Set[str]] = {}
        title_number_unit_families: Dict[str, Set[str]] = {}
        title_number_units: Dict[str, Set[str]] = {}

        for canonical_key, aliases in UNIT_ALIASES.items():
            family_name = CANONICAL_UNIT_TO_FAMILY.get(canonical_key, "other")
            for u in aliases:
                escaped_unit = re.escape(u)
                if u in {'"', "''", "%"}:
                    pattern = rf"(\d+(?:\.\d+)?)[-\s]*{escaped_unit}"
                else:
                    pattern = rf"\b(\d+(?:\.\d+)?)[-\s]*{escaped_unit}\b"

                for m in re.finditer(pattern, title_lower):
                    num = m.group(1)
                    if num not in title_number_units:
                        title_number_units[num] = set()
                    if num not in title_number_exact_units:
                        title_number_exact_units[num] = set()
                    if num not in title_number_unit_families:
                        title_number_unit_families[num] = set()

                    title_number_units[num].add(u)
                    title_number_exact_units[num].add(canonical_key)
                    title_number_unit_families[num].add(family_name)
                    title_unit_families[family_name] = canonical_key

        # 2. Extract 3D Dimensions: e.g. "10 x 8.7 x 1.8"
        dimensions_3d: Set[Tuple[str, str, str]] = set()
        for m in re.finditer(r"\b(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)\b", combined_lower):
            dimensions_3d.add((m.group(1), m.group(2), m.group(3)))

        # 3. Units and exact unit tokens associated with numbers across all sanitized text
        number_units: Dict[str, Set[str]] = {k: set(v) for k, v in title_number_units.items()}
        number_exact_units: Dict[str, Set[str]] = {k: set(v) for k, v in title_number_exact_units.items()}
        number_unit_families: Dict[str, Set[str]] = {k: set(v) for k, v in title_number_unit_families.items()}

        for canonical_key, aliases in UNIT_ALIASES.items():
            family_name = CANONICAL_UNIT_TO_FAMILY.get(canonical_key, "other")
            for u in aliases:
                escaped_unit = re.escape(u)
                if u in {'"', "''", "%"}:
                    pattern = rf"(\d+(?:\.\d+)?)[-\s]*{escaped_unit}"
                else:
                    pattern = rf"\b(\d+(?:\.\d+)?)[-\s]*{escaped_unit}\b"

                for m in re.finditer(pattern, combined_lower):
                    num = m.group(1)

                    # Title Precedence: If pressure is already defined in the title (e.g. 15 bar),
                    # a contradictory number in feature text for pressure is rejected
                    if family_name == "pressure" and family_name in title_unit_families and num not in title_numbers:
                        continue

                    if num not in number_units:
                        number_units[num] = set()
                    if num not in number_exact_units:
                        number_exact_units[num] = set()
                    if num not in number_unit_families:
                        number_unit_families[num] = set()

                    number_units[num].add(u)
                    number_exact_units[num].add(canonical_key)
                    number_unit_families[num].add(family_name)

        # Numbers extraction across title and sanitized features
        all_numbers: Set[str] = cls._extract_numbers(combined_lower)

        # 4. Materials
        explicit_materials: Set[str] = set()
        for mat in KNOWN_MATERIALS:
            if re.search(rf"\b{re.escape(mat)}\b", combined_lower):
                explicit_materials.add(mat)

        # 5. Certifications
        explicit_certifications: Set[str] = set()
        for cert in KNOWN_CERTIFICATIONS:
            if re.search(rf"\b{re.escape(cert)}\b", combined_lower):
                explicit_certifications.add(cert)

        # 6. Physical properties
        explicit_properties: Set[str] = set()
        for prop in COMMON_PHYSICAL_PROPERTIES:
            synonyms = PROPERTY_SYNONYMS.get(prop, {prop})
            if any(re.search(rf"\b{re.escape(s)}\b", combined_lower) for s in synonyms):
                explicit_properties.add(prop)
                explicit_properties.update(synonyms)

        # 7. Explicit modifiers present in canonical source text
        explicit_modifiers: Set[str] = set()
        explicit_modifiers.update(explicit_materials)
        explicit_modifiers.update(explicit_certifications)
        explicit_modifiers.update(explicit_properties)

        # 8. Entity-modifier binding extraction
        # Bind only from real source statements: title + factual attributes. SEO keyword
        # lists invent entities (e.g. "sleeved top") and would bind real modifiers to them.
        binding_text = [
            text
            for text in all_text
            if text == raw_title or not is_keyword_stuffed_text(text)
        ]
        entity_modifiers: Dict[str, Set[str]] = {}
        for text_clause in binding_text:
            clause_bindings = cls._extract_modifier_bindings(text_clause)
            for mod, entity in clause_bindings:
                is_real_mod = (
                    mod in explicit_materials
                    or mod in explicit_certifications
                    or mod in explicit_properties
                    or any(m in explicit_materials or m in explicit_properties for m in mod.split())
                    or bool(re.search(r"-[a-z]+", mod))
                    or mod in {"ykk", "ips", "4k", "uhd", "led", "usb-c", "polyurethane"}
                )
                if is_real_mod:
                    if entity not in entity_modifiers:
                        entity_modifiers[entity] = set()
                    entity_modifiers[entity].add(mod)
                    explicit_modifiers.add(mod)
                    if mod in PROPERTY_SYNONYMS:
                        entity_modifiers[entity].update(PROPERTY_SYNONYMS[mod])
                        explicit_modifiers.update(PROPERTY_SYNONYMS[mod])

        # Product-level entity aliases extracted strictly from the product title
        product_entity_aliases: Set[str] = {"product", "item", "unit", "piece", "machine", "device"}
        if raw_title:
            # Extract main product phrases from title segments before prepositions (for, with, featuring)
            title_main = re.split(r"\bfor\b|\bwith\b|\bfeaturing\b|\bequipped with\b", raw_title, flags=re.IGNORECASE)[0].strip()
            title_segments = re.split(r"[-–,:/|]+", title_main)
            title_nouns: List[str] = []
            for seg in title_segments:
                seg_tokens = [t for t in re.findall(r"[a-z0-9-]+", seg.lower()) if t not in STOPWORDS and len(t) > 1 and not re.match(r"^\d+(?:\.\d+)?$", t)]
                if seg_tokens:
                    product_entity_aliases.add(seg_tokens[-1])
                    title_nouns.append(seg_tokens[-1])
                    if len(seg_tokens) >= 2:
                        product_entity_aliases.add(f"{seg_tokens[-2]} {seg_tokens[-1]}")
                    product_entity_aliases.add(" ".join(seg_tokens))
            
            # Combine leading title nouns with subsequent alias nouns (e.g. 'laptop' + 'riser' -> 'laptop riser')
            all_title_tokens = [t for t in re.findall(r"[a-z0-9-]+", title_main.lower()) if t not in STOPWORDS and len(t) > 1 and not re.match(r"^\d+(?:\.\d+)?$", t)]
            if all_title_tokens and title_nouns:
                leading_noun = all_title_tokens[0]
                for tn in title_nouns:
                    product_entity_aliases.add(f"{leading_noun} {tn}")

        # Bind product-level modifiers to product aliases
        for p_alias in product_entity_aliases:
            if p_alias not in entity_modifiers:
                entity_modifiers[p_alias] = set()
            entity_modifiers[p_alias].update(explicit_modifiers)

        # 9. Concept tokens: strictly represented in canonical source facts
        grounded_concepts: Set[str] = set()
        if raw_title:
            grounded_concepts.update(cls._tokenize_concepts(raw_title))
        if description:
            grounded_concepts.update(cls._tokenize_concepts(description))
        for feat in sanitized_features:
            grounded_concepts.update(cls._tokenize_concepts(feat))
        grounded_concepts.update(explicit_materials)
        grounded_concepts.update(explicit_certifications)
        grounded_concepts.update(explicit_properties)
        grounded_concepts.update(explicit_modifiers)
        for ent, mods in entity_modifiers.items():
            for w in ent.split():
                if w not in STOPWORDS:
                    grounded_concepts.add(w)
            for m in mods:
                for w in m.split():
                    if w not in STOPWORDS:
                        grounded_concepts.add(w)

        # Expand synonyms into grounded concepts
        synonym_expansions = set()
        for c in grounded_concepts:
            if c in PROPERTY_SYNONYMS:
                synonym_expansions.update(PROPERTY_SYNONYMS[c])
        grounded_concepts.update(synonym_expansions)

        stemmed_concepts: Set[str] = {stem_word(c) for c in grounded_concepts}

        # 10. Number entity bindings
        number_entity_bindings: Dict[str, Set[Tuple[str, str]]] = {}
        for num, u_set in number_units.items():
            number_entity_bindings[num] = set()
            for text_clause in all_text:
                if num in text_clause:
                    clause_words = cls._tokenize_concepts(text_clause)
                    for w in clause_words:
                        if w in entity_modifiers:
                            number_entity_bindings[num].add((w, "measurement"))

        # 11. Build structured atomic SourceFacts
        atomic_facts: List[SourceFact] = []
        if raw_title:
            title_concepts = cls._tokenize_concepts(raw_title)
            atomic_facts.append(SourceFact(
                text=raw_title,
                fact_type="title",
                subject="product",
                predicate="is",
                object_name=raw_title,
                numbers=cls._extract_numbers(raw_title),
                modifiers={m for m in explicit_modifiers if m in raw_title.lower()},
                concepts=title_concepts
            ))

        for feat in (sanitized_features if sanitized_features else raw_features):
            feat_concepts = cls._tokenize_concepts(feat)
            feat_numbers = cls._extract_numbers(feat)
            feat_modifiers = {m for m in explicit_modifiers if m in feat.lower()}

            fact_type = "feature"
            if any(cert in feat.lower() for cert in explicit_certifications):
                fact_type = "certification"
            elif any(mat in feat.lower() for mat in explicit_materials):
                fact_type = "material"
            elif any(d in feat_numbers for d in number_units):
                fact_type = "specification"

            atomic_facts.append(SourceFact(
                text=feat,
                fact_type=fact_type,
                numbers=feat_numbers,
                modifiers=feat_modifiers,
                concepts=feat_concepts
            ))

        return CanonicalProductFacts(
            title=raw_title,
            description=description or None,
            features=sanitized_features if sanitized_features else raw_features,
            affiliate_url=getattr(product, "original_affiliate_url", "") or product.affiliate_url or "",
            asin=product.asin,
            price=product.price,
            image_url=product.image_url,
            source_url=product.source_url or "",
            atomic_facts=atomic_facts,
            entity_modifiers=entity_modifiers,
            number_exact_units=number_exact_units,
            number_entity_bindings=number_entity_bindings,
            source_numbers=all_numbers,
            number_units=number_units,
            number_unit_families=number_unit_families,
            dimensions_3d=dimensions_3d,
            explicit_materials=explicit_materials,
            explicit_certifications=explicit_certifications,
            explicit_properties=explicit_properties,
            explicit_modifiers=explicit_modifiers,
            grounded_concepts=grounded_concepts,
            stemmed_concepts=stemmed_concepts
        )
