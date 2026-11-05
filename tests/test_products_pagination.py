"""
tests/test_products_pagination.py - Focused tests for Products page pagination.

Pagination must be server-side: the route slices the filtered product list and
the response only contains the requested page, while search, status filters,
sorting and existing product links keep working.
"""

import unittest
from unittest.mock import patch

from web.app import create_app, PRODUCTS_PER_PAGE_OPTIONS


def _rows(count):
    """Minimal product rows matching what the products template consumes."""
    rows = []
    for i in range(1, count + 1):
        rows.append({
            "asin": f"B0PAGE{i:03d}",
            "title": f"Product {i}",
            "facts_status": "SUFFICIENT",
            "facts_count": 3,
            "content_status": "GENERATED",
            "validation_status": "READY_FOR_REVIEW",
            "publishing_status": "SCHEDULED",
            "retryable": False,
            "publishing_results": {},
            "scheduled_date": "2026-11-01",
            "daily_slot": 1,
            "published_at": None,
        })
    return rows


class TestProductsPagination(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-pagination"})
        self.client = self.app.test_client()

    @staticmethod
    def _get(client, **params):
        response = client.get("/products", query_string=params)
        return response, response.get_data(as_text=True)

    # ---------- defaults and page slicing ----------

    @patch("web.app.get_all_products")
    def test_default_page_size_is_25(self, mock_products):
        mock_products.return_value = _rows(60)
        self.assertEqual(PRODUCTS_PER_PAGE_OPTIONS, (25, 50, 100))

        _, html = self._get(self.client)
        self.assertIn("B0PAGE001", html)
        self.assertIn("B0PAGE025", html)
        self.assertNotIn("B0PAGE026", html, "page 2 rows must not be sent to the browser")
        self.assertIn("of 60 products", html)
        self.assertIn("Page 1 of 3", html)

    @patch("web.app.get_all_products")
    def test_second_page_returns_the_next_slice(self, mock_products):
        mock_products.return_value = _rows(60)
        _, html = self._get(self.client, page=2)

        self.assertIn("B0PAGE026", html)
        self.assertIn("B0PAGE050", html)
        self.assertNotIn("B0PAGE001", html)
        self.assertNotIn("B0PAGE051", html)
        self.assertIn("Showing 26–50 of 60 products", html)
        self.assertIn("Page 2 of 3", html)

    @patch("web.app.get_all_products")
    def test_selectable_page_sizes(self, mock_products):
        mock_products.return_value = _rows(60)

        _, html = self._get(self.client, per_page=50)
        self.assertIn("B0PAGE050", html)
        self.assertNotIn("B0PAGE051", html)
        self.assertIn("Page 1 of 2", html)
        for size in ("25", "50", "100"):
            self.assertIn(f'value="{size}"', html)

        _, html = self._get(self.client, per_page=100)
        self.assertIn("B0PAGE060", html)
        self.assertIn("Page 1 of 1", html)

    # ---------- invalid / out-of-range input ----------

    @patch("web.app.get_all_products")
    def test_invalid_per_page_falls_back_to_default(self, mock_products):
        mock_products.return_value = _rows(60)
        _, html = self._get(self.client, per_page="banana")
        self.assertIn("B0PAGE025", html)
        self.assertNotIn("B0PAGE026", html)

        _, html = self._get(self.client, per_page="7")
        self.assertIn("B0PAGE025", html)
        self.assertNotIn("B0PAGE026", html)

    @patch("web.app.get_all_products")
    def test_invalid_page_falls_back_to_first_page(self, mock_products):
        mock_products.return_value = _rows(60)
        _, html = self._get(self.client, page="not-a-page")
        self.assertIn("Page 1 of 3", html)

        _, html = self._get(self.client, page="0")
        self.assertIn("Page 1 of 3", html)

    @patch("web.app.get_all_products")
    def test_page_beyond_the_end_is_clamped(self, mock_products):
        mock_products.return_value = _rows(60)
        _, html = self._get(self.client, page=999)
        self.assertIn("B0PAGE051", html)
        self.assertNotIn("B0PAGE050", html)
        self.assertIn("Showing 51–60 of 60 products", html)
        self.assertIn("Page 3 of 3", html)

    @patch("web.app.get_all_products")
    def test_empty_result_set_renders_safely(self, mock_products):
        mock_products.return_value = []
        response, html = self._get(self.client)

        self.assertEqual(response.status_code, 200)
        self.assertIn("of 0 products", html)
        self.assertIn("Page 1 of 1", html)
        self.assertIn("No products match the selected criteria.", html)

    # ---------- navigation and preserved filters ----------

    @patch("web.app.get_all_products")
    def test_navigation_links_are_rendered(self, mock_products):
        mock_products.return_value = _rows(60)
        _, html = self._get(self.client, page=2)

        self.assertIn("Previous", html)
        self.assertIn("Next", html)
        # Numbered links for every page plus First/Last edge pages.
        self.assertIn("page=1", html)
        self.assertIn("page=3", html)

        _, first_page_html = self._get(self.client, page=1)
        self.assertNotIn(">Previous<", first_page_html)
        self.assertIn("Next", first_page_html)

    @patch("web.app.get_all_products")
    def test_search_and_filter_are_forwarded_and_forwarded_to_the_service(self, mock_products):
        mock_products.return_value = _rows(30)

        _, html = self._get(self.client, q="espresso", status="published", page=2)
        mock_products.assert_called_with(query="espresso", status_filter="published")
        self.assertIn('value="espresso"', html)
        self.assertIn("page=1", html)  # per-page selector restarts at page 1

    @patch("web.app.get_all_products")
    def test_first_page_does_not_render_a_previous_link(self, mock_products):
        mock_products.return_value = _rows(10)
        _, html = self._get(self.client)
        self.assertIn("Showing 1–10 of 10 products", html)
        self.assertNotIn(">Previous<", html)
        self.assertNotIn(">Next<", html)


if __name__ == "__main__":
    unittest.main()
