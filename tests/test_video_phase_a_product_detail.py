"""
Focused Phase A tests for Product Detail video upload, replacement, removal,
ContentPackage/QueueItem consistency, and queue synchronization.
"""

from contextlib import ExitStack
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from amazon.scraper import Product
from content.package import ContentPackage, STATUS_READY_FOR_REVIEW
from publishing.queue import (
    PublishingQueue,
    QueueItem,
    STATUS_PUBLISHED,
    STATUS_READY,
)
from publishing.r2_storage import R2Client, R2Config, R2UploadError
from publishing.sync import merge_publishing_queues
from web import services
from web.app import create_app


class TestVideoPhaseAProductDetail(unittest.TestCase):
    ASIN = "B0PHASEAV1"
    AFFILIATE_URL = "https://www.amazon.com/dp/B0PHASEAV1?tag=lifemode-20&ref_=as_li_ss_tl"
    COVER_IMAGE_URL = "https://m.media-amazon.com/images/I/71PhaseACover._AC_SL1500_.jpg"
    R2_BASE = "https://pub-8fcd679c40fd4aaa851f6ee7cdd4d083.r2.dev"
    QUEUE_ITEM_ID = "item_042_B0PHASEAV1"

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_video_phase_a_"))
        self.products_dir = self.temp_dir / "data" / "products"
        self.generated_dir = self.temp_dir / "data" / "generated"
        self.queue_dir = self.temp_dir / "data" / "queue"
        self.batches_dir = self.temp_dir / "data" / "batches"
        for directory in (self.products_dir, self.generated_dir, self.queue_dir, self.batches_dir):
            directory.mkdir(parents=True, exist_ok=True)

        self.queue_file = self.queue_dir / "publishing_queue.json"
        self.batches_file = self.batches_dir / "import_batches.json"

        self.product = Product(
            title="Stainless Steel Insulated Travel Tumbler 40 oz",
            asin=self.ASIN,
            price="$29.99",
            image_url=self.COVER_IMAGE_URL,
            affiliate_url=self.AFFILIATE_URL,
            original_affiliate_url=self.AFFILIATE_URL,
            source_url="https://www.amazon.com/dp/B0PHASEAV1",
            features=[
                "40 oz double-wall vacuum insulated 18/8 stainless steel body",
                "Includes leak-resistant flip straw lid and ergonomic side handle",
                "Cup-holder tapered base measures 3.1 inches in diameter",
            ],
        )
        (self.products_dir / f"asin_{self.ASIN}.json").write_text(
            json.dumps(self.product.to_dict(), indent=2),
            encoding="utf-8",
        )

        self.original_fb_copy = (
            f"The 40 oz HydroPeak tumbler features an 18/8 stainless steel body.\n\n"
            f"Includes a flip straw lid and side handle.\n\n"
            f"Link: {self.AFFILIATE_URL}\n\n"
            f"As an Amazon Associate I earn from qualifying purchases."
        )
        self.original_pin_title = "HydroPeak 40 oz Stainless Steel Travel Tumbler"
        self.original_pin_desc = (
            f"40 oz double-wall stainless steel tumbler with flip straw lid and side handle. "
            f"{self.AFFILIATE_URL}\n\nAs an Amazon Associate I earn from qualifying purchases."
        )
        self.original_th_copy = (
            f"40 oz stainless steel tumbler with flip straw lid and cup-holder base.\n\n"
            f"Link: {self.AFFILIATE_URL}\n\n"
            f"As an Amazon Associate I earn from qualifying purchases."
        )

        social_data = {
            "asin": self.ASIN,
            "affiliate_url": self.AFFILIATE_URL,
            "platforms": {
                "facebook": {"text": self.original_fb_copy},
                "pinterest": {
                    "title": self.original_pin_title,
                    "description": self.original_pin_desc,
                },
                "threads": {"text": self.original_th_copy},
            },
            "validation_metadata": {
                "facebook": {"status": "VALID", "retry_count": 0, "errors": []},
                "pinterest": {"status": "VALID", "retry_count": 0, "errors": []},
                "threads": {"status": "VALID", "retry_count": 0, "errors": []},
            },
        }
        pkg = ContentPackage.build_package(
            product=self.product,
            social_data=social_data,
            cache_file_path=f"data/products/asin_{self.ASIN}.json",
            model_name="test-model",
        )
        self.pkg_file = ContentPackage.save_content_package(
            pkg,
            f"asin_{self.ASIN}",
            target_dir=self.generated_dir,
        )

        queue = PublishingQueue(
            queue_id="queue_phase_a",
            created_at="2026-11-05T10:00:00+01:00",
            start_date="2026-11-06",
            total_days=30,
            products_per_day=2,
            target_capacity=60,
            items=[
                QueueItem(
                    queue_item_id=self.QUEUE_ITEM_ID,
                    original_url=self.AFFILIATE_URL,
                    asin=self.ASIN,
                    product_name=self.product.title,
                    scheduled_date="2026-11-06",
                    daily_slot=1,
                    scheduled_at="2026-11-06T15:00:00+01:00",
                    timezone="Europe/Budapest",
                    package_reference=f"data/generated/asin_{self.ASIN}_content_package.json",
                    status=STATUS_READY,
                    validation_status=STATUS_READY_FOR_REVIEW,
                    batch_id="batch_initial_01",
                )
            ],
        )
        queue.recalculate_counts()
        queue.save(self.queue_file)

        self.r2_cfg = R2Config(
            account_id="acct_test",
            access_key_id="key_test",
            secret_access_key="secret_test",
            bucket_name="lifemode-assets",
            public_base_url=self.R2_BASE,
            endpoint="https://acct_test.r2.cloudflarestorage.com",
        )
        self.mock_r2_client = MagicMock(spec=R2Client)
        self.mock_r2_client.put_object.return_value = 200

        self.app = create_app({"TESTING": True})
        self.client = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _enter_sandbox_patches(self, stack: ExitStack):
        stack.enter_context(patch.object(services, "PROJECT_ROOT", self.temp_dir))
        stack.enter_context(patch.object(services, "PRODUCTS_DIR", self.products_dir))
        stack.enter_context(patch.object(services, "GENERATED_DIR", self.generated_dir))
        stack.enter_context(patch.object(services, "QUEUE_FILE", self.queue_file))
        stack.enter_context(patch.object(services, "BATCHES_FILE", self.batches_file))
        stack.enter_context(patch.object(services, "load_application_config", return_value={}))
        stack.enter_context(patch("publishing.r2_storage.R2Config.from_env", return_value=self.r2_cfg))
        stack.enter_context(patch("publishing.r2_storage.R2Client", return_value=self.mock_r2_client))

    def _assert_copy_and_affiliate_preserved(self, pkg: dict, item: QueueItem):
        self.assertEqual(pkg["source"]["affiliate_url"], self.AFFILIATE_URL)
        self.assertEqual(pkg["status"], STATUS_READY_FOR_REVIEW)

        self.assertEqual(pkg["platforms"]["facebook"]["content"], self.original_fb_copy)
        self.assertIn(self.AFFILIATE_URL, pkg["platforms"]["facebook"]["content"])
        self.assertEqual(pkg["platforms"]["facebook"]["image_url"], self.COVER_IMAGE_URL)

        self.assertEqual(pkg["platforms"]["pinterest"]["title"], self.original_pin_title)
        self.assertEqual(pkg["platforms"]["pinterest"]["description"], self.original_pin_desc)
        self.assertEqual(pkg["platforms"]["pinterest"]["link"], self.AFFILIATE_URL)
        self.assertEqual(pkg["platforms"]["pinterest"]["image_url"], self.COVER_IMAGE_URL)

        self.assertEqual(pkg["platforms"]["threads"]["content"], self.original_th_copy)
        self.assertIn(self.AFFILIATE_URL, pkg["platforms"]["threads"]["content"])
        self.assertEqual(pkg["platforms"]["threads"]["image_url"], self.COVER_IMAGE_URL)

        self.assertEqual(item.queue_item_id, self.QUEUE_ITEM_ID)
        self.assertEqual(item.original_url, self.AFFILIATE_URL)
        self.assertEqual(item.scheduled_date, "2026-11-06")
        self.assertEqual(item.daily_slot, 1)
        self.assertEqual(item.scheduled_at, "2026-11-06T15:00:00+01:00")
        self.assertEqual(item.batch_id, "batch_initial_01")
        self.assertEqual(item.status, STATUS_READY)
        self.assertEqual(item.validation_status, STATUS_READY_FOR_REVIEW)

    def test_upload_replace_and_remove_video_via_dashboard_endpoints_preserves_all_product_data(self):
        with ExitStack() as stack:
            self._enter_sandbox_patches(stack)

            # 0. Before upload: Product Detail renders Upload Video form & Image Mode badge
            detail_before = self.client.get(f"/products/{self.ASIN}")
            self.assertEqual(detail_before.status_code, 200)
            html_before = detail_before.get_data(as_text=True)
            self.assertIn("No Video Attached", html_before)
            self.assertIn("Upload Video", html_before)
            self.assertIn("MP4 (.mp4)", html_before)
            self.assertIn("MOV (.mov)", html_before)
            self.assertIn("Image Mode", html_before)

            # 1. Upload initial video to existing product without video
            v1_bytes = b"\x00\x00\x00\x18ftypmp42initial_video_payload"
            resp_upload = self.client.post(
                f"/api/video/{self.ASIN}/upload",
                data={"video": (io.BytesIO(v1_bytes), "tumbler_clip_v1.mp4")},
                content_type="multipart/form-data",
                follow_redirects=True,
            )
            self.assertEqual(resp_upload.status_code, 200)
            html_after_upload = resp_upload.get_data(as_text=True)

            expected_v1_key = f"amazon-affiliate-bot/videos/{self.ASIN}/tumbler_clip_v1.mp4"
            expected_v1_url = f"{self.R2_BASE}/{expected_v1_key}"

            self.assertIn("Uploaded product video for ASIN B0PHASEAV1", html_after_upload)
            self.assertIn(expected_v1_url, html_after_upload)
            self.assertIn("Replace Video", html_after_upload)
            self.assertIn("Remove Video", html_after_upload)
            self.assertIn("Video Attached", html_after_upload)

            pkg_after_v1 = json.loads(self.pkg_file.read_text(encoding="utf-8"))
            queue_after_v1 = PublishingQueue.load(self.queue_file)
            item_after_v1 = queue_after_v1.items[0]

            self.assertEqual(pkg_after_v1["media_type"], "video")
            self.assertEqual(pkg_after_v1["video_url"], expected_v1_url)
            self.assertEqual(pkg_after_v1["video_object_key"], expected_v1_key)
            self.assertEqual(pkg_after_v1["video_content_type"], "video/mp4")
            for plat in ("facebook", "pinterest", "threads"):
                self.assertEqual(pkg_after_v1["platforms"][plat]["media_type"], "video")
                self.assertEqual(pkg_after_v1["platforms"][plat]["video_url"], expected_v1_url)
            self.assertEqual(item_after_v1.media_type, "video")
            self.assertEqual(item_after_v1.video_url, expected_v1_url)
            self._assert_copy_and_affiliate_preserved(pkg_after_v1, item_after_v1)

            # 2. Replace existing video with a MOV file
            v2_bytes = b"\x00\x00\x00\x14ftypqt  replacement_mov_payload_bytes"
            resp_replace = self.client.post(
                f"/api/video/{self.ASIN}/upload",
                data={"video": (io.BytesIO(v2_bytes), "tumbler_clip_v2.mov")},
                content_type="multipart/form-data",
                follow_redirects=True,
            )
            self.assertEqual(resp_replace.status_code, 200)
            html_after_replace = resp_replace.get_data(as_text=True)

            expected_v2_key = f"amazon-affiliate-bot/videos/{self.ASIN}/tumbler_clip_v2.mov"
            expected_v2_url = f"{self.R2_BASE}/{expected_v2_key}"

            self.assertIn("Replaced product video for ASIN B0PHASEAV1", html_after_replace)
            self.assertIn(expected_v2_url, html_after_replace)
            self.assertNotIn(expected_v1_url, html_after_replace)

            pkg_after_v2 = json.loads(self.pkg_file.read_text(encoding="utf-8"))
            queue_after_v2 = PublishingQueue.load(self.queue_file)
            item_after_v2 = queue_after_v2.items[0]

            self.assertEqual(pkg_after_v2["media_type"], "video")
            self.assertEqual(pkg_after_v2["video_url"], expected_v2_url)
            self.assertEqual(pkg_after_v2["video_object_key"], expected_v2_key)
            self.assertEqual(pkg_after_v2["video_content_type"], "video/quicktime")
            self.assertEqual(pkg_after_v2["video"]["filename"], "tumbler_clip_v2.mov")
            for plat in ("facebook", "pinterest", "threads"):
                self.assertEqual(pkg_after_v2["platforms"][plat]["video_url"], expected_v2_url)
            self.assertEqual(item_after_v2.media_type, "video")
            self.assertEqual(item_after_v2.video_url, expected_v2_url)
            self._assert_copy_and_affiliate_preserved(pkg_after_v2, item_after_v2)

            # 3. Remove attached video (detaches from ContentPackage & QueueItem without R2 delete)
            resp_remove = self.client.post(
                f"/api/video/{self.ASIN}/remove",
                follow_redirects=True,
            )
            self.assertEqual(resp_remove.status_code, 200)
            html_after_remove = resp_remove.get_data(as_text=True)

            self.assertIn("Removed attached video from ASIN B0PHASEAV1", html_after_remove)
            self.assertIn("No Video Attached", html_after_remove)
            self.assertIn("Upload Video", html_after_remove)
            self.assertIn("Image Mode", html_after_remove)
            self.assertNotIn(expected_v2_url, html_after_remove)

            pkg_after_remove = json.loads(self.pkg_file.read_text(encoding="utf-8"))
            queue_after_remove = PublishingQueue.load(self.queue_file)
            item_after_remove = queue_after_remove.items[0]

            self.assertNotIn("video_url", pkg_after_remove)
            self.assertNotIn("video", pkg_after_remove)
            self.assertNotIn("video_object_key", pkg_after_remove)
            self.assertNotEqual(pkg_after_remove.get("media_type"), "video")
            for plat in ("facebook", "pinterest", "threads"):
                self.assertNotIn("video_url", pkg_after_remove["platforms"][plat])
                self.assertNotEqual(pkg_after_remove["platforms"][plat].get("media_type"), "video")

            self.assertIsNone(item_after_remove.media_type)
            self.assertIsNone(item_after_remove.video_url)
            raw_queue_item = json.loads(self.queue_file.read_text(encoding="utf-8"))["items"][0]
            self.assertNotIn("media_type", raw_queue_item)
            self.assertNotIn("video_url", raw_queue_item)
            self._assert_copy_and_affiliate_preserved(pkg_after_remove, item_after_remove)

            # Confirm only PUT uploads occurred on R2 (no physical R2 object deletion)
            self.assertEqual(self.mock_r2_client.put_object.call_count, 2)

    def test_r2_upload_failure_leaves_existing_package_and_queue_completely_intact(self):
        with ExitStack() as stack:
            self._enter_sandbox_patches(stack)

            # First attach a valid video so we also verify failed REPLACE does not corrupt prior video
            initial_file = self.temp_dir / "working_v1.mp4"
            initial_file.write_bytes(b"\x00\x00\x00\x18ftypmp42working_v1")
            res_ok = services.attach_product_video_service(self.ASIN, initial_file)
            self.assertTrue(res_ok["success"])
            working_url = res_ok["video_url"]

            # Now simulate R2 network/upload failure on replacement attempt
            self.mock_r2_client.put_object.side_effect = R2UploadError("R2 simulated HTTP 503 timeout")

            resp_fail = self.client.post(
                f"/api/video/{self.ASIN}/upload",
                data={"video": (io.BytesIO(b"\x00\x00\x00\x18ftypmp42broken_v2"), "broken_v2.mp4")},
                content_type="multipart/form-data",
                follow_redirects=True,
            )
            self.assertEqual(resp_fail.status_code, 200)
            html_fail = resp_fail.get_data(as_text=True)
            self.assertIn("R2 video upload failed", html_fail)
            self.assertIn(working_url, html_fail)

            # Verify AJAX mode returns 400 JSON with explicit error message
            ajax_fail = self.client.post(
                f"/api/video/{self.ASIN}/upload",
                data={"video": (io.BytesIO(b"\x00\x00\x00\x18ftypmp42broken_v2"), "broken_v2.mp4")},
                content_type="multipart/form-data",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            self.assertEqual(ajax_fail.status_code, 400)
            self.assertFalse(ajax_fail.get_json()["success"])

            # Package and queue still point to working_v1.mp4 with all copy & affiliate links intact
            pkg_reloaded = json.loads(self.pkg_file.read_text(encoding="utf-8"))
            queue_reloaded = PublishingQueue.load(self.queue_file)
            self.assertEqual(pkg_reloaded["video_url"], working_url)
            self.assertEqual(queue_reloaded.items[0].video_url, working_url)
            self._assert_copy_and_affiliate_preserved(pkg_reloaded, queue_reloaded.items[0])

    def test_invalid_video_extension_and_published_item_lock_are_enforced(self):
        with ExitStack() as stack:
            self._enter_sandbox_patches(stack)

            # Unsupported .avi format rejected before touching R2
            resp_bad_ext = self.client.post(
                f"/api/video/{self.ASIN}/upload",
                data={"video": (io.BytesIO(b"avi_bytes"), "clip.avi")},
                content_type="multipart/form-data",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            self.assertEqual(resp_bad_ext.status_code, 400)
            self.assertIn("Unsupported video format", resp_bad_ext.get_json()["message"])
            self.mock_r2_client.put_object.assert_not_called()

            # Mark queue item as PUBLISHED -> upload and remove must both refuse mutation
            queue = PublishingQueue.load(self.queue_file)
            queue.items[0].status = STATUS_PUBLISHED
            queue.items[0].media_type = "video"
            queue.items[0].video_url = f"{self.R2_BASE}/amazon-affiliate-bot/videos/{self.ASIN}/published.mp4"
            queue.save(self.queue_file)

            res_up_locked = services.attach_product_video_service(
                self.ASIN,
                io.BytesIO(b"unused"),
            )
            self.assertFalse(res_up_locked["success"])
            self.assertIn("already been published", res_up_locked["message"])

            res_rm_locked = services.detach_product_video_service(self.ASIN)
            self.assertFalse(res_rm_locked["success"])
            self.assertIn("already been published", res_rm_locked["message"])

    def test_sync_merge_propagates_video_replace_and_remove_for_ready_items(self):
        remote_v1 = {
            "queue_id": "q_sync",
            "created_at": "2026-11-05T10:00:00+01:00",
            "start_date": "2026-11-06",
            "items": [
                {
                    "queue_item_id": self.QUEUE_ITEM_ID,
                    "asin": self.ASIN,
                    "status": STATUS_READY,
                    "scheduled_date": "2026-11-06",
                    "daily_slot": 1,
                    "media_type": "video",
                    "video_url": f"{self.R2_BASE}/amazon-affiliate-bot/videos/{self.ASIN}/v1.mp4",
                }
            ],
        }

        # Case 1: Local READY item replaced video with v2.mp4 -> merged queue must use v2.mp4
        local_v2 = json.loads(json.dumps(remote_v1))
        local_v2["items"][0]["video_url"] = f"{self.R2_BASE}/amazon-affiliate-bot/videos/{self.ASIN}/v2.mp4"
        merged_replace, _ = merge_publishing_queues(local_v2, remote_v1, repo_dir=self.temp_dir)
        self.assertEqual(
            merged_replace["items"][0]["video_url"],
            f"{self.R2_BASE}/amazon-affiliate-bot/videos/{self.ASIN}/v2.mp4",
        )

        # Case 2: Local READY item removed video -> merged queue must drop media_type & video_url
        local_removed = json.loads(json.dumps(remote_v1))
        local_removed["items"][0].pop("media_type", None)
        local_removed["items"][0].pop("video_url", None)
        merged_remove, _ = merge_publishing_queues(local_removed, remote_v1, repo_dir=self.temp_dir)
        self.assertNotIn("media_type", merged_remove["items"][0])
        self.assertNotIn("video_url", merged_remove["items"][0])

        # Case 3: Remote PUBLISHED item keeps its published video metadata even if local was stale
        remote_published = json.loads(json.dumps(remote_v1))
        remote_published["items"][0]["status"] = STATUS_PUBLISHED
        merged_published, _ = merge_publishing_queues(local_removed, remote_published, repo_dir=self.temp_dir)
        self.assertEqual(merged_published["items"][0]["status"], STATUS_PUBLISHED)
        self.assertEqual(
            merged_published["items"][0]["video_url"],
            f"{self.R2_BASE}/amazon-affiliate-bot/videos/{self.ASIN}/v1.mp4",
        )


    def test_auto_sync_reused_on_video_upload_and_remove_when_enabled(self):
        non_test_app = create_app({"TESTING": False})
        non_test_client = non_test_app.test_client()

        with ExitStack() as stack:
            self._enter_sandbox_patches(stack)
            mock_sync = stack.enter_context(
                patch("web.app.execute_sync_with_github", return_value={"success": True, "message": "Synced OK"})
            )
            stack.enter_context(patch.dict("os.environ", {"AUTO_SYNC_AFTER_IMPORT": "1"}, clear=False))

            resp_up = non_test_client.post(
                f"/api/video/{self.ASIN}/upload",
                data={"video": (io.BytesIO(b"\x00\x00\x00\x18ftypmp42sync_test"), "sync_test.mp4")},
                content_type="multipart/form-data",
                follow_redirects=True,
            )
            self.assertEqual(resp_up.status_code, 200)
            self.assertEqual(mock_sync.call_count, 1)
            self.assertIn("synchronized to GitHub", resp_up.get_data(as_text=True))

            resp_rm = non_test_client.post(
                f"/api/video/{self.ASIN}/remove",
                follow_redirects=True,
            )
            self.assertEqual(resp_rm.status_code, 200)
            self.assertEqual(mock_sync.call_count, 2)


if __name__ == "__main__":
    unittest.main()
