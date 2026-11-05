"""Focused HTTP behavior tests for the explicit failed-product recovery route."""

import unittest
from unittest.mock import patch

from web.app import create_app


class TestRecoveryEndpoint(unittest.TestCase):
    def setUp(self):
        self.app = create_app({"TESTING": True, "SECRET_KEY": "recovery-test"})
        self.client = self.app.test_client()
        self.path = "/api/recover-failed/B0H4Z5W52B?batch_id=batch-test"

    @patch("web.app.recover_failed_product")
    def test_headerless_api_error_returns_json_without_redirect(self, recover):
        recover.return_value = {"success": False, "message": "Quota availability is unknown."}

        response = self.client.post(self.path)

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.is_json)
        self.assertEqual(response.get_json()["success"], False)
        self.assertIn("Quota availability", response.get_json()["message"])
        self.assertIsNone(response.headers.get("Location"))
        recover.assert_called_once_with("B0H4Z5W52B", batch_id="batch-test")

    @patch("web.app.recover_failed_product")
    def test_json_success_is_returned_for_ajax_client_without_session(self, recover):
        recover.return_value = {"success": True, "status": "READY", "queue_item_id": "item_001"}

        response = self.client.post(
            self.path,
            headers={"X-Requested-With": "XMLHttpRequest"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["queue_item_id"], "item_001")
        self.assertIsNone(response.headers.get("Location"))

    @patch("web.app.recover_failed_product")
    def test_explicit_html_client_keeps_browser_redirect(self, recover):
        recover.return_value = {"success": False, "message": "Recovery failed."}

        response = self.client.post(self.path, headers={"Accept": "text/html"})

        self.assertEqual(response.status_code, 302)
        self.assertIn("/products/B0H4Z5W52B", response.headers["Location"])


if __name__ == "__main__":
    unittest.main()
