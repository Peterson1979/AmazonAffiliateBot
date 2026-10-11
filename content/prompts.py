"""
Platform-specific prompt builders for AmazonAffiliateBot (V2/V3/V4).
Defines pure language-transformation prompt templates and strict factual grounding rules for X, Facebook, Pinterest, and Threads.
Enforces the Universal Content Rule: The LLM is strictly a language transformation layer that rephrases,
condenses, and organizes confirmed SOURCE FACTS without adding marketing hooks, generalizations, or inferred claims.
"""

from typing import Dict, Callable, List, Union
from amazon.scraper import Product
from content.facts import FactExtractor, CanonicalProductFacts


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
9. Output ONLY the factual body text. Do not include affiliate links, disclosures, preamble, bullet markers, or quotes."""


def build_x_prompt(product: Union[Product, CanonicalProductFacts]) -> str:
    """
    Build prompt for generating an X (Twitter) post.
    Short, natural, readable product recommendation using the most relevant supported facts.
    Avoids turning the post into a specification dump.
    """
    source_facts = format_source_facts(product)
    return f"""You are writing a concise, natural product recommendation for X (Twitter).

{COMMON_CONTENT_RULES}

PLATFORM FORMAT (X):
1. Write 1 to 2 natural, readable sentences highlighting key confirmed specifications and components from SOURCE FACTS.
2. Select 2-3 core features and integrate them naturally using standard verbs like "features", "includes", "is equipped with", or "has" (e.g. "The Electactic espresso machine features 15 bar pressure, an integrated grinder, and a 2.3 litre water tank.").
3. Avoid raw specification dumps, bullet points, or outside verbs like "operates".
4. Tone: Natural, concise, clear, and informative. No hashtags, no sales hype, no emojis.
5. Output ONLY the factual post body text.

{source_facts}

Write the natural factual X post body text for the product in SOURCE FACTS above:
Post:"""


def build_facebook_prompt(product: Union[Product, CanonicalProductFacts]) -> str:
    """
    Build prompt for generating a Facebook post.
    2 natural, short paragraphs organizing confirmed facts without specification dumps or comma-heavy lists.
    """
    source_facts = format_source_facts(product)
    return f"""You are writing a clean, natural product highlight post for Facebook.

{COMMON_CONTENT_RULES}

PLATFORM FORMAT (Facebook):
1. Write EXACTLY 2 short paragraphs organizing confirmed physical specifications and components from SOURCE FACTS.
2. You MUST separate the two paragraphs with a blank line (double newline).
3. Paragraph 1 (1-2 sentences): Naturally introduce the product type and primary technical specifications or measurements from SOURCE FACTS.
4. Paragraph 2 (1-2 sentences): Naturally describe confirmed materials, components, or included pieces from SOURCE FACTS.
5. Do NOT write bullet points, list dashes (-), or comma-chained spec lists. Write complete, fluent sentences with clean punctuation.
6. DO NOT add outcome claims, ease-of-cleaning claims, or filler phrases.
7. Tone: Informative, polished, natural, and factual.
8. Output ONLY the factual post body text.

{source_facts}

Write the natural factual Facebook post body text for the product in SOURCE FACTS above:
Post:"""


def build_pinterest_prompt(product: Union[Product, CanonicalProductFacts]) -> str:
    """
    Build prompt for generating a Pinterest Pin (Title + Description).
    Generates a natural, product-oriented, searchable title and concise grounded description.
    """
    source_facts = format_source_facts(product)
    return f"""You are creating a natural, product-oriented Pinterest Pin title and description.

{COMMON_CONTENT_RULES}

PLATFORM FORMAT (Pinterest):
1. Output MUST be valid JSON with exact keys "title" and "description".
2. "title":
   - Write a natural, readable Pinterest-style product title (MAXIMUM 100 characters).
   - Prefer natural, product-oriented, concise product naming (e.g. "Truly Soft White Microfiber Full Sheet Set" or "Electactic 15 Bar Espresso Machine with Grinder and Steam Bar") over raw spec dumps or concatenated keyword strings.
   - Use the brand/product name combined with 1 or 2 key explicitly stated attributes from SOURCE FACTS.
   - Do NOT include hype or subjective words (no "best", "must-have", "amazing", "ultimate", "perfect").
3. "description":
   - Write 1 to 2 natural, concise sentences incorporating the strongest confirmed product characteristics from SOURCE FACTS (under 400 characters).
   - Write fluent sentences; do NOT dump raw comma-separated lists or bullet points.
   - State ONLY confirmed physical features and measurements present in SOURCE FACTS.
   - Do NOT use ungrounded outcome words, benefits, certifications, or marketing adjectives.

{source_facts}

Generate the JSON title and description for the product in SOURCE FACTS above:
{{
  "title": "...",
  "description": "..."
}}"""


def build_threads_prompt(product: Union[Product, CanonicalProductFacts]) -> str:
    """
    Build prompt for generating a Threads post.
    Natural, concise conversational product recommendation strictly grounded in confirmed facts.
    """
    source_facts = format_source_facts(product)
    return f"""You are writing a concise, natural product recommendation for Threads.

{COMMON_CONTENT_RULES}

PLATFORM FORMAT (Threads):
1. Write 1 to 2 natural, concise sentences highlighting the product and its key confirmed specifications using verbs like "features", "includes", "has".
2. Select 2-3 core features and express them in clean, natural phrasing rather than a raw specification list.
3. DO NOT use generalizations, posture/health claims, outcome verbs, or rhetorical opening questions.
4. Do NOT introduce unsupported conversational claims (such as "you'll love", "perfect for", "great for", "makes it easy", etc.).
5. Do NOT use bullet points, list dashes, or raw comma chains.
6. Tone: Clean, natural, factual, and direct. No hashtags, no sales hype.
7. Output ONLY the factual post body text.

{source_facts}

Write the natural factual Threads post body text for the product in SOURCE FACTS above:
Post:"""


PLATFORM_PROMPTS: Dict[str, Callable[[Product], str]] = {
    "x": build_x_prompt,
    "facebook": build_facebook_prompt,
    "pinterest": build_pinterest_prompt,
    "threads": build_threads_prompt,
}
