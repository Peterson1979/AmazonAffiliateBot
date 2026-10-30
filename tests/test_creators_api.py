"""Focused tests for the optional Amazon Creators API product lookup."""

import json
import logging
import socket
import shutil
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from amazon.creators_api import CreatorsAPIClient, CreatorsAPIError, environment_status, marketplace_from_url, product_from_item
from publishing.queue import resolve_product_for_url


class TestCreatorsAPI(unittest.TestCase):
    def setUp(self):
        self.item = {
            "asin": "B012345678",
            "detailPageURL": "https://www.amazon.co.uk/dp/B012345678?tag=api-20",
            "itemInfo": {
                "title": {"displayValue": "Verified API Product"},
                "features": {"displayValues": ["Verified feature one", "Verified feature two"]},
                "productInfo": {"description": {"displayValue": "Official description"}},
            },
            "images": {"primary": {"large": {"url": "https://m.media-amazon.com/images/I/api.jpg"}}},
            "offersV2": {"listings": [{"price": {"money": {"displayAmount": "£19.99"}}}]},
        }

    def test_auth_and_get_items_request_use_documented_fields(self):
        auth = Mock(status_code=200)
        auth.json.return_value = {"access_token": "secret-token"}
        item_response = Mock(status_code=200)
        item_response.json.return_value = {"itemsResult": {"items": [self.item]}}
        client = CreatorsAPIClient("id", "secret", "3.2", "www.amazon.co.uk", "homeideas4a01-20")
        with patch("amazon.creators_api.requests.post", side_effect=[auth, item_response]) as post:
            item = client.get_item("B012345678")
        self.assertEqual(item["asin"], "B012345678")
        auth_call = post.call_args_list[0]
        self.assertEqual(auth_call.args[0], "https://api.amazon.co.uk/auth/o2/token")
        self.assertEqual(auth_call.kwargs["json"]["scope"], "creatorsapi::default")
        api_call = post.call_args_list[1]
        self.assertEqual(api_call.args[0], "https://creatorsapi.amazon/catalog/v1/getItems")
        self.assertEqual(api_call.kwargs["headers"]["x-marketplace"], "www.amazon.co.uk")
        self.assertEqual(api_call.kwargs["json"]["partnerTag"], "homeideas4a01-20")
        self.assertIn("itemInfo.features", api_call.kwargs["json"]["resources"])
        self.assertNotIn("keywords", api_call.kwargs["json"])

    def test_normalization_preserves_original_url_and_official_fields(self):
        original = "https://link.amazon/B012345678"
        product = product_from_item(self.item, original)
        self.assertEqual(product.original_affiliate_url, original)
        self.assertEqual(product.affiliate_url, original)
        self.assertEqual(product.title, "Verified API Product")
        self.assertEqual(product.description, "Official description")
        self.assertEqual(product.features, ["Verified feature one", "Verified feature two"])
        self.assertEqual(product.image_url, "https://m.media-amazon.com/images/I/api.jpg")
        self.assertEqual(product.price, "£19.99")

    def test_rate_limit_is_safe_and_not_retried(self):
        auth = Mock(status_code=200)
        auth.json.return_value = {"access_token": "token"}
        limited = Mock(status_code=429)
        client = CreatorsAPIClient("id", "secret", "3.1", "www.amazon.com", "tag-20")
        with patch("amazon.creators_api.requests.post", side_effect=[auth, limited]) as post:
            with self.assertRaises(CreatorsAPIError) as raised:
                client.get_item("B012345678")
        self.assertIn("429", str(raised.exception))
        self.assertEqual(post.call_count, 2)

    def test_oauth_transport_diagnostic_redacts_secrets_and_reports_chain(self):
        inner = requests.exceptions.ProxyError(
            "proxy rejected client_secret=client-secret access_token=access-token"
        )
        outer = requests.exceptions.ConnectionError(
            "connect failed for https://user:password@example.invalid/ "
            "Authorization: Bearer bearer-token client_id=client-id"
        )
        outer.__cause__ = inner
        client = CreatorsAPIClient("client-id", "client-secret", "3.1", "www.amazon.com", "tag-20")
        with patch("amazon.creators_api.requests.post", side_effect=outer):
            with self.assertLogs("amazon.creators_api", level=logging.ERROR) as captured:
                with self.assertRaises(requests.exceptions.ConnectionError):
                    client._access_token()
        logged = "\n".join(captured.output)
        self.assertIn("ConnectionError", logged)
        self.assertIn("ProxyError", logged)
        self.assertIn("connect failed", logged)
        for secret in ("client-id", "client-secret", "password", "bearer-token", "access-token"):
            self.assertNotIn(secret, logged)
        self.assertIn("[REDACTED]", logged)

    def test_oauth_local_diagnostic_records_dns_success_and_proxy_boolean(self):
        auth = Mock(status_code=200)
        auth.json.return_value = {"access_token": "token"}
        client = CreatorsAPIClient(
            "client-id", "client-secret", "3.1", "www.amazon.com", "tag-20",
            diagnostic_mode=True,
        )
        with patch("amazon.creators_api.socket.getaddrinfo", return_value=[("result",)] ) as dns:
            with patch("amazon.creators_api.requests.post", return_value=auth) as post:
                with self.assertLogs("amazon.creators_api", level=logging.INFO) as captured:
                    client._access_token()
        dns.assert_called_once_with("api.amazon.com", 443)
        post.assert_called_once()
        logged = "\n".join(captured.output)
        self.assertIn('"dns_outcome": "success"', logged)
        self.assertIn('"explicit_proxy_configured": false', logged)
        self.assertNotIn("client-secret", logged)

    def test_oauth_local_diagnostic_records_sanitized_dns_failure_and_preserves_request(self):
        auth = Mock(status_code=200)
        auth.json.return_value = {"access_token": "token"}
        client = CreatorsAPIClient(
            "client-id", "client-secret", "3.1", "www.amazon.com", "tag-20",
            diagnostic_mode=True,
        )
        dns_error = socket.gaierror("resolver saw client-secret")
        with patch("amazon.creators_api.socket.getaddrinfo", side_effect=dns_error) as dns:
            with patch("amazon.creators_api.requests.post", return_value=auth) as post:
                with self.assertLogs("amazon.creators_api", level=logging.INFO) as captured:
                    client._access_token()
        dns.assert_called_once_with("api.amazon.com", 443)
        post.assert_called_once()
        logged = "\n".join(captured.output)
        self.assertIn('"dns_outcome": "failed"', logged)
        self.assertIn('"dns_exception_type": "gaierror"', logged)
        self.assertIn("[REDACTED]", logged)
        self.assertNotIn("client-secret", logged)

    def test_short_link_requires_explicit_marketplace(self):
        self.assertIsNone(marketplace_from_url("https://link.amazon/B012345678"))
        self.assertEqual(
            marketplace_from_url("https://link.amazon/B012345678", "www.amazon.co.uk"),
            "www.amazon.co.uk",
        )

    def test_environment_status_is_secret_free(self):
        with patch.dict(
            "os.environ",
            {
                "AMAZON_CREATORS_API_CLIENT_ID": "client-id",
                "AMAZON_CREATORS_API_CLIENT_SECRET": "client-secret",
                "AMAZON_CREATORS_API_VERSION": "3.2",
                "AMAZON_CREATORS_API_MARKETPLACE": "www.amazon.co.uk",
            },
            clear=True,
        ):
            status = environment_status()
        self.assertTrue(status["configured"])
        self.assertNotIn("client-id", json.dumps(status))
        self.assertNotIn("client-secret", json.dumps(status))

    def test_resolver_uses_api_before_scraper_without_rewriting_url(self):
        original = "https://www.amazon.co.uk/dp/B012345678?tag=homeideas4a01-20"
        api = Mock()
        api.get_item.return_value = self.item
        temp = ROOT / "tests" / "_creators_api_tmp"
        shutil.rmtree(temp, ignore_errors=True)
        temp.mkdir(parents=True)
        try:
            with patch("amazon.scraper.scrape_amazon_product") as scraper:
                ok, product, key, errors = resolve_product_for_url(
                    original,
                    products_dir=str(temp),
                    allow_scrape=True,
                    creators_api_client=api,
                )
        finally:
            shutil.rmtree(temp, ignore_errors=True)
        self.assertTrue(ok)
        self.assertEqual(key, "asin_B012345678")
        self.assertEqual(product.original_affiliate_url, original)
        scraper.assert_not_called()


if __name__ == "__main__":
    unittest.main()
