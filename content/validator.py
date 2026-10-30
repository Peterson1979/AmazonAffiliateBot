"""
Claim Validator module for AmazonAffiliateBot (V2/V3/V4).
Enforces the Universal Source Grounding Rule: Social content must be based ONLY on factual propositions
contained in structured CanonicalProductFacts data.

Layered Validation Architecture:
- Layer A: Structural and compliance validation (field lengths, presence of exact affiliate URL and disclosure, no placeholders, paragraph counts).
- Layer B: Exact unit, numeric, and attribute grounding (exact source unit tokens required, intra-family unit hallucination rejection, dimension checking).
- Layer C: Universal bans (commercial claims, fake discounts, fake ratings, rhetorical sales hooks, personal testimonials, unsupported marketing escalations, posture/health claims).
- Layer D: Proposition-level grounding, entity-aware modifier binding, unsupported outcome/benefit/causal rejection, and fail-closed concept traceability.
"""

from typing import Dict, Any, List, Tuple, Set, Optional, Union
import re
from amazon.scraper import Product
from content.facts import (
    FactExtractor,
    CanonicalProductFacts,
    SourceFact,
    UNIT_ALIASES,
    UNIT_FAMILIES,
    CANONICAL_UNIT_TO_FAMILY,
    KNOWN_MATERIALS,
    KNOWN_CERTIFICATIONS,
    COMMON_PHYSICAL_PROPERTIES,
    PROPERTY_SYNONYMS,
    STOPWORDS,
    stem_word,
    get_canonical_unit,
    get_unit_family
)


# Forbidden commercial / pricing patterns unless verbatim in structured product facts
PRICE_PATTERNS = [
    r"\$[0-9]+(?:\.[0-9]+)?",
    r"\b[0-9]+(?:\.[0-9]+)?\s*(?:usd|eur|gbp|huf|\$|€|£|dollars?|cents?|bucks)\b",
    r"\b(?:price|pricing|costs?)\s+is\s+[0-9]+",
]

DISCOUNT_DEAL_PATTERNS = [
    r"[0-9]+%\s*off",
    r"\b(?:discount|promo code|coupon|sale price|special deal|limited time deal|save\s+[0-9]+%)\b",
    r"\b(?:clearance\s+(?:sale|deals?|prices?|pricing|discounts?|events?|offers?|savings?|items?|inventory|rack|stock|section)|(?:on|in|for)\s+clearance)\b",
]

SHIPPING_COMMERCIAL_PATTERNS = [
    r"\bfree shipping\b",
    r"\bprime delivery\b",
    r"\bnext-day delivery\b",
    r"\bin stock now\b",
    r"\border now\b",
    r"\bbuy now\b",
]

# Rating and social proof patterns
RATING_PATTERNS = [
    r"\b[0-9](?:\.[0-9])?\s*(?:stars?|star rating)\b",
    r"\b5-star\b",
    r"\b4-star\b",
    r"\btop-rated\b",
    r"\bbest-selling\b",
    r"\bbestseller\b",
    r"\b#1 rated\b",
    r"\bcustomer favorite\b",
    r"\bcustomers love\b",
    r"\bthousands of (?:happy|satisfied|users|customers)\b",
    r"\bloved by (?:thousands|users|customers)\b",
    r"\bhighly rated\b",
    r"\brave reviews\b",
    r"\breviewers agree\b",
    r"\baward-winning\b",
]

# First-person testimonial / fake personal experience
PERSONAL_EXPERIENCE_PATTERNS = [
    r"\b(?:i|my|we|our)\b\s+(?:bought|purchased|tested|tried|use|recommend|noticed|found|ordered|have been using|reviewed)\b",
    r"\bin my experience\b",
    r"\bafter testing this\b",
    r"\bi was skeptical\b",
    r"\bi love (?:this|these|it)\b",
    r"\bmy favorite\b",
    r"\bwe love\b",
]

# Rhetorical sales hooks and imagined user struggles / pain points
RHETORICAL_HOOK_PATTERNS = [
    (r"^\s*(?:looking for|want to|need a|ready for|searching for|tired of|struggling with|ever (?:wondered|wanted|had|felt)|here'?s why|meet the)\b", "Forbidden rhetorical hook/sales question"),
    (r"\blooking for a way to\b", "Forbidden rhetorical hook: 'looking for a way to'"),
    (r"\btired of\b", "Forbidden rhetorical hook: 'tired of'"),
    (r"\bsick of\b", "Forbidden rhetorical hook: 'sick of'"),
    (r"\bstruggling with\b", "Forbidden invented user problem: 'struggling with'"),
    (r"\bsay goodbye to\b", "Forbidden marketing phrase: 'say goodbye to'"),
    (r"\bstop dealing with\b", "Forbidden invented user problem: 'stop dealing with'"),
    (r"\bif you suffer from\b", "Forbidden invented user problem: 'if you suffer from'"),
    (r"\b(?:find|finding|finds|found) (?:your|the|that)?\s*(?:ideal|perfect|just-right) (?:position|fit|spot|setup)\b", "Forbidden sales hook"),
    (r"\bupgrade your (?:workspace|desk|setup|life|kitchen|coffee|routine)\b", "Forbidden sales escalation: 'upgrade your...'"),
    (r"\belevate your (?:workspace|laptop|desk|setup|game|routine|coffee|kitchen)\b", "Forbidden sales escalation: 'elevate your...'"),
]

# Generic marketing hype / superlatives
GENERIC_HYPE_PATTERNS = [
    r"\bgame[- ]changer\b",
    r"\bmust[- ]have\b",
    r"\bperfect for you\b",
    r"\bchange(?:s)? your life\b",
    r"\bimproves? your life\b",
    r"\bmiracle\b",
    r"\bunbeatable\b",
    r"\bholy grail\b",
]

# Unsupported generalizations and marketing escalations (Layer C bans)
UNSUPPORTED_GENERALIZATIONS = [
    (r"\buniversal compatibility\b", "Unsupported generalization: 'universal compatibility'"),
    (r"\buniversally compatible\b", "Unsupported generalization: 'universally compatible'"),
    (r"\bpopular models\b", "Unsupported marketing phrase: 'popular models'"),
    (r"\bpopular devices\b", "Unsupported marketing phrase: 'popular devices'"),
    (r"\beffortless(?:ly)?\b", "Unsupported marketing escalation: 'effortless/effortlessly'"),
    (r"\bfits? effortlessly\b", "Unsupported marketing escalation: 'fits effortlessly'"),
    (r"\bfits? easily into\b", "Unsupported marketing escalation: 'fits easily into'"),
    (r"\bideal for professionals\b", "Unsupported audience claim: 'ideal for professionals'"),
    (r"\bperfect for travel\b", "Unsupported scenario claim: 'perfect for travel'"),
    (r"\bperfect for work\b", "Unsupported scenario claim: 'perfect for work'"),
    (r"\bideal for travel\b", "Unsupported scenario claim: 'ideal for travel'"),
    (r"\bideal for work\b", "Unsupported scenario claim: 'ideal for work'"),
    (r"\bwide range\b", "Unsupported generalization: 'wide range'"),
    (r"\ball laptops\b", "Unsupported generalization: 'all laptops'"),
    (r"\bany laptop\b", "Unsupported generalization: 'any laptop'"),
    (r"\bmost laptops\b", "Unsupported generalization: 'most laptops'"),
    (r"\bevery laptop\b", "Unsupported generalization: 'every laptop'"),
    (r"\bvirtually any\b", "Unsupported generalization: 'virtually any'"),
    (r"\ball devices\b", "Unsupported generalization: 'all devices'"),
    (r"\bany device\b", "Unsupported generalization: 'any device'"),
    (r"\bmost devices\b", "Unsupported generalization: 'most devices'"),
    (r"\bbuilt to last\b", "Unsupported outcome claim: 'built to last'"),
    (r"\bpeace of mind\b", "Unsupported psychological benefit: 'peace of mind'"),
]

# Forbidden posture / health / ergonomic outcomes (Layer C unconditional bans)
FORBIDDEN_POSTURE_HEALTH_PATTERNS = [
    (r"\b(?:improving|improves?|improved|better)\s+posture\b", "Forbidden posture/ergonomic outcome: 'improving posture'"),
    (r"\bhealthier(?:\s+sitting|\s+work|\s+lifestyle)?\b", "Forbidden health/ergonomic claim: 'healthier'"),
    (r"\bdesigned for healthier\b", "Forbidden health claim: 'designed for healthier'"),
    (r"\breduces?\s+(?:strain|tension|fatigue|pain)\b", "Forbidden ergonomic outcome: 'reduces strain'"),
    (r"\bprevents?\s+(?:strain|tension|fatigue|pain)\b", "Forbidden ergonomic outcome: 'prevents strain'"),
    (r"\bpersonalized comfort\b", "Forbidden comfort outcome: 'personalized comfort'"),
    (r"\boptimal performance\b", "Forbidden outcome claim: 'optimal performance'"),
    (r"\b(?:back pain|neck pain|neck strain|wrist pain|eye strain|headache|carpal tunnel)\b", "Unsupported health/ergonomic symptom claim"),
]

# Universal causal, outcome, and benefit relationship patterns
# (Unconditionally banned in generated social copy)
CAUSAL_OUTCOME_PATTERNS = [
    (r"\bmakes?\s+([a-z\s]+)\s+(?:faster|easier|easy|simple|effortless|convenient|breeze|delight|better|richer|smoother|safer|warmer|cooler|quicker|cleaner)\b", "Unsupported causal/outcome claim"),
    (r"\b(?:easy|simple|effortless|convenient)\s+(?:cleaning|maintenance|cleanup|setup|installation|assembly)\b", "Unsupported benefit claim"),
    (r"\b(?:saves?|saving|saved)\s+(?:time|money|effort|energy|space|hassle|cleanup|work|steps)\b", "Unsupported outcome claim"),
    (r"\b(?:improves?|improving|improved)\s+(?:convenience|performance|results|efficiency|speed|quality|comfort|productivity|stability|airflow|taste|flavor|aroma|experience|safety|posture|health)\b", "Unsupported outcome claim"),
    (r"\b(?:increases?|increasing|increased)\s+(?:convenience|performance|results|efficiency|speed|quality|comfort|productivity|stability|power|warmth|cooling)\b", "Unsupported outcome claim"),
    (r"\b(?:enhances?|enhancing|enhanced)\s+(?:convenience|performance|results|efficiency|speed|quality|comfort|productivity|stability|airflow|taste|flavor|aroma|experience|safety)\b", "Unsupported outcome claim"),
    (r"\b(?:boosts?|boosting|boosted)\s+(?:convenience|performance|results|efficiency|speed|quality|comfort|productivity|stability|power)\b", "Unsupported outcome claim"),
    (r"\b(?:reduces?|reducing|reduced|prevents?|preventing|prevented)\s+(?:strain|fatigue|effort|cleanup|maintenance|time|stress|noise|vibration|pain|injuries)\b", "Unsupported outcome claim"),
    (r"\b(?:delivers?|delivering|delivered)\s+(?:an?\s+)?(?:maximum|rich|professional|brave|perfect|optimal|superior|exceptional|unmatched)\s+[a-z-]+(?:\s+[a-z-]+)?", "Unsupported outcome claim"),
    (r"\b(?:produces?|producing|produced)\s+(?:an?\s+)?(?:silky|velvety|luxurious|dense|perfect|professional|soft|richer|superior|exceptional|rich)\s+[a-z-]+(?:\s+[a-z-]+)?", "Unsupported outcome claim"),
    (r"\b(?:creates?|creating|created)\s+(?:an?\s+)?(?:silky|velvety|luxurious|dense|perfect|professional|soft|richer|superior|exceptional|heavenly|serene|peaceful|blissful)\s+[a-z-]+(?:\s+[a-z-]+)?", "Unsupported outcome claim"),
    (r"\b(?:provides?|providing|provided)\s+(?:an?\s+)?(?:immersive|luxurious|rich|professional|brave|optimal|peak|unmatched|superior|exceptional|better|maximum|heavenly|serene)\s+[a-z-]+(?:\s+[a-z-]+)?", "Unsupported outcome claim"),
    (r"\b(?:provides?|providing|provided|delivers?|delivering|delivered|creates?|creating|created|produces?|producing|produced)\s+(?:an?\s+)?(?:[a-z-]+\s+)*(?:comfort|bliss|serenity|relaxation|convenience|stability|luxury|warmth|protection)\b", "Unsupported outcome/benefit claim"),
    (r"\b(?:enables?|enabling|enabled)\s+(?:an?\s+)?(?:fast|quick|rapid|effortless|safe|easy|seamless)\s+[a-z-]+(?:\s+[a-z-]+)?", "Unsupported capability/outcome claim"),
    (r"\b(?:allows?|allowing|allowed)\s+(?:for\s+)?(?:fast|quick|easy|effortless|seamless|optimal)\s+[a-z-]+(?:\s+[a-z-]+)?", "Unsupported capability/outcome claim"),
    (r"\bkeeps?\s+(?:you|it|devices?|laptops?|food|water)\s+(?:warmer|cooler|cool|warm|dry|safe|healthy|comfortable|fresh|organized|secure|stable)\b", "Unsupported state/benefit claim"),
    (r"\bdesigned for (?:healthier|comfort|sitting|work|gaming|remote|professionals|travel)\b", "Unsupported inferred design purpose claim"),
    (r"\bprotects?\s+you\s+(?:during|in|from)\b", "Unsupported protection claim"),
    (r"\bfor\s+(?:rich|richer|dense|silky|velvety|better|maximum|optimal|smoother|faster|interference-free|worry-free|peaceful|peace|luxury|comfort|relaxation)\s+[a-z-]+\b", "Unsupported outcome clause"),
    (r"\bfor\s+[a-z-]+\s+(?:taste|flavor|aroma|cream|foam|microfoam|cleanup|function)\b", "Unsupported outcome clause"),
    (r"\b(?:to\s+)?extract\s+(?:the\s+)?(?:maximum|rich|optimal|full|best)?\s*(?:aroma|flavor|taste|depth|crema|cream|essence)\b", "Unsupported outcome/performance claim"),
    (r"\b(?:to\s+)?extract\s+the\s+maximum\b", "Unsupported performance claim: 'extract the maximum'"),
    (r"\b(?:maximum|optimal|full|rich)\s+(?:aroma|depth|taste|flavor|crema|cream)\b", "Unsupported sensory outcome claim"),
    (r"\b(?:rich|brave|full)\s+(?:taste|flavor|aroma)\b", "Unsupported sensory outcome claim"),
    (r"\b(?:dense|silky|velvety)\s+(?:cream|foam|microfoam|texture|milk)\b", "Unsupported sensory outcome claim"),
    (r"\b(?:interference-free|worry-free|trouble-free|hassle-free|clog-free)\s+(?:function|operation|grinding|use|performance)\b", "Unsupported outcome claim"),
    (r"\b(?:perfect|flawless)\s+(?:photos?|pictures?|shots?|results?|finish|drinks?)\b", "Unsupported outcome/performance claim"),
    (r"\b(?:say goodbye to|stop dealing with)\b", "Forbidden marketing phrase"),
    (r"\b(?:enjoy|experience)\s+(?:the\s+)?[a-z\s-]+\b", "Unsupported outcome/conversational claim"),
    (r"\bpeace\s+with\s+every\b", "Unsupported benefit claim"),
    (r"\b(?:consistent|seamless)\s+(?:experience|results|function|quality)\b", "Unsupported outcome claim"),
    (r"\boffers?\s+(?:a\s+)?(?:powerful\s+)?[0-9\s-]*\s*(?:pump|motor)?\s*to\s+(?:extract|deliver|provide|produce)\b", "Unsupported performance claim"),
    (r"\b(?:for\s+a\s+truly\s+professional\s+finish|professional\s+finish)\b", "Unsupported outcome claim"),
    (r"\b(?:perfect|ideal|great|suitable)\s+for\s+(?!at-home\b|espresso\b|lattes?\b|cappuccinos?\b|travel\b|work\b|gaming\b|daily\b|room\b|every\b|all\b|any\b)[a-z\s,-]+\b", "Unsupported scenario/audience claim"),
]

# Comparative claim tokens requiring explicit support in canonical source facts
COMPARATIVE_TOKENS = {
    "better", "faster", "more effective", "superior", "richer", "smoother", "stronger",
    "warmer", "cooler", "longer-lasting", "cleaner", "easier", "lighter"
}

# Unpopulated placeholders
PLACEHOLDER_PATTERNS = [
    r"\{product_title\}",
    r"\{affiliate_url\}",
    r"\{disclosure\}",
    r"\[insert link\]",
    r"\[your link\]",
    r"\[link\]",
    r"\[product\]",
    r"\[price\]",
]


class ClaimValidator:
    """
    Deterministic, Layered Universal Source-Grounded Claim Validator.
    Verifies that all generated social copy strictly adheres to confirmed CanonicalProductFacts.
    Eliminates raw source text fallback: validation relies exclusively on canonical facts.
    """

    @staticmethod
    def _extract_numbers_from_text(text: str) -> Set[str]:
        """Extract all numerical tokens (integers and decimals) from text."""
        raw_numbers = re.findall(r"\d+(?:\.\d+)?", text)
        return set(raw_numbers)

    @classmethod
    def validate_content_claims(
        cls,
        text_to_check: str,
        product_or_facts: Union[Product, CanonicalProductFacts],
        disclosure: str = ""
    ) -> List[str]:
        """
        Validate that all factual claims, numeric units, modifier bindings,
        and semantic statements in text_to_check are strictly supported by CanonicalProductFacts.
        Does NOT use raw source text: operates solely on structured canonical facts.
        """
        issues: List[str] = []
        if not text_to_check or not text_to_check.strip():
            return ["Generated content is empty."]

        if isinstance(product_or_facts, CanonicalProductFacts):
            facts = product_or_facts
        else:
            facts = FactExtractor.extract_canonical_facts(product_or_facts)

        source_numbers = facts.source_numbers

        # Clean text for claim validation
        cleaned_text = text_to_check
        if facts.affiliate_url:
            cleaned_text = cleaned_text.replace(facts.affiliate_url, "")
        if disclosure:
            cleaned_text = cleaned_text.replace(disclosure, "")
        cleaned_text_no_urls = re.sub(r'https?://\S+', '', cleaned_text)
        cleaned_lower = cleaned_text_no_urls.lower()

        # Build canonical propositions inventory for proposition-level verification
        canonical_fact_texts = [f.text.lower() for f in facts.atomic_facts]

        # ==================== LAYER A: PLACEHOLDERS ====================
        for pat in PLACEHOLDER_PATTERNS:
            if re.search(pat, text_to_check, re.IGNORECASE):
                issues.append(f"Contains unpopulated placeholder matching {pat}")

        # ==================== LAYER C: UNIVERSAL BANS & COMMERCIAL PATTERNS ====================
        # Universal generalizations (unconditionally banned in generated social copy)
        for pat, err_msg in UNSUPPORTED_GENERALIZATIONS:
            if re.search(pat, cleaned_lower):
                issues.append(err_msg)

        # Forbidden posture / health / ergonomic outcomes (unconditionally banned)
        for pat, err_msg in FORBIDDEN_POSTURE_HEALTH_PATTERNS:
            if re.search(pat, cleaned_lower):
                issues.append(err_msg)

        # Rhetorical sales hooks & opening questions (unconditionally banned)
        for pat, err_msg in RHETORICAL_HOOK_PATTERNS:
            match = re.search(pat, cleaned_lower)
            if match:
                matched_str = match.group(0)
                issues.append(f"{err_msg}: '{matched_str}'")

        # Price claims (unless verified in structured price)
        for pat in PRICE_PATTERNS:
            matches = re.findall(pat, cleaned_lower)
            for m in matches:
                price_str = (facts.price or "").lower()
                if m not in price_str:
                    issues.append(f"Unsupported price/currency claim: '{m}'")

        # Discount / deal claims
        for pat in DISCOUNT_DEAL_PATTERNS:
            matches = re.findall(pat, cleaned_lower)
            for m in matches:
                issues.append(f"Unsupported discount/deal claim: '{m}'")

        # Shipping / commercial claims
        for pat in SHIPPING_COMMERCIAL_PATTERNS:
            matches = re.findall(pat, cleaned_lower)
            for m in matches:
                issues.append(f"Unsupported commercial/shipping claim: '{m}'")

        # Rating and social proof claims
        for pat in RATING_PATTERNS:
            matches = re.findall(pat, cleaned_lower)
            for m in matches:
                issues.append(f"Unsupported rating/social proof claim: '{m}'")

        # Personal experience / fake testimonials
        for pat in PERSONAL_EXPERIENCE_PATTERNS:
            matches = re.findall(pat, cleaned_lower)
            for m in matches:
                issues.append(f"Forbidden personal experience claim: '{m}'")

        # Generic marketing hype
        for pat in GENERIC_HYPE_PATTERNS:
            matches = re.findall(pat, cleaned_lower)
            for m in matches:
                issues.append(f"Forbidden generic marketing hype: '{m}'")

        # Overbroad compatibility claims
        if re.search(r"\bcompatible with all (?:laptops|devices|notebooks|tablets)\b", cleaned_lower):
            issues.append("Unsupported broad compatibility claim: 'compatible with all...'")

        # ==================== LAYER B: EXACT UNIT & NUMERIC GROUNDING ====================
        # 1. Numbers set check
        numbers_in_text = cls._extract_numbers_from_text(cleaned_text_no_urls)
        for num in numbers_in_text:
            if num in source_numbers:
                continue
            # Allow numbered list indices 1-4 if formatted as bullets (e.g. "1.", "2.")
            if num in {"1", "2", "3", "4"} and re.search(rf"\b{num}\.\s+", cleaned_text):
                continue
            issues.append(f"Unsupported numerical specification or measurement: '{num}'")

        # 2. 3D Dimensions: e.g. "10 x 8.7 x 1.7 inches" vs "10 x 8.7 x 1.8 inches"
        gen_dimensions = re.findall(r"\b(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)\b", cleaned_lower)
        for gen_dim in gen_dimensions:
            if facts.dimensions_3d and gen_dim not in facts.dimensions_3d:
                src_dim_str = ", ".join(f"{d[0]} x {d[1]} x {d[2]}" for d in facts.dimensions_3d)
                issues.append(f"Dimension mismatch: generated '{gen_dim[0]} x {gen_dim[1]} x {gen_dim[2]}' does not match source dimensions ({src_dim_str})")

        # 3. Dimensions cannot have weight units
        if re.search(r"\b\d+(?:\.\d+)?\s*x\s*\d+(?:\.\d+)?\s*x\s*\d+(?:\.\d+)?\s*(?:lbs?|pounds?|kg|kgs?|g|oz)\b", cleaned_lower):
            issues.append("Dimensions cannot be specified in weight units (lbs/kg).")

        # 4. Exact Unit Token & Canonical Key Grounding
        for canonical_key, aliases in UNIT_ALIASES.items():
            gen_family = CANONICAL_UNIT_TO_FAMILY.get(canonical_key, "other")
            for u in aliases:
                escaped_u = re.escape(u)
                if u in {'"', "''", "%"}:
                    pattern = rf"(\d+(?:\.\d+)?)[-\s]*{escaped_u}"
                else:
                    pattern = rf"\b(\d+(?:\.\d+)?)[-\s]*{escaped_u}\b"

                for m in re.finditer(pattern, cleaned_lower):
                    num = m.group(1)

                    # Check 1: Unit Family Consistency
                    if num in facts.number_unit_families:
                        known_families = facts.number_unit_families[num]
                        if gen_family not in known_families:
                            issues.append(
                                f"Attribute/unit mismatch: '{num}' is associated with {', '.join(sorted(known_families))} in source facts, not {gen_family} ({u})."
                            )

                    # Check 2: Exact Canonical Unit Grounding
                    if num in facts.source_numbers:
                        known_canonical_units = facts.number_exact_units.get(num, set())
                        if not known_canonical_units and num in facts.number_units:
                            known_canonical_units = {
                                get_canonical_unit(tok) for tok in facts.number_units.get(num, set())
                                if get_canonical_unit(tok)
                            }

                        if known_canonical_units:
                            if canonical_key not in known_canonical_units:
                                src_units_display = "/".join(sorted(list(facts.number_units.get(num, known_canonical_units))))
                                issues.append(
                                    f"Unit mismatch: generated '{num} {u}' uses unit '{u}', but source facts specify '{num} {src_units_display}'."
                                )
                        elif num in facts.source_numbers:
                            # If number had no unit in source facts, attaching a physical unit is ungrounded
                            if gen_family in {"pressure", "length", "weight", "volume", "power", "speed"}:
                                issues.append(
                                    f"Unit mismatch: number '{num}' has no unit associated in source facts, but was generated with unit '{u}'."
                                )

        # ==================== LAYER D: PROPOSITION GROUNDING & MODIFIER BINDING ====================

        # 1. Causal, Outcome, and Benefit Relationship Checks
        for pat, err_msg in CAUSAL_OUTCOME_PATTERNS:
            for match in re.finditer(pat, cleaned_lower):
                matched_phrase = match.group(0).strip()
                issues.append(f"{err_msg}: '{matched_phrase}'")

        # 2. Unsupported Comparatives Check
        for comp in COMPARATIVE_TOKENS:
            if re.search(rf"\b{re.escape(comp)}\b", cleaned_lower):
                if comp not in facts.grounded_concepts:
                    is_comp_in_facts = any(re.search(rf"\b{re.escape(comp)}\b", f_txt) for f_txt in canonical_fact_texts)
                    if not is_comp_in_facts:
                        issues.append(f"Unsupported comparative claim: '{comp}' is not supported by source facts.")

        # 3. Entity-Aware Modifier Binding Verification
        # Split text into clauses by punctuation, prepositions, verbs, and coordinating conjunctions
        clauses = re.split(r"[,.!?;:\n]+|\bwith\b|\band\b|\bfeaturing\b|\bincludes?\b|\bequipped with\b|\bfor\b", cleaned_lower)
        for clause in clauses:
            clause_tokens = re.findall(r"[a-z0-9-]+", clause)
            if not clause_tokens:
                continue
            for i, tok in enumerate(clause_tokens):
                mod_found = None
                step = 1
                if i + 1 < len(clause_tokens):
                    two_word = f"{tok} {clause_tokens[i+1]}"
                    if two_word in facts.explicit_modifiers:
                        mod_found = two_word
                        step = 2
                if not mod_found and tok in facts.explicit_modifiers:
                    mod_found = tok
                    step = 1

                if mod_found:
                    next_idx = i + step
                    # Skip intervening stopwords, numbers, or other explicit modifiers
                    while next_idx < len(clause_tokens) and (
                        clause_tokens[next_idx] in STOPWORDS
                        or clause_tokens[next_idx] in facts.explicit_modifiers
                        or re.match(r"^\d+(?:\.\d+)?$", clause_tokens[next_idx])
                    ):
                        next_idx += 1

                    noun_phrase_tokens: List[str] = []
                    while next_idx < len(clause_tokens) and clause_tokens[next_idx] not in STOPWORDS and not re.match(r"^\d+(?:\.\d+)?$", clause_tokens[next_idx]):
                        noun_phrase_tokens.append(clause_tokens[next_idx])
                        next_idx += 1
                        if len(noun_phrase_tokens) >= 3:
                            break

                    if noun_phrase_tokens:
                        full_np = " ".join(noun_phrase_tokens)
                        head_noun = noun_phrase_tokens[-1]
                        two_noun = f"{noun_phrase_tokens[-2]} {noun_phrase_tokens[-1]}" if len(noun_phrase_tokens) >= 2 else head_noun

                        mod_synonyms = PROPERTY_SYNONYMS.get(mod_found, {mod_found})

                        # Check modifier in source facts
                        is_mod_grounded = any(
                            m in facts.explicit_modifiers or m in facts.grounded_concepts
                            for m in mod_synonyms
                        )
                        if not is_mod_grounded:
                            issues.append(f"Unsupported adjective/attribute '{mod_found}' is not grounded in source facts.")
                            continue

                        # If entity modifiers mapping is present, verify binding
                        if facts.entity_modifiers:
                            entity_matched = None
                            for candidate_ent in (full_np, two_noun, head_noun):
                                if candidate_ent in facts.entity_modifiers:
                                    entity_matched = candidate_ent
                                    break

                            if entity_matched is not None:
                                allowed_mods = facts.entity_modifiers.get(entity_matched, set())
                                is_bound = any(m in allowed_mods for m in mod_synonyms)
                                if not is_bound:
                                    issues.append(
                                        f"Modifier mismatch: modifier '{mod_found}' is not associated with entity '{full_np}' in source facts."
                                    )

        # 4. Fail-Closed Concept Traceability (Zero Raw-Source-Text Fallback)
        text_without_numbers = re.sub(r"\b\d+(?:\.\d+)?\b", " ", cleaned_lower)
        words_in_text = re.findall(r"[a-z]+", text_without_numbers)
        for w in words_in_text:
            if w in STOPWORDS or w in facts.source_numbers:
                continue
            if len(w) <= 2:
                continue
            # Known unit tokens
            if any(w in u_set for u_set in UNIT_FAMILIES.values()):
                continue
            if any(w in aliases for aliases in UNIT_ALIASES.values()):
                continue

            stemmed_w = stem_word(w)

            # Check if concept is strictly grounded in canonical facts concepts
            is_grounded = (
                w in facts.grounded_concepts
                or stemmed_w in facts.stemmed_concepts
                or any(stemmed_w == stem_word(c) for c in facts.grounded_concepts)
            )
            if not is_grounded:
                issues.append(f"Untraceable concept '{w}' in generated claim cannot be mapped to source facts.")

        return issues

    @classmethod
    def validate_platform_content(
        cls,
        platform: str,
        content: Dict[str, Any],
        product: Union[Product, CanonicalProductFacts],
        disclosure: str = ""
    ) -> Tuple[bool, List[str]]:
        """
        Validate generated platform content for claim grounding, compliance, and formatting.
        Active platforms: facebook, pinterest, threads.
        """
        platform_key = platform.lower().strip()
        issues: List[str] = []

        if not content:
            return False, ["Content is empty."]

        if isinstance(product, CanonicalProductFacts):
            facts = product
        else:
            facts = FactExtractor.extract_canonical_facts(product)

        # Check if source facts are sufficient
        if not facts.is_sufficient():
            return False, ["INSUFFICIENT_SOURCE_FACTS: Product title or features are insufficient to safely generate content."]

        if platform_key == "pinterest":
            title = content.get("title", "").strip()
            description = content.get("description", "").strip()

            if not title:
                issues.append("Pinterest title is missing.")
            elif len(title) > 100:
                issues.append(f"Pinterest title exceeds 100 characters ({len(title)} chars).")

            if not description:
                issues.append("Pinterest description is missing.")
            elif len(description) > 800:
                issues.append(f"Pinterest description exceeds 800 characters ({len(description)} chars).")

            # Validate claims in title and description
            title_issues = cls.validate_content_claims(title, facts, disclosure)
            desc_issues = cls.validate_content_claims(description, facts, disclosure)
            issues.extend([f"Pinterest Title: {iss}" for iss in title_issues])
            issues.extend([f"Pinterest Description: {iss}" for iss in desc_issues])

            # Compliance check
            if facts.affiliate_url and facts.affiliate_url not in description:
                issues.append("Affiliate URL is missing from Pinterest description.")
            if disclosure and disclosure.strip() not in description:
                issues.append("Affiliate disclosure is missing from Pinterest description.")

        elif platform_key in ("x", "facebook", "threads"):
            text = content.get("text", "").strip()
            if not text:
                issues.append(f"{platform_key.upper()} post text is empty.")

            # Platform-specific structural checks
            if platform_key == "facebook":
                body_text = text
                if facts.affiliate_url:
                    body_text = body_text.replace(facts.affiliate_url, "")
                if disclosure:
                    body_text = body_text.replace(disclosure, "")
                body_paragraphs = [
                    p.strip() for p in re.split(r"\n\s*\n", body_text)
                    if p.strip() and not p.strip().lower().startswith("link:")
                ]
                if len(body_paragraphs) == 1:
                    lines = [
                        l.strip() for l in body_text.split("\n")
                        if l.strip() and not l.strip().lower().startswith("link:")
                    ]
                    if len(lines) >= 2:
                        body_paragraphs = lines

                if len(body_paragraphs) < 2:
                    issues.append(f"Facebook post must contain 2 to 4 body paragraphs (found {len(body_paragraphs)}).")
                elif len(body_paragraphs) > 4:
                    issues.append(f"Facebook post has too many body paragraphs ({len(body_paragraphs)}).")

            # Validate claims
            text_issues = cls.validate_content_claims(text, facts, disclosure)
            issues.extend([f"{platform_key.upper()}: {iss}" for iss in text_issues])

            # Compliance check
            if facts.affiliate_url and facts.affiliate_url not in text:
                issues.append(f"Affiliate URL is missing from {platform_key.upper()} post.")
            if disclosure and disclosure.strip() not in text:
                issues.append(f"Affiliate disclosure is missing from {platform_key.upper()} post.")

        else:
            issues.append(f"Unsupported platform '{platform_key}'. Supported: facebook, pinterest, threads.")

        return len(issues) == 0, issues
