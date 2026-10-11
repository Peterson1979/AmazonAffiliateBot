"""Amazon scraper and discovery module for AmazonAffiliateBot."""
from .scraper import Product, scrape_amazon_product, extract_asin_from_url
from .discovery import AmazonDiscoveryEngine, DiscoveryHistory, DiscoveredCandidate

__all__ = [
    "Product",
    "scrape_amazon_product",
    "extract_asin_from_url",
    "AmazonDiscoveryEngine",
    "DiscoveryHistory",
    "DiscoveredCandidate",
]
