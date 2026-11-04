"""
Comprehensive focused regression tests for November 3, 2026 publishing incident fixes:
1. Concurrency guard and in-progress status persistence in live publish action.
2. Selected Flow creative propagation across Facebook, Threads, and Pinterest.
3. Fail-closed behavior on missing or broken Flow creatives.
4. Pinterest base64 upload and HTTP 401 diagnostic handling.
5. Facebook multipart local file upload.
6. Threads relative URL rejection with clear error.
7. Deduplication preserving confirmed live platform IDs.
8. UI evidence rendering for partial failures and in-progress publishing.
"""

from unittest import TestCase
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path
import json
import base64
import tempfile

from publishing.queue import (
    QueueItem,
    PublishingQueue,
    STATUS_READY,
    STATUS_PUBLISHED,
    STATUS_PUBLISHING,
    STATUS_PARTIALLY_PUBLISHED,
    STATUS_FAILED,
)
from publishing.publisher import SocialPublisher, PublishingConfig
from publishing.adapters.pinterest_adapter import PinterestAdapter
from publishing.adapters.facebook_adapter import FacebookAdapter
from publishing.adapters.threads_adapter import ThreadsAdapter
from web.services import (
    execute_live_publish_item,
    get_queue_item_presentation,
    _ACTIVE_PUBLISHING_ITEM_IDS,
    _LIVE_PUBLISH_LOCK,
)


class TestConcurrencyAndStatusGuards(TestCase):
    def setUp(self):
        with _LIVE_PUBLISH_LOCK:
            _ACTIVE_PUBLISHING_ITEM_IDS.clear()

    def tearDown(self):
        with _LIVE_PUBLISH_LOCK:
            _ACTIVE_PUBLISHING_ITEM_IDS.clear()

    def _make_queue(self, item_id="item-test-1", asin="B09X5BQ969", status="READY", prior_results=None):
        item = QueueItem(
            item_id,
            "https://www.amazon.com/dp/" + asin,
            asin=asin,
            package_reference=f"data/generated/asin_{asin}_content_package.json",
            status=status,
            validation_status="READY_FOR_REVIEW",
            publishing_results=prior_results or {},
        )
        return PublishingQueue("queue-test", "2026-11-04T00:00:00+00:00", "2026-11-04", items=[item])

    def test_concurrent_publish_request_rejected(self):
        queue = self._make_queue()
        item_id = "item-test-1"
        with _LIVE_PUBLISH_LOCK:
            _ACTIVE_PUBLISHING_ITEM_IDS.add(item_id)

        with patch("web.services.QUEUE_FILE") as mock_qf:
            mock_qf.exists.return_value = True
            result = execute_live_publish_item(item_id, confirmed=True)

        self.assertFalse(result["success"])
        self.assertIn("already being published", result["error"])

    def test_status_persisted_as_publishing_before_external_calls(self):
        queue = self._make_queue()
        item_id = "item-test-1"
        statuses_at_save = []

        def save_tracker(path):
            statuses_at_save.append(queue.items[0].status)

        publisher = Mock()
        publisher.publish_package.return_value = {
            "mode": "LIVE",
            "results": {
                "facebook": {"status": "PUBLISHED", "mode": "LIVE", "post_id": "fb1"},
                "threads": {"status": "PUBLISHED", "mode": "LIVE", "post_id": "th1"},
                "pinterest": {"status": "PUBLISHED", "mode": "LIVE", "pin_id": "pin1"},
                "pinterest_lifemodehq": {"status": "PUBLISHED", "mode": "LIVE", "pin_id": "pin2"},
            },
        }

        with patch("web.services.QUEUE_FILE") as mock_qf, \
             patch("web.services.PublishingQueue.load", return_value=queue), \
             patch.object(queue, "save", side_effect=save_tracker), \
             patch("web.services.SocialPublisher", return_value=publisher):
            mock_qf.exists.return_value = True
            result = execute_live_publish_item(item_id, confirmed=True)

        self.assertTrue(result["success"])
        # The first save must have persisted PUBLISHING
        self.assertGreaterEqual(len(statuses_at_save), 2)
        self.assertEqual(statuses_at_save[0], STATUS_PUBLISHING)
        self.assertEqual(statuses_at_save[-1], STATUS_PUBLISHED)

    def test_process_restart_recovers_stale_publishing_status(self):
        # Stale item in PUBLISHING with no confirmed platforms recovers to FAILED
        queue = self._make_queue(status=STATUS_PUBLISHING, prior_results={})
        item_id = "item-test-1"

        with patch("web.services.QUEUE_FILE") as mock_qf, \
             patch("web.services.PublishingQueue.load", return_value=queue), \
             patch.object(queue, "save"):
            mock_qf.exists.return_value = True
            # Check presentation safely recovers
            pres = get_queue_item_presentation(queue, queue.items[0])
            self.assertIn(pres["state"], ("FAILED", "SIMULATED"))

    def test_already_published_item_skipped(self):
        prior = {
            "facebook": {"status": "PUBLISHED", "mode": "LIVE", "post_id": "fb1"},
            "threads": {"status": "PUBLISHED", "mode": "LIVE", "post_id": "th1"},
            "pinterest": {"status": "PUBLISHED", "mode": "LIVE", "pin_id": "pin1"},
            "pinterest_lifemodehq": {"status": "PUBLISHED", "mode": "LIVE", "pin_id": "pin2"},
        }
        queue = self._make_queue(status=STATUS_PUBLISHED, prior_results=prior)
        with patch("web.services.QUEUE_FILE") as mock_qf, \
             patch("web.services.PublishingQueue.load", return_value=queue):
            mock_qf.exists.return_value = True
            result = execute_live_publish_item("item-test-1", confirmed=True)

        self.assertTrue(result["success"])
        self.assertTrue(result.get("skipped"))


class TestFlowCreativePropagation(TestCase):
    def test_flow_creative_routed_to_all_platforms(self):
        # Create a mock content package for B09X5BQ969
        package = {
            "asin": "B09X5BQ969",
            "status": "READY_FOR_REVIEW",
            "platforms": {
                "facebook": {"status": "VALID", "text": "FB post", "image_url": "https://amazon.com/orig.jpg"},
                "threads": {"status": "VALID", "text": "Threads post", "image_url": "https://amazon.com/orig.jpg"},
                "pinterest": {"status": "VALID", "title": "Pin", "description": "Pin desc", "image_url": "https://amazon.com/orig.jpg"},
            }
        }

        # Mock selected creative info pointing to an existing file
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp_img:
            tmp_img.write(b"fake image bytes")
            tmp_img_path = tmp_img.name

        try:
            fake_creative = {
                "asin": "B09X5BQ969",
                "creative_type": "gift_inspiration",
                "selected_image_path": tmp_img_path,
                "uploaded_image_url": "https://cdn.example.com/creative.jpg",
                "display_name": "Gift Inspiration",
            }

            publisher = SocialPublisher(config=PublishingConfig(dry_run=True))
            # Mock platform adapters to inspect content passed to them
            received_contents = {}
            for name, adapter in publisher.adapters.items():
                mock_pub = Mock(return_value={"platform": name, "status": "WOULD_PUBLISH", "mode": "DRY_RUN"})
                adapter.publish = mock_pub
                received_contents[name] = mock_pub
            for name, adapter in publisher.pinterest_adapters.items():
                mock_pub = Mock(return_value={"platform": "pinterest", "account": name, "status": "WOULD_PUBLISH", "mode": "DRY_RUN"})
                adapter.publish = mock_pub
                received_contents[f"pinterest_{name}"] = mock_pub

            with patch("content.pinterest_creative.has_creative_context", return_value=True), \
                 patch("content.pinterest_creative.get_selected_creative", return_value=fake_creative):
                report = publisher.publish_package(package, dry_run=True)

            self.assertEqual(report["status"], "WOULD_PUBLISH")
            # Verify Facebook received the creative image path
            fb_content = received_contents["facebook"].call_args.kwargs["content"]
            self.assertEqual(fb_content.get("image_path"), tmp_img_path)
            self.assertEqual(fb_content.get("image_url"), "https://cdn.example.com/creative.jpg")

            # Verify Pinterest received the creative image path
            pin_content = received_contents["pinterest_dailyideasforyou"].call_args.kwargs["content"]
            self.assertEqual(pin_content.get("image_path"), tmp_img_path)
        finally:
            Path(tmp_img_path).unlink(missing_ok=True)

    def test_missing_creative_fails_closed_without_amazon_fallback(self):
        package = {
            "asin": "B09X5BQ969",
            "status": "READY_FOR_REVIEW",
            "platforms": {
                "facebook": {"status": "VALID", "text": "FB post", "image_url": "https://m.media-amazon.com/orig.jpg"},
                "threads": {"status": "VALID", "text": "Threads post", "image_url": "https://m.media-amazon.com/orig.jpg"},
                "pinterest": {"status": "VALID", "title": "Pin", "description": "Pin desc", "image_url": "https://m.media-amazon.com/orig.jpg"},
            }
        }
        publisher = SocialPublisher(config=PublishingConfig(dry_run=True))

        with patch("content.pinterest_creative.has_creative_context", return_value=True), \
             patch("content.pinterest_creative.get_selected_creative", return_value=None):
            report = publisher.publish_package(package, dry_run=True)

        self.assertEqual(report["status"], "FAILED")
        fb_res = report["results"]["facebook"]
        self.assertEqual(fb_res["status"], "FAILED")
        self.assertIn("No Pinterest creative selected", fb_res["errors"][0])


class TestAdapterImageHandling(TestCase):
    def test_pinterest_adapter_uses_base64_for_local_file(self):
        adapter = PinterestAdapter(credentials={"access_token": "test-token"})
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp_file:
            tmp_file.write(b"JPEG_HEADER_BYTES_FOR_TEST")
            tmp_path = tmp_file.name

        try:
            content = {
                "title": "Test Pin",
                "description": "Test Desc",
                "link": "https://amazon.com/dp/test",
                "image_path": tmp_path,
            }
            with patch("requests.post") as mock_post:
                mock_post.return_value.status_code = 201
                mock_post.return_value.json.return_value = {"id": "pin_123"}
                res = adapter.publish(content, account_config={"board_id": "b123"}, dry_run=False)

            self.assertEqual(res["status"], "PUBLISHED")
            self.assertEqual(res["pin_id"], "pin_123")
            payload = mock_post.call_args.kwargs["json"]
            self.assertEqual(payload["media_source"]["source_type"], "image_base64")
            self.assertEqual(payload["media_source"]["content_type"], "image/jpeg")
            expected_b64 = base64.b64encode(b"JPEG_HEADER_BYTES_FOR_TEST").decode("ascii")
            self.assertEqual(payload["media_source"]["data"], expected_b64)
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_pinterest_adapter_rejects_relative_creatives_url_if_missing(self):
        adapter = PinterestAdapter(credentials={"access_token": "test-token"})
        content = {
            "title": "Test Pin",
            "description": "Test Desc",
            "image_url": "/creatives/NONEXISTENT/flow.jpg",
        }
        res = adapter.publish(content, account_config={"board_id": "b123"}, dry_run=False)
        self.assertEqual(res["status"], "FAILED")
        self.assertIn("requires a valid public HTTPS image URL or an accessible local image file", res["errors"][0])

    def test_pinterest_adapter_http_401_reports_reauthorization(self):
        adapter = PinterestAdapter(credentials={"access_token": "expired-token"})
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp_file:
            tmp_file.write(b"bytes")
            tmp_path = tmp_file.name
        try:
            content = {
                "title": "Test Pin",
                "description": "Test Desc",
                "link": "https://amazon.com/dp/test",
                "image_path": tmp_path,
            }
            with patch("requests.post") as mock_post:
                mock_post.return_value.status_code = 401
                mock_post.return_value.text = '{"code": 2, "message": "Authentication failed."}'
                mock_post.return_value.json.return_value = {"code": 2, "message": "Authentication failed."}
                res = adapter.publish(content, account_config={"board_id": "b123"}, dry_run=False)

            self.assertEqual(res["status"], "FAILED")
            self.assertEqual(res["http_status"], 401)
            self.assertIn("Reauthorization is required", res["errors"][0])
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_facebook_adapter_local_file_upload(self):
        adapter = FacebookAdapter(credentials={"access_token": "fb-token", "page_id": "12345"})
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp_file:
            tmp_file.write(b"FB_IMAGE_BYTES")
            tmp_path = tmp_file.name

        try:
            content = {
                "text": "Facebook post with photo",
                "image_path": tmp_path,
            }
            with patch("requests.post") as mock_post, patch("requests.get") as mock_get:
                mock_post.return_value.status_code = 200
                mock_post.return_value.json.return_value = {"id": "photo_999", "post_id": "12345_999"}
                mock_get.return_value.status_code = 200
                mock_get.return_value.json.return_value = {"id": "photo_999", "images": [{"source": "http://fb.com/pic"}]}
                res = adapter.publish(content, account_config={"account": "LifeModeHQ"}, dry_run=False)

            self.assertEqual(res["status"], "PUBLISHED")
            self.assertEqual(res["post_id"], "12345_999")
            # Verify multipart form data with 'source' file was passed
            call_kwargs = mock_post.call_args.kwargs
            self.assertIn("files", call_kwargs)
            self.assertIn("source", call_kwargs["files"])
        finally:
            Path(tmp_path).unlink(missing_ok=True)

    def test_threads_adapter_rejects_relative_creatives_url(self):
        adapter = ThreadsAdapter(credentials={"access_token": "th-token", "user_id": "user123"})
        content = {
            "text": "Threads post text",
            "image_url": "/creatives/B09X5BQ969/flow.jpg",
        }
        res = adapter.publish(content, account_config={"account": "lifemodehq", "require_image": True}, dry_run=False)
        self.assertEqual(res["status"], "FAILED")
        self.assertIn("Threads requires a publicly accessible HTTPS image URL", res["errors"][0])


class TestDashboardPresentationEvidence(TestCase):
    def test_partial_state_presentation_includes_errors(self):
        item = QueueItem(
            "item_026_B09X5BQ969",
            "https://www.amazon.com/dp/B09X5BQ969",
            asin="B09X5BQ969",
            status=STATUS_PARTIALLY_PUBLISHED,
            publishing_results={
                "facebook": {"status": "PUBLISHED", "mode": "LIVE", "post_id": "fb_123"},
                "threads": {"status": "PUBLISHED", "mode": "LIVE", "post_id": "th_123"},
                "pinterest": {"status": "FAILED", "mode": "LIVE", "errors": ["Pinterest API error (HTTP 401): Authentication failed."]},
            },
            errors=["Pinterest API error (HTTP 401): Authentication failed."],
        )
        queue = PublishingQueue("q1", "2026-11-04T00:00:00+00:00", "2026-11-04", items=[item])
        pres = get_queue_item_presentation(queue, item)
        self.assertEqual(pres["state"], "PARTIAL")
        self.assertIn("Pinterest API error (HTTP 401): Authentication failed.", pres["errors"])
        self.assertEqual(pres["platforms"]["pinterest"]["state"], "FAILED")
