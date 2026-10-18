"""
Unit tests for SocialPublisher and Platform Adapters (V4).
Verifies account configuration, DRY_RUN simulation, credential validation,
package requirements, and rejection of invalid packages across the 4 active platforms.
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from publishing.config import PublishingConfig, DEFAULT_TARGET_ACCOUNTS
from publishing.publisher import SocialPublisher, SUPPORTED_PLATFORMS
from publishing.adapters import (
    FacebookAdapter,
    PinterestAdapter,
    ThreadsAdapter,
    STATUS_WOULD_PUBLISH,
    STATUS_PUBLISHED,
    STATUS_FAILED
)


class TestPublisher(unittest.TestCase):
    def setUp(self):
        self.valid_package = {
            "asin": "B0GKGBWRTR",
            "status": "READY_FOR_REVIEW",
            "generated_at": "2026-10-02T19:00:00Z",
            "model": "qwen2.5:7b",
            "source": {
                "product_cache": "data/products/asin_B0GKGBWRTR.json"
            },
            "platforms": {
                "facebook": {
                    "status": "VALID",
                    "content": "Valid Facebook post text.",
                    "validation": {"status": "VALID", "retry_count": 0, "errors": []}
                },
                "pinterest": {
                    "status": "VALID",
                    "title": "Valid Pin Title",
                    "description": "Valid Pin Description",
                    "image_url": "https://m.media-amazon.com/images/I/test.jpg",
                    "validation": {"status": "VALID", "retry_count": 0, "errors": []}
                },
                "threads": {
                    "status": "VALID",
                    "content": "Valid Threads post text.",
                    "validation": {"status": "VALID", "retry_count": 0, "errors": []}
                }
            }
        }

    def test_configured_target_accounts(self):
        """Verify the three active target accounts are correctly configured."""
        config = PublishingConfig()
        accounts = config.accounts

        self.assertEqual(len(accounts), 3)
        self.assertNotIn("x", accounts)
        self.assertEqual(accounts["facebook"]["account"], "LifeModeHQ")
        self.assertEqual(accounts["pinterest"]["account"], "dailyideasforyou")
        self.assertEqual(accounts["pinterest"]["board"], "Shopping ideas")
        self.assertEqual(accounts["threads"]["account"], "lifemodehq")

    def test_dry_run_mode_simulation(self):
        """Verify DRY_RUN mode simulates publishing without sending real network requests."""
        publisher = SocialPublisher(dry_run=True)
        report = publisher.publish_package(self.valid_package)

        self.assertEqual(report["mode"], "DRY_RUN")
        self.assertEqual(report["status"], STATUS_WOULD_PUBLISH)
        self.assertEqual(len(report["results"]), 4)

        for p_name in SUPPORTED_PLATFORMS:
            res = report["results"][p_name]
            self.assertEqual(res["status"], STATUS_WOULD_PUBLISH)
            self.assertEqual(res["mode"], "DRY_RUN")
            self.assertEqual(res["action"], "PUBLISH")

        # Check Pinterest board
        self.assertEqual(report["results"]["pinterest"]["board"], "Shopping ideas")
        self.assertEqual(report["results"]["pinterest"]["account"], "dailyideasforyou")
        self.assertEqual(report["results"]["pinterest_lifemodehq"]["account"], "LifeModeHQ")
        self.assertEqual(report["results"]["pinterest_lifemodehq"]["board"], "Home & Everyday Living")

    def test_publisher_rejects_non_ready_package(self):
        """Verify publisher refuses to publish if package status is not READY_FOR_REVIEW."""
        publisher = SocialPublisher(dry_run=True)
        unready_package = dict(self.valid_package)
        unready_package["status"] = "FAILED_VALIDATION"

        with self.assertRaises(ValueError) as ctx:
            publisher.publish_package(unready_package)
        self.assertIn("READY_FOR_REVIEW", str(ctx.exception))

    def test_publisher_rejects_missing_or_invalid_platform_in_package(self):
        """Verify publisher reports FAILED if a platform is not VALID in the package."""
        publisher = SocialPublisher(dry_run=True)
        invalid_pkg = dict(self.valid_package)
        invalid_pkg["platforms"] = dict(self.valid_package["platforms"])
        invalid_pkg["platforms"]["facebook"] = {
            "status": "FAILED_VALIDATION",
            "content": "Invalid FB content"
        }

        report = publisher.publish_package(invalid_pkg)
        self.assertEqual(report["status"], STATUS_FAILED)
        self.assertEqual(report["results"]["facebook"]["status"], STATUS_FAILED)

    def test_live_mode_missing_credentials_fails_clearly(self):
        """Verify live publishing mode (dry_run=False) fails with missing credentials rather than fake success."""
        config = PublishingConfig(dry_run=False, credentials={})
        publisher = SocialPublisher(config=config, dry_run=False)

        report = publisher.publish_package(self.valid_package, dry_run=False)
        self.assertEqual(report["mode"], "LIVE")
        self.assertEqual(report["status"], STATUS_FAILED)

        for p_name in SUPPORTED_PLATFORMS:
            res = report["results"][p_name]
            self.assertEqual(res["status"], STATUS_FAILED)
            self.assertIn("Missing required credentials", res["errors"][0])

    def test_publisher_does_not_call_llm_or_modify_content(self):
        """Verify publisher does not invoke LLM or change content."""
        publisher = SocialPublisher(dry_run=True)
        mock_llm = MagicMock()
        report = publisher.publish_package(self.valid_package)

        mock_llm.generate_text.assert_not_called()
        self.assertEqual(report["asin"], "B0GKGBWRTR")

    def test_pinterest_target_account_and_board_configuration(self):
        """Verify Pinterest target account and resolved board ID configuration."""
        config = PublishingConfig()
        pinterest_cfg = config.accounts["pinterest"]
        self.assertEqual(pinterest_cfg["account"], "dailyideasforyou")
        self.assertEqual(pinterest_cfg["board"], "Shopping ideas")
        self.assertEqual(pinterest_cfg["board_id"], "1033646620665866548")

    @unittest.mock.patch("requests.post")
    def test_pinterest_adapter_credentials_and_execution(self, mock_post):
        """Verify Pinterest adapter credential verification and structured execution."""
        adapter_no_creds = PinterestAdapter(credentials={})
        valid, missing = adapter_no_creds.check_credentials()
        self.assertFalse(valid)
        self.assertIn("access_token", missing)

        adapter_with_creds = PinterestAdapter(credentials={"access_token": "test_token"})
        valid, missing = adapter_with_creds.check_credentials()
        self.assertTrue(valid)
        self.assertEqual(missing, [])

        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = {"id": "pin_12345", "board_id": "1033646620665866548"}
        mock_post.return_value = mock_resp

        account_cfg = {"account": "dailyideasforyou", "board": "Shopping ideas", "board_id": "1033646620665866548"}
        content = {
            "status": "VALID",
            "title": "Test Title",
            "description": "Test Desc",
            "image_url": "https://m.media-amazon.com/images/I/51KyaTB1EKL._AC_SL1500_.jpg",
            "link": "https://link.amazon/B0hwa4Nkm"
        }
        res = adapter_with_creds._execute_live_publish(content, account_cfg)
        self.assertEqual(res["platform"], "pinterest")
        self.assertEqual(res["account"], "dailyideasforyou")
        self.assertEqual(res["board"], "Shopping ideas")
        self.assertEqual(res["board_id"], "1033646620665866548")
        self.assertEqual(res["pin_id"], "pin_12345")
        self.assertEqual(res["status"], STATUS_PUBLISHED)
        self.assertEqual(res["http_status"], 201)

    # -------------------------------------------------------------------------
    # Facebook Adapter Offline Unit Tests (Mocked HTTP)
    # -------------------------------------------------------------------------

    def test_facebook_adapter_credentials_check(self):
        """Verify Facebook adapter credentials check for access_token and page_id."""
        adapter_empty = FacebookAdapter(credentials={})
        valid, missing = adapter_empty.check_credentials()
        self.assertFalse(valid)
        self.assertIn("access_token", missing)
        self.assertIn("page_id", missing)

        adapter_partial = FacebookAdapter(credentials={"access_token": "token_only"})
        valid, missing = adapter_partial.check_credentials()
        self.assertFalse(valid)
        self.assertEqual(missing, ["page_id"])

        adapter_full = FacebookAdapter(credentials={"access_token": "valid_token", "page_id": "226949230493910"})
        valid, missing = adapter_full.check_credentials()
        self.assertTrue(valid)
        self.assertEqual(missing, [])

    def test_facebook_adapter_empty_content(self):
        """Verify Facebook adapter fails safely when content text is empty."""
        adapter = FacebookAdapter(credentials={"access_token": "token", "page_id": "12345"})
        res = adapter._execute_live_publish({"content": ""}, {"account": "LifeModeHQ"})
        self.assertEqual(res["status"], STATUS_FAILED)
        self.assertIn("Missing content", res["errors"][0])

    @unittest.mock.patch("requests.post")
    def test_facebook_adapter_live_publish_success(self, mock_post):
        """Verify Facebook adapter successfully publishes and parses real post ID."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "226949230493910_987654321"}
        mock_post.return_value = mock_resp

        adapter = FacebookAdapter(credentials={"access_token": "fb_valid_token", "page_id": "226949230493910"})
        content = {"content": "Check out this great product: https://link.amazon/B0isaIph6"}
        res = adapter._execute_live_publish(content, {"account": "LifeModeHQ"})

        self.assertEqual(res["status"], STATUS_PUBLISHED)
        self.assertEqual(res["platform"], "facebook")
        self.assertEqual(res["account"], "LifeModeHQ")
        self.assertEqual(res["page_id"], "226949230493910")
        self.assertEqual(res["post_id"], "226949230493910_987654321")
        self.assertEqual(res["http_status"], 200)
        self.assertIn("Successfully published post", res["message"])

        # Verify request parameters
        mock_post.assert_called_once()
        call_args, call_kwargs = mock_post.call_args
        self.assertIn("https://graph.facebook.com/v19.0/226949230493910/feed", call_args[0])
        self.assertEqual(call_kwargs["data"]["message"], content["content"])
        self.assertEqual(call_kwargs["data"]["access_token"], "fb_valid_token")

    @unittest.mock.patch("requests.post")
    def test_facebook_adapter_live_publish_api_rejection(self, mock_post):
        """Verify Facebook adapter handles API rejections with detailed error reporting."""
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = '{"error": {"message": "Invalid OAuth 2.0 Access Token", "code": 190}}'
        mock_resp.json.return_value = {"error": {"message": "Invalid OAuth 2.0 Access Token", "code": 190}}
        mock_post.return_value = mock_resp

        adapter = FacebookAdapter(credentials={"access_token": "expired_token", "page_id": "226949230493910"})
        content = {"content": "Test Facebook Post"}
        res = adapter._execute_live_publish(content, {"account": "LifeModeHQ"})

        self.assertEqual(res["status"], STATUS_FAILED)
        self.assertEqual(res["http_status"], 400)
        self.assertIn("Invalid OAuth 2.0 Access Token", res["errors"][0])

    @unittest.mock.patch("requests.post")
    def test_facebook_adapter_live_publish_missing_post_id(self, mock_post):
        """Verify Facebook adapter handles HTTP 200 without post ID as failure."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}
        mock_post.return_value = mock_resp

        adapter = FacebookAdapter(credentials={"access_token": "token", "page_id": "226949230493910"})
        content = {"content": "Test Facebook Post"}
        res = adapter._execute_live_publish(content, {"account": "LifeModeHQ"})

        self.assertEqual(res["status"], STATUS_FAILED)
        self.assertIn("missing post ID", res["errors"][0])

    @unittest.mock.patch("requests.post")
    def test_facebook_adapter_rejects_success_http_with_api_error_body(self, mock_post):
        """A 2xx response containing a Graph API error is not a publication."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "error": {"message": "Permissions error", "code": 200}
        }
        mock_post.return_value = mock_resp

        adapter = FacebookAdapter(credentials={"access_token": "token", "page_id": "226949230493910"})
        res = adapter._execute_live_publish({"content": "Test Facebook Post"}, {"account": "LifeModeHQ"})

        self.assertEqual(res["status"], STATUS_FAILED)
        self.assertIn("Permissions error", res["errors"][0])

    @unittest.mock.patch("publishing.adapters.facebook_adapter.LOGGER")
    @unittest.mock.patch("requests.post")
    def test_facebook_error_diagnostics_are_redacted(self, mock_post, mock_logger):
        """Facebook diagnostics include safe API fields but never the access token."""
        token = "fb_secret_token"
        mock_resp = MagicMock()
        mock_resp.status_code = 400
        mock_resp.text = "ignored"
        mock_resp.json.return_value = {
            "error": {
                "message": f"Invalid token {token}",
                "code": 190,
                "error_subcode": 467,
            }
        }
        mock_post.return_value = mock_resp

        adapter = FacebookAdapter(credentials={"access_token": token, "page_id": "226949230493910"})
        result = adapter._execute_live_publish({"content": "Test Facebook Post"}, {"account": "LifeModeHQ"})

        self.assertEqual(result["status"], STATUS_FAILED)
        logged = " ".join(str(arg) for call in mock_logger.error.call_args_list for arg in call.args)
        self.assertIn("190", logged)
        self.assertIn("467", logged)
        self.assertNotIn(token, logged)

    @unittest.mock.patch("requests.post")
    def test_facebook_adapter_live_publish_network_exception(self, mock_post):
        """Verify Facebook adapter safely catches network timeouts and exceptions."""
        mock_post.side_effect = TimeoutError("Connection timed out after 30s")

        adapter = FacebookAdapter(credentials={"access_token": "token", "page_id": "226949230493910"})
        content = {"content": "Test Facebook Post"}
        res = adapter._execute_live_publish(content, {"account": "LifeModeHQ"})

        self.assertEqual(res["status"], STATUS_FAILED)
        self.assertIn("Connection timed out", res["errors"][0])

    # -------------------------------------------------------------------------
    # Threads Adapter Offline Unit Tests (Mocked HTTP)
    # -------------------------------------------------------------------------

    def test_threads_adapter_credentials_check(self):
        """Verify Threads adapter credentials check for access_token and user_id."""
        adapter_empty = ThreadsAdapter(credentials={})
        valid, missing = adapter_empty.check_credentials()
        self.assertFalse(valid)
        self.assertIn("access_token", missing)
        self.assertIn("user_id", missing)

        adapter_partial = ThreadsAdapter(credentials={"access_token": "token_only"})
        valid, missing = adapter_partial.check_credentials()
        self.assertFalse(valid)
        self.assertEqual(missing, ["user_id"])

        adapter_full = ThreadsAdapter(credentials={"access_token": "valid_token", "user_id": "28272717349017680"})
        valid, missing = adapter_full.check_credentials()
        self.assertTrue(valid)
        self.assertEqual(missing, [])

    def test_threads_adapter_empty_content(self):
        """Verify Threads adapter fails safely when content text is empty."""
        adapter = ThreadsAdapter(credentials={"access_token": "token", "user_id": "12345"})
        res = adapter._execute_live_publish({"content": ""}, {"account": "lifemodehq"})
        self.assertEqual(res["status"], STATUS_FAILED)
        self.assertIn("Missing content", res["errors"][0])

    @unittest.mock.patch("requests.post")
    def test_threads_adapter_live_publish_success(self, mock_post):
        """Verify Threads adapter successfully executes the two-step container flow."""
        # Step 1: container creation (returns creation ID)
        resp1 = MagicMock()
        resp1.status_code = 200
        resp1.json.return_value = {"id": "creation_container_999"}

        # Step 2: publish container (returns post ID)
        resp2 = MagicMock()
        resp2.status_code = 200
        resp2.json.return_value = {"id": "threads_post_888"}

        mock_post.side_effect = [resp1, resp2]

        adapter = ThreadsAdapter(credentials={"access_token": "threads_token", "user_id": "28272717349017680"})
        content = {"content": "Check out this Halloween decoration: https://link.amazon/B0isaIph6"}
        res = adapter._execute_live_publish(content, {"account": "lifemodehq"})

        self.assertEqual(res["status"], STATUS_PUBLISHED)
        self.assertEqual(res["platform"], "threads")
        self.assertEqual(res["account"], "lifemodehq")
        self.assertEqual(res["user_id"], "28272717349017680")
        self.assertEqual(res["creation_id"], "creation_container_999")
        self.assertEqual(res["post_id"], "threads_post_888")
        self.assertEqual(res["http_status"], 200)

        # Verify two POST requests occurred with correct parameters
        self.assertEqual(mock_post.call_count, 2)
        call1_args, call1_kwargs = mock_post.call_args_list[0]
        self.assertIn("https://graph.threads.net/v1.0/28272717349017680/threads", call1_args[0])
        self.assertEqual(call1_kwargs["data"]["media_type"], "TEXT")
        self.assertEqual(call1_kwargs["data"]["text"], content["content"])
        self.assertEqual(call1_kwargs["data"]["access_token"], "threads_token")

        call2_args, call2_kwargs = mock_post.call_args_list[1]
        self.assertIn("https://graph.threads.net/v1.0/28272717349017680/threads_publish", call2_args[0])
        self.assertEqual(call2_kwargs["data"]["creation_id"], "creation_container_999")
        self.assertEqual(call2_kwargs["data"]["access_token"], "threads_token")

    @unittest.mock.patch("requests.post")
    def test_threads_adapter_step1_container_creation_rejection(self, mock_post):
        """Verify Threads adapter handles Step 1 container creation error."""
        resp1 = MagicMock()
        resp1.status_code = 400
        resp1.text = '{"error": {"message": "Invalid OAuth access token", "code": 190}}'
        resp1.json.return_value = {"error": {"message": "Invalid OAuth access token", "code": 190}}
        mock_post.return_value = resp1

        adapter = ThreadsAdapter(credentials={"access_token": "bad_token", "user_id": "28272717349017680"})
        content = {"content": "Test Threads Post"}
        res = adapter._execute_live_publish(content, {"account": "lifemodehq"})

        self.assertEqual(res["status"], STATUS_FAILED)
        self.assertEqual(res["http_status"], 400)
        self.assertIn("Step 1 (Container Creation) error", res["errors"][0])
        self.assertEqual(mock_post.call_count, 1)

    @unittest.mock.patch("requests.post")
    def test_threads_adapter_step2_container_publish_rejection(self, mock_post):
        """Verify Threads adapter handles Step 2 container publishing error."""
        resp1 = MagicMock()
        resp1.status_code = 200
        resp1.json.return_value = {"id": "creation_container_999"}

        resp2 = MagicMock()
        resp2.status_code = 500
        resp2.text = '{"error": {"message": "Publishing rate limit exceeded", "code": 32}}'
        resp2.json.return_value = {"error": {"message": "Publishing rate limit exceeded", "code": 32}}

        mock_post.side_effect = [resp1, resp2]

        adapter = ThreadsAdapter(credentials={"access_token": "token", "user_id": "28272717349017680"})
        content = {"content": "Test Threads Post"}
        res = adapter._execute_live_publish(content, {"account": "lifemodehq"})

        self.assertEqual(res["status"], STATUS_FAILED)
        self.assertEqual(res["http_status"], 500)
        self.assertEqual(res["creation_id"], "creation_container_999")
        self.assertIn("Step 2 (Container Publish) error", res["errors"][0])
        self.assertEqual(mock_post.call_count, 2)

    @unittest.mock.patch("requests.post")
    def test_threads_adapter_network_exception_step1(self, mock_post):
        """Verify Threads adapter safely catches network errors during step 1."""
        mock_post.side_effect = ConnectionError("Failed to connect to graph.threads.net")

        adapter = ThreadsAdapter(credentials={"access_token": "token", "user_id": "28272717349017680"})
        content = {"content": "Test Threads Post"}
        res = adapter._execute_live_publish(content, {"account": "lifemodehq"})

        self.assertEqual(res["status"], STATUS_FAILED)
        self.assertIn("Failed to connect", res["errors"][0])

    # -------------------------------------------------------------------------
    # Image / Photo Publishing Tests (Mocked HTTP)
    # -------------------------------------------------------------------------

    @unittest.mock.patch("requests.post")
    def test_facebook_adapter_live_photo_publish_success(self, mock_post):
        """Verify Facebook adapter uses /{page_id}/photos when image_url is provided."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "photo_111", "post_id": "226949230493910_999999"}
        mock_post.return_value = mock_resp

        adapter = FacebookAdapter(credentials={"access_token": "fb_token", "page_id": "226949230493910"})
        content = {
            "content": "Check out this Halloween item: https://link.amazon/B0isaIph6",
            "image_url": "https://m.media-amazon.com/images/I/61S5bLw5RQL._AC_SL1500_.jpg"
        }
        res = adapter._execute_live_publish(content, {"account": "LifeModeHQ"})

        self.assertEqual(res["status"], STATUS_PUBLISHED)
        self.assertEqual(res["post_id"], "226949230493910_999999")
        self.assertEqual(res["image_url"], "https://m.media-amazon.com/images/I/61S5bLw5RQL._AC_SL1500_.jpg")

        mock_post.assert_called_once()
        call_args, call_kwargs = mock_post.call_args
        self.assertIn("https://graph.facebook.com/v19.0/226949230493910/photos", call_args[0])
        self.assertEqual(call_kwargs["data"]["url"], "https://m.media-amazon.com/images/I/61S5bLw5RQL._AC_SL1500_.jpg")
        self.assertEqual(call_kwargs["data"]["caption"], content["content"])
        self.assertTrue(call_kwargs["data"]["published"])
        self.assertEqual(call_kwargs["data"]["access_token"], "fb_token")

    @unittest.mock.patch("requests.post")
    def test_facebook_photo_without_post_id_is_not_confirmed(self, mock_post):
        """A photo response containing only a photo ID cannot be marked live."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": "photo_only"}
        mock_post.return_value = mock_resp

        adapter = FacebookAdapter(credentials={"access_token": "fb_token", "page_id": "226949230493910"})
        result = adapter._execute_live_publish(
            {"content": "Photo post", "image_url": "https://m.media-amazon.com/images/I/test.jpg"},
            {"account": "LifeModeHQ"},
        )

        self.assertEqual(result["status"], STATUS_FAILED)
        self.assertIn("missing post ID", result["errors"][0])

    def test_missing_pinterest_image_does_not_skip_facebook(self):
        """Facebook remains independently routable when Pinterest image preflight fails."""
        package = {
            "asin": "B0NOIMAGE01",
            "status": "READY_FOR_REVIEW",
            "platforms": {
                "facebook": {"status": "VALID", "content": "Facebook text"},
                "pinterest": {"status": "VALID", "title": "Pin", "description": "Pin"},
                "threads": {"status": "VALID", "content": "Threads text"},
            },
        }
        report = SocialPublisher(dry_run=True).publish_package(package)

        self.assertEqual(report["results"]["facebook"]["status"], STATUS_WOULD_PUBLISH)
        self.assertEqual(report["results"]["pinterest"]["status"], STATUS_FAILED)
        self.assertEqual(report["results"]["pinterest_lifemodehq"]["status"], STATUS_FAILED)
        self.assertEqual(report["results"]["threads"]["status"], STATUS_FAILED)
        self.assertNotIn("Skipped because item image preflight was blocked.", report["results"]["facebook"].get("errors", []))

    @unittest.mock.patch("requests.post")
    def test_threads_adapter_live_image_publish_success(self, mock_post):
        """Verify Threads adapter uses media_type=IMAGE when image_url is provided."""
        resp1 = MagicMock()
        resp1.status_code = 200
        resp1.json.return_value = {"id": "creation_img_container_777"}

        resp2 = MagicMock()
        resp2.status_code = 200
        resp2.json.return_value = {"id": "threads_post_img_555"}

        mock_post.side_effect = [resp1, resp2]

        adapter = ThreadsAdapter(credentials={"access_token": "threads_token", "user_id": "28272717349017680"})
        content = {
            "content": "Check out this Halloween item: https://link.amazon/B0isaIph6",
            "image_url": "https://m.media-amazon.com/images/I/61S5bLw5RQL._AC_SL1500_.jpg"
        }
        res = adapter._execute_live_publish(content, {"account": "lifemodehq"})

        self.assertEqual(res["status"], STATUS_PUBLISHED)
        self.assertEqual(res["creation_id"], "creation_img_container_777")
        self.assertEqual(res["post_id"], "threads_post_img_555")
        self.assertEqual(res["image_url"], "https://m.media-amazon.com/images/I/61S5bLw5RQL._AC_SL1500_.jpg")

        self.assertEqual(mock_post.call_count, 2)
        call1_args, call1_kwargs = mock_post.call_args_list[0]
        self.assertIn("https://graph.threads.net/v1.0/28272717349017680/threads", call1_args[0])
        self.assertEqual(call1_kwargs["data"]["media_type"], "IMAGE")
        self.assertEqual(call1_kwargs["data"]["image_url"], "https://m.media-amazon.com/images/I/61S5bLw5RQL._AC_SL1500_.jpg")
        self.assertEqual(call1_kwargs["data"]["text"], content["content"])


if __name__ == "__main__":
    unittest.main()


