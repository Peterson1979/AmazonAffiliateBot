"""
Content generation module for AmazonAffiliateBot (V2).
Provides platform-specific prompt builders, factual claim validation, canonical fact extraction, and social media copy generation.
"""

from .facts import FactExtractor, CanonicalProductFacts, SourceFact
from .prompts import PLATFORM_PROMPTS
from .validator import ClaimValidator
from .generator import ContentGenerator
from .package import ContentPackage

__all__ = [
    "ContentGenerator",
    "ClaimValidator",
    "PLATFORM_PROMPTS",
    "ContentPackage",
    "FactExtractor",
    "CanonicalProductFacts",
    "SourceFact"
]
