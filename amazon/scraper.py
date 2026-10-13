"""
Amazon Product Scraper using Selenium and Firefox.
Extracts product information and normalizes it into a Python dataclass.
"""

from dataclasses import dataclass, field, asdict
import json
import os
import re
import time
from pathlib import Path
from typing import List, Optional, Dict, Any
from urllib.parse import urlparse

from selenium import webdriver
from selenium.webdriver.firefox.options import Options
from selenium.webdriver.firefox.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import WebDriverException, TimeoutException


@dataclass
class Product:
    """Normalized product data container. Contains only primitive Python types."""
    title: str
    features: List[str] = field(default_factory=list)
    affiliate_url: str = ""
    source_url: str = ""
    price: Optional[str] = None
    asin: Optional[str] = None
    image_url: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        """Convert product dataclass to a JSON-serializable dictionary."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Product":
        """Instantiate Product from a dictionary."""
        return cls(
            title=data.get("title", ""),
            features=data.get("features", []),
            affiliate_url=data.get("affiliate_url", ""),
            source_url=data.get("source_url", ""),
            price=data.get("price"),
            asin=data.get("asin"),
            image_url=data.get("image_url"),
        )

    def to_json(self, indent: int = 2) -> str:
        """Serialize product data to JSON string."""
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


def extract_asin_from_url(url: str) -> Optional[str]:
    """Extract 10-character Amazon ASIN from a product or redirect URL if available."""
    if not url:
        return None
    import urllib.parse
    decoded_url = urllib.parse.unquote(str(url))

    match = re.search(r'/(?:dp|gp/product|product)/([A-Z0-9]{10})', decoded_url, re.IGNORECASE)
    if match:
        return match.group(1).upper()
    
    # Try finding ASIN in query params like /dp/B0... or ?asin=B0... or &dp=B0...
    match_query = re.search(r'[?&](?:asin|dp)=([A-Z0-9]{10})', decoded_url, re.IGNORECASE)
    if match_query:
        return match_query.group(1).upper()
        
    return None


def is_valid_image_url(value: Any) -> bool:
    """Accept only a non-empty HTTPS URL suitable for an image API."""
    if not isinstance(value, str) or not value.strip():
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme == "https" and bool(parsed.netloc) and bool(parsed.path)


def _resolve_local_firefox_binary() -> Optional[Path]:
    """Find an installed Firefox binary without invoking Selenium Manager."""
    configured = os.environ.get("AMAZON_FIREFOX_BINARY", "").strip()
    candidates = [
        Path(configured) if configured else None,
        Path(r"C:\Program Files\Mozilla Firefox\firefox.exe"),
        Path(r"C:\Program Files (x86)\Mozilla Firefox\firefox.exe"),
    ]
    return next((path for path in candidates if path and path.is_file()), None)


def _resolve_local_geckodriver() -> Optional[Path]:
    """Find a local geckodriver; never trigger Selenium Manager downloads."""
    configured = os.environ.get("AMAZON_GECKODRIVER", "").strip()
    if configured:
        path = Path(configured)
        return path if path.is_file() else None

    candidates = [
        Path.home() / ".cache" / "selenium" / "geckodriver" / "win64" / "0.37.1" / "geckodriver.exe",
        Path.home() / ".cache" / "selenium" / "geckodriver.exe",
    ]
    cache_root = Path.home() / ".cache" / "selenium" / "geckodriver"
    if cache_root.is_dir():
        candidates.extend(cache_root.glob("**/geckodriver.exe"))
    existing = [path for path in candidates if path.is_file()]
    return max(existing, key=lambda path: path.stat().st_mtime) if existing else None


def init_firefox_driver(headless: bool = False, firefox_profile: str = "") -> webdriver.Firefox:
    """Initialize Selenium Firefox WebDriver with robust options."""
    try:
        firefox_binary = _resolve_local_firefox_binary()
        geckodriver = _resolve_local_geckodriver()
        if not firefox_binary:
            raise RuntimeError(
                "Firefox executable not found. Install Firefox or set AMAZON_FIREFOX_BINARY."
            )
        if not geckodriver:
            raise RuntimeError(
                "geckodriver executable not found. Install geckodriver or set AMAZON_GECKODRIVER."
            )

        options = Options()
        options.binary_location = str(firefox_binary)
        if headless:
            options.add_argument("--headless")
        
        # User agent and stability configurations
        options.set_preference("dom.webdriver.enabled", False)
        options.set_preference("useAutomationExtension", False)
        
        if firefox_profile and firefox_profile.strip():
            from selenium.webdriver.firefox.firefox_profile import FirefoxProfile
            options.profile = FirefoxProfile(firefox_profile.strip())

        driver = webdriver.Firefox(service=Service(executable_path=str(geckodriver)), options=options)
        driver.set_page_load_timeout(45)
        return driver
    except Exception as e:
        raise RuntimeError(f"Firefox/Selenium could not start: {e}") from e


def scrape_amazon_product(
    affiliate_url: str,
    headless: bool = False,
    firefox_profile: str = "",
    wait_timeout: int = 15
) -> Product:
    """
    Scrapes product details from an Amazon affiliate URL.

    Args:
        affiliate_url: The Amazon affiliate URL to navigate to.
        headless: Whether to run Firefox in headless mode.
        firefox_profile: Optional path or name of a Firefox profile.
        wait_timeout: Maximum seconds to wait for page elements.

    Returns:
        Product: A normalized Product dataclass containing extracted strings and lists.

    Raises:
        RuntimeError: If Firefox fails to start or page cannot be reached.
        ValueError: If essential product information (such as title) cannot be found.
    """
    driver = None
    try:
        driver = init_firefox_driver(headless=headless, firefox_profile=firefox_profile)
    except Exception as e:
        raise RuntimeError(f"Firefox/Selenium could not start: {e}") from e

    try:
        # Navigate to the affiliate URL
        driver.get(affiliate_url)
        
        # Allow any initial redirects to settle
        time.sleep(2)

        # Wait for either title element or main container
        try:
            WebDriverWait(driver, wait_timeout).until(
                lambda d: d.find_elements(By.ID, "productTitle")
                or d.find_elements(By.ID, "title")
                or d.find_elements(By.TAG_NAME, "h1")
                or d.title != ""
            )
        except TimeoutException:
            pass  # We will evaluate what elements are available below

        final_url = str(driver.current_url)

        # Extract ASIN
        asin = extract_asin_from_url(final_url) or extract_asin_from_url(affiliate_url)

        # --- 1. Product Title Extraction ---
        title = ""
        title_candidates = [
            (By.ID, "productTitle"),
            (By.ID, "title"),
            (By.ID, "titleSection"),
            (By.CSS_SELECTOR, "h1.a-size-large"),
            (By.CSS_SELECTOR, "h1#title"),
        ]

        for by, selector in title_candidates:
            elements = driver.find_elements(by, selector)
            if elements:
                candidate_text = elements[0].text.strip()
                if not candidate_text:
                    candidate_text = elements[0].get_attribute("textContent").strip()
                if candidate_text:
                    title = candidate_text
                    break

        # Fallback to page title if specific DOM title isn't found
        if not title and driver.title:
            page_title = driver.title.strip()
            # Clean common Amazon suffixes
            cleaned = re.sub(r':\s*Amazon\.[a-z.]+$', '', page_title, flags=re.IGNORECASE)
            cleaned = re.sub(r'^Amazon\.[a-z.]+:\s*', '', cleaned, flags=re.IGNORECASE)
            if cleaned and not cleaned.lower().startswith("robot check") and not cleaned.lower().startswith("sorry"):
                title = cleaned.strip()

        if not title:
            raise ValueError("Amazon product title could not be found")

        # Clean title whitespace
        title = " ".join(title.split())

        # --- 2. Product Features Extraction ---
        features: List[str] = []
        feature_selectors = [
            "#feature-bullets ul li span.a-list-item",
            "#featurebullets_feature_div ul li span.a-list-item",
            "#aboutThisItem_feature_div ul li span.a-list-item",
            "#productFactsDesktopExpander ul li span.a-list-item",
            "#feature-bullets-expanded ul li span.a-list-item",
            ".a-unordered-list.a-vertical.a-spacing-mini li span.a-list-item"
        ]

        seen_features = set()
        for selector in feature_selectors:
            elements = driver.find_elements(By.CSS_SELECTOR, selector)
            for elem in elements:
                try:
                    text = elem.text.strip()
                    if not text:
                        text = elem.get_attribute("textContent").strip()
                    
                    if not text:
                        continue
                    
                    # Normalize spaces
                    clean_text = " ".join(text.split())
                    
                    # Filter out non-feature boilerplate
                    lower_text = clean_text.lower()
                    if lower_text.startswith("make sure this fits") or lower_text.startswith("see more"):
                        continue
                    if lower_text in seen_features or len(clean_text) < 3:
                        continue

                    seen_features.add(lower_text)
                    features.append(clean_text)
                except Exception:
                    continue
            
            if features:
                # If we collected features from the primary selector, stop or continue to combine
                break

        # If still no features, try short product overview / description elements
        if not features:
            desc_elements = driver.find_elements(By.CSS_SELECTOR, "#productDescription p, #productDescription span")
            for elem in desc_elements:
                try:
                    text = elem.text.strip()
                    if text and len(text) > 10:
                        clean_text = " ".join(text.split())
                        if clean_text.lower() not in seen_features:
                            seen_features.add(clean_text.lower())
                            features.append(clean_text)
                except Exception:
                    pass

        # --- 3. Price Extraction (Optional) ---
        price = None
        price_selectors = [
            ".a-price .a-offscreen",
            "#priceblock_ourprice",
            "#priceblock_dealprice",
            "#corePrice_feature_div .a-offscreen",
            "#corePriceDisplay_desktop_feature_div .a-offscreen",
            "span.a-price-whole"
        ]
        for sel in price_selectors:
            price_elems = driver.find_elements(By.CSS_SELECTOR, sel)
            if price_elems:
                try:
                    p_text = price_elems[0].get_attribute("textContent").strip()
                    if not p_text:
                        p_text = price_elems[0].text.strip()
                    if p_text and any(char.isdigit() for char in p_text):
                        price = " ".join(p_text.split())
                        break
                except Exception:
                    continue

        # --- 4. Product Image Extraction ---
        image_url = None
        img_selectors = [
            "#landingImage",
            "#imgBlkFront",
            "#main-image",
            "#ebooks-img-canvas img",
            "img.a-dynamic-image",
            "img.s-image"
        ]
        for isel in img_selectors:
            img_elems = driver.find_elements(By.CSS_SELECTOR, isel)
            if img_elems:
                try:
                    img_src = img_elems[0].get_attribute("data-old-hires") or img_elems[0].get_attribute("src")
                    if not img_src or "transparent" in img_src.lower():
                        dyn_data = img_elems[0].get_attribute("data-a-dynamic-image")
                        if dyn_data:
                            import json as j_mod
                            dyn_dict = j_mod.loads(dyn_data)
                            if dyn_dict:
                                img_src = list(dyn_dict.keys())[-1]
                    if is_valid_image_url(img_src) and "media-amazon.com" in img_src:
                        image_url = img_src
                        break
                except Exception:
                    continue

        # Build normalized Product object (all strings/lists, no WebElements)
        product = Product(
            title=str(title),
            features=[str(f) for f in features],
            affiliate_url=str(affiliate_url),
            source_url=str(final_url),
            price=str(price) if price else None,
            asin=str(asin) if asin else None,
            image_url=str(image_url) if image_url else None,
        )

        return product

    finally:
        if driver is not None:
            try:
                driver.quit()
            except Exception:
                pass
