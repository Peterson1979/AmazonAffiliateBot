"""
Phase 3 End-to-End Integration Tests for the Video-Based Affiliate Publishing Workflow.

Verifies the complete connected pipeline with mocked external APIs (zero real network calls):
1. Single Amazon product URL import + local video attachment via dashboard POST /add-product.
2. Cloudflare R2 upload and ContentPackage video attachment (preserving affiliate URL and cover image_url).
3. PublishingQueue persistence with video metadata intact (and bidirectional sync preservation).
4. QueuePublisher (scheduled) & execute_live_publish_item (manual) forwarding video metadata to SocialPublisher.
5. Orchestrator routing to:
   - Pinterest: video Pin on both configured accounts (dailyideasforyou & LifeModeHQ)
   - Facebook: normal native Page video post (/videos, never Reels)
   - Threads: native VIDEO container + polling + publish
6. Independent per-platform result & failure persistence, dashboard presentation, and duplicate-safe retries.
"""

from contextlib import ExitStack
import io
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from amazon.scraper import Product
from content.package import ContentPackage, STATUS_VALID
from publishing.queue import (
    PublishingQueue,
    QueuePublisher,
    STATUS_PARTIALLY_PUBLISHED,
    STATUS_PUBLISHED,
    STATUS_READY,
)
from publishing.r2_storage import R2Client, R2Config
from publishing.sync import find_sync_stage_files, merge_publishing_queues
from web import app as web_app
from web import services


def _http_resp(status_code=200, json_data=None, content=b"", text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = content
    resp.text = text if text else (json.dumps(json_data) if json_data is not None else "")
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("No JSON body")
    return resp


class TestVideoPhase3EndToEndIntegration(unittest.TestCase):
    ASIN = "B09V3KXJPB"
    AFFILIATE_URL = "https://www.amazon.com/dp/B09V3KXJPB?tag=homeideas4a01-20"
    COVER_IMAGE_URL = "https://m.media-amazon.com/images/I/71e2eCoverImage.jpg"
    VIDEO_BYTES = b"\x00\x00\x00\x18ftypmp42_PHASE3_E2E_VIDEO_STREAM"

    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="video_phase3_e2e_"))
        self.products_dir = self.root / "data" / "products"
        self.generated_dir = self.root / "data" / "generated"
        self.queue_file = self.root / "data" / "queue" / "publishing_queue.json"
        self.batches_file = self.root / "data" / "batches" / "import_batches.json"
        self.log_file = self.root / "data" / "logs" / "daily_publisher.log"

        self.products_dir.mkdir(parents=True, exist_ok=True)
        self.generated_dir.mkdir(parents=True, exist_ok=True)
        self.queue_file.parent.mkdir(parents=True, exist_ok=True)
        self.batches_file.parent.mkdir(parents=True, exist_ok=True)
        self.log_file.parent.mkdir(parents=True, exist_ok=True)

        # Pre-seed verified product cache for deterministic offline resolution
        product = Product(
            title="Compact Countertop Burr Coffee Grinder",
            description="Conical stainless steel burr coffee grinder with 40 grind settings.",
            features=["40 precise grind settings", "Anti-static grounds bin"],
            affiliate_url=self.AFFILIATE_URL,
            original_affiliate_url=self.AFFILIATE_URL,
            source_url=self.AFFILIATE_URL,
            price="$59.99",
            asin=self.ASIN,
            image_url=self.COVER_IMAGE_URL,
        )
        (self.products_dir / f"asin_{self.ASIN}.json").write_text(
            product.to_json(), encoding="utf-8"
        )

        self.r2_cfg = R2Config(
            account_id="cf-e2e-acc",
            access_key_id="cf-e2e-key",
            secret_access_key="cf-e2e-secret",
            bucket_name="lifemode-assets",
            public_base_url="https://assets.lifemode.example",
            endpoint="https://cf-e2e-acc.r2.cloudflarestorage.com",
        )
        self.expected_r2_video_url = (
            f"https://assets.lifemode.example/amazon-affiliate-bot/videos/{self.ASIN}/product_demo.mp4"
        )
        self.local_video_file = self.root / "product_demo.mp4"
        self.local_video_file.write_bytes(self.VIDEO_BYTES)

        self.app = web_app.create_app({"TESTING": True})
        self.client = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    @staticmethod
    def _mock_generate(product, disclosure):
        url = product.affiliate_url
        return {
            "platforms": {
                "facebook": {"text": f"Countertop burr grinder with 40 settings. {disclosure}\n\n{url}"},
                "pinterest": {
                    "title": "Compact Burr Coffee Grinder",
                    "description": f"40 grind settings for daily pour-over and espresso. #ad {url}",
                },
                "threads": {"text": f"40 grind settings in a compact footprint. #ad\n\n{url}"},
            },
            "validation_metadata": {
                "facebook": {"status": STATUS_VALID, "retry_count": 0, "errors": []},
                "pinterest": {"status": STATUS_VALID, "retry_count": 0, "errors": []},
                "threads": {"status": STATUS_VALID, "retry_count": 0, "errors": []},
            },
        }

    def _enter_sandbox_patches(self, stack: ExitStack) -> MagicMock:
        mock_r2_client = MagicMock(spec=R2Client)
        mock_r2_client.put_object.return_value = 200

        stack.enter_context(patch.object(services, "QUEUE_FILE", self.queue_file))
        stack.enter_context(patch.object(web_app, "DEFAULT_QUEUE_FILE", self.queue_file))
        stack.enter_context(patch.object(services, "BATCHES_FILE", self.batches_file))
        stack.enter_context(patch.object(services, "PRODUCTS_DIR", self.products_dir))
        stack.enter_context(patch.object(services, "GENERATED_DIR", self.generated_dir))
        stack.enter_context(patch.object(services, "DEFAULT_LOG_FILE", self.log_file))
        stack.enter_context(
            patch("web.services.ContentGenerator.generate_all_platforms", side_effect=self._mock_generate)
        )
        stack.enter_context(patch("publishing.r2_storage.R2Config.from_env", return_value=self.r2_cfg))
        stack.enter_context(patch("publishing.r2_storage.R2Client", return_value=mock_r2_client))
        stack.enter_context(
            patch.dict(
                os.environ,
                {
                    "DRY_RUN": "false",
                    "FACEBOOK_PAGE_ACCESS_TOKEN": "fb_e2e_tok",
                    "FACEBOOK_PAGE_ID": "44556677",
                    "PINTEREST_ACCESS_TOKEN": "pin_daily_e2e_tok",
                    "PINTEREST_LIFEMODEHQ_ACCESS_TOKEN": "pin_lifemode_e2e_tok",
                    "THREADS_ACCESS_TOKEN": "th_e2e_tok",
                    "THREADS_USER_ID": "998877",
                },
                clear=False,
            )
        )
        return mock_r2_client

    def test_full_e2e_dashboard_import_r2_upload_queue_and_scheduled_publish_all_platforms(self):
        post_calls = []
        get_calls = []

        def fake_post(url, **kwargs):
            post_calls.append((url, kwargs))
            # Guardrail: Facebook MUST use normal native Page /videos, NEVER /video_reels
            self.assertNotIn("video_reels", url)

            if url == "https://graph-video.facebook.com/v19.0/44556677/videos":
                return _http_resp(200, {"id": "fb_native_vid_501"})
            if url == "https://api.pinterest.com/v5/media":
                auth = kwargs["headers"]["Authorization"]
                if "pin_daily_e2e_tok" in auth:
                    return _http_resp(
                        201,
                        {
                            "media_id": "pin_media_daily",
                            "upload_url": "https://s3.pinterest.example/daily",
                            "upload_parameters": {"key": "daily_obj"},
                        },
                    )
                if "pin_lifemode_e2e_tok" in auth:
                    return _http_resp(
                        201,
                        {
                            "media_id": "pin_media_life",
                            "upload_url": "https://s3.pinterest.example/life",
                            "upload_parameters": {"key": "life_obj"},
                        },
                    )
            if url.startswith("https://s3.pinterest.example/"):
                return _http_resp(204)
            if url == "https://api.pinterest.com/v5/pins":
                auth = kwargs["headers"]["Authorization"]
                if "pin_daily_e2e_tok" in auth:
                    return _http_resp(201, {"id": "pin_id_daily_701"})
                return _http_resp(201, {"id": "pin_id_life_702"})
            if url == "https://graph.threads.net/v1.0/998877/threads":
                return _http_resp(200, {"id": "th_vid_container_801"})
            if url == "https://graph.threads.net/v1.0/998877/threads_publish":
                return _http_resp(200, {"id": "th_vid_post_802"})
            raise AssertionError(f"Unexpected POST URL: {url}")

        def fake_get(url, **kwargs):
            get_calls.append((url, kwargs))
            if url == self.expected_r2_video_url:
                return _http_resp(200, content=self.VIDEO_BYTES)
            if "graph.facebook.com/v19.0/fb_native_vid_501" in url:
                return _http_resp(
                    200,
                    {
                        "id": "fb_native_vid_501",
                        "permalink_url": "/LifeModeHQ/videos/fb_native_vid_501/",
                        "status": {"video_status": "ready"},
                    },
                )
            if url == "https://api.pinterest.com/v5/boards":
                return _http_resp(
                    200,
                    {"items": [{"id": "board_lifemode_resolved_2", "name": "Home & Everyday Living"}]},
                )
            if url == "https://api.pinterest.com/v5/media/pin_media_daily":
                return _http_resp(200, {"media_id": "pin_media_daily", "status": "succeeded"})
            if url == "https://api.pinterest.com/v5/media/pin_media_life":
                return _http_resp(200, {"media_id": "pin_media_life", "status": "succeeded"})
            if "graph.threads.net/v1.0/th_vid_container_801" in url:
                return _http_resp(200, {"status": "FINISHED"})
            if "graph.threads.net/v1.0/998877/threads" in url:
                return _http_resp(
                    200,
                    {
                        "data": [
                            {
                                "id": "th_vid_post_802",
                                "media_type": "VIDEO",
                                "permalink": "https://www.threads.com/@lifemodehq/post/th_vid_post_802",
                            }
                        ]
                    },
                )
            raise AssertionError(f"Unexpected GET URL: {url}")

        with ExitStack() as stack:
            mock_r2 = self._enter_sandbox_patches(stack)
            stack.enter_context(patch("time.sleep"))
            stack.enter_context(patch("requests.post", side_effect=fake_post))
            stack.enter_context(patch("requests.get", side_effect=fake_get))

            # 1 & 2: Dashboard import with attached MP4 -> R2 upload -> ContentPackage
            import_resp = self.client.post(
                "/add-product",
                data={
                    "url": self.AFFILIATE_URL,
                    "urls": "",
                    "video": (io.BytesIO(self.VIDEO_BYTES), "product_demo.mp4"),
                },
                content_type="multipart/form-data",
            )
            self.assertEqual(import_resp.status_code, 200)
            self.assertIn("ACCEPTED", import_resp.get_data(as_text=True))

            mock_r2.put_object.assert_called_once()
            self.assertEqual(
                mock_r2.put_object.call_args.args[0],
                f"amazon-affiliate-bot/videos/{self.ASIN}/product_demo.mp4",
            )
            self.assertEqual(mock_r2.put_object.call_args.args[1], self.VIDEO_BYTES)

            # 3: Verify queue item has video metadata & exact affiliate URL intact
            queue_before = PublishingQueue.load(self.queue_file)
            self.assertEqual(len(queue_before.items), 1)
            queued_item = queue_before.items[0]
            self.assertEqual(queued_item.status, STATUS_READY)
            self.assertEqual(queued_item.media_type, "video")
            self.assertEqual(queued_item.video_url, self.expected_r2_video_url)
            self.assertEqual(queued_item.original_url, self.AFFILIATE_URL)

            # 4 & 5: Execute real QueuePublisher -> SocialPublisher -> all 4 live platform adapters
            # Advance evaluation clock 5 minutes past the item's scheduled_at on the same Budapest day
            due_dt = QueuePublisher.parse_datetime(queued_item.scheduled_at)
            eval_iso = due_dt.replace(minute=min(due_dt.minute + 5, 59)).isoformat()

            pub_summary = QueuePublisher.publish_due_items(
                queue_file=self.queue_file,
                config={},  # Exercises cloud runner path (including LifeModeHQ board resolution)
                now_override=eval_iso,
                dry_run=False,
                log_file=self.log_file,
            )

            self.assertEqual(pub_summary["published_count"], 1)
            self.assertEqual(pub_summary["failed_count"], 0)
            self.assertEqual(pub_summary["partially_published_count"], 0)

            # 6: Verify persisted queue outcomes & dashboard presentation across all 4 destinations
            queue_after = PublishingQueue.load(self.queue_file)
            published_item = queue_after.items[0]
            self.assertEqual(published_item.status, STATUS_PUBLISHED)
            self.assertEqual(published_item.media_type, "video")
            self.assertEqual(published_item.video_url, self.expected_r2_video_url)

            res = published_item.publishing_results
            self.assertEqual(res["facebook"]["status"], "PUBLISHED")
            self.assertEqual(res["facebook"]["video_id"], "fb_native_vid_501")
            self.assertEqual(res["facebook"]["media_type"], "VIDEO")

            self.assertEqual(res["pinterest"]["status"], "PUBLISHED")
            self.assertEqual(res["pinterest"]["account"], "dailyideasforyou")
            self.assertEqual(res["pinterest"]["pin_id"], "pin_id_daily_701")
            self.assertEqual(res["pinterest"]["cover_image_url"], self.COVER_IMAGE_URL)
            self.assertEqual(res["pinterest"]["link"], self.AFFILIATE_URL)

            self.assertEqual(res["pinterest_lifemodehq"]["status"], "PUBLISHED")
            self.assertEqual(res["pinterest_lifemodehq"]["account"], "LifeModeHQ")
            self.assertEqual(res["pinterest_lifemodehq"]["board_id"], "board_lifemode_resolved_2")
            self.assertEqual(res["pinterest_lifemodehq"]["pin_id"], "pin_id_life_702")
            self.assertEqual(res["pinterest_lifemodehq"]["cover_image_url"], self.COVER_IMAGE_URL)
            self.assertEqual(res["pinterest_lifemodehq"]["link"], self.AFFILIATE_URL)

            self.assertEqual(res["threads"]["status"], "PUBLISHED")
            self.assertEqual(res["threads"]["post_id"], "th_vid_post_802")
            self.assertEqual(res["threads"]["media_type"], "VIDEO")

            # Verify Facebook payload used normal native /videos with file_url & published=True
            fb_calls = [c for c in post_calls if "graph-video.facebook.com" in c[0]]
            self.assertEqual(len(fb_calls), 1)
            self.assertEqual(fb_calls[0][1]["data"]["file_url"], self.expected_r2_video_url)
            self.assertTrue(fb_calls[0][1]["data"]["published"])
            self.assertIn(self.AFFILIATE_URL, fb_calls[0][1]["data"]["description"])

            # Verify dashboard presentation recognizes all 4 destinations as live PUBLISHED
            presentation = services.get_queue_item_presentation(
                queue_after, published_item, current_dt=due_dt
            )
            self.assertEqual(presentation["state"], "PUBLISHED")
            for dest in ("facebook", "pinterest", "pinterest_lifemodehq", "threads"):
                self.assertEqual(presentation["platforms"][dest]["state"], "PUBLISHED")

    def test_e2e_partial_platform_failure_and_manual_live_retry_skips_succeeded_destinations(self):
        threads_should_fail = {"value": True}
        fb_call_count = {"count": 0}
        pin_daily_call_count = {"count": 0}
        pin_life_call_count = {"count": 0}
        th_call_count = {"count": 0}

        def fake_post(url, **kwargs):
            if "graph-video.facebook.com" in url:
                fb_call_count["count"] += 1
                return _http_resp(200, {"id": "fb_vid_partial_1"})
            if url == "https://api.pinterest.com/v5/media":
                auth = kwargs["headers"]["Authorization"]
                if "pin_daily_e2e_tok" in auth:
                    pin_daily_call_count["count"] += 1
                    return _http_resp(
                        201,
                        {
                            "media_id": "m_d",
                            "upload_url": "https://s3.pinterest.example/d",
                            "upload_parameters": {"k": "1"},
                        },
                    )
                pin_life_call_count["count"] += 1
                return _http_resp(
                    201,
                    {
                        "media_id": "m_l",
                        "upload_url": "https://s3.pinterest.example/l",
                        "upload_parameters": {"k": "2"},
                    },
                )
            if url.startswith("https://s3.pinterest.example/"):
                return _http_resp(204)
            if url == "https://api.pinterest.com/v5/pins":
                auth = kwargs["headers"]["Authorization"]
                return _http_resp(
                    201, {"id": "pin_d_ok" if "pin_daily_e2e_tok" in auth else "pin_l_ok"}
                )
            if url == "https://graph.threads.net/v1.0/998877/threads":
                th_call_count["count"] += 1
                if threads_should_fail["value"]:
                    return _http_resp(500, {"error": {"message": "Transient Threads video outage"}})
                return _http_resp(200, {"id": "th_cont_recovered"})
            if url == "https://graph.threads.net/v1.0/998877/threads_publish":
                return _http_resp(200, {"id": "th_post_recovered"})
            raise AssertionError(f"Unexpected POST: {url}")

        def fake_get(url, **kwargs):
            if url == self.expected_r2_video_url:
                return _http_resp(200, content=self.VIDEO_BYTES)
            if "graph.facebook.com" in url:
                return _http_resp(200, {"id": "fb_vid_partial_1", "permalink_url": "/LifeModeHQ/videos/fb_vid_partial_1/"})
            if url == "https://api.pinterest.com/v5/boards":
                return _http_resp(200, {"items": [{"id": "b_life", "name": "Home & Everyday Living"}]})
            if "api.pinterest.com/v5/media/" in url:
                return _http_resp(200, {"status": "succeeded"})
            if "graph.threads.net/v1.0/th_cont_recovered" in url:
                return _http_resp(200, {"status": "FINISHED"})
            if "graph.threads.net/v1.0/998877/threads" in url:
                return _http_resp(200, {"data": [{"id": "th_post_recovered", "media_type": "VIDEO", "permalink": "https://www.threads.com/@lifemodehq/post/th_post_recovered"}]})
            raise AssertionError(f"Unexpected GET: {url}")

        with ExitStack() as stack:
            self._enter_sandbox_patches(stack)
            stack.enter_context(patch("time.sleep"))
            stack.enter_context(patch("requests.post", side_effect=fake_post))
            stack.enter_context(patch("requests.get", side_effect=fake_get))

            services.add_products_from_urls(
                self.AFFILIATE_URL,
                video_file=self.local_video_file,
            )
            queue = PublishingQueue.load(self.queue_file)
            item = queue.items[0]
            due_dt = QueuePublisher.parse_datetime(item.scheduled_at)

            # First run: Threads fails, Facebook + both Pinterest accounts succeed independently
            report1 = QueuePublisher.publish_due_items(
                queue_file=self.queue_file,
                config={},
                now_override=due_dt.isoformat(),
                dry_run=False,
                log_file=self.log_file,
            )
            self.assertEqual(report1["partially_published_count"], 1)

            q_mid = PublishingQueue.load(self.queue_file)
            self.assertEqual(q_mid.items[0].status, STATUS_PARTIALLY_PUBLISHED)
            self.assertEqual(q_mid.items[0].publishing_results["facebook"]["status"], "PUBLISHED")
            self.assertEqual(q_mid.items[0].publishing_results["pinterest"]["status"], "PUBLISHED")
            self.assertEqual(q_mid.items[0].publishing_results["pinterest_lifemodehq"]["status"], "PUBLISHED")
            self.assertEqual(q_mid.items[0].publishing_results["threads"]["status"], "FAILED")

            # Retry via dashboard manual publish: Threads recovers, other 3 are SKIP_DUPLICATE
            threads_should_fail["value"] = False
            manual_res = services.execute_live_publish_item(
                item.queue_item_id,
                confirmed=True,
                config={},
            )
            self.assertTrue(manual_res["success"])
            self.assertEqual(manual_res["status"], STATUS_PUBLISHED)
            self.assertEqual(manual_res["confirmed_platforms"], 1)
            self.assertEqual(manual_res["skipped_platforms"], 3)
            self.assertEqual(fb_call_count["count"], 1)
            self.assertEqual(pin_daily_call_count["count"], 1)
            self.assertEqual(pin_life_call_count["count"], 1)
            self.assertEqual(th_call_count["count"], 2)

    def test_queue_publisher_forwards_queue_item_video_metadata_and_sync_preserves_video_fields(self):
        with ExitStack() as stack:
            self._enter_sandbox_patches(stack)

            services.add_products_from_urls(
                self.AFFILIATE_URL,
                video_file=self.local_video_file,
            )

            # Verify sync merge preserves media_type & video_url both when item is new locally
            # and when remote has an older copy lacking video fields
            local_queue_raw = json.loads(self.queue_file.read_text(encoding="utf-8"))
            remote_empty = {
                "queue_id": "q_remote",
                "created_at": "2026-11-05T10:00:00+01:00",
                "start_date": "2026-11-05",
                "items": [],
            }
            merged_new, _ = merge_publishing_queues(local_queue_raw, remote_empty, repo_dir=self.root)
            self.assertEqual(merged_new["items"][0]["media_type"], "video")
            self.assertEqual(merged_new["items"][0]["video_url"], self.expected_r2_video_url)

            remote_without_video = {
                "queue_id": "q_remote",
                "created_at": "2026-11-05T10:00:00+01:00",
                "start_date": "2026-11-05",
                "items": [
                    {
                        k: v
                        for k, v in local_queue_raw["items"][0].items()
                        if k not in ("media_type", "video_url")
                    }
                ],
            }
            merged_enriched, _ = merge_publishing_queues(local_queue_raw, remote_without_video, repo_dir=self.root)
            self.assertEqual(merged_enriched["items"][0]["media_type"], "video")
            self.assertEqual(merged_enriched["items"][0]["video_url"], self.expected_r2_video_url)

            staged = find_sync_stage_files(merged_new, repo_dir=self.root)
            self.assertIn(f"data/generated/asin_{self.ASIN}_content_package.json", staged)

            # Simulate on-disk package missing video_url while QueueItem retains video_url:
            # QueuePublisher must forward QueueItem.video_url to the orchestrator!
            pkg_file = self.generated_dir / f"asin_{self.ASIN}_content_package.json"
            pkg_on_disk = json.loads(pkg_file.read_text(encoding="utf-8"))
            ContentPackage.detach_video(pkg_on_disk)
            pkg_file.write_text(json.dumps(pkg_on_disk), encoding="utf-8")

            queue = PublishingQueue.load(self.queue_file)
            due_dt = QueuePublisher.parse_datetime(queue.items[0].scheduled_at)

            captured_packages = []
            real_publish_package = services.SocialPublisher.publish_package

            def spy_publish_package(pub_self, package, *args, **kwargs):
                captured_packages.append(dict(package))
                return real_publish_package(pub_self, package, *args, **kwargs)

            with patch.object(services.SocialPublisher, "publish_package", spy_publish_package):
                dry_report = QueuePublisher.publish_due_items(
                    queue_file=self.queue_file,
                    config={},
                    now_override=due_dt.isoformat(),
                    dry_run=True,
                    log_file=self.log_file,
                )

            self.assertEqual(len(captured_packages), 1)
            self.assertEqual(captured_packages[0]["media_type"], "video")
            self.assertEqual(captured_packages[0]["video_url"], self.expected_r2_video_url)
            self.assertEqual(dry_report["items_processed"][0]["platform_statuses"]["facebook"], "WOULD_PUBLISH")

    def test_reimporting_cached_product_without_video_clears_stale_package_video_metadata(self):
        with ExitStack() as stack:
            self._enter_sandbox_patches(stack)

            # First import WITH video
            services.add_products_from_urls(
                self.AFFILIATE_URL,
                video_file=self.local_video_file,
            )
            queue = PublishingQueue.load(self.queue_file)
            queue.remove_item(queue.items[0].queue_item_id)
            queue.save(self.queue_file)

            # Re-import same ASIN WITHOUT video -> cached package must have stale video stripped
            res2 = services.add_products_from_urls(self.AFFILIATE_URL)
            self.assertEqual(res2["accepted"], 1)

            pkg_file = self.generated_dir / f"asin_{self.ASIN}_content_package.json"
            reloaded_pkg = json.loads(pkg_file.read_text(encoding="utf-8"))
            self.assertNotIn("video_url", reloaded_pkg)
            self.assertNotEqual(reloaded_pkg.get("media_type"), "video")
            for plat in ("facebook", "pinterest", "threads"):
                self.assertNotIn("video_url", reloaded_pkg["platforms"][plat])

            reloaded_queue = PublishingQueue.load(self.queue_file)
            self.assertIsNone(reloaded_queue.items[0].media_type)
            self.assertIsNone(reloaded_queue.items[0].video_url)


if __name__ == "__main__":
    unittest.main()
