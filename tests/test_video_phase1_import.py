"""Focused Phase 1 tests for video-based affiliate import, R2 upload, package, queue, and detail UI."""

import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from amazon.scraper import Product
from content.package import ContentPackage, STATUS_READY_FOR_REVIEW, STATUS_VALID, is_valid_video_url
from publishing.queue import PublishingQueue, QueueItem, STATUS_READY
from publishing.r2_storage import (
    DEFAULT_VIDEO_KEY_PREFIX,
    DEFAULT_VIDEO_UPLOAD_TIMEOUT,
    R2Client,
    R2Config,
    get_video_content_type,
    get_video_object_key,
    upload_video_to_r2,
    validate_video_file,
)
from web import app as web_app
from web import services

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class TestR2VideoStorage(unittest.TestCase):
    def setUp(self):
        self.valid_config = R2Config(
            account_id="cf-acc-video",
            access_key_id="cf-key-video",
            secret_access_key="cf-sec-video",
            bucket_name="lifemode-assets",
            public_base_url="https://assets.lifemode.example",
            endpoint="https://cf-acc-video.r2.cloudflarestorage.com",
        )

    def test_video_object_key_and_mime_types(self):
        self.assertEqual(DEFAULT_VIDEO_KEY_PREFIX, "amazon-affiliate-bot/videos")
        self.assertEqual(
            get_video_object_key("b0vidtest1", "demo clip 01.mp4"),
            "amazon-affiliate-bot/videos/B0VIDTEST1/demo_clip_01.mp4",
        )
        self.assertEqual(
            get_video_object_key("B0VIDTEST1", r"C:\videos\showcase.MOV"),
            "amazon-affiliate-bot/videos/B0VIDTEST1/showcase.mov",
        )
        self.assertEqual(get_video_content_type("clip.mp4"), "video/mp4")
        self.assertEqual(get_video_content_type("clip.MOV"), "video/quicktime")

    def test_video_validation_enforces_extension_empty_and_configurable_size_limit(self):
        ok_mp4, err_mp4 = validate_video_file("clip.mp4", size_bytes=1024, max_size_bytes=2048)
        self.assertTrue(ok_mp4, err_mp4)

        ok_mov, err_mov = validate_video_file("clip.mov", size_bytes=2048, max_size_bytes=2048)
        self.assertTrue(ok_mov, err_mov)

        ok_avi, err_avi = validate_video_file("clip.avi", size_bytes=512, max_size_bytes=2048)
        self.assertFalse(ok_avi)
        self.assertIn("MP4", err_avi)

        ok_empty, err_empty = validate_video_file("clip.mp4", size_bytes=0, max_size_bytes=2048)
        self.assertFalse(ok_empty)
        self.assertIn("empty", err_empty.lower())

        ok_large, err_large = validate_video_file("clip.mp4", size_bytes=4096, max_size_bytes=2048)
        self.assertFalse(ok_large)
        self.assertIn("exceeds", err_large.lower())

    def test_upload_video_to_r2_preserves_bytes_and_passes_timeout(self):
        raw_bytes = b"\x00\x00\x00\x18ftypmp42_EXACT_VIDEO_PAYLOAD"
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as tmp:
            tmp.write(raw_bytes)
            tmp_path = Path(tmp.name)

        try:
            mock_opener = MagicMock()
            mock_resp = MagicMock()
            mock_resp.status = 200
            mock_resp.read.return_value = b""
            mock_opener.return_value.__enter__.return_value = mock_resp

            client = R2Client(self.valid_config, opener=mock_opener)
            res = upload_video_to_r2(
                asin="B0VIDTEST1",
                local_video_path=tmp_path,
                filename_override="product_reel.mp4",
                timeout=420,
                config=self.valid_config,
                client=client,
            )

            self.assertTrue(res["success"], res)
            self.assertEqual(res["object_key"], "amazon-affiliate-bot/videos/B0VIDTEST1/product_reel.mp4")
            self.assertEqual(
                res["public_url"],
                "https://assets.lifemode.example/amazon-affiliate-bot/videos/B0VIDTEST1/product_reel.mp4",
            )
            self.assertEqual(res["content_type"], "video/mp4")
            self.assertEqual(res["size_bytes"], len(raw_bytes))

            sent_req = mock_opener.call_args.args[0]
            sent_timeout = mock_opener.call_args.kwargs.get("timeout")
            self.assertEqual(sent_req.data, raw_bytes)
            self.assertEqual(sent_req.get_method(), "PUT")
            self.assertEqual(sent_timeout, 420)
        finally:
            tmp_path.unlink(missing_ok=True)


class TestContentPackageAndQueueVideoCompatibility(unittest.TestCase):
    AFFILIATE_URL = "https://www.amazon.com/dp/B0VIDTEST1?tag=homeideas4a01-20&linkCode=ll1&ref_=as_li_ss_tl"
    IMAGE_URL = "https://m.media-amazon.com/images/I/verified-cover.jpg"
    VIDEO_URL = "https://assets.lifemode.example/amazon-affiliate-bot/videos/B0VIDTEST1/demo.mp4"

    def _build_base_package(self):
        product = Product(
            title="Insulated Travel Mug",
            features=["Keeps drinks hot for 6 hours", "Fits standard cup holders"],
            affiliate_url=self.AFFILIATE_URL,
            original_affiliate_url=self.AFFILIATE_URL,
            asin="B0VIDTEST1",
            image_url=self.IMAGE_URL,
        )
        social = {
            "platforms": {
                "facebook": {"content": f"Great mug {self.AFFILIATE_URL}"},
                "pinterest": {"title": "Travel Mug", "description": f"Pin {self.AFFILIATE_URL}"},
                "threads": {"content": f"Threads {self.AFFILIATE_URL}"},
            }
        }
        return ContentPackage.build_package(product, social, timestamp="2026-11-05T12:00:00+00:00")

    def test_package_without_video_unchanged_and_attach_verified_video_preserves_image_and_affiliate_url(self):
        pkg = self._build_base_package()
        self.assertNotIn("media_type", pkg)
        self.assertNotIn("video_url", pkg)

        # Reject invalid non-HTTPS URL without mutating package
        self.assertFalse(is_valid_video_url("http://insecure.example/v.mp4"))
        self.assertFalse(ContentPackage.attach_verified_video(pkg, "C:/local/video.mp4"))
        self.assertNotIn("video_url", pkg)

        # Attach verified R2 HTTPS video URL
        ok = ContentPackage.attach_verified_video(
            pkg,
            video_url=self.VIDEO_URL,
            object_key="amazon-affiliate-bot/videos/B0VIDTEST1/demo.mp4",
            content_type="video/mp4",
            filename="demo.mp4",
            size_bytes=54321,
        )
        self.assertTrue(ok)
        self.assertEqual(pkg["media_type"], "video")
        self.assertEqual(pkg["video_url"], self.VIDEO_URL)
        self.assertEqual(pkg["source"]["affiliate_url"], self.AFFILIATE_URL)
        self.assertEqual(pkg["platforms"]["pinterest"]["link"], self.AFFILIATE_URL)

        # Existing cover image_url preserved across every platform entry
        for platform in ("facebook", "pinterest", "threads"):
            entry = pkg["platforms"][platform]
            self.assertEqual(entry["media_type"], "video")
            self.assertEqual(entry["video_url"], self.VIDEO_URL)
            self.assertEqual(entry["image_url"], self.IMAGE_URL)

    def test_queue_item_backward_compatibility_without_migration(self):
        legacy_dict = {
            "queue_item_id": "item_001_B0LEGACY01",
            "original_url": "https://www.amazon.com/dp/B0LEGACY01?tag=partner-20",
            "asin": "B0LEGACY01",
            "product_name": "Legacy Product",
            "scheduled_date": "2026-11-06",
            "daily_slot": 1,
            "scheduled_at": "2026-11-06T15:00:00+01:00",
            "timezone": "Europe/Budapest",
            "package_reference": "data/generated/asin_B0LEGACY01_content_package.json",
            "status": "READY",
            "validation_status": "READY_FOR_REVIEW",
            "errors": [],
            "created_at": "2026-11-05T10:00:00+00:00",
            "published_at": None,
            "publishing_results": None,
            "batch_id": "batch_legacy",
        }
        legacy_item = QueueItem.from_dict(legacy_dict)
        self.assertIsNone(legacy_item.media_type)
        self.assertIsNone(legacy_item.video_url)
        # Non-video items do not inject extra keys into serialized dict
        self.assertEqual(legacy_item.to_dict(), legacy_dict)

        video_item = QueueItem(
            queue_item_id="item_002_B0VIDTEST1",
            original_url=self.AFFILIATE_URL,
            asin="B0VIDTEST1",
            product_name="Insulated Travel Mug",
            scheduled_date="2026-11-06",
            daily_slot=2,
            scheduled_at="2026-11-06T21:00:00+01:00",
            package_reference="data/generated/asin_B0VIDTEST1_content_package.json",
            status=STATUS_READY,
            validation_status=STATUS_READY_FOR_REVIEW,
            media_type="video",
            video_url=self.VIDEO_URL,
        )
        restored = QueueItem.from_dict(video_item.to_dict())
        self.assertEqual(restored.media_type, "video")
        self.assertEqual(restored.video_url, self.VIDEO_URL)
        self.assertEqual(restored.original_url, self.AFFILIATE_URL)
        self.assertEqual(restored.package_reference, "data/generated/asin_B0VIDTEST1_content_package.json")


class TestDashboardVideoImportWorkflow(unittest.TestCase):
    URL_1 = "https://www.amazon.com/dp/B0VIDWF001?tag=homeideas4a01-20&ref=exact_track_1"
    URL_2 = "https://www.amazon.com/dp/B0VIDWF002?tag=homeideas4a01-20&ref=exact_track_2"
    COVER_IMG = "https://m.media-amazon.com/images/I/cover-wf001.jpg"

    def setUp(self):
        base_tmp = PROJECT_ROOT / ".test-tmp"
        base_tmp.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(dir=base_tmp))
        self.queue_file = self.root / "publishing_queue.json"
        self.batches_file = self.root / "import_batches.json"
        self.products_dir = self.root / "products"
        self.generated_dir = self.root / "generated"
        self.products_dir.mkdir(parents=True, exist_ok=True)
        self.generated_dir.mkdir(parents=True, exist_ok=True)

        for asin, url, title in (
            ("B0VIDWF001", self.URL_1, "Compact Espresso Grinder"),
            ("B0VIDWF002", self.URL_2, "Pour Over Coffee Kettle"),
        ):
            prod = Product(
                title=title,
                features=["40mm conical steel burrs", "30 grind settings"],
                affiliate_url=url,
                original_affiliate_url=url,
                source_url=url,
                asin=asin,
                image_url=self.COVER_IMG,
            )
            (self.products_dir / f"asin_{asin}.json").write_text(
                json.dumps(prod.to_dict(), indent=2),
                encoding="utf-8",
            )

        self.r2_cfg = R2Config(
            account_id="cf-acc",
            access_key_id="cf-key",
            secret_access_key="cf-sec",
            bucket_name="lifemode-assets",
            public_base_url="https://assets.lifemode.example",
            endpoint="https://cf-acc.r2.cloudflarestorage.com",
        )
        self.app = web_app.create_app({"TESTING": True})
        self.client = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def _mock_generate(product, disclosure):
        url = product.affiliate_url
        return {
            "platforms": {
                "facebook": {"text": f"Grounded FB copy.\n\n{url}"},
                "pinterest": {"title": "Espresso Grinder", "description": f"Grounded Pin copy. {url}"},
                "threads": {"text": f"Grounded Threads copy.\n\n{url}"},
            },
            "validation_metadata": {
                "facebook": {"status": STATUS_VALID, "retry_count": 0, "errors": []},
                "pinterest": {"status": STATUS_VALID, "retry_count": 0, "errors": []},
                "threads": {"status": STATUS_VALID, "retry_count": 0, "errors": []},
            },
        }

    def _patched_env(self, generate_fn=None):
        return (
            patch.object(services, "QUEUE_FILE", self.queue_file),
            patch.object(web_app, "DEFAULT_QUEUE_FILE", self.queue_file),
            patch.object(services, "BATCHES_FILE", self.batches_file),
            patch.object(services, "PRODUCTS_DIR", self.products_dir),
            patch.object(services, "GENERATED_DIR", self.generated_dir),
            patch("web.services.ContentGenerator.generate_all_platforms", side_effect=generate_fn or self._mock_generate),
        )

    def test_single_product_video_upload_via_dashboard_persists_package_queue_and_detail_preview(self):
        video_bytes = b"\x00\x00\x00\x18ftypqt  MOV_TEST_BYTES_98765"
        mock_r2_client = MagicMock(spec=R2Client)
        mock_r2_client.put_object.return_value = 200

        p1, p2, p3, p4, p5, p6 = self._patched_env()
        with p1, p2, p3, p4, p5, p6, \
             patch("publishing.r2_storage.R2Config.from_env", return_value=self.r2_cfg), \
             patch("publishing.r2_storage.R2Client", return_value=mock_r2_client):
            resp = self.client.post(
                "/add-product",
                data={
                    "url": self.URL_1,
                    "urls": "",
                    "video": (io.BytesIO(video_bytes), "grinder_showcase.mov"),
                },
                content_type="multipart/form-data",
            )
            self.assertEqual(resp.status_code, 200)
            body = resp.get_data(as_text=True)
            self.assertIn("ACCEPTED", body)
            self.assertIn("Video Uploaded to R2", body)

            # Verify exact R2 upload call
            mock_r2_client.put_object.assert_called_once_with(
                "amazon-affiliate-bot/videos/B0VIDWF001/grinder_showcase.mov",
                video_bytes,
                content_type="video/quicktime",
                timeout=DEFAULT_VIDEO_UPLOAD_TIMEOUT,
            )

            expected_r2_url = (
                "https://assets.lifemode.example/amazon-affiliate-bot/videos/B0VIDWF001/grinder_showcase.mov"
            )

            # Verify ContentPackage persistence
            pkg_file = self.generated_dir / "asin_B0VIDWF001_content_package.json"
            self.assertTrue(pkg_file.exists())
            pkg = json.loads(pkg_file.read_text(encoding="utf-8"))
            self.assertEqual(pkg["asin"], "B0VIDWF001")
            self.assertEqual(pkg["media_type"], "video")
            self.assertEqual(pkg["video_url"], expected_r2_url)
            self.assertEqual(pkg["source"]["affiliate_url"], self.URL_1)
            self.assertEqual(pkg["platforms"]["pinterest"]["link"], self.URL_1)
            self.assertEqual(pkg["platforms"]["pinterest"]["image_url"], self.COVER_IMG)
            for plat in ("facebook", "pinterest", "threads"):
                self.assertEqual(pkg["platforms"][plat]["video_url"], expected_r2_url)
                self.assertEqual(pkg["platforms"][plat]["media_type"], "video")

            # Verify QueueItem persistence
            queue = PublishingQueue.load(self.queue_file)
            self.assertEqual(len(queue.items), 1)
            q_item = queue.items[0]
            self.assertEqual(q_item.asin, "B0VIDWF001")
            self.assertEqual(q_item.original_url, self.URL_1)
            self.assertEqual(q_item.media_type, "video")
            self.assertEqual(q_item.video_url, expected_r2_url)

            # Verify Product Detail UI displays video preview, upload status, and ASIN
            detail_resp = self.client.get("/products/B0VIDWF001")
            self.assertEqual(detail_resp.status_code, 200)
            detail_html = detail_resp.get_data(as_text=True)
            self.assertIn("Attached Product Video", detail_html)
            self.assertIn("Uploaded to Cloudflare R2", detail_html)
            self.assertIn("B0VIDWF001", detail_html)
            self.assertIn(expected_r2_url, detail_html)
            self.assertIn("<video", detail_html)

    def test_r2_upload_is_skipped_when_content_validation_fails_or_product_unresolved(self):
        def failing_gen(product, disclosure):
            return {
                "platforms": {
                    "facebook": {"error": "Validation failed", "status": "FAILED_VALIDATION"},
                    "pinterest": {"error": "Validation failed", "status": "FAILED_VALIDATION"},
                    "threads": {"error": "Validation failed", "status": "FAILED_VALIDATION"},
                },
                "validation_metadata": {},
            }

        p1, p2, p3, p4, p5, p6 = self._patched_env(generate_fn=failing_gen)
        with p1, p2, p3, p4, p5, p6, \
             patch("web.services.upload_video_to_r2") as mock_upload_r2:
            # 1. Validation fails -> R2 must NOT be called
            res_fail = services.add_products_from_urls(
                self.URL_1,
                video_file=SimpleUpload("demo.mp4", b"video-bytes"),
            )
            self.assertEqual(res_fail["accepted"], 0)
            mock_upload_r2.assert_not_called()

            # 2. Unresolvable product -> R2 must NOT be called
            with patch("web.services.resolve_product_for_url", return_value=(False, None, None, ["Scrape failed"])):
                res_unres = services.add_products_from_urls(
                    "https://www.amazon.com/dp/B0MISSING1?tag=homeideas4a01-20",
                    video_file=SimpleUpload("demo.mp4", b"video-bytes"),
                )
                self.assertEqual(res_unres["accepted"], 0)
                mock_upload_r2.assert_not_called()

    def test_image_only_and_batch_workflows_remain_unchanged(self):
        p1, p2, p3, p4, p5, p6 = self._patched_env()
        with p1, p2, p3, p4, p5, p6, \
             patch("web.services.upload_video_to_r2") as mock_upload_r2:
            resp = self.client.post(
                "/add-product",
                data={"url": "", "urls": f"{self.URL_1}\n{self.URL_2}"},
            )
            self.assertEqual(resp.status_code, 200)
            mock_upload_r2.assert_not_called()

            queue = PublishingQueue.load(self.queue_file)
            self.assertEqual(len(queue.items), 2)
            for item in queue.items:
                self.assertIsNone(item.media_type)
                self.assertIsNone(item.video_url)

            pkg_1 = json.loads((self.generated_dir / "asin_B0VIDWF001_content_package.json").read_text(encoding="utf-8"))
            self.assertNotIn("video_url", pkg_1)
            self.assertEqual(pkg_1["source"]["affiliate_url"], self.URL_1)

            # Product detail for image-only product does not render Attached Product Video block
            detail_html = self.client.get("/products/B0VIDWF001").get_data(as_text=True)
            self.assertNotIn("Attached Product Video", detail_html)

    def test_batch_urls_with_attached_video_is_rejected_without_uploading(self):
        p1, p2, p3, p4, p5, p6 = self._patched_env()
        with p1, p2, p3, p4, p5, p6, \
             patch("web.services.upload_video_to_r2") as mock_upload_r2:
            res = services.add_products_from_urls(
                f"{self.URL_1}\n{self.URL_2}",
                video_file=SimpleUpload("demo.mp4", b"video-bytes"),
            )
            self.assertFalse(res["success"])
            self.assertEqual(res["accepted"], 0)
            mock_upload_r2.assert_not_called()

    def test_r2_upload_failure_rejects_item_and_does_not_queue(self):
        p1, p2, p3, p4, p5, p6 = self._patched_env()
        with p1, p2, p3, p4, p5, p6, \
             patch("web.services.upload_video_to_r2", return_value={"success": False, "error": "R2 network timeout"}):
            res = services.add_products_from_urls(
                self.URL_1,
                video_file=SimpleUpload("demo.mp4", b"video-bytes"),
            )
            self.assertEqual(res["accepted"], 0)
            self.assertEqual(res["rejected"], 1)
            self.assertEqual(res["results"][0]["status"], "VIDEO_UPLOAD_FAILED")
            self.assertIn("R2 network timeout", res["results"][0]["reason"])
            queue = PublishingQueue.load(self.queue_file)
            self.assertEqual(len(queue.items), 0)

    def test_existing_production_queue_file_remains_compatible_without_migration(self):
        real_queue_path = PROJECT_ROOT / "data" / "queue" / "publishing_queue.json"
        if real_queue_path.exists():
            queue = PublishingQueue.load(real_queue_path)
            self.assertGreater(len(queue.items), 0)
            for item in queue.items:
                round_trip = QueueItem.from_dict(item.to_dict())
                self.assertEqual(round_trip.queue_item_id, item.queue_item_id)
                self.assertEqual(round_trip.original_url, item.original_url)
                self.assertEqual(round_trip.package_reference, item.package_reference)


class SimpleUpload:
    """Minimal FileStorage-compatible stream wrapper for service-level tests."""

    def __init__(self, filename: str, payload: bytes):
        self.filename = filename
        self._stream = io.BytesIO(payload)

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


if __name__ == "__main__":
    unittest.main()
