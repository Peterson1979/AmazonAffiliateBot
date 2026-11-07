"""
Platform-specific prompt builders for AmazonAffiliateBot (V2/V3/V4).
Defines pure language-transformation prompt templates and strict factual grounding rules for Facebook, Pinterest, and Threads.
Enforces the Universal Content Rule: The LLM is strictly a language transformation layer that rephrases,
condenses, and organizes confirmed SOURCE FACTS without adding marketing hooks, generalizations, or inferred claims.
"""

from typing import Dict, Callable, List, Union, Optional
import re
from amazon.scraper import Product
from content.facts import FactExtractor, CanonicalProductFacts


# Controlled set of approved, compliant, natural English Call to Action templates
APPROVED_CTA_TEMPLATES: List[str] = [
    "Explore the product details and available options below.",
    "Check the specifications and current availability.",
    "See more details and explore the available options.",
    "Find out more about this product at the link below.",
    "Explore the full product details and options at the link below.",
    "Check the full specifications and current availability below.",
    "Discover the product details and available options at the link below.",
    "See the specifications and available options at the link below.",
    "Explore the details and current availability below.",
    "Check out the full specifications and available options below.",
    "Find more details and explore the options at the link below.",
    "See full details and current availability at the link below.",
    "Check the specifications and current availability below.",
    "Explore the available options and product details below.",
    "See more details and check available options at the link below.",
    "Find out more and check current availability below.",
]

# Strict regex matching approved exploration CTA structures while rejecting commercial hype
APPROVED_CTA_REGEX = re.compile(
    r"\b(?:"
    r"(?:find\s+out\s+more|learn\s+more)\s+about\s+this\s+product(?:\s+(?:at|via|in)?\s*(?:the\s+)?link)?(?:\s+below)?"
    r"|"
    r"(?:find\s+out\s+more|find\s+more\s+details)\s+(?:and|or|&)\s+(?:explore|check(?:\s+out)?|see|discover)\s+(?:the\s+|all\s+|current\s+|available\s+|product\s+)*(?:options|details|specifications|specs|availability)(?:\s+(?:at|via|in)?\s*(?:the\s+)?link)?(?:\s+below)?"
    r"|"
    r"(?:find\s+out\s+more)\s+(?:and|or|&)\s+(?:check\s+current\s+availability|explore\s+options)(?:\s+(?:at|via|in)?\s*(?:the\s+)?link)?(?:\s+below)?"
    r"|"
    r"(?:explore|check(?:\s+out)?|see|discover)\s+"
    r"(?:the\s+|more\s+|full\s+|all\s+|current\s+|available\s+|product\s+)*"
    r"(?:details|specifications|specs|options|availability|features)"
    r"(?:\s+(?:and|or|&)\s+(?:explore|check(?:\s+out)?|see|discover)?\s*(?:the\s+|more\s+|full\s+|all\s+|current\s+|available\s+|product\s+)*(?:details|specifications|specs|options|availability|features))?"
    r"(?:\s+(?:about\s+this\s+product))?"
    r"(?:\s+(?:at|via|in)?\s*(?:the\s+)?link)?"
    r"(?:\s+below)?"
    r")\b[.:]?",
    re.IGNORECASE
)


def get_approved_cta_for_platform(platform_key: str, seed: str = "", offset: int = 0) -> str:
    """Select a deterministic, non-repetitive approved CTA template.

    Varies by platform/account and product seed (e.g. ASIN or title) to prevent
    identical CTA phrasing across platforms, accounts, and products.
    """
    platform_offsets = {
        "facebook": 0,
        "pinterest": 1,
        "pinterest_dailyideasforyou": 1,
        "dailyideasforyou": 1,
        "threads": 2,
        "pinterest_lifemodehq": 3,
        "lifemodehq": 3,
    }
    plat_key = platform_key.lower().strip()
    plat_off = platform_offsets.get(plat_key, 0)
    base_val = abs(hash(seed)) if seed else 0
    idx = (base_val + plat_off + offset) % len(APPROVED_CTA_TEMPLATES)
    return APPROVED_CTA_TEMPLATES[idx]


def format_source_facts(product_or_facts: Union[Product, CanonicalProductFacts]) -> str:
    """
    Format confirmed product details into a clean, delimited SOURCE FACTS block
    using the CanonicalProductFacts representation.
    """
    if isinstance(product_or_facts, CanonicalProductFacts):
        facts = product_or_facts
    else:
        facts = FactExtractor.extract_canonical_facts(product_or_facts)
    return facts.format_for_prompt()


# Backwards compatibility aliases
format_source_product_inventory = format_source_facts
format_source_product_data = format_source_facts


COMMON_CONTENT_RULES = """REPHRASING INSTRUCTIONS & CONSTRAINTS:
1. COMPLETE FACTUAL UNIVERSE: The SOURCE FACTS listed above represent the complete factual universe available. You may ONLY REPHRASE, CONDENSE, and ORGANIZE the exact physical and technical specifications listed in SOURCE FACTS.
2. PREFER OMISSION: If a statement cannot be directly mapped to one or more supplied canonical facts, omit it. Prefer omission over completing, interpreting, or improving a sentence. Factual completeness is less important than factual correctness.
3. NATURAL WRITING & PROPER SENTENCE STRUCTURE:
   - Write natural, fluent, cohesive marketing copy with complete subject-verb sentences.
   - DO NOT create raw specification dumps, bullet points, dash prefixes (-), list formatting, or comma-chained attribute lists.
   - Naturally select and integrate 2-3 of the most relevant supported facts rather than forcing every available specification into the copy.
   - Use clean, proper punctuation. Never output dangling commas, duplicated punctuation, or incomplete sentence fragments.
4. STRICT PROPOSITION GROUNDING:
   - State only raw physical attributes, components, materials, and measurements explicitly present in SOURCE FACTS.
   - Use ONLY standard, factual connecting verbs such as "features", "includes", "is equipped with", "comes with", "has", "is crafted from", or "contains". DO NOT use outside verbs (e.g. do NOT write "operates", "functions", "serves as", "delivers", "produces").
   - DO NOT infer benefits, outcomes, quality, performance, ease/convenience, consistency, superiority, speed, or causal relationships (e.g. NEVER write "to extract the maximum aroma", "for easy cleaning", "produces microfoam", "ensures consistent experience", "allows for easy preparation", "delivers richer", "peace of mind", "improves", "easy to clean", "keeps you comfortable", "interference-free function", "perfect photos", "rich taste").
   - DO NOT invent adjectives that introduce a new factual property (no "silky", "velvety", "durable", "sturdy", "premium", "powerful", "compact", "effortless", "professional", "perfect") unless explicitly present as a confirmed fact in SOURCE FACTS.
   - Preserve entity-modifier relationships: do not assign an adjective describing one part to a different part.
5. NO HEALTH OR ERGONOMIC OUTCOMES: DO NOT write health, ergonomic, medical, or comfort outcome claims (do NOT write "improving posture", "better posture", "healthier", "personalized comfort", "reduces strain", "designed for healthier sitting").
6. NO MARKETING COMMENTARY OR AUDIENCE INVENTIONS:
   - DO NOT add marketing commentary, ungrounded generalizations, puffery, promotional openers, or inferred claims.
   - DO NOT start sentences with: "Introducing", "Meet the", "Discover", "Here is", "Looking for", "Enjoy", "Say goodbye to".
   - DO NOT use words such as: "safety", "stability", "ensures", "ensuring", "guarantees", "provides better", "helps you", "keeps you", "peace of mind", "worry-free", "essential", "must-have", "companion", "upgrade", "effortless", "effortlessly", "fits effortlessly", "fits easily into", "perfect for", "ideal for", "designed for", "great for", "suitable for", "experience", "enjoy", "thanks to", "thanks", "allows for", "allowing", "preparation", "aromatic", "various", "prevents", "preventing", "delivers", "universal compatibility", "popular models", "wide range", "all devices", "any device", "most devices", "virtually any", "versatile", "solution", "addition", "choice", "option".
   - DO NOT invent audiences (e.g. "for remote workers", "for professionals", "for gamers", "for coffee lovers") or imagined use cases.
7. NUMERIC & UNIT ACCURACY:
   - Use ONLY numbers, measurements, and exact units explicitly listed in SOURCE FACTS.
   - NEVER invent, swap, convert, substitute, or introduce numbers or units not present in SOURCE FACTS.
8. Use the exact product title or a clean, natural product-oriented compression of it from SOURCE FACTS.
9. Output ONLY the factual body text. Do not include affiliate links, disclosures, preamble, bullet markers, or quotes.
10. APPROVED CALL TO ACTION (CTA):
    - You may end the post with a single, approved, neutral call to action encouraging product exploration.
    - Approved patterns include:
      * "Explore the product details and available options below."
      * "Check the specifications and current availability."
      * "See more details and explore the available options."
      * "Find out more about this product at the link below."
    - DO NOT use urgent, aggressive, or ungrounded purchase commands (NEVER write "Buy now", "Order today", "Hurry", "Limited time", "Sale", "Discount", "Don't miss out", "Best price")."""


def build_facebook_prompt(
    product: Union[Product, CanonicalProductFacts],
    media_type: str = "image"
) -> str:
    """
    Build prompt for generating a Facebook post.
    Supports both image posts and video posts with distinct hooks.
    2 natural, short paragraphs organizing confirmed facts without specification dumps.
    """
    source_facts = format_source_facts(product)
    is_video = str(media_type).lower().strip() == "video"

    if is_video:
        p1_instruction = (
            "Paragraph 1 (1-2 sentences): Naturally invite viewers to watch the video demonstration above "
            "(e.g. 'Watch the video above to see this [product type] demonstrated.') and introduce the primary technical specifications from SOURCE FACTS."
        )
        p2_instruction = (
            "Paragraph 2 (1-2 sentences): Naturally describe confirmed materials, components, or demonstrated features from SOURCE FACTS, "
            "ending with an approved exploration CTA (e.g. 'Explore the product details and available options below.')."
        )
        task_desc = "video showcase highlight post"
    else:
        p1_instruction = (
            "Paragraph 1 (1-2 sentences): Naturally introduce the product type and primary technical specifications or measurements from SOURCE FACTS."
        )
        p2_instruction = (
            "Paragraph 2 (1-2 sentences): Naturally describe confirmed materials, components, or included pieces from SOURCE FACTS, "
            "optionally ending with an approved exploration CTA (e.g. 'Explore the product details and available options below.')."
        )
        task_desc = "clean, natural product highlight post"

    return f"""You are writing a {task_desc} for Facebook.

{COMMON_CONTENT_RULES}

PLATFORM FORMAT (Facebook):
1. Write EXACTLY 2 short paragraphs organizing confirmed physical specifications and components from SOURCE FACTS.
2. You MUST separate the two paragraphs with a blank line (double newline).
3. {p1_instruction}
4. {p2_instruction}
5. Do NOT write bullet points, list dashes (-), or comma-chained spec lists. Write complete, fluent sentences with clean punctuation.
6. DO NOT add outcome claims, ease-of-cleaning claims, or filler phrases. Never invent video demonstrations or capabilities not in SOURCE FACTS.
7. Tone: Informative, polished, natural, and factual.
8. Output ONLY the factual post body text.

{source_facts}

Write the natural factual Facebook post body text for the product in SOURCE FACTS above:
Post:"""


def build_pinterest_prompt(
    product: Union[Product, CanonicalProductFacts],
    account: str = "dailyideasforyou",
    media_type: str = "image"
) -> str:
    """
    Build prompt for generating a Pinterest Pin (Title + Description).
    Differentiates between accounts:
    - 'dailyideasforyou': product discovery, practical shopping ideas, useful product features.
    - 'lifemodehq': lifestyle, home, everyday living and practical product applications.
    Differentiates between media types:
    - 'image': visual design, physical attributes, specifications.
    - 'video': invites viewers to watch the video demonstration of confirmed features.
    """
    source_facts = format_source_facts(product)
    account_key = str(account).lower().strip()
    is_lifemode = "lifemode" in account_key
    is_video = str(media_type).lower().strip() == "video"

    if is_lifemode:
        account_angle = "Home, lifestyle, and everyday living integration"
        if is_video:
            title_guideline = (
                '- Write a readable lifestyle video Pin title (MAXIMUM 100 characters).\n'
                '   - Emphasize home and everyday living utility combined with 1 or 2 key explicitly stated attributes from SOURCE FACTS (e.g. "[Product] for Everyday Home Living - Video Tour").\n'
                '   - Do NOT include hype or subjective words (no "best", "must-have", "amazing", "ultimate", "perfect").'
            )
            desc_guideline = (
                '- Write 1 to 2 natural, concise sentences inviting viewers to watch how the product sets up for daily living and home spaces, stating ONLY confirmed specifications from SOURCE FACTS (under 400 characters).\n'
                '   - End with an approved exploration CTA (e.g. "Explore the product details and available options below.").'
            )
        else:
            title_guideline = (
                '- Write a natural, readable lifestyle Pin title (MAXIMUM 100 characters).\n'
                '   - Emphasize everyday living or home decor application combined with key attributes from SOURCE FACTS (e.g. "[Product] for Everyday Living - [Key Spec]").\n'
                '   - Do NOT include hype or subjective words (no "best", "must-have", "amazing", "ultimate", "perfect").'
            )
            desc_guideline = (
                '- Write 1 to 2 natural, concise sentences highlighting household and daily living application, confirmed materials, and practical design from SOURCE FACTS (under 400 characters).\n'
                '   - Optionally end with an approved exploration CTA (e.g. "Explore the product details and available options below.").'
            )
    else:
        # dailyideasforyou (default)
        account_angle = "Shopping discovery, practical shopping ideas, and useful product features"
        if is_video:
            title_guideline = (
                '- Write a natural, readable Pinterest-style video discovery product title (MAXIMUM 100 characters).\n'
                '   - Combine brand/product name with key specifications or features demonstrated (e.g. "[Brand/Product] Demo - [Key Feature]").\n'
                '   - Do NOT include hype or subjective words (no "best", "must-have", "amazing", "ultimate", "perfect").'
            )
            desc_guideline = (
                '- Write 1 to 2 natural, concise sentences inviting viewers to watch the video demonstration of the confirmed features from SOURCE FACTS (under 400 characters).\n'
                '   - End with an approved exploration CTA (e.g. "See more details and explore the available options.").'
            )
        else:
            title_guideline = (
                '- Write a natural, readable Pinterest-style product title (MAXIMUM 100 characters).\n'
                '   - Prefer natural, product-oriented, concise product naming (e.g. "Truly Soft White Microfiber Full Sheet Set" or "Electactic 15 Bar Espresso Machine with Grinder and Steam Bar") over raw spec dumps or concatenated keyword strings.\n'
                '   - Use the brand/product name combined with 1 or 2 key explicitly stated attributes from SOURCE FACTS.\n'
                '   - Do NOT include hype or subjective words (no "best", "must-have", "amazing", "ultimate", "perfect").'
            )
            desc_guideline = (
                '- Write 1 to 2 natural, concise sentences incorporating the strongest confirmed product characteristics from SOURCE FACTS (under 400 characters), optionally ending with an approved exploration CTA (e.g. "See more details and explore the available options.").\n'
                '   - Write fluent sentences; do NOT dump raw comma-separated lists or bullet points.\n'
                '   - State ONLY confirmed physical features and measurements present in SOURCE FACTS.'
            )

    return f"""You are creating a natural, product-oriented Pinterest Pin title and description.
Account Focus: {account_angle}

{COMMON_CONTENT_RULES}

PLATFORM FORMAT (Pinterest):
1. Output MUST be valid JSON with exact keys "title" and "description".
2. "title":
   {title_guideline}
3. "description":
   {desc_guideline}
   - Do NOT use ungrounded outcome words, benefits, certifications, or marketing adjectives. Never invent video demonstrations not in SOURCE FACTS.

{source_facts}

Generate the JSON title and description for the product in SOURCE FACTS above:
{{
  "title": "...",
  "description": "..."
}}"""


def build_threads_prompt(
    product: Union[Product, CanonicalProductFacts],
    media_type: str = "image"
) -> str:
    """
    Build prompt for generating a Threads post.
    Supports both image posts and video posts with distinct hooks.
    Natural, concise conversational product recommendation strictly grounded in confirmed facts.
    """
    source_facts = format_source_facts(product)
    is_video = str(media_type).lower().strip() == "video"

    if is_video:
        format_instruction = (
            "1. Write 1 to 2 natural, concise sentences inviting users to watch the video demonstration above and highlighting key confirmed specifications "
            "(e.g. 'Watch the video above to see this [product] in action, featuring [confirmed specs].'), ending with an approved exploration CTA (e.g. 'Check the specifications and current availability.')."
        )
        task_desc = "video recommendation"
    else:
        format_instruction = (
            "1. Write 1 to 2 natural, concise sentences highlighting the product and its key confirmed specifications using verbs like 'features', 'includes', 'has', "
            "optionally ending with an approved exploration CTA (e.g. 'Check the specifications and current availability.')."
        )
        task_desc = "concise, natural product recommendation"

    return f"""You are writing a {task_desc} for Threads.

{COMMON_CONTENT_RULES}

PLATFORM FORMAT (Threads):
{format_instruction}
2. Select 2-3 core features and express them in clean, natural phrasing rather than a raw specification list.
3. DO NOT use generalizations, posture/health claims, outcome verbs, or rhetorical opening questions. Never invent video actions or capabilities not in SOURCE FACTS.
4. Do NOT introduce unsupported conversational claims (such as "you'll love", "perfect for", "great for", "makes it easy", etc.).
5. Do NOT use bullet points, list dashes, or raw comma chains.
6. Tone: Clean, natural, factual, and direct. No hashtags, no sales hype.
7. Output ONLY the factual post body text.

{source_facts}

Write the natural factual Threads post body text for the product in SOURCE FACTS above:
Post:"""


PLATFORM_PROMPTS: Dict[str, Callable[[Product], str]] = {
    "facebook": build_facebook_prompt,
    "pinterest": build_pinterest_prompt,
    "threads": build_threads_prompt,
}
