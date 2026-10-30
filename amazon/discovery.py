"""
Amazon Product Discovery and Candidate Extraction Module for AmazonAffiliateBot.
Automates product search and candidate discovery across configurable categories using Selenium + Firefox.
Enforces strict quality filtering, persistent ASIN deduplication, and zero fabrication.
"""

from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional, Tuple, Set, Union, Callable
from pathlib import Path
from urllib.parse import quote_plus, urlparse, parse_qs
from datetime import datetime
import json
import re
import time

from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, WebDriverException
from zoneinfo import ZoneInfo

from amazon.scraper import Product, extract_asin_from_url, init_firefox_driver


def get_budapest_timezone() -> ZoneInfo:
    return ZoneInfo("Europe/Budapest")


get_budapest_tz = get_budapest_timezone


DEFAULT_DISCOVERY_DIR = Path("data/discovery")
DEFAULT_HISTORY_FILE = DEFAULT_DISCOVERY_DIR / "discovery_history.json"
DEFAULT_CONFIG_QUERIES = [
    "kitchen accessories and gadgets",
    "home office ergonomic desk setup",
    "camping and hiking gear essentials",
    "espresso machine accessories",
    "portable tech accessories"
]
DEFAULT_TRACKING_TAG = "homeideas4a01-20"


@dataclass
class DiscoveredCandidate:
    """Represents a raw candidate product discovered on Amazon search."""
    asin: str
    title: str
    source_url: str
    affiliate_url: str
    query: str
    price: Optional[str] = None
    image_url: Optional[str] = None
    discovered_at: str = field(default_factory=lambda: datetime.now(get_budapest_timezone()).isoformat())

    def to_dict(self) -> Dict[str, Any]:
        return {
            "asin": self.asin,
            "title": self.title,
            "source_url": self.source_url,
            "affiliate_url": self.affiliate_url,
            "query": self.query,
            "price": self.price,
            "image_url": self.image_url,
            "discovered_at": self.discovered_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DiscoveredCandidate":
        return cls(
            asin=data.get("asin", ""),
            title=data.get("title", ""),
            source_url=data.get("source_url", ""),
            affiliate_url=data.get("affiliate_url", ""),
            query=data.get("query", ""),
            price=data.get("price"),
            image_url=data.get("image_url"),
            discovered_at=data.get("discovered_at", datetime.now(get_budapest_tz()).isoformat()),
        )


class DiscoveryHistory:
    """
    Persistent repository of all discovered and processed ASINs to ensure persistent deduplication.
    Prevents re-processing products previously accepted, queued, or rejected.
    """

    def __init__(self, history_file: Optional[Union[str, Path]] = None):
        self.history_file = Path(history_file) if history_file else DEFAULT_HISTORY_FILE
        self.records: Dict[str, Dict[str, Any]] = {}
        self.load()

    def load(self) -> None:
        """Load history from JSON file."""
        if not self.history_file.exists():
            self.records = {}
            return
        try:
            with open(self.history_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    self.records = data.get("records", {})
        except Exception:
            self.records = {}

    def save(self) -> Path:
        """Save history to JSON file."""
        self.history_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.history_file, "w", encoding="utf-8") as f:
            json.dump({
                "updated_at": datetime.now(get_budapest_tz()).isoformat(),
                "total_records": len(self.records),
                "records": self.records
            }, f, indent=2, ensure_ascii=False)
        return self.history_file

    def is_known_asin(self, asin: str) -> bool:
        """Check if ASIN was already discovered and recorded."""
        if not asin:
            return False
        return asin.upper() in self.records

    def record_candidate(
        self,
        candidate: DiscoveredCandidate,
        status: str,
        rejection_reason: Optional[str] = None
    ) -> None:
        """Record candidate outcome in persistent history."""
        if not candidate.asin:
            return
        asin_key = candidate.asin.upper()
        self.records[asin_key] = {
            "asin": asin_key,
            "title": candidate.title,
            "source_url": candidate.source_url,
            "affiliate_url": candidate.affiliate_url,
            "query": candidate.query,
            "price": candidate.price,
            "status": status,  # ACCEPTED, QUEUED, REJECTED
            "rejection_reason": rejection_reason,
            "recorded_at": datetime.now(get_budapest_tz()).isoformat()
        }


class AmazonDiscoveryEngine:
    """
    Amazon Product Discovery Engine using Selenium + Firefox.
    Navigates search pages, extracts candidate ASINs and metadata,
    and applies quality and deduplication filters.
    """

    def __init__(
        self,
        tracking_tag: str = DEFAULT_TRACKING_TAG,
        history: Optional[DiscoveryHistory] = None,
        headless: bool = True,
        firefox_profile: str = ""
    ):
        self.tracking_tag = tracking_tag or DEFAULT_TRACKING_TAG
        self.history = history or DiscoveryHistory()
        self.headless = headless
        self.firefox_profile = firefox_profile

    def build_search_url(self, query: str, page: int = 1) -> str:
        """Construct canonical Amazon search URL."""
        encoded_query = quote_plus(query.strip())
        url = f"https://www.amazon.com/s?k={encoded_query}&s=relevanceblender"
        if page > 1:
            url += f"&page={page}"
        return url

    def build_affiliate_url(self, asin: str) -> str:
        """Construct standard Amazon affiliate URL with tracking tag."""
        clean_asin = asin.upper().strip()
        tag = self.tracking_tag.strip()
        if tag:
            return f"https://www.amazon.com/dp/{clean_asin}?tag={tag}"
        return f"https://www.amazon.com/dp/{clean_asin}"

    def filter_candidate(
        self,
        candidate: DiscoveredCandidate,
        existing_asins: Set[str],
        existing_urls: Set[str],
        batch_asins: Set[str]
    ) -> Tuple[bool, Optional[str]]:
        """
        Evaluate candidate against quality, completeness, and deduplication rules:
        - Must have valid 10-character alphanumeric ASIN
        - Must have non-empty title (> 5 characters)
        - Must not be duplicate of existing queue ASIN
        - Must not be duplicate of existing queue affiliate URL
        - Must not be duplicate within current batch
        - Must not be duplicate of previously processed ASIN in history
        """
        # 1. ASIN check
        if not candidate.asin or len(candidate.asin) != 10 or not candidate.asin.isalnum():
            return False, "INVALID_ASIN"

        asin_upper = candidate.asin.upper()

        # 2. Title check
        if not candidate.title or len(candidate.title.strip()) < 5:
            return False, "MISSING_TITLE"

        # 3. Batch deduplication
        if asin_upper in batch_asins:
            return False, "DUPLICATE_IN_BATCH"

        # 4. Queue ASIN deduplication
        if asin_upper in existing_asins:
            return False, "DUPLICATE_IN_QUEUE"

        # 5. Queue URL deduplication
        if candidate.affiliate_url in existing_urls or candidate.source_url in existing_urls:
            return False, "DUPLICATE_URL"

        # 6. Persistent History deduplication
        if self.history.is_known_asin(asin_upper):
            return False, "DUPLICATE_IN_HISTORY"

        return True, None

    def search_and_extract_candidates(
        self,
        query: str,
        max_candidates: int = 10,
        driver_provider: Optional[Callable[[], Any]] = None
    ) -> List[DiscoveredCandidate]:
        """
        Execute an Amazon search query and extract raw candidate products.
        Fails gracefully if blocked or challenged, returning whatever valid candidates were extracted.
        """
        search_url = self.build_search_url(query)
        candidates: List[DiscoveredCandidate] = []
        driver = None

        try:
            if driver_provider:
                driver = driver_provider()
            else:
                driver = init_firefox_driver(headless=self.headless, firefox_profile=self.firefox_profile)

            driver.get(search_url)
            time.sleep(2)

            # Wait for search results container
            try:
                WebDriverWait(driver, 10).until(
                    lambda d: d.find_elements(By.CSS_SELECTOR, "div[data-asin]")
                    or d.find_elements(By.CSS_SELECTOR, "div.s-result-item")
                )
            except TimeoutException:
                pass

            # Detect anti-bot / captcha page
            page_source_lower = driver.page_source.lower()
            if "robot check" in page_source_lower or "enter the characters you see below" in page_source_lower:
                # Bounded fail-closed: return empty candidates without fabrication
                return candidates

            result_items = driver.find_elements(By.CSS_SELECTOR, "div[data-asin], div.s-result-item")

            seen_in_page = set()
            for item in result_items:
                if len(candidates) >= max_candidates:
                    break

                try:
                    asin = item.get_attribute("data-asin")
                    if not asin:
                        # Try to find asin from link
                        links = item.find_elements(By.CSS_SELECTOR, "a[href*='/dp/'], a[href*='/gp/product/']")
                        if links:
                            href = links[0].get_attribute("href") or ""
                            asin = extract_asin_from_url(href)

                    if not asin or len(asin.strip()) != 10 or not asin.strip().isalnum():
                        continue

                    asin = asin.strip().upper()
                    if asin in seen_in_page:
                        continue

                    # Extract title
                    title = ""
                    title_elems = item.find_elements(By.CSS_SELECTOR, "h2 a span, h2 span, h2 a")
                    if title_elems:
                        title = title_elems[0].text.strip() or title_elems[0].get_attribute("textContent").strip()

                    if not title or len(title) < 5:
                        continue

                    # Extract price (optional)
                    price = None
                    price_elems = item.find_elements(By.CSS_SELECTOR, ".a-price .a-offscreen, span.a-price-whole")
                    if price_elems:
                        p_text = price_elems[0].get_attribute("textContent").strip() or price_elems[0].text.strip()
                        if p_text:
                            price = p_text

                    # Extract image (optional)
                    image_url = None
                    img_elems = item.find_elements(By.CSS_SELECTOR, "img.s-image")
                    if img_elems:
                        image_url = img_elems[0].get_attribute("src")

                    canonical_url = f"https://www.amazon.com/dp/{asin}"
                    affiliate_url = self.build_affiliate_url(asin)

                    cand = DiscoveredCandidate(
                        asin=asin,
                        title=" ".join(title.split()),
                        source_url=canonical_url,
                        affiliate_url=affiliate_url,
                        query=query,
                        price=price,
                        image_url=image_url
                    )
                    candidates.append(cand)
                    seen_in_page.add(asin)

                except Exception:
                    continue

        except Exception:
            pass
        finally:
            if driver and not driver_provider:
                try:
                    driver.quit()
                except Exception:
                    pass

        return candidates
