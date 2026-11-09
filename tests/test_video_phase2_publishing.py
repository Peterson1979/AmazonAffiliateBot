"""
Focused offline/mocked tests for Phase 2 video-based affiliate publishing:
- ThreadsAdapter VIDEO containers, transcoding status polling, and FINISHED gate
- FacebookAdapter Page video publishing via graph-video.facebook.com and live confirmation
- PinterestAdapter /v5/media registration, multipart S3 upload, media polling, and video_id Pin creation with cover_image_url
- Multi-account Pinterest video publishing (dailyideasforyou & LifeModeHQ)
- SocialPublisher image-vs-video routing, Flow creative bypass for video packages, independent platform failure isolation, and backward compatibility
"""

import json
from pathlib import Path
import shutil
import unittest
from unittest.mock import MagicMock, patch

from amazon.scraper import Product
from content.package import ContentPackage
from publishing.adapters.base import STATUS_FAILED, STATUS_PUBLISHED, STATUS_WOULD_PUBLISH
from publishing.adapters.facebook_adapter import FacebookAdapter
from publishing.adapters.pinterest_adapter import PinterestAdapter
from publishing.adapters.threads_adapter import ThreadsAdapter
from publishing.config import PublishingConfig
from publishing.publisher import SocialPublisher, _confirmed_live_result
from publishing.queue import QueuePublisher, PublishingQueue, STATUS_PARTIALLY_PUBLISHED


R2_MP4_URL = "https://pub-example.r2.dev/amazon-affiliate-bot/videos/B08J5F3G18/demo_clip.mp4"
R2_MOV_URL = "https://pub-example.r2.dev/amazon-affiliate-bot/videos/B08J5F3G18/demo_clip.mov"
PRODUCT_IMAGE_URL = "https://m.media-amazon.com/images/I/71exampleCover.jpg"
AFFILIATE_URL = "https://www.amazon.com/dp/B08J5F3G18?tag=dailyideas0c7-20"


def _mock_response(status_code=200, json_data=None, content=b"", text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = content
    resp.text = text if text else (json.dumps(json_data) if json_data is not None else "")
    if json_data is not None:
        resp.json.return_value = json_data
    else:
        resp.json.side_effect = ValueError("No JSON body")
    return resp


def _build_ready_package(with_video: bool = True, video_url: str = R2_MP4_URL, image_url: str = PRODUCT_IMAGE_URL) -> dict:
    product = Product(
        title="Insulated Stainless Steel Travel Mug",
        description="Leakproof vacuum insulated travel mug.",
        features=["Double-wall insulation", "Fits cup holders"],
        affiliate_url=AFFILIATE_URL,
        original_affiliate_url=AFFILIATE_URL,
        source_url=AFFILIATE_URL,
        price="$24.99",
        asin="B08J5F3G18",
        image_url=image_url,
    )
    valid_text = f"Great everyday travel mug! #ad {AFFILIATE_URL}"
    social_data = {
        "asin": "B08J5F3G18",
        "platforms": {
            "facebook": {"content": valid_text},
            "threads": {"content": valid_text},
            "pinterest": {
                "title": "Insulated Travel Mug Idea",
                "description": valid_text,
                "board_suggestion": "Shopping ideas",
                "image_url": image_url,
                "link": AFFILIATE_URL,
            },
        },
    }
    pkg_dict = ContentPackage.build_package(
        product=product,
        social_data=social_data,
    )
    if with_video:
        ContentPackage.attach_verified_video(pkg_dict, video_url)
    return pkg_dict


def _live_config() -> PublishingConfig:
    cfg = PublishingConfig(dry_run=False)
    cfg.accounts = {k: dict(v) for k, v in cfg.accounts.items()}
    cfg.pinterest_accounts = {k: dict(v) for k, v in cfg.pinterest_accounts.items()}
    cfg.credentials = {
        "facebook": {"access_token": "fb_secret_token_123", "page_id": "99887766"},
        "pinterest": {"access_token": "pin_daily_token"},
        "pinterest_accounts": {
            "dailyideasforyou": {"access_token": "pin_daily_token"},
            "lifemodehq": {"access_token": "pin_lifemode_token"},
        },
        "threads": {"access_token": "th_secret_token", "user_id": "11223344"},
    }
    cfg.accounts["threads"]["video_poll_interval"] = 0
    cfg.accounts["threads"]["video_poll_max_attempts"] = 5
    cfg.pinterest_accounts["dailyideasforyou"]["board_id"] = "board_daily_1"
    cfg.pinterest_accounts["dailyideasforyou"]["video_poll_interval"] = 0
    cfg.pinterest_accounts["dailyideasforyou"]["video_poll_max_attempts"] = 5
    cfg.pinterest_accounts["lifemodehq"]["board_id"] = "board_life_2"
    cfg.pinterest_accounts["lifemodehq"]["video_poll_interval"] = 0
    cfg.pinterest_accounts["lifemodehq"]["video_poll_max_attempts"] = 5
    return cfg


class TestThreadsVideoPublishing(unittest.TestCase):
    def setUp(self):
        self.adapter = ThreadsAdapter(
            credentials={"access_token": "th_token_abc", "user_id": "556677"}
        )
        self.account_cfg = {
            "account": "lifemodehq",
            "video_poll_interval": 0,
            "video_poll_max_attempts": 5,
        }

    @patch("publishing.adapters.threads_adapter.time.sleep")
    @patch("publishing.adapters.threads_adapter.requests.get")
    @patch("publishing.adapters.threads_adapter.requests.post")
    def test_threads_video_container_polling_and_publish_after_finished(self, mock_post, mock_get, _mock_sleep):
        mock_post.side_effect = [
            _mock_response(200, {"id": "vid_container_101"}),
            _mock_response(200, {"id": "th_video_post_999"}),
        ]
        mock_get.side_effect = [
            _mock_response(200, {"status": "IN_PROGRESS"}),
            _mock_response(200, {"status": "IN_PROGRESS"}),
            _mock_response(200, {"status": "FINISHED"}),
            _mock_response(
                200,
                {
                    "data": [
                        {
                            "id": "th_video_post_999",
                            "media_type": "VIDEO",
                            "permalink": "https://www.threads.com/@lifemodehq/post/CxyzVideo",
                        }
                    ]
                },
            ),
        ]

        res = self.adapter.publish(
            content={"content": f"Video post #ad {AFFILIATE_URL}", "video_url": R2_MP4_URL},
            account_config=self.account_cfg,
            dry_run=False,
        )

        self.assertEqual(res["status"], STATUS_PUBLISHED)
        self.assertEqual(res["post_id"], "th_video_post_999")
        self.assertEqual(res["creation_id"], "vid_container_101")
        self.assertEqual(res["media_type"], "VIDEO")
        self.assertEqual(res["verified_media_type"], "VIDEO")
        self.assertEqual(res["video_url"], R2_MP4_URL)
        self.assertEqual(res["permalink"], "https://www.threads.com/@lifemodehq/post/CxyzVideo")
        self.assertTrue(_confirmed_live_result(res))

        # Verify Step 1 container creation payload
        step1_call = mock_post.call_args_list[0]
        self.assertEqual(step1_call.args[0], "https://graph.threads.net/v1.0/556677/threads")
        self.assertEqual(step1_call.kwargs["data"]["media_type"], "VIDEO")
        self.assertEqual(step1_call.kwargs["data"]["video_url"], R2_MP4_URL)

        # Verify Step 2 publish payload
        step2_call = mock_post.call_args_list[1]
        self.assertEqual(step2_call.args[0], "https://graph.threads.net/v1.0/556677/threads_publish")
        self.assertEqual(step2_call.kwargs["data"]["creation_id"], "vid_container_101")
        self.assertEqual(mock_get.call_count, 4)  # 3 status polls + 1 permalink lookup

    @patch("publishing.adapters.threads_adapter.time.sleep")
    @patch("publishing.adapters.threads_adapter.requests.get")
    @patch("publishing.adapters.threads_adapter.requests.post")
    def test_threads_video_does_not_publish_when_polling_times_out(self, mock_post, mock_get, _mock_sleep):
        mock_post.return_value = _mock_response(200, {"id": "vid_container_timeout"})
        mock_get.return_value = _mock_response(200, {"status": "IN_PROGRESS"})

        res = self.adapter.publish(
            content={"content": f"Video post #ad {AFFILIATE_URL}", "video_url": R2_MP4_URL},
            account_config={"account": "lifemodehq", "video_poll_interval": 0, "video_poll_max_attempts": 3},
            dry_run=False,
        )

        self.assertEqual(res["status"], STATUS_FAILED)
        self.assertEqual(mock_post.call_count, 1)  # Container created, threads_publish NEVER called
        self.assertEqual(mock_get.call_count, 3)
        self.assertIn("did not reach FINISHED", res["errors"][0])

    @patch("publishing.adapters.threads_adapter.time.sleep")
    @patch("publishing.adapters.threads_adapter.requests.get")
    @patch("publishing.adapters.threads_adapter.requests.post")
    def test_threads_video_aborts_immediately_on_container_error(self, mock_post, mock_get, _mock_sleep):
        mock_post.return_value = _mock_response(200, {"id": "vid_container_err"})
        mock_get.return_value = _mock_response(
            200, {"status": "ERROR", "error_message": "Unsupported codec"}
        )

        res = self.adapter.publish(
            content={"content": f"Video post #ad {AFFILIATE_URL}", "video_url": R2_MP4_URL},
            account_config=self.account_cfg,
            dry_run=False,
        )

        self.assertEqual(res["status"], STATUS_FAILED)
        self.assertEqual(mock_post.call_count, 1)  # Never calls threads_publish
        self.assertIn("Unsupported codec", res["errors"][0])

    @patch("publishing.adapters.threads_adapter.requests.post")
    def test_threads_rejects_local_or_non_https_video_url_before_api_call(self, mock_post):
        res = self.adapter.publish(
            content={"content": "Caption", "video_url": "C:/local/clip.mp4"},
            account_config=self.account_cfg,
            dry_run=False,
        )
        self.assertEqual(res["status"], STATUS_FAILED)
        mock_post.assert_not_called()

    @patch("publishing.adapters.threads_adapter.time.sleep")
    @patch("publishing.adapters.threads_adapter.requests.get")
    @patch("publishing.adapters.threads_adapter.requests.post")
    def test_threads_existing_image_behavior_unchanged(self, mock_post, mock_get, _mock_sleep):
        mock_post.side_effect = [
            _mock_response(200, {"id": "img_container_1"}),
            _mock_response(200, {"id": "th_img_post_1"}),
        ]
        mock_get.side_effect = [
            _mock_response(200, {"status": "FINISHED"}),
            _mock_response(200, {"data": []}),
        ]

        res = self.adapter.publish(
            content={"content": "Image post", "image_url": PRODUCT_IMAGE_URL},
            account_config={"account": "lifemodehq", "require_image": True},
            dry_run=False,
        )
        self.assertEqual(res["status"], STATUS_PUBLISHED)
        self.assertEqual(mock_post.call_args_list[0].kwargs["data"]["media_type"], "IMAGE")
        self.assertEqual(mock_post.call_args_list[0].kwargs["data"]["image_url"], PRODUCT_IMAGE_URL)


class TestFacebookVideoPublishing(unittest.TestCase):
    def setUp(self):
        self.adapter = FacebookAdapter(
            credentials={"access_token": "fb_secret_tok_999", "page_id": "123456789"}
        )

    @patch("publishing.adapters.facebook_adapter.requests.get")
    @patch("publishing.adapters.facebook_adapter.requests.post")
    def test_facebook_video_publish_and_live_confirmation(self, mock_post, mock_get):
        mock_post.return_value = _mock_response(200, {"id": "8877665544"})
        mock_get.return_value = _mock_response(
            200,
            {
                "id": "8877665544",
                "permalink_url": "/LifeModeHQ/videos/8877665544/",
                "status": {"video_status": "ready"},
            },
        )

        caption = f"Check out this travel mug! #ad {AFFILIATE_URL}"
        res = self.adapter.publish(
            content={"content": caption, "video_url": R2_MP4_URL},
            account_config={"account": "LifeModeHQ"},
            dry_run=False,
        )

        self.assertEqual(res["status"], STATUS_PUBLISHED)
        self.assertEqual(res["video_id"], "8877665544")
        self.assertEqual(res["post_id"], "8877665544")
        self.assertEqual(res["permalink_url"], "https://www.facebook.com/LifeModeHQ/videos/8877665544/")
        self.assertEqual(res["media_type"], "VIDEO")
        self.assertTrue(res["verified_video_read"])
        self.assertTrue(_confirmed_live_result(res))

        mock_post.assert_called_once()
        self.assertEqual(
            mock_post.call_args.args[0],
            "https://graph-video.facebook.com/v27.0/123456789/videos",
        )
        sent_payload = mock_post.call_args.kwargs["data"]
        self.assertEqual(sent_payload["file_url"], R2_MP4_URL)
        self.assertEqual(sent_payload["description"], caption)
        self.assertTrue(sent_payload["published"])
        self.assertEqual(sent_payload["access_token"], "fb_secret_tok_999")

    @patch("publishing.adapters.facebook_adapter.requests.post")
    def test_facebook_video_missing_id_fails_closed(self, mock_post):
        mock_post.return_value = _mock_response(200, {})

        res = self.adapter.publish(
            content={"content": "Caption", "video_url": R2_MP4_URL},
            account_config={"account": "LifeModeHQ"},
            dry_run=False,
        )
        self.assertEqual(res["status"], STATUS_FAILED)
        self.assertFalse(_confirmed_live_result(res))

    @patch("publishing.adapters.facebook_adapter.requests.post")
    def test_facebook_video_http_error_redacts_token(self, mock_post):
        mock_post.return_value = _mock_response(
            400,
            {"error": {"code": 100, "message": "Bad file_url with token fb_secret_tok_999"}},
        )

        res = self.adapter.publish(
            content={"content": "Caption", "video_url": R2_MP4_URL},
            account_config={"account": "LifeModeHQ"},
            dry_run=False,
        )
        self.assertEqual(res["status"], STATUS_FAILED)
        self.assertNotIn("fb_secret_tok_999", json.dumps(res))
        self.assertIn("[REDACTED]", res["errors"][0])

    @patch("publishing.adapters.facebook_adapter.requests.get")
    @patch("publishing.adapters.facebook_adapter.requests.post")
    def test_facebook_existing_photo_behavior_unchanged(self, mock_post, mock_get):
        mock_post.return_value = _mock_response(200, {"id": "photo_1", "post_id": "123456789_999"})
        mock_get.return_value = _mock_response(
            200, {"id": "photo_1", "link": "https://www.facebook.com/photo/1", "images": [{"source": PRODUCT_IMAGE_URL}]}
        )

        res = self.adapter.publish(
            content={"content": "Photo post", "image_url": PRODUCT_IMAGE_URL},
            account_config={"account": "LifeModeHQ", "require_image": True},
            dry_run=False,
        )
        self.assertEqual(res["status"], STATUS_PUBLISHED)
        self.assertEqual(mock_post.call_args.args[0], "https://graph.facebook.com/v27.0/123456789/photos")


class TestPinterestVideoPublishing(unittest.TestCase):
    def setUp(self):
        self.adapter = PinterestAdapter(credentials={"access_token": "pin_tok_1"})
        self.account_cfg = {
            "account": "dailyideasforyou",
            "board": "Shopping ideas",
            "board_id": "board_100",
            "video_poll_interval": 0,
            "video_poll_max_attempts": 5,
        }

    @patch("publishing.adapters.pinterest_adapter.time.sleep")
    @patch("publishing.adapters.pinterest_adapter.requests.post")
    @patch("publishing.adapters.pinterest_adapter.requests.get")
    def test_pinterest_full_video_pin_workflow_with_cover_image_url(self, mock_get, mock_post, _mock_sleep):
        video_binary = b"\x00\x00\x00\x18ftypmp42mockvideo"
        upload_params = {
            "key": "uploads/abc-123",
            "policy": "eyJleHBpcmF0aW9uIjoiMjAyNiJ9",
            "x-amz-signature": "sig123",
            "Content-Type": "multipart/form-data",
        }

        # GET calls: 1) download R2 video, 2) poll registered, 3) poll processing, 4) poll succeeded
        mock_get.side_effect = [
            _mock_response(200, content=video_binary),
            _mock_response(200, {"media_id": "media_555", "status": "registered"}),
            _mock_response(200, {"media_id": "media_555", "status": "processing"}),
            _mock_response(200, {"media_id": "media_555", "status": "succeeded"}),
        ]

        # POST calls: 1) /v5/media register, 2) S3 multipart upload (204), 3) /v5/pins create
        mock_post.side_effect = [
            _mock_response(
                201,
                {
                    "media_id": "media_555",
                    "media_type": "video",
                    "upload_url": "https://pinterest-media-upload.s3-accelerate.amazonaws.com/",
                    "upload_parameters": upload_params,
                },
            ),
            _mock_response(204, text=""),
            _mock_response(201, {"id": "pin_video_777"}),
        ]

        res = self.adapter.publish(
            content={
                "title": "Insulated Mug Video",
                "description": f"Keep coffee hot! #ad {AFFILIATE_URL}",
                "video_url": R2_MOV_URL,
                "image_url": PRODUCT_IMAGE_URL,
                "cover_image_url": PRODUCT_IMAGE_URL,
                "link": AFFILIATE_URL,
            },
            account_config=self.account_cfg,
            dry_run=False,
        )

        self.assertEqual(res["status"], STATUS_PUBLISHED)
        self.assertEqual(res["pin_id"], "pin_video_777")
        self.assertEqual(res["pin_url"], "https://www.pinterest.com/pin/pin_video_777/")
        self.assertEqual(res["media_id"], "media_555")
        self.assertEqual(res["cover_image_url"], PRODUCT_IMAGE_URL)
        self.assertEqual(res["media_type"], "VIDEO")
        self.assertTrue(_confirmed_live_result(res))

        # Step a: /v5/media registration
        reg_call = mock_post.call_args_list[0]
        self.assertEqual(reg_call.args[0], "https://api.pinterest.com/v5/media")
        self.assertEqual(reg_call.kwargs["json"], {"media_type": "video"})
        self.assertEqual(reg_call.kwargs["headers"]["Authorization"], "Bearer pin_tok_1")

        # Step c: S3 multipart upload (no Authorization header, correct MOV MIME)
        s3_call = mock_post.call_args_list[1]
        self.assertEqual(
            s3_call.args[0],
            "https://pinterest-media-upload.s3-accelerate.amazonaws.com/",
        )
        self.assertNotIn("headers", s3_call.kwargs)
        self.assertEqual(s3_call.kwargs["data"], upload_params)
        uploaded_tuple = s3_call.kwargs["files"]["file"]
        self.assertEqual(uploaded_tuple[0], "demo_clip.mov")
        self.assertEqual(uploaded_tuple[1], video_binary)
        self.assertEqual(uploaded_tuple[2], "video/quicktime")

        # Step e: /v5/pins creation with video_id and cover_image_url
        pin_call = mock_post.call_args_list[2]
        self.assertEqual(pin_call.args[0], "https://api.pinterest.com/v5/pins")
        pin_payload = pin_call.kwargs["json"]
        self.assertEqual(pin_payload["board_id"], "board_100")
        self.assertEqual(pin_payload["link"], AFFILIATE_URL)
        self.assertEqual(
            pin_payload["media_source"],
            {
                "source_type": "video_id",
                "media_id": "media_555",
                "cover_image_url": PRODUCT_IMAGE_URL,
            },
        )

    @patch("publishing.adapters.pinterest_adapter.time.sleep")
    @patch("publishing.adapters.pinterest_adapter.requests.post")
    @patch("publishing.adapters.pinterest_adapter.requests.get")
    def test_pinterest_polling_failed_aborts_before_pin_creation(self, mock_get, mock_post, _mock_sleep):
        mock_get.side_effect = [
            _mock_response(200, content=b"mp4bytes"),
            _mock_response(200, {"media_id": "media_bad", "status": "failed"}),
        ]
        mock_post.side_effect = [
            _mock_response(
                201,
                {
                    "media_id": "media_bad",
                    "upload_url": "https://s3.example.com/upload",
                    "upload_parameters": {"key": "k"},
                },
            ),
            _mock_response(204),
        ]

        res = self.adapter.publish(
            content={
                "title": "Mug",
                "description": "Desc",
                "video_url": R2_MP4_URL,
                "image_url": PRODUCT_IMAGE_URL,
            },
            account_config=self.account_cfg,
            dry_run=False,
        )

        self.assertEqual(res["status"], STATUS_FAILED)
        self.assertEqual(mock_post.call_count, 2)  # Registered + S3 upload, /v5/pins NEVER called

    @patch("publishing.adapters.pinterest_adapter.time.sleep")
    @patch("publishing.adapters.pinterest_adapter.requests.post")
    @patch("publishing.adapters.pinterest_adapter.requests.get")
    def test_pinterest_polling_timeout_aborts_before_pin_creation(self, mock_get, mock_post, _mock_sleep):
        mock_get.side_effect = [
            _mock_response(200, content=b"mp4bytes"),
            _mock_response(200, {"media_id": "media_slow", "status": "processing"}),
            _mock_response(200, {"media_id": "media_slow", "status": "processing"}),
        ]
        mock_post.side_effect = [
            _mock_response(
                201,
                {
                    "media_id": "media_slow",
                    "upload_url": "https://s3.example.com/upload",
                    "upload_parameters": {"key": "k"},
                },
            ),
            _mock_response(204),
        ]

        cfg = dict(self.account_cfg, video_poll_max_attempts=2)
        res = self.adapter.publish(
            content={
                "title": "Mug",
                "description": "Desc",
                "video_url": R2_MP4_URL,
                "image_url": PRODUCT_IMAGE_URL,
            },
            account_config=cfg,
            dry_run=False,
        )

        self.assertEqual(res["status"], STATUS_FAILED)
        self.assertEqual(mock_post.call_count, 2)
        self.assertIn("did not reach 'succeeded'", res["errors"][0])

    @patch("publishing.adapters.pinterest_adapter.requests.post")
    @patch("publishing.adapters.pinterest_adapter.requests.get")
    def test_pinterest_missing_cover_image_fails_fast_before_network(self, mock_get, mock_post):
        res = self.adapter.publish(
            content={"title": "Mug", "description": "Desc", "video_url": R2_MP4_URL, "image_url": ""},
            account_config=self.account_cfg,
            dry_run=False,
        )
        self.assertEqual(res["status"], STATUS_FAILED)
        mock_get.assert_not_called()
        mock_post.assert_not_called()

    @patch("publishing.adapters.pinterest_adapter.requests.post")
    def test_pinterest_existing_image_pin_behavior_unchanged(self, mock_post):
        mock_post.return_value = _mock_response(201, {"id": "pin_img_123"})

        res = self.adapter.publish(
            content={
                "title": "Static Pin",
                "description": f"Desc #ad {AFFILIATE_URL}",
                "image_url": PRODUCT_IMAGE_URL,
                "link": AFFILIATE_URL,
            },
            account_config=self.account_cfg,
            dry_run=False,
        )
        self.assertEqual(res["status"], STATUS_PUBLISHED)
        self.assertEqual(
            mock_post.call_args.kwargs["json"]["media_source"],
            {"source_type": "image_url", "url": PRODUCT_IMAGE_URL},
        )


class TestOrchestratorVideoRoutingAndMultiAccount(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = Path(".test-tmp") / "video_phase2"
        if self.tmp_dir.exists():
            shutil.rmtree(self.tmp_dir)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        if self.tmp_dir.exists():
            shutil.rmtree(self.tmp_dir)

    def test_dry_run_routes_video_and_preserves_image_only_compatibility(self):
        publisher = SocialPublisher(dry_run=True)

        video_pkg = _build_ready_package(with_video=True)
        video_report = publisher.publish_package(video_pkg, dry_run=True)
        self.assertEqual(video_report["status"], STATUS_WOULD_PUBLISH)
        for key in ("facebook", "pinterest", "pinterest_lifemodehq", "threads"):
            self.assertEqual(video_report["results"][key]["status"], STATUS_WOULD_PUBLISH)
            self.assertEqual(video_report["results"][key]["media_type"], "VIDEO")
            self.assertEqual(video_report["results"][key]["video_url"], R2_MP4_URL)
        self.assertEqual(video_report["results"]["pinterest"]["cover_image_url"], PRODUCT_IMAGE_URL)
        self.assertEqual(video_report["results"]["pinterest_lifemodehq"]["cover_image_url"], PRODUCT_IMAGE_URL)

        image_pkg = _build_ready_package(with_video=False)
        image_report = publisher.publish_package(image_pkg, dry_run=True)
        self.assertEqual(image_report["status"], STATUS_WOULD_PUBLISH)
        for key in ("facebook", "pinterest", "pinterest_lifemodehq", "threads"):
            self.assertEqual(image_report["results"][key]["status"], STATUS_WOULD_PUBLISH)
            self.assertNotIn("video_url", image_report["results"][key])

    @patch("content.pinterest_creative.has_creative_context", return_value=True)
    @patch("content.pinterest_creative.get_selected_creative", return_value=None)
    def test_video_package_bypasses_unselected_static_flow_creative_gate(self, _mock_sel, _mock_has):
        publisher = SocialPublisher(dry_run=True)
        video_pkg = _build_ready_package(with_video=True)
        report = publisher.publish_package(video_pkg, dry_run=True)
        self.assertEqual(report["status"], STATUS_WOULD_PUBLISH)

        # Whereas an image-only package with unselected Flow creative still enforces selection
        image_pkg = _build_ready_package(with_video=False)
        img_report = publisher.publish_package(image_pkg, dry_run=True)
        self.assertEqual(img_report["status"], STATUS_FAILED)
        self.assertIn("No Pinterest creative selected", img_report["results"]["pinterest"]["errors"][0])

    @patch("time.sleep")
    @patch("requests.get")
    @patch("requests.post")
    def test_live_orchestrator_publishes_video_to_all_four_destinations_including_both_pinterest_accounts(
        self,
        mock_post,
        mock_get,
        _mock_sleep,
    ):
        pin_auth_tokens_seen = []

        def fake_post(url, **kwargs):
            if "graph-video.facebook.com" in url:
                return _mock_response(200, {"id": "fb_vid_100"})
            if url == "https://api.pinterest.com/v5/media":
                auth = kwargs["headers"]["Authorization"]
                pin_auth_tokens_seen.append(auth)
                if "pin_daily_token" in auth:
                    return _mock_response(
                        201,
                        {
                            "media_id": "media_daily_1",
                            "upload_url": "https://s3.example.com/daily",
                            "upload_parameters": {"key": "daily_k"},
                        },
                    )
                return _mock_response(
                    201,
                    {
                        "media_id": "media_life_2",
                        "upload_url": "https://s3.example.com/life",
                        "upload_parameters": {"key": "life_k"},
                    },
                )
            if url.startswith("https://s3.example.com/"):
                return _mock_response(204)
            if url == "https://api.pinterest.com/v5/pins":
                auth = kwargs["headers"]["Authorization"]
                if "pin_daily_token" in auth:
                    return _mock_response(201, {"id": "pin_daily_901"})
                return _mock_response(201, {"id": "pin_life_902"})
            if url == "https://graph.threads.net/v1.0/11223344/threads":
                return _mock_response(200, {"id": "th_vid_cont_1"})
            if url == "https://graph.threads.net/v1.0/11223344/threads_publish":
                return _mock_response(200, {"id": "th_vid_post_1"})
            raise AssertionError(f"Unexpected POST URL: {url}")

        def fake_get(url, **kwargs):
            if url == R2_MP4_URL:
                return _mock_response(200, content=b"mp4_bytes")
            if "graph.facebook.com/v27.0/fb_vid_100" in url:
                return _mock_response(
                    200, {"id": "fb_vid_100", "permalink_url": "/LifeModeHQ/videos/fb_vid_100/"}
                )
            if url == "https://api.pinterest.com/v5/media/media_daily_1":
                return _mock_response(200, {"media_id": "media_daily_1", "status": "succeeded"})
            if url == "https://api.pinterest.com/v5/media/media_life_2":
                return _mock_response(200, {"media_id": "media_life_2", "status": "succeeded"})
            if "graph.threads.net/v1.0/th_vid_cont_1" in url:
                return _mock_response(200, {"status": "FINISHED"})
            if "graph.threads.net/v1.0/11223344/threads" in url:
                return _mock_response(
                    200,
                    {
                        "data": [
                            {
                                "id": "th_vid_post_1",
                                "media_type": "VIDEO",
                                "permalink": "https://www.threads.com/@lifemodehq/post/th_vid_post_1",
                            }
                        ]
                    },
                )
            raise AssertionError(f"Unexpected GET URL: {url}")

        mock_post.side_effect = fake_post
        mock_get.side_effect = fake_get

        publisher = SocialPublisher(config=_live_config(), dry_run=False)
        pkg = _build_ready_package(with_video=True)
        report = publisher.publish_package(pkg, dry_run=False)

        self.assertEqual(report["status"], STATUS_PUBLISHED)
        res = report["results"]

        self.assertEqual(res["facebook"]["status"], STATUS_PUBLISHED)
        self.assertEqual(res["facebook"]["video_id"], "fb_vid_100")

        self.assertEqual(res["pinterest"]["status"], STATUS_PUBLISHED)
        self.assertEqual(res["pinterest"]["account"], "dailyideasforyou")
        self.assertEqual(res["pinterest"]["board"], "Shopping ideas")
        self.assertEqual(res["pinterest"]["board_id"], "board_daily_1")
        self.assertEqual(res["pinterest"]["media_id"], "media_daily_1")
        self.assertEqual(res["pinterest"]["pin_id"], "pin_daily_901")
        self.assertEqual(res["pinterest"]["cover_image_url"], PRODUCT_IMAGE_URL)

        self.assertEqual(res["pinterest_lifemodehq"]["status"], STATUS_PUBLISHED)
        self.assertEqual(res["pinterest_lifemodehq"]["account"], "LifeModeHQ")
        self.assertEqual(res["pinterest_lifemodehq"]["board"], "Home & Everyday Living")
        self.assertEqual(res["pinterest_lifemodehq"]["board_id"], "board_life_2")
        self.assertEqual(res["pinterest_lifemodehq"]["media_id"], "media_life_2")
        self.assertEqual(res["pinterest_lifemodehq"]["pin_id"], "pin_life_902")
        self.assertEqual(res["pinterest_lifemodehq"]["cover_image_url"], PRODUCT_IMAGE_URL)

        self.assertEqual(res["threads"]["status"], STATUS_PUBLISHED)
        self.assertEqual(res["threads"]["post_id"], "th_vid_post_1")
        self.assertEqual(res["threads"]["media_type"], "VIDEO")

        self.assertEqual(pin_auth_tokens_seen, ["Bearer pin_daily_token", "Bearer pin_lifemode_token"])

    def test_independent_platform_failure_isolation_and_queue_partial_publication(self):
        publisher = SocialPublisher(config=_live_config(), dry_run=False)

        # Simulate Facebook raising an unexpected runtime error while Pinterest & Threads succeed
        publisher.adapters["facebook"].publish = MagicMock(side_effect=RuntimeError("Simulated FB crash"))
        publisher.pinterest_adapters["dailyideasforyou"].publish = MagicMock(
            return_value={
                "platform": "pinterest",
                "account": "dailyideasforyou",
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_PUBLISHED,
                "pin_id": "pin_ok_1",
                "pin_url": "https://www.pinterest.com/pin/pin_ok_1/",
                "media_type": "VIDEO",
            }
        )
        publisher.pinterest_adapters["lifemodehq"].publish = MagicMock(
            return_value={
                "platform": "pinterest",
                "account": "LifeModeHQ",
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": ["Pinterest LifeModeHQ token expired"],
            }
        )
        publisher.adapters["threads"].publish = MagicMock(
            return_value={
                "platform": "threads",
                "account": "lifemodehq",
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_PUBLISHED,
                "post_id": "th_ok_1",
                "permalink": "https://www.threads.com/@lifemodehq/post/th_ok_1",
                "media_type": "VIDEO",
            }
        )

        pkg_dict = _build_ready_package(with_video=True)
        pkg_file = self.tmp_dir / "package_B08J5F3G18_video.json"
        pkg_file.write_text(json.dumps(pkg_dict), encoding="utf-8")

        queue_file = self.tmp_dir / "publishing_queue.json"
        log_file = self.tmp_dir / "publisher.log"
        queue = PublishingQueue.from_dict(
            {
                "queue_id": "q_video_test",
                "created_at": "2026-11-05T14:00:00+01:00",
                "start_date": "2026-11-05",
                "items": [
                    {
                        "queue_item_id": "item_vid_1",
                        "original_url": AFFILIATE_URL,
                        "asin": "B08J5F3G18",
                        "product_name": "Insulated Stainless Steel Travel Mug",
                        "scheduled_date": "2026-11-05",
                        "daily_slot": 1,
                        "scheduled_at": "2026-11-05T15:00:00+01:00",
                        "package_reference": str(pkg_file),
                        "status": "READY",
                        "validation_status": "VALID",
                        "media_type": "video",
                        "video_url": R2_MP4_URL,
                    }
                ],
            }
        )
        queue.save(queue_file)

        summary = QueuePublisher.publish_due_items(
            queue_file=queue_file,
            now_override="2026-11-05T15:05:00+01:00",
            dry_run=False,
            publisher=publisher,
            log_file=log_file,
        )

        self.assertEqual(summary["items_processed"][0]["status"], STATUS_PARTIALLY_PUBLISHED)
        reloaded = PublishingQueue.load(queue_file)
        saved_item = reloaded.items[0]
        self.assertEqual(saved_item.status, STATUS_PARTIALLY_PUBLISHED)
        self.assertEqual(saved_item.media_type, "video")
        self.assertEqual(saved_item.video_url, R2_MP4_URL)
        self.assertEqual(saved_item.publishing_results["facebook"]["status"], STATUS_FAILED)
        self.assertEqual(saved_item.publishing_results["pinterest"]["status"], STATUS_PUBLISHED)
        self.assertEqual(saved_item.publishing_results["pinterest_lifemodehq"]["status"], STATUS_FAILED)
        self.assertEqual(saved_item.publishing_results["threads"]["status"], STATUS_PUBLISHED)

        # Retry skips already-published destinations (SKIP_DUPLICATE)
        retry_report = publisher.publish_package(
            pkg_dict,
            dry_run=False,
            prior_results=saved_item.publishing_results,
        )
        self.assertEqual(retry_report["results"]["pinterest"]["action"], "SKIP_DUPLICATE")
        self.assertEqual(retry_report["results"]["threads"]["action"], "SKIP_DUPLICATE")
        self.assertEqual(publisher.pinterest_adapters["dailyideasforyou"].publish.call_count, 1)
        self.assertEqual(publisher.adapters["threads"].publish.call_count, 1)

    def test_invalid_video_url_in_package_falls_back_cleanly_to_image_path(self):
        publisher = SocialPublisher(dry_run=True)
        pkg = _build_ready_package(with_video=False)
        pkg["video_url"] = "file:///local/not_uploaded.mp4"
        pkg["platforms"]["threads"]["video_url"] = "file:///local/not_uploaded.mp4"

        report = publisher.publish_package(pkg, dry_run=True)
        self.assertEqual(report["status"], STATUS_WOULD_PUBLISH)
        for key in ("facebook", "pinterest", "pinterest_lifemodehq", "threads"):
            self.assertNotIn("video_url", report["results"][key])

    def test_publish_pinterest_account_single_helper_supports_video_pin(self):
        publisher = SocialPublisher(config=_live_config(), dry_run=True)
        video_pkg = _build_ready_package(with_video=True)
        res = publisher.publish_pinterest_account(video_pkg, "lifemodehq", dry_run=True)
        self.assertEqual(res["status"], STATUS_WOULD_PUBLISH)
        self.assertEqual(res["media_type"], "VIDEO")
        self.assertEqual(res["video_url"], R2_MP4_URL)
        self.assertEqual(res["cover_image_url"], PRODUCT_IMAGE_URL)


if __name__ == "__main__":
    unittest.main()
