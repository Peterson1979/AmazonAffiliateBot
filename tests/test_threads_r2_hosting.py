"""
Focused unit tests for Cloudflare R2 image hosting and Threads publishing:
1. R2 object-key generation (amazon-affiliate-bot/creatives/<ASIN>/<filename>).
2. Public HTTPS URL resolution.
3. R2 configuration validation (missing credentials, malformed URLs).
4. R2 upload behavior (dry-run, live byte preservation, missing file guard).
5. Threads request payload construction with public HTTPS URL.
6. Fail-closed rejection of local-only paths before any Threads API request.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from publishing.adapters.threads_adapter import ThreadsAdapter
from publishing.publisher import PublishingConfig, SocialPublisher
from publishing.r2_storage import (
    DEFAULT_KEY_PREFIX,
    R2Client,
    R2Config,
    R2ConfigError,
    R2UploadError,
    get_creative_object_key,
    get_image_content_type,
    get_public_creative_url,
    upload_creative_to_r2,
)


class TestR2StorageAndUrlGeneration(unittest.TestCase):
    def test_object_key_generation(self):
        """Verify object key format is amazon-affiliate-bot/creatives/<ASIN>/<filename>."""
        asin = "B09X5BQ969"
        path = "data/creatives/B09X5BQ969/gift_inspiration_flow.jpg"
        key = get_creative_object_key(asin, path)
        self.assertEqual(key, "amazon-affiliate-bot/creatives/B09X5BQ969/gift_inspiration_flow.jpg")

        # Case normalization and Windows path handling
        win_path = Path("C:/data/creatives/b09x5bq969/everyday_pick_flow.png")
        key_win = get_creative_object_key("b09x5bq969", win_path)
        self.assertEqual(key_win, "amazon-affiliate-bot/creatives/B09X5BQ969/everyday_pick_flow.png")

    def test_public_url_resolution(self):
        """Verify public HTTPS URL correctly combines base URL and object key."""
        base_url = "https://images.lifemode.com/assets"
        object_key = "amazon-affiliate-bot/creatives/B09X5BQ969/gift_inspiration_flow.jpg"
        resolved = get_public_creative_url(base_url, object_key)
        self.assertEqual(
            resolved,
            "https://images.lifemode.com/assets/amazon-affiliate-bot/creatives/B09X5BQ969/gift_inspiration_flow.jpg"
        )

        # Handles trailing slashes gracefully
        resolved_slash = get_public_creative_url("https://pub-123.r2.dev/", "/amazon-affiliate-bot/creatives/B01/test.jpg")
        self.assertEqual(resolved_slash, "https://pub-123.r2.dev/amazon-affiliate-bot/creatives/B01/test.jpg")

    def test_image_content_type_detection(self):
        """Verify MIME detection for supported creative image types."""
        self.assertEqual(get_image_content_type("img.jpg"), "image/jpeg")
        self.assertEqual(get_image_content_type("img.jpeg"), "image/jpeg")
        self.assertEqual(get_image_content_type("img.png"), "image/png")
        self.assertEqual(get_image_content_type("img.webp"), "image/webp")

    def test_r2_config_validation(self):
        """Verify required fields and validation checks."""
        # Missing account_id
        cfg_missing = R2Config(
            account_id="",
            access_key_id="test_key",
            secret_access_key="test_secret",
            bucket_name="lifemode-assets",
            public_base_url="https://images.lifemode.com",
        )
        with self.assertRaises(R2ConfigError) as ctx:
            cfg_missing.validate()
        self.assertIn("R2_ACCOUNT_ID", str(ctx.exception))

        # Missing multiple
        cfg_empty = R2Config("", "", "", "", "")
        with self.assertRaises(R2ConfigError) as ctx:
            cfg_empty.validate()
        self.assertIn("R2_ACCOUNT_ID", str(ctx.exception))
        self.assertIn("R2_ACCESS_KEY_ID", str(ctx.exception))
        self.assertIn("R2_SECRET_ACCESS_KEY", str(ctx.exception))
        self.assertIn("R2_PUBLIC_BASE_URL", str(ctx.exception))

        # Invalid URL scheme
        cfg_bad_url = R2Config("acc1", "key1", "sec1", "bkt1", "ftp://invalid-url.com")
        with self.assertRaises(R2ConfigError) as ctx:
            cfg_bad_url.validate()
        self.assertIn("must be a valid HTTP(S) URL", str(ctx.exception))

        # Valid config
        cfg_valid = R2Config("acc1", "key1", "sec1", "lifemode-assets", "https://images.lifemode.com")
        cfg_valid.validate()  # Does not raise


class TestR2UploadBehavior(unittest.TestCase):
    def setUp(self):
        self.temp_file = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        self.temp_file.write(b"CREATIVE_ORIGINAL_IMAGE_BYTES_12345")
        self.temp_file.close()
        self.temp_path = Path(self.temp_file.name)

        self.valid_config = R2Config(
            account_id="cf-acc-test",
            access_key_id="cf-key-test",
            secret_access_key="cf-secret-test",
            bucket_name="lifemode-assets",
            public_base_url="https://images.lifemode.com",
            endpoint="https://cf-acc-test.r2.cloudflarestorage.com",
        )

    def tearDown(self):
        self.temp_path.unlink(missing_ok=True)

    def test_upload_missing_file_rejected(self):
        """Fails gracefully if local file does not exist on disk."""
        res = upload_creative_to_r2("B09X5BQ969", "non_existent_file.jpg", config=self.valid_config)
        self.assertFalse(res["success"])
        self.assertIn("does not exist", res["error"])

    def test_upload_dry_run_returns_url_without_uploading(self):
        """Dry-run resolves the deterministic public URL without calling R2 network APIs."""
        res = upload_creative_to_r2(
            "B09X5BQ969",
            self.temp_path,
            dry_run=True,
            config=self.valid_config,
        )
        self.assertTrue(res["success"])
        self.assertTrue(res["dry_run"])
        self.assertFalse(res["uploaded"])
        self.assertEqual(
            res["public_url"],
            f"https://images.lifemode.com/amazon-affiliate-bot/creatives/B09X5BQ969/{self.temp_path.name}"
        )

    def test_upload_live_preserves_bytes_and_sends_put(self):
        """Live upload preserves exact bytes unchanged and uploads via R2Client PUT."""
        mock_client = Mock(spec=R2Client)
        mock_client.put_object.return_value = 200

        res = upload_creative_to_r2(
            "B09X5BQ969",
            self.temp_path,
            dry_run=False,
            config=self.valid_config,
            client=mock_client,
        )

        self.assertTrue(res["success"])
        self.assertTrue(res["uploaded"])
        self.assertEqual(res["size_bytes"], len(b"CREATIVE_ORIGINAL_IMAGE_BYTES_12345"))
        self.assertEqual(res["content_type"], "image/jpeg")

        # Verify client called with exact bytes and correct key
        mock_client.put_object.assert_called_once()
        call_args = mock_client.put_object.call_args
        self.assertEqual(call_args[0][0], f"amazon-affiliate-bot/creatives/B09X5BQ969/{self.temp_path.name}")
        self.assertEqual(call_args[0][1], b"CREATIVE_ORIGINAL_IMAGE_BYTES_12345")
        self.assertEqual(call_args[1]["content_type"], "image/jpeg")


class TestThreadsPayloadAndRejection(unittest.TestCase):
    def setUp(self):
        self.temp_file = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        self.temp_file.write(b"CREATIVE_BYTES_FOR_THREADS")
        self.temp_file.close()
        self.temp_path = Path(self.temp_file.name)

        self.adapter = ThreadsAdapter(credentials={"access_token": "mock-th-token", "user_id": "123456789"})

    def tearDown(self):
        self.temp_path.unlink(missing_ok=True)

    def test_threads_adapter_rejects_local_relative_path_directly(self):
        """Verify ThreadsAdapter immediately rejects local relative path before any API call."""
        content = {
            "text": "Discover luxury fragrance: Bellavita Honey Oud",
            "image_url": f"/creatives/B09X5BQ969/{self.temp_path.name}",
        }
        with patch("requests.post") as mock_post:
            res = self.adapter.publish(content, account_config={"account": "lifemodehq"}, dry_run=False)

        self.assertEqual(res["status"], "FAILED")
        self.assertIn("Threads requires a publicly accessible HTTPS image URL", res["errors"][0])
        # Crucial: NO network call was attempted
        mock_post.assert_not_called()

    def test_threads_adapter_constructs_container_payload_with_https_url(self):
        """Verify ThreadsAdapter constructs Step 1 container creation with media_type=IMAGE and image_url."""
        public_url = "https://images.lifemode.com/amazon-affiliate-bot/creatives/B09X5BQ969/flow.jpg"
        content = {
            "text": "Discover luxury fragrance: Bellavita Honey Oud",
            "image_url": public_url,
        }

        with patch("requests.post") as mock_post, patch("requests.get") as mock_get:
            # Mock step 1 (container creation)
            resp1 = Mock()
            resp1.status_code = 200
            resp1.json.return_value = {"id": "container_12345"}

            # Mock status check for container processing
            resp_status = Mock()
            resp_status.status_code = 200
            resp_status.json.return_value = {"status": "FINISHED"}

            # Mock step 2 (container publish)
            resp2 = Mock()
            resp2.status_code = 200
            resp2.json.return_value = {"id": "post_67890"}

            # Mock permalink resolution
            resp_p = Mock()
            resp_p.status_code = 200
            resp_p.json.return_value = {"data": [{"id": "post_67890", "permalink": "https://threads.net/@lifemodehq/post/1"}]}

            mock_post.side_effect = [resp1, resp2]
            mock_get.side_effect = [resp_status, resp_p]

            res = self.adapter.publish(content, account_config={"account": "lifemodehq"}, dry_run=False)

        self.assertEqual(res["status"], "PUBLISHED")
        self.assertEqual(res["post_id"], "post_67890")

        # Inspect Step 1 payload passed to graph.threads.net
        step1_call = mock_post.call_args_list[0]
        step1_url = step1_call[0][0]
        step1_data = step1_call[1]["data"]

        self.assertIn("123456789/threads", step1_url)
        self.assertEqual(step1_data["media_type"], "IMAGE")
        self.assertEqual(step1_data["image_url"], public_url)
        self.assertEqual(step1_data["text"], "Discover luxury fragrance: Bellavita Honey Oud")

    def test_social_publisher_uploads_to_r2_and_passes_url_to_threads(self):
        """Verify SocialPublisher uploads local creative to R2 and passes public URL to ThreadsAdapter."""
        public_url = "https://images.lifemode.com/amazon-affiliate-bot/creatives/B09X5BQ969/flow.jpg"

        package = {
            "asin": "B09X5BQ969",
            "status": "READY_FOR_REVIEW",
            "platforms": {
                "threads": {
                    "status": "VALID",
                    "content": "Discover Bellavita Honey Oud",
                }
            }
        }

        mock_creative = {
            "asin": "B09X5BQ969",
            "selected_image_path": str(self.temp_path),
            "uploaded_image_url": f"/creatives/B09X5BQ969/{self.temp_path.name}",
            "resolved_image_path": str(self.temp_path),
        }

        publisher = SocialPublisher(config=PublishingConfig.from_dict({}, dry_run_override=True))
        mock_th_adapter = Mock()
        mock_th_adapter.publish.return_value = {
            "platform": "threads",
            "status": "WOULD_PUBLISH",
            "mode": "DRY_RUN",
        }
        publisher.adapters["threads"] = mock_th_adapter

        with patch("content.pinterest_creative.has_creative_context", return_value=True), \
             patch("content.pinterest_creative.get_selected_creative", return_value=mock_creative), \
             patch("publishing.r2_storage.upload_creative_to_r2", return_value={"success": True, "public_url": public_url}) as mock_upload:

            report = publisher.publish_package(package, dry_run=True)

        mock_upload.assert_called_once()
        mock_th_adapter.publish.assert_called_once()
        call_kwargs = mock_th_adapter.publish.call_args.kwargs
        call_args = mock_th_adapter.publish.call_args.args
        passed_content = call_kwargs.get("content") or (call_args[0] if call_args else {})
        self.assertEqual(passed_content["image_url"], public_url)

    def test_social_publisher_fails_closed_if_r2_upload_fails(self):
        """Verify SocialPublisher fails closed if R2 upload fails, rejecting local relative path."""
        package = {
            "asin": "B09X5BQ969",
            "status": "READY_FOR_REVIEW",
            "platforms": {
                "threads": {
                    "status": "VALID",
                    "content": "Discover Bellavita Honey Oud",
                }
            }
        }

        mock_creative = {
            "asin": "B09X5BQ969",
            "selected_image_path": str(self.temp_path),
            "uploaded_image_url": f"/creatives/B09X5BQ969/{self.temp_path.name}",
            "resolved_image_path": str(self.temp_path),
        }

        publisher = SocialPublisher(config=PublishingConfig.from_dict({}, dry_run_override=True))

        with patch("content.pinterest_creative.has_creative_context", return_value=True), \
             patch("content.pinterest_creative.get_selected_creative", return_value=mock_creative), \
             patch("publishing.r2_storage.upload_creative_to_r2", return_value={"success": False, "error": "R2 network failure"}):

            report = publisher.publish_package(package, dry_run=True)

        th_result = report["results"]["threads"]
        self.assertEqual(th_result["status"], "FAILED")
        self.assertIn("Threads requires a publicly accessible HTTPS image URL", th_result["errors"][0])


if __name__ == "__main__":
    unittest.main()
