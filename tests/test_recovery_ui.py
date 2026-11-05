"""Focused tests for the browser recovery action."""

import unittest
from unittest.mock import patch

from web.app import create_app


class TestRecoveryUI(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "SECRET_KEY": "recovery-ui-test"})
        self.client = self.app.test_client()

    @patch("web.app.load_import_batches")
    @patch("web.app.get_product_detail")
    def test_failed_batch_renders_one_scoped_recovery_form(self, detail, batches):
        detail.return_value = {
            "asin": "B0H4Z5W52B",
            "title": "Existing product",
            "status": "FAILED_VALIDATION",
            "affiliate_url": "https://link.amazon/B01fd1Jv5",
            "facts": {},
            "platforms": {},
        }
        batches.return_value = [{
            "batch_id": "batch_20261020T181235361960_996e7287",
            "entries": [{
                "asin": "B0H4Z5W52B",
                "url": "https://link.amazon/B01fd1Jv5",
                "status": "CONTENT_GENERATION_FAILED",
            }],
        }]

        response = self.client.get("/products/B0H4Z5W52B")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertEqual(html.count('action="/api/recover-failed/B0H4Z5W52B"'), 1)
        self.assertIn('name="batch_id" value="batch_20261020T181235361960_996e7287"', html)
        self.assertIn("https://link.amazon/B01fd1Jv5", html)

    @patch("web.app.recover_failed_product")
    @patch("web.app.load_import_batches", return_value=[])
    @patch("web.app.get_product_detail")
    def test_browser_form_submits_existing_recovery_route_once(self, detail, _batches, recover):
        detail.return_value = {
            "asin": "B0H4Z5W52B",
            "title": "Existing product",
            "status": "FAILED_VALIDATION",
            "affiliate_url": "https://link.amazon/B01fd1Jv5",
            "facts": {},
            "platforms": {},
        }
        recover.return_value = {"success": False, "message": "Quota unavailable."}

        response = self.client.post(
            "/api/recover-failed/B0H4Z5W52B",
            data={"batch_id": "batch_20261020T181235361960_996e7287"},
            headers={"Accept": "text/html"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertIn("/products/B0H4Z5W52B", response.headers["Location"])
        recover.assert_called_once_with(
            "B0H4Z5W52B",
            batch_id="batch_20261020T181235361960_996e7287",
        )


if __name__ == "__main__":
    unittest.main()
