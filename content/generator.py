"""
Content Generator module for AmazonAffiliateBot (V2/V3/V4).
Orchestrates platform-specific social media copy generation using local Ollama (qwen2.5:7b).
Supported active platforms: Facebook, Pinterest, and Threads.
Enforces strict claim validation and maximum 1 regeneration attempt.
"""

from typing import Dict, Any, List, Optional, Tuple, Union
import json
import re
from pathlib import Path

from amazon.scraper import Product
from ai.ollama import OllamaClient, OllamaEmptyResponse, OllamaProviderError
from content.facts import FactExtractor, CanonicalProductFacts, is_keyword_stuffed_text
from content.prompts import (
    PLATFORM_PROMPTS,
    COMMON_CONTENT_RULES,
    format_source_facts,
    get_approved_cta_for_platform,
    build_facebook_prompt,
    build_pinterest_prompt,
    build_threads_prompt
)
from content.validator import (
    ClaimValidator,
    PRICE_PATTERNS,
    DISCOUNT_DEAL_PATTERNS,
    PERSONAL_EXPERIENCE_PATTERNS
)


# Token budget per platform for CPU efficiency and snappiness
PLATFORM_PREDICT_TOKENS = {
    "facebook": 320,
    "pinterest": 180,
    "pinterest_dailyideasforyou": 180,
    "pinterest_lifemodehq": 180,
    "threads": 120,
}


def recommended_num_ctx(prompt: str, num_predict: int, configured_num_ctx: int = 1024) -> int:
    """Choose a bounded context window that can contain this prompt and its output.

    Ollama's configured default remains the floor. Long, fact-heavy Amazon prompts
    must not be forced into the 1024-token window, which can make prompt evaluation
    disproportionately slow on the local model. This is only request sizing; it
    does not alter the provider, model, timeout, or source facts.
    """
    prompt_tokens = max(1, (len(prompt) + 2) // 3)  # conservative for mixed text
    required = prompt_tokens + max(1, int(num_predict)) + 64
    context = max(int(configured_num_ctx), required)
    context = ((context + 255) // 256) * 256
    return min(context, 4096)


def effective_num_predict(platform_key: str, configured_num_predict: object = None) -> int:
    """Respect the configured provider output budget when it is lower."""
    platform_budget = PLATFORM_PREDICT_TOKENS.get(platform_key, 160)
    if isinstance(configured_num_predict, int) and configured_num_predict > 0:
        return min(platform_budget, configured_num_predict)
    return platform_budget


def is_empty_response_error(error: BaseException) -> bool:
    """True only for an explicit "provider returned no content" failure.

    Transport failures (connection refused, timeout, HTTP error) stay
    provider-wide and keep their existing short-circuit behavior. An empty
    completion proves only that this one platform produced no content, so it is
    handled per platform and must never be accepted as valid content.
    """
    if isinstance(error, OllamaEmptyResponse):
        return True
    return getattr(error, "category", None) == "empty_response"


def clean_markdown_fences(text: str) -> str:
    """Remove surrounding markdown code blocks (```json ... ``` or ``` ...) if present."""
    text = text.strip()
    match = re.match(r"^```(?:json)?\s*([\s\S]*?)\s*```$", text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return text


def clean_formatting(text: str) -> str:
    """
    Deterministic post-generation formatting cleanup.
    Cleans purely syntactic issues:
    - Awkward punctuation combinations: ',.' -> '.', ';.' -> '.', '..+' -> '.', ',,' -> ','
    - Dangling commas, colons, semicolons, or dashes before sentence-ending punctuation or line ends
    - Spacing around punctuation: 'word , word' -> 'word, word', 'word .' -> 'word.'
    - Markdown list bullets and dashes at start of sentences/paragraphs ('- ', '* ', '• ')
    - Consecutive whitespace, blank lines, or double periods
    """
    if not text:
        return ""

    lines = [line.strip() for line in text.splitlines()]
    cleaned_lines = []

    for line in lines:
        if not line:
            cleaned_lines.append("")
            continue

        # Strip leading bullet points / list markers
        line = re.sub(r"^[\s\*\-•–—]+\s*", "", line)
        line = re.sub(r"^\d+\.\s+", "", line)

        # Remove mid-line bullet dashes separating sentences: e.g. "pump. - Enhanced" -> "pump. Enhanced"
        line = re.sub(r"(?<=[.!?])\s*[\-•*–—]\s+", " ", line)

        # Fix spacing before punctuation: 'word .' -> 'word.', 'word ,' -> 'word,'
        line = re.sub(r"\s+([,.;:!?])", r"\1", line)

        # Fix comma/semicolon/dash before period: ',.' -> '.', ';.' -> '.', '-.' -> '.'
        line = re.sub(r"[,;:\-–—\s]+\.", ".", line)

        # Fix multiple commas: ',,' or ', ,' -> ','
        line = re.sub(r",[\s,]*,\s*", ", ", line)

        # Fix multiple periods: '...' is ellipsis, but '..' or '....' -> '.'
        line = re.sub(r"\.{4,}", ".", line)
        line = re.sub(r"(?<!\.)\.\.(?!\.)", ".", line)

        # Fix comma before exclamation/question: ',!' -> '!', ',?' -> '?'
        line = re.sub(r"[,;:\-–—\s]+([!?])", r"\1", line)

        # Fix trailing dangling punctuation at line end: e.g. "sentence," -> "sentence."
        line = re.sub(r"[,;:\-–—]+\s*$", ".", line)

        # Collapse multiple spaces
        line = re.sub(r"[ \t]{2,}", " ", line).strip()

        cleaned_lines.append(line)

    cleaned_text = "\n".join(cleaned_lines)
    # Collapse 3+ newlines to 2 newlines
    cleaned_text = re.sub(r"\n{3,}", "\n\n", cleaned_text).strip()
    return cleaned_text


def sanitize_generated_text(text: str) -> str:
    """Strip common LLM response prefixes like 'Post:', 'Recommendation:', etc."""
    text = clean_markdown_fences(text).strip()
    prefixes = [
        r"^(?:CORRECTED|CORRECTION|FIXED|REVISED)\s*(?:POST|TEXT|RECOMMENDATION)?:\s*",
        r"^(?:Post|Recommendation|Threads post|Facebook post|Title|Description):\s*",
        r"^Here is (?:the|your) (?:post|recommendation|title|description):\s*",
    ]
    for pat in prefixes:
        text = re.sub(pat, "", text, flags=re.IGNORECASE).strip()
    return clean_formatting(text)


def parse_pinterest_response(raw_text: str, product_or_facts: Union[Product, CanonicalProductFacts], disclosure: str = "") -> Dict[str, str]:
    """Parse raw LLM output into structured Pinterest title and description."""
    fenced_cleaned = clean_markdown_fences(raw_text).strip()
    title_fallback = product_or_facts.title.split(" - ")[0].split(":")[0].strip()[:80] if product_or_facts.title else ""

    # 1. Try direct JSON parsing on cleaned text or extracted JSON block
    json_candidate = fenced_cleaned
    json_block_match = re.search(r'\{[\s\S]*\}', fenced_cleaned)
    if json_block_match:
        json_candidate = json_block_match.group(0)

    try:
        data = json.loads(json_candidate)
        if isinstance(data, dict) and "title" in data and "description" in data:
            title = clean_formatting(sanitize_generated_text(str(data["title"])))[:100].strip()
            desc = clean_formatting(sanitize_generated_text(str(data["description"])))
            if len(desc) > 520:
                truncated = desc[:520]
                last_period = max(truncated.rfind(". "), truncated.rfind(".\n"), truncated.rfind("."))
                if last_period > 100:
                    desc = truncated[:last_period + 1].strip()
            if title and desc:
                return {"title": title, "description": desc}
    except Exception:
        pass

    # 2. Key-value regex extraction for title and description
    title_match = re.search(r'["\']?(?:title|pin[ _-]?title)["\']?\s*:\s*["\']?([^"\n\r]+)["\']?', fenced_cleaned, re.IGNORECASE)
    desc_match = re.search(r'["\']?(?:description|pin[ _-]?description)["\']?\s*:\s*["\']?([\s\S]+?)(?:["\']\s*,?\s*["\']?[a-z_]+["\']?\s*:|["\']?\s*\}|$)', fenced_cleaned, re.IGNORECASE)

    if title_match:
        title = title_match.group(1).strip()
    else:
        lines = [l.strip() for l in fenced_cleaned.split("\n") if l.strip() and not l.strip().startswith("{")]
        title = sanitize_generated_text(lines[0]) if lines and len(lines[0]) <= 100 else title_fallback

    if desc_match:
        desc = desc_match.group(1).strip()
    else:
        desc = sanitize_generated_text(fenced_cleaned)

    # Clean any trailing or leading JSON brackets/quotes
    title = re.sub(r'^[{"\'\s]+|[}"\'\s]+$', '', title).strip()
    desc = re.sub(r'^[{"\'\s]+|[}"\'\s]+$', '', desc).strip()
    title = clean_formatting(sanitize_generated_text(title))
    desc = clean_formatting(sanitize_generated_text(desc))

    if not title:
        title = title_fallback

    if len(desc) > 520:
        truncated = desc[:520]
        last_period = max(truncated.rfind(". "), truncated.rfind(".\n"), truncated.rfind("."))
        if last_period > 100:
            desc = truncated[:last_period + 1].strip()

    return {"title": title[:100].strip(), "description": desc}


def sanitize_outcome_clauses(text: str) -> str:
    """Strip universal ungrounded purpose/outcome appended clauses from generated text."""
    # Strip e.g. ", to extract the maximum aroma and depth", ", for rich taste and dense cream", " for interference-free function", ", for easy cleaning"
    cleaned = re.sub(
        r'[,;\s]+(?:for\s+(?:rich|richer|dense|silky|velvety|better|maximum|optimal|smoother|faster|easier|easy|convenient|consistent|interference-free|worry-free|peaceful|peace|luxury|comfort|relaxation)\s+[a-z\s,-]+|to\s+extract\s+[a-z\s,-]+|designed\s+to\s+[a-z\s,-]+|producing\s+[a-z\s,-]+|providing\s+[a-z\s,-]+)(?=[.!?,;]|$)',
        '',
        text,
        flags=re.IGNORECASE
    )
    cleaned = clean_formatting(cleaned)
    return cleaned


def format_facebook_paragraphs(text: str) -> str:
    """Ensure Facebook post text contains exactly 2 paragraphs separated by a blank line."""
    lines = [clean_formatting(p.strip()) for p in re.split(r'\n\s*\n', text) if p.strip()]
    if len(lines) == 2:
        return f"{lines[0]}\n\n{lines[1]}"
    
    # Split sentences into 2 balanced paragraphs
    sentences = [clean_formatting(s.strip()) for s in re.split(r'(?<=[.!?])\s+', text) if s.strip()]
    if len(sentences) >= 2:
        mid = max(1, len(sentences) // 2)
        p1 = " ".join(sentences[:mid])
        p2 = " ".join(sentences[mid:])
        return f"{p1}\n\n{p2}"
    
    return clean_formatting(text)


def build_canonical_fallback_sentence(facts: CanonicalProductFacts) -> str:
    """
    Construct a deterministic, 100% factually grounded fallback sentence directly
    from confirmed canonical product facts when generated text lacks body content.
    """
    # 1. Search atomic facts for confirmed specifications, components, materials, or features
    for fact in facts.atomic_facts:
        if fact.fact_type in ("specification", "component", "material", "feature"):
            if is_keyword_stuffed_text(fact.text):
                continue
            cand = sanitize_outcome_clauses(fact.text)
            cand = re.sub(r'^[【\[].*?[】\]]\s*', '', cand).strip()
            cand = re.split(r'\s+-\s+|\bfor\b', cand, flags=re.IGNORECASE)[0].strip()
            cand = cand.strip("【】[]() -:;,.")
            cand = clean_formatting(cand).rstrip(".")
            if len(cand) >= 10:
                if cand.lower().startswith(("the ", "this ", "features", "includes", "equipped with", "constructed with", "made of", "with")):
                    cand_sentence = f"{cand}."
                else:
                    cand_sentence = f"Features {cand}."
                cand_sentence = clean_formatting(cand_sentence)
                if not cand_sentence.endswith((".", "!", "?")):
                    cand_sentence += "."
                issues = ClaimValidator.validate_content_claims(cand_sentence, facts, disclosure="")
                if not issues and len(cand_sentence.split()) >= 3:
                    return cand_sentence

    # 2. Try explicit materials
    if facts.explicit_materials:
        mat = sorted(list(facts.explicit_materials))[0]
        cand_sentence = f"Features {mat} construction."
        if not ClaimValidator.validate_content_claims(cand_sentence, facts, disclosure=""):
            return cand_sentence

    # 3. Fallback to clean, grounded product title
    title_cand = clean_formatting(facts.title.split(" - ")[0].split(":")[0].strip()).strip("【】[]() -:;,.")
    if title_cand.lower().startswith(("features", "includes")):
        cand_sentence = f"{title_cand}."
    else:
        cand_sentence = f"Features {title_cand}."
        if ClaimValidator.validate_content_claims(cand_sentence, facts, disclosure=""):
            cand_sentence = f"{title_cand}."
    return clean_formatting(cand_sentence)


def build_lifemodehq_fallback(facts: CanonicalProductFacts, media_type: str = "image") -> Dict[str, str]:
    """
    Construct distinct, grounded fallback copy specifically for Pinterest LifeModeHQ.
    Emphasizes lifestyle/everyday living and ensures LifeModeHQ copy never duplicates dailyideasforyou.
    """
    is_video = str(media_type).lower().strip() == "video"
    raw_title = clean_formatting(facts.title.split(" - ")[0].split(":")[0].strip()).strip("【】[]() -:;,.")
    if len(raw_title) > 60:
        raw_title = raw_title[:60].rsplit(" ", 1)[0]

    if is_video:
        title = f"{raw_title} for Everyday Living - Video"[:100]
    else:
        title = f"{raw_title} for Everyday Living"[:100]

    eligible = [
        f for f in facts.atomic_facts
        if f.fact_type in ("specification", "component", "material", "feature")
        and not is_keyword_stuffed_text(f.text)
    ]
    order = eligible[::-1] if len(eligible) > 1 else eligible
    body_sentence = None
    for fact in order:
        cand = sanitize_outcome_clauses(fact.text)
        cand = re.sub(r'^[【\[].*?[】\]]\s*', '', cand).strip()
        cand = re.split(r'\s+-\s+|\bfor\b', cand, flags=re.IGNORECASE)[0].strip().strip("【】[]() -:;,.").rstrip(".")
        if len(cand) >= 10:
            if cand.lower().startswith(("the ", "this ", "features", "includes", "equipped with", "constructed with", "made of", "with", "built with")):
                cand_sentence = f"{cand}."
            else:
                cand_sentence = f"Features {cand}."
            cand_sentence = clean_formatting(cand_sentence)
            if not cand_sentence.endswith((".", "!", "?")):
                cand_sentence += "."
            if not ClaimValidator.validate_content_claims(cand_sentence, facts, disclosure=""):
                body_sentence = cand_sentence
                break

    if not body_sentence:
        body_sentence = build_canonical_fallback_sentence(facts)

    cta = "Explore the product details and available options below."
    desc = f"{body_sentence} {cta}"
    if facts.affiliate_url:
        desc = f"{desc} {facts.affiliate_url}"
    return {"title": clean_formatting(title), "description": clean_formatting(desc)}


def extract_grounded_platform_content(
    platform_key: str,
    raw_text: str,
    facts: CanonicalProductFacts,
    disclosure: str,
    allow_fallback: bool = False
) -> Optional[Dict[str, Any]]:
    """
    Extract, filter, and structure generated content so that every sentence is deterministically
    verified against CanonicalProductFacts propositions.
    Returns None if no valid grounded propositions could be extracted from the model's output.
    """
    if platform_key.startswith("pinterest"):
        parsed = parse_pinterest_response(raw_text, facts, disclosure)
        title_raw = clean_formatting(parsed.get("title", "").strip())
        title_issues = ClaimValidator.validate_content_claims(title_raw, facts, disclosure="", is_title=True)
        if not title_issues and title_raw:
            title = title_raw[:100].strip()
        else:
            title = clean_formatting(facts.title.split(" - ")[0].split(":")[0].strip())[:90]

        desc_raw = parsed.get("description", "").strip()
        desc_clean = sanitize_outcome_clauses(desc_raw)
        desc_clean = clean_formatting(desc_clean)
        sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+|\n+', desc_clean) if s.strip()]
        valid_sentences = []
        for s in sentences:
            s_clean = re.sub(r'https?://\S+', '', s).strip()
            s_clean = clean_formatting(s_clean)
            if not s_clean or len(s_clean) < 4:
                continue
            issues = ClaimValidator.validate_content_claims(s_clean, facts, disclosure="")
            if issues:
                sub_clauses = re.split(r'\bfor\b|\bdelivering\b|\bproducing\b|\bproviding\b|\bensuring\b', s_clean, flags=re.IGNORECASE)
                if sub_clauses and len(sub_clauses[0].strip()) > 10:
                    cand_sub = clean_formatting(sub_clauses[0].strip())
                    if not cand_sub.endswith(('.', '!', '?')):
                        cand_sub = cand_sub + '.'
                    cand_sub = clean_formatting(cand_sub)
                    if not ClaimValidator.validate_content_claims(cand_sub, facts, disclosure=""):
                        s_clean = cand_sub
                        issues = []
            if not issues:
                s_clean = clean_formatting(s_clean)
                if not s_clean.endswith(('.', '!', '?')):
                    s_clean = s_clean + '.'
                valid_sentences.append(s_clean)

        if valid_sentences:
            factual_sentences = [s for s in valid_sentences if not ClaimValidator.is_approved_cta(s)]
            cta_sentences = [s for s in valid_sentences if ClaimValidator.is_approved_cta(s)]
            if not factual_sentences:
                if not allow_fallback:
                    return None
                factual_sentences = [build_canonical_fallback_sentence(facts)]
            cta_to_use = cta_sentences[0] if cta_sentences else ""
            selected_sentences = factual_sentences[:2]
            if cta_to_use:
                selected_sentences.append(cta_to_use)
            description = clean_formatting(" ".join(selected_sentences))
            return {"title": title, "description": description[:450].strip()}
        else:
            if allow_fallback:
                fallback_sentence = build_canonical_fallback_sentence(facts)
                return {"title": title, "description": fallback_sentence[:450].strip()}
            return None

    # Text-based platforms: Facebook, Threads
    # Check for hard Layer C violations (price, discounts, fake testimonials)
    for pat in PRICE_PATTERNS + DISCOUNT_DEAL_PATTERNS + PERSONAL_EXPERIENCE_PATTERNS:
        if re.search(pat, raw_text, re.IGNORECASE):
            if pat in PRICE_PATTERNS:
                price_str = (facts.price or "").lower()
                matches = re.findall(pat, raw_text.lower())
                if any(m not in price_str for m in matches):
                    return None
            else:
                return None

    clean_text = sanitize_generated_text(raw_text)
    clean_text = clean_formatting(clean_text)
    clean_text = sanitize_outcome_clauses(clean_text)
    clean_text = clean_formatting(clean_text)

    # Split into candidate sentences
    sentences = [s.strip() for s in re.split(r'(?<=[.!?])\s+|\n+', clean_text) if s.strip()]
    valid_sentences = []
    for s in sentences:
        s_clean = re.sub(r'https?://\S+', '', s).strip()
        s_clean = clean_formatting(s_clean)
        if not s_clean or len(s_clean) < 4:
            continue
        issues = ClaimValidator.validate_content_claims(s_clean, facts, disclosure="")
        if issues:
            # Try trimming trailing outcome/purpose clauses
            sub_clauses = re.split(r'\bfor\b|\bdelivering\b|\bproducing\b|\bproviding\b|\bensuring\b', s_clean, flags=re.IGNORECASE)
            if sub_clauses and len(sub_clauses[0].strip()) > 10:
                cand_sub = clean_formatting(sub_clauses[0].strip())
                if not cand_sub.endswith(('.', '!', '?')):
                    cand_sub = cand_sub + '.'
                cand_sub = clean_formatting(cand_sub)
                cand_issues = ClaimValidator.validate_content_claims(cand_sub, facts, disclosure="")
                if not cand_issues:
                    s_clean = cand_sub
                    issues = []
        if not issues:
            s_clean = clean_formatting(s_clean)
            if not s_clean.endswith(('.', '!', '?')):
                s_clean = s_clean + '.'
            valid_sentences.append(s_clean)

    if not valid_sentences:
        return None

    if platform_key == "facebook":
        factual_sentences = [s for s in valid_sentences if not ClaimValidator.is_approved_cta(s)]
        cta_sentences = [s for s in valid_sentences if ClaimValidator.is_approved_cta(s)]
        cta_to_use = cta_sentences[0] if cta_sentences else ""

        if not factual_sentences:
            if not allow_fallback:
                return None
            factual_sentences = [build_canonical_fallback_sentence(facts)]

        if len(factual_sentences) >= 2:
            mid = max(1, len(factual_sentences) // 2)
            p1 = clean_formatting(" ".join(factual_sentences[:mid]))
            p2_body = clean_formatting(" ".join(factual_sentences[mid:]))
            p2 = clean_formatting(f"{p2_body} {cta_to_use}".strip()) if cta_to_use else p2_body
            return {"text": f"{p1}\n\n{p2}"}
        else:
            p1 = clean_formatting(factual_sentences[0])
            p2 = ""
            for fact in facts.atomic_facts:
                if fact.fact_type in ("specification", "component", "material", "feature"):
                    # SEO keyword lists and search-phrase dumps are never real content:
                    # skip them instead of splicing a keyword blob into the post.
                    if is_keyword_stuffed_text(fact.text):
                        continue
                    cand = re.split(r'\s+-\s+|\bfor\b', fact.text, flags=re.IGNORECASE)[0].strip()
                    cand = clean_formatting(cand).rstrip(".")
                    if cand and len(cand) > 3 and cand.lower() not in p1.lower():
                        cand_sentence = f"Includes {cand}." if not cand.lower().startswith(("includes", "features", "equipped", "with")) else f"{cand}."
                        cand_sentence = clean_formatting(cand_sentence)
                        if not ClaimValidator.validate_content_claims(cand_sentence, facts, disclosure=""):
                            p2 = cand_sentence
                            break
            if not p2:
                # Never duplicate the same sentence into both paragraphs: when only
                # one grounded sentence exists, attach CTA to p2 or return p1.
                if cta_to_use:
                    return {"text": f"{p1}\n\n{cta_to_use}"}
                return {"text": p1}
            p2 = clean_formatting(f"{p2} {cta_to_use}".strip()) if cta_to_use else p2
            return {"text": f"{p1}\n\n{p2}"}
    else:
        # Threads
        factual_sentences = [s for s in valid_sentences if not ClaimValidator.is_approved_cta(s)]
        cta_sentences = [s for s in valid_sentences if ClaimValidator.is_approved_cta(s)]
        if not factual_sentences:
            if not allow_fallback:
                return None
            factual_sentences = [build_canonical_fallback_sentence(facts)]
        cta_to_use = cta_sentences[0] if cta_sentences else ""
        selected_sentences = factual_sentences[:2]
        if cta_to_use:
            selected_sentences.append(cta_to_use)
        combined = clean_formatting(" ".join(selected_sentences))
        return {"text": combined}


class ContentGenerator:
    """
    Modular social media content generator for V2/V3/V4.
    Generates factual, compliant copy for Facebook, Pinterest, and Threads.
    Enforces claim grounding and maximum one regeneration attempt.
    """

    def __init__(self, ollama_client: Optional[OllamaClient] = None):
        self.ollama = ollama_client or OllamaClient()
        self.last_validation_metadata: Dict[str, Dict[str, Any]] = {}

    def validate_platform_content(
        self,
        platform: str,
        content: Dict[str, Any],
        product_or_url: Union[Product, CanonicalProductFacts, str],
        disclosure: str = ""
    ) -> Tuple[bool, List[str]]:
        """
        Validate generated output for compliance, factual grounding, and completeness.

        Returns:
            (is_valid, list_of_issues)
        """
        if isinstance(product_or_url, (Product, CanonicalProductFacts)):
            target_source = product_or_url
        else:
            target_source = Product(title="", features=[], affiliate_url=str(product_or_url))

        return ClaimValidator.validate_platform_content(
            platform=platform,
            content=content,
            product=target_source,
            disclosure=disclosure
        )

    def _ensure_compliance(
        self,
        platform: str,
        content: Dict[str, Any],
        facts: CanonicalProductFacts,
        disclosure: str
    ) -> Dict[str, Any]:
        """Guarantee that affiliate link, approved CTA, and disclosure are present in the final data."""
        platform_key = platform.lower().strip()
        seed = facts.asin or facts.title or ""

        if platform_key.startswith("pinterest"):
            title = clean_formatting(content.get("title", ""))[:100].strip()
            desc = clean_formatting(content.get("description", "")).strip()

            # Ensure there is at least one factually grounded body sentence before the CTA
            body_sentences = ClaimValidator.extract_body_sentences(desc, facts, disclosure)
            if not body_sentences:
                fallback_body = build_canonical_fallback_sentence(facts)
                existing_cta = ClaimValidator.extract_approved_cta(desc)
                if existing_cta:
                    desc = f"{fallback_body} {existing_cta}"
                else:
                    desc = fallback_body

            existing_cta = ClaimValidator.extract_approved_cta(desc)
            if not existing_cta:
                cta = get_approved_cta_for_platform(platform_key, seed=seed)
                # Strip URL and disclosure if already attached to avoid duplication
                if facts.affiliate_url and facts.affiliate_url in desc:
                    desc = desc.replace(facts.affiliate_url, "").strip()
                if disclosure and disclosure.strip() in desc:
                    desc = desc.replace(disclosure.strip(), "").strip()
                desc = clean_formatting(desc)
                if desc and not desc.endswith((".", "!", "?")):
                    desc = f"{desc}."
                desc = f"{desc} {cta}".strip()

            if facts.affiliate_url and facts.affiliate_url not in desc:
                desc = f"{desc} {facts.affiliate_url}".strip()
            if disclosure and disclosure.strip() not in desc:
                desc = f"{desc}\n\n{disclosure.strip()}".strip()
            content["title"] = title if title else clean_formatting(facts.title[:80])
            content["description"] = desc
        else:
            text = clean_formatting(content.get("text", "")).strip()

            # Ensure there is at least one factually grounded body sentence before the CTA
            body_sentences = ClaimValidator.extract_body_sentences(text, facts, disclosure)
            if not body_sentences:
                fallback_body = build_canonical_fallback_sentence(facts)
                existing_cta = ClaimValidator.extract_approved_cta(text)
                if existing_cta:
                    text = f"{fallback_body} {existing_cta}"
                else:
                    text = fallback_body

            existing_cta = ClaimValidator.extract_approved_cta(text)
            if not existing_cta:
                cta = get_approved_cta_for_platform(platform_key, seed=seed)
                # Strip URL and disclosure if already attached
                if facts.affiliate_url and facts.affiliate_url in text:
                    text = text.replace(f"Link: {facts.affiliate_url}", "").replace(facts.affiliate_url, "").strip()
                if disclosure and disclosure.strip() in text:
                    text = text.replace(disclosure.strip(), "").strip()
                text = clean_formatting(text)

                if platform_key == "facebook":
                    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', text) if p.strip()]
                    if len(paragraphs) >= 2:
                        p_last = paragraphs[-1]
                        if not p_last.endswith((".", "!", "?")):
                            p_last = f"{p_last}."
                        paragraphs[-1] = f"{p_last} {cta}"
                        text = "\n\n".join(paragraphs)
                    elif len(paragraphs) == 1:
                        p0 = paragraphs[0]
                        if not p0.endswith((".", "!", "?")):
                            p0 = f"{p0}."
                        text = f"{p0}\n\n{cta}"
                    else:
                        fallback_body = build_canonical_fallback_sentence(facts)
                        text = f"{fallback_body}\n\n{cta}"
                else:
                    # Threads
                    if text and not text.endswith((".", "!", "?")):
                        text = f"{text}."
                    text = f"{text} {cta}".strip()
            else:
                if platform_key == "facebook":
                    body_before_cta, cta_part = ClaimValidator.strip_approved_cta(text)
                    paragraphs = [p.strip() for p in re.split(r'\n\s*\n', body_before_cta) if p.strip()]
                    if len(paragraphs) == 1:
                        p0 = paragraphs[0]
                        if not p0.endswith((".", "!", "?")):
                            p0 = f"{p0}."
                        text = f"{p0}\n\n{cta_part}"

            if facts.affiliate_url and facts.affiliate_url not in text:
                text = f"{text}\n\nLink: {facts.affiliate_url}".strip()
            if disclosure and disclosure.strip() not in text:
                text = f"{text}\n\n{disclosure.strip()}".strip()
            content["text"] = text

        return content

    def generate_for_platform(
        self,
        platform: str,
        product: Union[Product, CanonicalProductFacts],
        disclosure: str,
        account: Optional[str] = None,
        media_type: str = "image"
    ) -> Dict[str, Any]:
        """
        Generate content for a specific social platform with strictly 1 regeneration on failure.

        Args:
            platform: Platform name ('facebook', 'pinterest', 'threads', 'pinterest_lifemodehq').
            product: Normalized Product or CanonicalProductFacts.
            disclosure: Configurable affiliate disclosure string.
            account: Target account override ('dailyideasforyou', 'lifemodehq').
            media_type: Media format type ('image' or 'video').

        Returns:
            Dict[str, Any]: Platform content dictionary.

        Raises:
            ValueError: If platform is not supported.
            RuntimeError: If source facts are insufficient or output fails validation after 1 regeneration attempt.
        """
        platform_key = platform.lower().strip()
        account_key = (account or "").lower().strip()
        media_type_clean = (media_type or "image").lower().strip()
        if media_type_clean not in ("image", "video"):
            media_type_clean = "image"

        pin_account = "dailyideasforyou"
        if platform_key.startswith("pinterest"):
            pin_account = account_key or ("lifemodehq" if "lifemodehq" in platform_key else "dailyideasforyou")
            if pin_account == "lifemodehq" or "lifemodehq" in platform_key:
                platform_key = "pinterest_lifemodehq"
            else:
                platform_key = "pinterest"
            prompt_builder = lambda facts: build_pinterest_prompt(facts, account=pin_account, media_type=media_type_clean)
        elif platform_key == "facebook":
            prompt_builder = lambda facts: build_facebook_prompt(facts, media_type=media_type_clean)
        elif platform_key == "threads":
            prompt_builder = lambda facts: build_threads_prompt(facts, media_type=media_type_clean)
        else:
            raise ValueError(f"Unsupported platform: '{platform}'. Choose from ('facebook', 'pinterest', 'threads', 'pinterest_lifemodehq')")

        if isinstance(product, CanonicalProductFacts):
            facts = product
        else:
            facts = FactExtractor.extract_canonical_facts(product)

        if not facts.is_sufficient():
            self.last_validation_metadata[platform_key] = {
                "status": "INSUFFICIENT_SOURCE_FACTS",
                "retry_count": 0,
                "errors": ["Product title or features are insufficient to safely generate content."]
            }
            raise RuntimeError("INSUFFICIENT_SOURCE_FACTS: Product title or features are insufficient to safely generate content.")

        prompt = prompt_builder(facts)
        num_predict = effective_num_predict(
            platform_key,
            getattr(self.ollama, "num_predict", None),
        )
        num_ctx = recommended_num_ctx(
            prompt,
            num_predict,
            getattr(self.ollama, "num_ctx", 1024),
        )
        # 1. First Generation Attempt
        retry_count = 0
        first_error: Optional[BaseException] = None
        try:
            raw_output = self.ollama.generate_text(
                prompt=prompt,
                num_predict=num_predict,
                num_ctx=num_ctx,
                temperature=0.0,
                json_format=False
            )
        except OllamaProviderError as exc:
            if not is_empty_response_error(exc):
                raise
            raw_output = ""
            first_error = exc

        if first_error is not None:
            # An empty response is a per-platform content failure: it consumes
            # this platform's single allowed regeneration instead of aborting
            # the whole multi-platform run.
            is_valid = False
            issues = [
                f"Content provider returned an empty response for '{platform_key}' "
                f"on the first generation attempt."
            ]
        else:
            extracted = extract_grounded_platform_content(platform_key, raw_output, facts, disclosure, allow_fallback=False)
            if extracted is not None:
                result = self._ensure_compliance(platform_key, extracted, facts, disclosure)
                is_valid, issues = self.validate_platform_content(platform_key, result, facts, disclosure)
            else:
                is_valid = False
                base_issues = ClaimValidator.validate_content_claims(raw_output, facts, disclosure=disclosure)
                missing_body_msg = (
                    "Pinterest description must contain at least one factually grounded product description sentence before the CTA."
                    if platform_key.startswith("pinterest")
                    else f"{platform_key.upper()} post must contain at least one factually grounded product description sentence before the CTA."
                )
                issues = base_issues if base_issues else [missing_body_msg]

        # 2. Validate First Output & If invalid, attempt regeneration EXACTLY ONCE
        if not is_valid:
            retry_count = 1
            issues_summary = "\n- ".join(issues)
            if platform_key.startswith("pinterest"):
                account_angle = (
                    "Lifestyle and home living angle for LifeModeHQ"
                    if "lifemodehq" in platform_key
                    else "Discovery and practical shopping ideas for dailyideasforyou"
                )
                media_rule = (
                    "Video format: Write a natural opening hook suited to video clip."
                    if media_type_clean == "video"
                    else "Image format: Focus on product appearance, design, and physical attributes."
                )
                retry_prompt = (
                    f"You are a technical product copy editor creating a natural, factual Pinterest Pin JSON ({account_angle}. {media_rule}).\n\n"
                    f"{COMMON_CONTENT_RULES}\n\n"
                    f"CORRECTION REQUIRED - PREVIOUS ATTEMPT FAILED WITH ISSUES:\n"
                    f"- {issues_summary}\n\n"
                    f"CRITICAL FIX INSTRUCTIONS:\n"
                    f"1. Remove every unsupported claim, outcome verb, and ungrounded phrase mentioned above.\n"
                    f"2. Only state confirmed physical features and measurements directly from SOURCE FACTS.\n"
                    f"3. Do not add ungrounded outcome verbs, benefits, certifications, or marketing conclusions.\n"
                    f"4. Keep the description concise (1-2 natural factual sentences stating only verified components/specifications).\n"
                    f"5. Do NOT use bullet points, list dashes, or raw specification dumps. Use clean punctuation.\n\n"
                    f"{format_source_facts(facts)}\n\n"
                    f"Respond with valid JSON now:\n"
                    f"{{\n  \"title\": \"...\",\n  \"description\": \"...\"\n}}"
                )
            else:
                if platform_key == "facebook":
                    if media_type_clean == "video":
                        format_rule = (
                            "1. Write EXACTLY 2 short paragraphs separated by a blank line (double newline) organizing confirmed physical facts.\n"
                            "2. Include a natural opening hook suited to video demonstration.\n"
                            "3. Do NOT write bullet points, list dashes (-), or a single paragraph."
                        )
                    else:
                        format_rule = (
                            "1. Write EXACTLY 2 short paragraphs separated by a blank line (double newline) organizing confirmed physical facts.\n"
                            "2. Focus on product appearance, design, features, and practical attributes.\n"
                            "3. Do NOT write bullet points, list dashes (-), or a single paragraph."
                        )
                else:
                    if media_type_clean == "video":
                        format_rule = "1. Rewrite the post in 1 to 2 natural, concise factual sentences with a natural opening hook suited to video."
                    else:
                        format_rule = "1. Rewrite the post in 1 to 2 natural, concise factual sentences stating only confirmed physical components and measurements."

                retry_prompt = (
                    f"You are writing a natural factual {platform_key.upper()} post.\n\n"
                    f"{COMMON_CONTENT_RULES}\n\n"
                    f"CORRECTION REQUIRED - PREVIOUS ATTEMPT FAILED WITH ISSUES:\n"
                    f"- {issues_summary}\n\n"
                    f"CRITICAL FIX RULES:\n"
                    f"{format_rule}\n"
                    f"3. State ONLY confirmed physical features, components, and specifications directly from SOURCE FACTS.\n"
                    f"4. DO NOT use words, adjectives, or concepts not present in SOURCE FACTS (e.g. no 'versatile', no 'solution', no 'addition').\n"
                    f"5. Do not invent benefits, outcomes, ease-of-cleaning, or causal claims.\n"
                    f"6. Write complete, fluent sentences with clean punctuation (no dangling commas or spec lists).\n\n"
                    f"{format_source_facts(facts)}\n\n"
                    f"Write the corrected post body text now:\n"
                    f"Post:"
                )

            retry_num_ctx = recommended_num_ctx(
                retry_prompt,
                num_predict,
                getattr(self.ollama, "num_ctx", 1024),
            )

            try:
                retry_output = self.ollama.generate_text(
                    prompt=retry_prompt,
                    num_predict=num_predict,
                    num_ctx=retry_num_ctx,
                    temperature=0.0,
                    json_format=False
                )
            except OllamaProviderError as exc:
                if not is_empty_response_error(exc):
                    raise
                # Both allowed attempts returned no content: fail closed with an
                # explicit error. Empty output is never marked valid.
                empty_issues = [
                    f"Content provider returned an empty response for '{platform_key}' "
                    f"after 1 regeneration attempt."
                ]
                self.last_validation_metadata[platform_key] = {
                    "status": "FAILED_VALIDATION",
                    "retry_count": 1,
                    "errors": empty_issues
                }
                raise RuntimeError(
                    f"Content generation for '{platform_key}' failed validation after 1 regeneration attempt: "
                    f"{'; '.join(empty_issues)}"
                ) from exc

            retry_extracted = extract_grounded_platform_content(platform_key, retry_output, facts, disclosure, allow_fallback=True)
            if retry_extracted is not None:
                result = self._ensure_compliance(platform_key, retry_extracted, facts, disclosure)
                is_valid, retry_issues = self.validate_platform_content(platform_key, result, facts, disclosure)
            else:
                is_valid = False
                retry_issues = ClaimValidator.validate_content_claims(retry_output, facts, disclosure=disclosure) or ["Output contains unsupported claims."]

            if not is_valid:
                self.last_validation_metadata[platform_key] = {
                    "status": "FAILED_VALIDATION",
                    "retry_count": retry_count,
                    "errors": retry_issues
                }
                raise RuntimeError(
                    f"Content generation for '{platform_key}' failed validation after 1 regeneration attempt: {'; '.join(retry_issues)}"
                )

        result["media_type"] = media_type_clean
        if "text" in result and "content" not in result:
            result["content"] = result["text"]
        if platform_key.startswith("pinterest"):
            result["account"] = pin_account

        self.last_validation_metadata[platform_key] = {
            "status": "VALID",
            "retry_count": retry_count,
            "errors": []
        }
        return result

    def generate_all_platforms(
        self,
        product: Union[Product, CanonicalProductFacts],
        disclosure: str,
        include_variants: bool = False
    ) -> Dict[str, Any]:
        """
        Generate content across all supported platforms: Facebook, Pinterest, and Threads.

        Args:
            product: Normalized Product dataclass or CanonicalProductFacts.
            disclosure: Configurable affiliate disclosure string.
            include_variants: When True, also generates video variants and account-specific Pinterest copy.

        Returns:
            Dict[str, Any]: Combined generation dictionary with 'platforms' and validation metadata.
        """
        if isinstance(product, CanonicalProductFacts):
            facts = product
        else:
            facts = FactExtractor.extract_canonical_facts(product)

        platforms_results: Dict[str, Any] = {}
        for platform_key in ("facebook", "pinterest", "threads"):
            try:
                platforms_results[platform_key] = self.generate_for_platform(
                    platform=platform_key,
                    product=facts,
                    disclosure=disclosure,
                    account="dailyideasforyou" if platform_key == "pinterest" else None,
                    media_type="image"
                )
            except OllamaProviderError as e:
                platforms_results[platform_key] = {"error": str(e)}
                if is_empty_response_error(e):
                    # One platform returning no content does not prove the shared
                    # provider is down: record the failure for this platform and
                    # keep generating the remaining platforms.
                    self.last_validation_metadata[platform_key] = {
                        "status": "FAILED_VALIDATION",
                        "retry_count": self.last_validation_metadata.get(platform_key, {}).get("retry_count", 0),
                        "errors": [str(e)]
                    }
                    continue
                # Provider failures are shared by all platforms; stop instead of
                # spending another full model timeout on each platform.
                for remaining in ("facebook", "pinterest", "threads"):
                    if remaining not in platforms_results:
                        platforms_results[remaining] = {
                            "error": f"Not attempted because {getattr(self.ollama, 'provider_name', 'content provider')} failed at {platform_key}: {e}"
                        }
                break
            except Exception as e:
                platforms_results[platform_key] = {"error": str(e)}

        if include_variants:
            # Facebook video
            try:
                fb_vid = self.generate_for_platform("facebook", product=facts, disclosure=disclosure, media_type="video")
            except Exception as e:
                fb_vid = {"content": platforms_results.get("facebook", {}).get("content", ""), "error": str(e)}

            # Threads video
            try:
                th_vid = self.generate_for_platform("threads", product=facts, disclosure=disclosure, media_type="video")
            except Exception as e:
                th_vid = {"content": platforms_results.get("threads", {}).get("content", ""), "error": str(e)}

            # Pinterest dailyideasforyou video
            try:
                pin_vid = self.generate_for_platform("pinterest", product=facts, disclosure=disclosure, account="dailyideasforyou", media_type="video")
            except Exception as e:
                pin_vid = {
                    "title": platforms_results.get("pinterest", {}).get("title", ""),
                    "description": platforms_results.get("pinterest", {}).get("description", ""),
                    "error": str(e)
                }

            # Pinterest lifemodehq image & video
            try:
                lm_img = self.generate_for_platform("pinterest_lifemodehq", product=facts, disclosure=disclosure, account="lifemodehq", media_type="image")
            except Exception as e:
                lm_fb_img = build_lifemodehq_fallback(facts, media_type="image")
                lm_img = {
                    "title": lm_fb_img["title"],
                    "description": lm_fb_img["description"],
                    "error": str(e)
                }

            try:
                lm_vid = self.generate_for_platform("pinterest_lifemodehq", product=facts, disclosure=disclosure, account="lifemodehq", media_type="video")
            except Exception as e:
                lm_fb_vid = build_lifemodehq_fallback(facts, media_type="video")
                lm_vid = {
                    "title": lm_fb_vid["title"],
                    "description": lm_fb_vid["description"],
                    "error": str(e)
                }

            if "facebook" in platforms_results and "error" not in platforms_results["facebook"]:
                platforms_results["facebook"]["variants"] = {
                    "image": {"content": platforms_results["facebook"].get("content", "")},
                    "video": {"content": fb_vid.get("content") or fb_vid.get("text", "")}
                }
            if "threads" in platforms_results and "error" not in platforms_results["threads"]:
                platforms_results["threads"]["variants"] = {
                    "image": {"content": platforms_results["threads"].get("content", "")},
                    "video": {"content": th_vid.get("content") or th_vid.get("text", "")}
                }
            if "pinterest" in platforms_results and "error" not in platforms_results["pinterest"]:
                platforms_results["pinterest"]["variants"] = {
                    "image": {
                        "title": platforms_results["pinterest"].get("title", ""),
                        "description": platforms_results["pinterest"].get("description", "")
                    },
                    "video": {
                        "title": pin_vid.get("title", ""),
                        "description": pin_vid.get("description", "")
                    }
                }
                platforms_results["pinterest"]["accounts"] = {
                    "dailyideasforyou": {
                        "title": platforms_results["pinterest"].get("title", ""),
                        "description": platforms_results["pinterest"].get("description", ""),
                        "variants": platforms_results["pinterest"]["variants"]
                    },
                    "lifemodehq": {
                        "title": lm_img.get("title", ""),
                        "description": lm_img.get("description", ""),
                        "variants": {
                            "image": {
                                "title": lm_img.get("title", ""),
                                "description": lm_img.get("description", "")
                            },
                            "video": {
                                "title": lm_vid.get("title", ""),
                                "description": lm_vid.get("description", "")
                            }
                        }
                    }
                }
                platforms_results["pinterest_lifemodehq"] = {
                    "title": lm_img.get("title", ""),
                    "description": lm_img.get("description", ""),
                    "status": lm_img.get("status", "VALID"),
                    "account": "lifemodehq",
                    "variants": platforms_results["pinterest"]["accounts"]["lifemodehq"]["variants"]
                }
        else:
            # Default single-variant structure for backward compatibility
            for p_key in ("facebook", "threads"):
                if p_key in platforms_results and "error" not in platforms_results[p_key]:
                    cnt = platforms_results[p_key].get("content") or platforms_results[p_key].get("text", "")
                    platforms_results[p_key]["variants"] = {
                        "image": {"content": cnt},
                        "video": {"content": cnt}
                    }
            if "pinterest" in platforms_results and "error" not in platforms_results["pinterest"]:
                t = platforms_results["pinterest"].get("title", "")
                d = platforms_results["pinterest"].get("description", "")
                lm_fb_img = build_lifemodehq_fallback(facts, media_type="image")
                lm_fb_vid = build_lifemodehq_fallback(facts, media_type="video")
                platforms_results["pinterest"]["variants"] = {
                    "image": {"title": t, "description": d},
                    "video": {"title": t, "description": d}
                }
                platforms_results["pinterest"]["accounts"] = {
                    "dailyideasforyou": {
                        "title": t,
                        "description": d,
                        "variants": platforms_results["pinterest"]["variants"]
                    },
                    "lifemodehq": {
                        "title": lm_fb_img["title"],
                        "description": lm_fb_img["description"],
                        "variants": {
                            "image": {"title": lm_fb_img["title"], "description": lm_fb_img["description"]},
                            "video": {"title": lm_fb_vid["title"], "description": lm_fb_vid["description"]}
                        }
                    }
                }

        provider_diagnostics = getattr(self.ollama, "response_metadata_history", None)
        result = {
            "asin": facts.asin or "",
            "affiliate_url": facts.affiliate_url or "",
            "platforms": platforms_results,
            "validation_metadata": self.last_validation_metadata,
            "generated_with": self.ollama.model
        }
        if isinstance(provider_diagnostics, list) and provider_diagnostics:
            result["provider_diagnostics"] = provider_diagnostics[-20:]
        return result

    def generate_all_variants(
        self,
        product: Union[Product, CanonicalProductFacts],
        disclosure: str
    ) -> Dict[str, Any]:
        """Generate full social copy suite across all platforms, media types, and accounts."""
        return self.generate_all_platforms(product=product, disclosure=disclosure, include_variants=True)

    @staticmethod
    def save_social_content(
        social_data: Dict[str, Any],
        cache_key: str,
        target_dir: Optional[Path] = None
    ) -> Path:
        """Save generated social media content to JSON file."""
        if target_dir is None:
            target_dir = Path("data/generated")
        target_dir.mkdir(parents=True, exist_ok=True)
        file_path = target_dir / f"{cache_key}_social.json"
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(social_data, f, indent=2, ensure_ascii=False)
        return file_path
