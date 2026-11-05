"""Focused mocked tests for the short-link ASIN page-content fallback.

Covers the failure `https://link.amazon/B01JvH5z4` -> "Scraped product ...
has missing ASIN.": when neither the submitted URL nor the post-redirect URL
carries a 10-character ASIN, the scraper may fall back to explicit ASIN data
already on the loaded page (canonical link, then ASIN inputs).

Mocks only: the WebDriver is a stub and every element query is canned.
No Firefox, no network, no queue/batch/config access.
"""

import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from amazon.scraper import (
    Product,
    _extract_asin_from_page,
    _normalize_asin,
    extract_asin_from_url,
    scrape_amazon_product,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent

SHORT_URL = "https://link.amazon/B01JvH5z4"   # 9-char token: never an ASIN
PAGE_ASIN = "B01ABC2DE3"                      # canonical-page ASIN (10 chars)
INPUT_ASIN = "B0009X29WK"                     # explicit input ASIN (10 chars)

ASIN_PAGE_SELECTORS = ('link[rel="canonical"]', "input#ASIN", 'input[name="ASIN"]')


def make_element(**attrs):
    element = MagicMock()
    element.text = attrs.pop("text", "")
    element.get_attribute.side_effect = lambda name: attrs.get(name, "")
    return element


def make_driver(current_url, canonical_href=None, asin_input=None, name_asin=None,
                forbid_asin_queries=False):
    """Stub WebDriver canned with page data (empty lists = element absent)."""
    driver = MagicMock()
    driver.current_url = current_url
    driver.title = "Stub Product: Amazon.co.uk"

    page_elements = {}
    if canonical_href is not None:
        page_elements['link[rel="canonical"]'] = [make_element(href=canonical_href)]
    if asin_input is not None:
        page_elements["input#ASIN"] = [make_element(value=asin_input)]
    if name_asin is not None:
        page_elements['input[name="ASIN"]'] = [make_element(value=name_asin)]

    def find_elements(by, selector):
        if forbid_asin_queries and selector in ASIN_PAGE_SELECTORS:
            raise AssertionError(f"page ASIN fallback must not be queried: {selector}")
        if selector in ("productTitle", "title"):
            return [make_element(text="Stub Product Title")]
        return list(page_elements.get(selector, []))

    driver.find_elements.side_effect = find_elements
    return driver


def run_scrape(driver, affiliate_url):
    """Run scrape_amazon_product against a stub driver (no Firefox/network)."""
    with patch("amazon.scraper.init_firefox_driver", return_value=driver), \
         patch("amazon.scraper.WebDriverWait"), \
         patch("amazon.scraper.time.sleep"):
        return scrape_amazon_product(affiliate_url, headless=True)


class PageAsinFallbackTests(unittest.TestCase):

    # 1. Canonical URL supplies the ASIN when both submitted and final URLs lack one
    def test_canonical_url_asin_used_when_submitted_and_final_urls_lack_one(self):
        self.assertIsNone(extract_asin_from_url(SHORT_URL))
        driver = make_driver(
            SHORT_URL, canonical_href=f"https://www.amazon.co.uk/dp/{PAGE_ASIN}?psc=1"
        )
        product = run_scrape(driver, SHORT_URL)

        self.assertEqual(product.asin, PAGE_ASIN)
        self.assertEqual(product.affiliate_url, SHORT_URL)
        self.assertEqual(product.source_url, SHORT_URL)

    # 2. Explicit ASIN input supplies the ASIN when the canonical URL has none
    def test_explicit_asin_input_used_when_canonical_url_has_none(self):
        product = run_scrape(make_driver(SHORT_URL, asin_input=INPUT_ASIN), SHORT_URL)
        self.assertEqual(product.asin, INPUT_ASIN)

        # the name-based input is honoured when the id-based one is absent
        product = run_scrape(make_driver(SHORT_URL, name_asin=INPUT_ASIN), SHORT_URL)
        self.assertEqual(product.asin, INPUT_ASIN)

        # direct helper check: no canonical link at all
        self.assertEqual(
            _extract_asin_from_page(make_driver(SHORT_URL, asin_input=INPUT_ASIN)),
            INPUT_ASIN,
        )

    # 3. No valid ASIN anywhere: stays None and the existing rejection is unchanged
    def test_no_valid_asin_anywhere_keeps_none_and_existing_rejection(self):
        driver = make_driver(SHORT_URL)
        self.assertIsNone(_extract_asin_from_page(driver))

        product = run_scrape(make_driver(SHORT_URL), SHORT_URL)
        self.assertIsNone(product.asin)

        # publishing.queue must still reject with the exact pre-existing message
        from publishing.queue import resolve_product_for_url

        root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        stub = Product(
            title="Stub Product Title",
            features=["Folds flat for storage"],
            affiliate_url=SHORT_URL,
            original_affiliate_url=SHORT_URL,
            source_url="https://example.invalid/landing",
            asin=None,
            image_url="https://m.media-amazon.com/images/I/test.jpg",
        )
        with patch("amazon.scraper.scrape_amazon_product", return_value=stub):
            ok, prod, key, errors = resolve_product_for_url(
                SHORT_URL, products_dir=root, allow_scrape=True
            )

        self.assertFalse(ok)
        self.assertIsNone(prod.asin)
        self.assertIsNone(key)
        self.assertEqual(
            errors, [f"Scraped product for '{SHORT_URL}' has missing ASIN."]
        )

    # 4. Strict 10-character alphanumeric validation, incl. rejecting B01JvH5z4
    def test_only_strict_ten_character_alphanumeric_asins_are_accepted(self):
        self.assertEqual(_normalize_asin("b01abc2de3"), "B01ABC2DE3")
        self.assertEqual(_normalize_asin("  B01ABC2DE3\t"), "B01ABC2DE3")

        for rejected in (
            "B01JvH5z4",    # the 9-character short-link token
            "b01jvh5z4",    # same token, lowercased
            "B01ABC2DE",    # 9 chars
            "B01ABC2DE34",  # 11 chars
            "B01-BC2DE3",   # non-alphanumeric
            "B01 BC2DE3",   # embedded space
            "",
            "   ",
            None,
            1234567890,
        ):
            self.assertIsNone(
                _normalize_asin(rejected), f"should be rejected: {rejected!r}"
            )

        # a page field holding the 9-char token must not produce an ASIN
        self.assertIsNone(
            _extract_asin_from_page(make_driver(SHORT_URL, asin_input="B01JvH5z4"))
        )

    # 5. URL extraction wins; the page fallback is never queried
    def test_url_extraction_takes_precedence_and_page_fallback_is_not_queried(self):
        final_url = f"https://www.amazon.co.uk/dp/{PAGE_ASIN}?th=1"
        driver = make_driver(final_url, forbid_asin_queries=True)

        # make_driver( forbid_asin_queries=True ) raises AssertionError if any
        # ASIN selector is ever queried, so reaching here proves the fallback
        # was skipped entirely.
        product = run_scrape(driver, SHORT_URL)

        self.assertEqual(product.asin, PAGE_ASIN)

    # 6. Canonical URL wins over explicit ASIN inputs
    def test_canonical_url_takes_precedence_over_explicit_asin_inputs(self):
        driver = make_driver(
            SHORT_URL,
            canonical_href=f"https://www.amazon.com/dp/{PAGE_ASIN}",
            asin_input=INPUT_ASIN,
            name_asin=INPUT_ASIN,
        )

        self.assertEqual(_extract_asin_from_page(driver), PAGE_ASIN)
        self.assertEqual(run_scrape(driver, SHORT_URL).asin, PAGE_ASIN)


if __name__ == "__main__":
    unittest.main()
