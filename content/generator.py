"""
Content Generator module for AmazonAffiliateBot (V2/V3/V4).
Orchestrates platform-specific social media copy generation using local Ollama (qwen2.5:7b).
Supported active platforms: X, Facebook, Pinterest, and Threads.
Enforces strict claim validation and maximum 1 regeneration attempt.
"""

from typing import Dict, Any, List, Optional, Tuple, Union
import json
import re
from pathlib import Path

from amazon.scraper import Product
from ai.ollama import OllamaClient
from content.facts import FactExtractor, CanonicalProductFacts
from content.prompts import (
    PLATFORM_PROMPTS,
    COMMON_CONTENT_RULES,
    format_source_facts
)
from content.validator import (
    ClaimValidator,
    PRICE_PATTERNS,
    DISCOUNT_DEAL_PATTERNS,
    PERSONAL_EXPERIENCE_PATTERNS
)


# Token budget per platform for CPU efficiency and snappiness
PLATFORM_PREDICT_TOKENS = {
    "x": 100,
    "facebook": 320,
    "pinterest": 180,
    "threads": 120,
}


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
        r"^(?:Post|Recommendation|Threads post|X post|Facebook post|Title|Description):\s*",
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


def extract_grounded_platform_content(
    platform_key: str,
    raw_text: str,
    facts: CanonicalProductFacts,
    disclosure: str
) -> Optional[Dict[str, Any]]:
    """
    Extract, filter, and structure generated content so that every sentence is deterministically
    verified against CanonicalProductFacts propositions.
    Returns None if no valid grounded propositions could be extracted from the model's output.
    """
    if platform_key == "pinterest":
        parsed = parse_pinterest_response(raw_text, facts, disclosure)
        title_raw = clean_formatting(parsed.get("title", "").strip())
        title_issues = ClaimValidator.validate_content_claims(title_raw, facts, disclosure="")
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
            description = clean_formatting(" ".join(valid_sentences[:2]))
            return {"title": title, "description": description[:400].strip()}
        return None

    # Text-based platforms: X, Facebook, Threads
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
        if len(valid_sentences) >= 2:
            mid = max(1, len(valid_sentences) // 2)
            p1 = clean_formatting(" ".join(valid_sentences[:mid]))
            p2 = clean_formatting(" ".join(valid_sentences[mid:]))
            return {"text": f"{p1}\n\n{p2}"}
        else:
            p1 = clean_formatting(valid_sentences[0])
            p2 = ""
            for fact in facts.atomic_facts:
                if fact.fact_type in ("specification", "component", "material", "feature"):
                    cand = re.split(r'\s+-\s+|\bfor\b', fact.text, flags=re.IGNORECASE)[0].strip()
                    cand = clean_formatting(cand).rstrip(".")
                    if cand and len(cand) > 3 and cand.lower() not in p1.lower():
                        cand_sentence = f"Includes {cand}." if not cand.lower().startswith(("includes", "features", "equipped", "with")) else f"{cand}."
                        cand_sentence = clean_formatting(cand_sentence)
                        if not ClaimValidator.validate_content_claims(cand_sentence, facts, disclosure=""):
                            p2 = cand_sentence
                            break
            if not p2:
                p2 = p1
            return {"text": f"{p1}\n\n{p2}"}
    else:
        # X / Threads
        combined = clean_formatting(" ".join(valid_sentences[:2]))
        return {"text": combined}


class ContentGenerator:
    """
    Modular social media content generator for V2/V3/V4.
    Generates factual, compliant copy for X, Facebook, Pinterest, and Threads.
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
        """Guarantee that affiliate link and disclosure are present in the final data."""
        if platform == "pinterest":
            title = clean_formatting(content.get("title", ""))[:100].strip()
            desc = clean_formatting(content.get("description", "")).strip()
            if facts.affiliate_url and facts.affiliate_url not in desc:
                desc = f"{desc} {facts.affiliate_url}".strip()
            if disclosure and disclosure.strip() not in desc:
                desc = f"{desc}\n\n{disclosure.strip()}".strip()
            content["title"] = title if title else clean_formatting(facts.title[:80])
            content["description"] = desc
        else:
            text = clean_formatting(content.get("text", "")).strip()
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
        disclosure: str
    ) -> Dict[str, Any]:
        """
        Generate content for a specific social platform with strictly 1 regeneration on failure.

        Args:
            platform: Platform name ('x', 'facebook', 'pinterest', 'threads').
            product: Normalized Product or CanonicalProductFacts.
            disclosure: Configurable affiliate disclosure string.

        Returns:
            Dict[str, Any]: Platform content dictionary.

        Raises:
            ValueError: If platform is not supported.
            RuntimeError: If source facts are insufficient or output fails validation after 1 regeneration attempt.
        """
        platform_key = platform.lower().strip()
        prompt_builder = PLATFORM_PROMPTS.get(platform_key)
        if not prompt_builder:
            raise ValueError(f"Unsupported platform: '{platform}'. Choose from {list(PLATFORM_PROMPTS.keys())}")

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
        num_predict = PLATFORM_PREDICT_TOKENS.get(platform_key, 160)
        # 1. First Generation Attempt
        retry_count = 0
        raw_output = self.ollama.generate_text(
            prompt=prompt,
            num_predict=num_predict,
            num_ctx=1024,
            temperature=0.0,
            json_format=False
        )

        extracted = extract_grounded_platform_content(platform_key, raw_output, facts, disclosure)
        if extracted is not None:
            result = self._ensure_compliance(platform_key, extracted, facts, disclosure)
            is_valid, issues = self.validate_platform_content(platform_key, result, facts, disclosure)
        else:
            is_valid = False
            issues = ClaimValidator.validate_content_claims(raw_output, facts, disclosure=disclosure) or ["Output contains unsupported claims."]

        # 2. Validate First Output & If invalid, attempt regeneration EXACTLY ONCE
        if not is_valid:
            retry_count = 1
            issues_summary = "\n- ".join(issues)
            if platform_key == "pinterest":
                retry_prompt = (
                    f"You are a technical product copy editor creating a natural, factual Pinterest Pin JSON.\n\n"
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
                    format_rule = (
                        "1. Write EXACTLY 2 short paragraphs separated by a blank line (double newline) organizing confirmed physical facts.\n"
                        "2. Do NOT write bullet points, list dashes (-), or a single paragraph."
                    )
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

            retry_output = self.ollama.generate_text(
                prompt=retry_prompt,
                num_predict=num_predict,
                num_ctx=1024,
                temperature=0.0,
                json_format=False
            )

            retry_extracted = extract_grounded_platform_content(platform_key, retry_output, facts, disclosure)
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

        self.last_validation_metadata[platform_key] = {
            "status": "VALID",
            "retry_count": retry_count,
            "errors": []
        }
        return result

    def generate_all_platforms(
        self,
        product: Union[Product, CanonicalProductFacts],
        disclosure: str
    ) -> Dict[str, Any]:
        """
        Generate content across all 4 supported platforms: X, Facebook, Pinterest, and Threads.

        Args:
            product: Normalized Product dataclass or CanonicalProductFacts.
            disclosure: Configurable affiliate disclosure string.

        Returns:
            Dict[str, Any]: Combined generation dictionary with 'platforms' and validation metadata.
        """
        if isinstance(product, CanonicalProductFacts):
            facts = product
        else:
            facts = FactExtractor.extract_canonical_facts(product)

        platforms_results: Dict[str, Any] = {}
        for platform_key in ("x", "facebook", "pinterest", "threads"):
            try:
                platforms_results[platform_key] = self.generate_for_platform(
                    platform=platform_key,
                    product=facts,
                    disclosure=disclosure
                )
            except Exception as e:
                platforms_results[platform_key] = {
                    "error": str(e)
                }

        return {
            "asin": facts.asin or "",
            "affiliate_url": facts.affiliate_url or "",
            "platforms": platforms_results,
            "validation_metadata": self.last_validation_metadata,
            "generated_with": self.ollama.model
        }

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
