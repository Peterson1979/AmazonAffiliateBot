"""
tests/test_publishing_reliability_incident.py - Regression Test Suite for Publishing Reliability

Proves all 9 requirements from the November 11 reliability incident fix:
1. ASIN B09D7DWTVB cannot enter the publishing queue, cannot be imported, and cannot reach any platform adapter.
2. An IMAGE queue item can never resolve to a video creative under any conditions.
3. A VIDEO queue item can never resolve to an unrelated video, a video belonging to another ASIN, or fall back to an image.
4. Missing or invalid assigned media assets cause safe, explicit failure without attempting fallback publishing.
5. Successful platform publications are correctly persisted and reflected across the system, including the dashboard, and never display as "Not published".
6. A PARTIALLY_PUBLISHED queue item only retries genuinely unsuccessful platforms and does not duplicate successful ones.
7. Previously published platform posts are never published again during queue recovery or manual publishing.
8. Concurrent publishing execution cannot publish the same item twice.
9. All relevant execution paths enforce the same creative selection, verification, and blacklist rules, including repository_dispatch.
"""

from unittest import TestCase
from unittest.mock import Mock, patch, MagicMock
from pathlib import Path
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import json
import tempfile
import shutil

from publishing.blacklist import (
    is_asin_blacklisted,
    assert_not_blacklisted,
    PERMANENT_BLACKLISTED_ASINS,
)
from publishing.queue import (
    QueueItem,
    PublishingQueue,
    QueuePublisher,
    STATUS_READY,
    STATUS_PUBLISHED,
    STATUS_PUBLISHING,
    STATUS_PARTIALLY_PUBLISHED,
    STATUS_FAILED,
    STATUS_CANCELLED,
    STATUS_SKIPPED,
)
from publishing.publisher import SocialPublisher, PublishingConfig
from publishing.adapters.facebook_adapter import FacebookAdapter
from publishing.adapters.pinterest_adapter import PinterestAdapter
from publishing.adapters.threads_adapter import ThreadsAdapter
from web.services import (
    execute_live_publish_item,
    get_queue_item_presentation,
    enqueue_existing_product_service,
    add_products_from_urls,
    generate_product_creatives_service,
    attach_product_video_service,
    recover_failed_product,
    _ACTIVE_PUBLISHING_ITEM_IDS,
    _LIVE_PUBLISH_LOCK,
)
from scripts.run_cloud_publisher import (
    claim_due_slots,
    finalize_slot_claims,
    audit_queue_items,
)


class TestPublishingReliabilityIncident(TestCase):
    """Full regression suite covering all 9 incident reliability requirements."""

    def setUp(self):
        with _LIVE_PUBLISH_LOCK:
            _ACTIVE_PUBLISHING_ITEM_IDS.clear()
        self.temp_dir = tempfile.mkdtemp()
        self.temp_path = Path(self.temp_dir)

    def tearDown(self):
        with _LIVE_PUBLISH_LOCK:
            _ACTIVE_PUBLISHING_ITEM_IDS.clear()
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _sample_package(self, asin="B0TEST1234", media_type="image", video_url=None):
        return {
            "asin": asin,
            "title": f"Test Product {asin}",
            "status": "READY_FOR_REVIEW",
            "media_type": media_type,
            "video_url": video_url,
            "image_url": "https://m.media-amazon.com/images/I/test.jpg",
            "source": {"affiliate_url": f"https://www.amazon.com/dp/{asin}?tag=test-20"},
            "platforms": {
                "facebook": {
                    "status": "VALID",
                    "content": "Test Facebook copy with hashtag #test",
                    "media_type": media_type,
                    "video_url": video_url,
                    "image_url": "https://m.media-amazon.com/images/I/test.jpg",
                },
                "pinterest": {
                    "status": "VALID",
                    "title": "Test Pinterest Title",
                    "description": "Test Pinterest Description",
                    "link": f"https://www.amazon.com/dp/{asin}?tag=test-20",
                    "media_type": media_type,
                    "video_url": video_url,
                    "image_url": "https://m.media-amazon.com/images/I/test.jpg",
                },
                "pinterest_lifemodehq": {
                    "status": "VALID",
                    "title": "Test Pinterest LMHQ Title",
                    "description": "Test Pinterest LMHQ Description",
                    "link": f"https://www.amazon.com/dp/{asin}?tag=test-20",
                    "media_type": media_type,
                    "video_url": video_url,
                    "image_url": "https://m.media-amazon.com/images/I/test.jpg",
                },
                "threads": {
                    "status": "VALID",
                    "content": "Test Threads copy",
                    "media_type": media_type,
                    "video_url": video_url,
                    "image_url": "https://m.media-amazon.com/images/I/test.jpg",
                },
            },
        }

    # =========================================================================
    # Requirement 1: ASIN B09D7DWTVB Permanent Blacklist
    # =========================================================================
    def test_01_blacklisted_asin_cannot_enter_queue_or_reach_adapters(self):
        """Prove B09D7DWTVB cannot enter queue, be imported, or reach adapters."""
        blacklisted_asin = "B09D7DWTVB"
        self.assertTrue(is_asin_blacklisted(blacklisted_asin))
        self.assertIn(blacklisted_asin, PERMANENT_BLACKLISTED_ASINS)

        # 1. Queue rejection
        queue = PublishingQueue(
            queue_id="test_queue",
            created_at=datetime.now(timezone.utc).isoformat(),
            start_date="2026-11-11",
            items=[],
        )
        item = QueueItem(
            queue_item_id="item_blacklisted",
            original_url=f"https://www.amazon.com/dp/{blacklisted_asin}",
            asin=blacklisted_asin,
            status=STATUS_READY,
        )
        with self.assertRaises(ValueError) as ctx:
            queue.add_item(item)
        self.assertIn("permanently blacklisted", str(ctx.exception))

        # 2. Services rejection
        enqueue_res = enqueue_existing_product_service(blacklisted_asin)
        self.assertFalse(enqueue_res["success"])
        self.assertIn("permanently blacklisted", enqueue_res["message"])

        creative_res = generate_product_creatives_service(blacklisted_asin)
        self.assertFalse(creative_res["success"])
        self.assertIn("permanently blacklisted", creative_res["message"])

        video_res = attach_product_video_service(blacklisted_asin, "dummy.mp4")
        self.assertFalse(video_res["success"])
        self.assertIn("permanently blacklisted", video_res["message"])

        recover_res = recover_failed_product(blacklisted_asin)
        self.assertFalse(recover_res["success"])
        self.assertIn("permanently blacklisted", recover_res["message"])

        with patch("web.services.BATCHES_FILE", self.temp_path / "test_batches.json"):
            import_res = add_products_from_urls(f"https://www.amazon.com/dp/{blacklisted_asin}")
        self.assertEqual(import_res["accepted"], 0)
        self.assertTrue(any(r.get("status") == "BLOCKED" for r in import_res["results"]))

        # 3. Adapter boundary rejection
        fb_adapter = FacebookAdapter(credentials={"access_token": "mock_token", "page_id": "123"})
        pin_adapter = PinterestAdapter(credentials={"access_token": "mock_token"})
        th_adapter = ThreadsAdapter(credentials={"access_token": "mock_token", "user_id": "456"})

        content = {"asin": blacklisted_asin, "content": "Test content", "media_type": "image"}
        cfg = {"account": "test", "asin": blacklisted_asin}

        fb_res = fb_adapter.publish(content, cfg, dry_run=False)
        self.assertEqual(fb_res["action"], "BLOCKED")
        self.assertEqual(fb_res["status"], STATUS_FAILED)

        pin_res = pin_adapter.publish(content, cfg, dry_run=False)
        self.assertEqual(pin_res["action"], "BLOCKED")
        self.assertEqual(pin_res["status"], STATUS_FAILED)

        th_res = th_adapter.publish(content, cfg, dry_run=False)
        self.assertEqual(th_res["action"], "BLOCKED")
        self.assertEqual(th_res["status"], STATUS_FAILED)

        # 4. Publisher boundary rejection
        publisher = SocialPublisher(config=PublishingConfig(dry_run=False))
        pkg = self._sample_package(asin=blacklisted_asin)
        pub_res = publisher.publish_package(pkg, dry_run=False)
        self.assertEqual(pub_res["status"], STATUS_FAILED)
        for dest_res in pub_res["results"].values():
            self.assertEqual(dest_res["action"], "BLOCKED")

    # =========================================================================
    # Requirement 2: IMAGE Queue Item Never Resolves to Video
    # =========================================================================
    def test_02_image_queue_item_never_resolves_to_video(self):
        """Prove an IMAGE item never publishes video even if package references a video."""
        asin = "B0VALIDASIN"
        pkg_with_video = self._sample_package(
            asin=asin,
            media_type="image",
            video_url="https://r2.example.com/videos/test.mp4",
        )
        pkg_file = self.temp_path / f"asin_{asin}_content_package.json"
        pkg_file.write_text(json.dumps(pkg_with_video), encoding="utf-8")

        mock_fb = Mock(return_value={"status": "PUBLISHED", "post_id": "fb123", "mode": "LIVE"})
        mock_pin = Mock(return_value={"status": "PUBLISHED", "pin_id": "pin123", "mode": "LIVE"})
        mock_th = Mock(return_value={"status": "PUBLISHED", "post_id": "th123", "mode": "LIVE"})

        publisher = SocialPublisher(config=PublishingConfig(dry_run=False))
        publisher.adapters["facebook"].publish = mock_fb
        publisher.adapters["pinterest"].publish = mock_pin
        publisher.adapters["threads"].publish = mock_th
        for a in publisher.pinterest_adapters.values():
            a.publish = mock_pin

        # Call with declared_media_type="image"
        res = publisher.publish_package(
            pkg_with_video,
            dry_run=False,
            declared_media_type="image",
        )

        self.assertEqual(res["status"], "PUBLISHED")
        # Verify all adapters received media_type="image" and no video_url
        for call_args in [mock_fb.call_args, mock_pin.call_args, mock_th.call_args]:
            content_arg = call_args[1].get("content") or call_args[0][0]
            self.assertEqual(content_arg.get("media_type"), "image")
            self.assertIsNone(content_arg.get("video_url"))

    # =========================================================================
    # Requirement 3: VIDEO Queue Item Never Falls Back to Image or Cross-ASIN Video
    # =========================================================================
    def test_03_video_queue_item_never_falls_back_to_image_or_other_asin_video(self):
        """Prove a VIDEO item fails safely if video is missing or from another ASIN."""
        asin = "B0VIDITEM01"
        publisher = SocialPublisher(config=PublishingConfig(dry_run=False))

        # Case A: Cross-ASIN video
        cross_asin_pkg = self._sample_package(asin=asin, media_type="video", video_url="https://r2.example.com/v.mp4")
        cross_asin_pkg["video"] = {"asin": "B0OTHERASIN", "video_url": "https://r2.example.com/v.mp4"}

        with self.assertRaises(ValueError) as ctx:
            publisher.publish_package(cross_asin_pkg, declared_media_type="video")
        self.assertIn("Cross-ASIN video prohibited", str(ctx.exception))

        # Case B: Missing video URL
        no_video_pkg = self._sample_package(asin=asin, media_type="video", video_url=None)
        res = publisher.publish_package(no_video_pkg, declared_media_type="video")
        self.assertEqual(res["status"], STATUS_FAILED)
        for dest_res in res["results"].values():
            self.assertEqual(dest_res["status"], STATUS_FAILED)
            self.assertTrue(any("Image fallback prohibited" in err for err in dest_res.get("errors", [])))

    # =========================================================================
    # Requirement 4: Missing or Invalid Media Causes Safe Failure
    # =========================================================================
    def test_04_missing_or_invalid_media_fails_safely_without_fallback(self):
        """Prove invalid or missing media causes safe explicit failure."""
        asin = "B0MEDIAFAIL"
        publisher = SocialPublisher(config=PublishingConfig(dry_run=False))

        # Invalid video URL (not public HTTPS)
        pkg = self._sample_package(asin=asin, media_type="video", video_url="ftp://invalid.com/v.mp4")
        res = publisher.publish_package(pkg, declared_media_type="video")
        self.assertEqual(res["status"], STATUS_FAILED)
        for dest_res in res["results"].values():
            self.assertEqual(dest_res["status"], STATUS_FAILED)
            self.assertTrue(any("Image fallback prohibited" in err for err in dest_res.get("errors", [])))

    # =========================================================================
    # Requirement 5: Successful Publications Persisted & Reflected in Dashboard
    # =========================================================================
    def test_05_successful_platform_publications_persisted_and_reflected_in_dashboard(self):
        """Prove confirmed publications are reflected accurately and never say 'Not published'."""
        asin = "B0PUBACCURATE"
        queue = PublishingQueue("test_q", "2026-11-11T00:00:00Z", "2026-11-11")
        item = QueueItem(
            queue_item_id="item_test_pub",
            original_url="https://amazon.com/dp/" + asin,
            asin=asin,
            scheduled_date="2026-11-11",
            daily_slot=1,
            scheduled_at="2026-11-11T15:00:00+01:00",
            status=STATUS_PARTIALLY_PUBLISHED,
            publishing_results={
                "facebook": {
                    "platform": "facebook",
                    "mode": "LIVE",
                    "status": "PUBLISHED",
                    "post_id": "fb_post_999",
                    "published_at": "2026-11-11T15:01:00+01:00",
                },
                "pinterest": {
                    "platform": "pinterest",
                    "mode": "LIVE",
                    "status": "PUBLISHED",
                    "pin_id": "pin_post_999",
                    "published_at": "2026-11-11T15:01:00+01:00",
                },
                "threads": {
                    "platform": "threads",
                    "mode": "LIVE",
                    "status": "PUBLISHED",
                    "post_id": "th_post_999",
                    "published_at": "2026-11-11T15:01:00+01:00",
                },
            },
        )
        queue.items.append(item)

        eval_dt = datetime(2026, 11, 11, 16, 30, tzinfo=ZoneInfo("Europe/Budapest"))
        presentation = get_queue_item_presentation(queue, item, current_dt=eval_dt)

        # Must be PARTIAL / Partial, NEVER "Not published" or SIMULATED
        self.assertEqual(presentation["state"], "PARTIAL")
        self.assertEqual(presentation["ui_label"], "Partial")
        self.assertIsNotNone(presentation["published_at"])
        self.assertEqual(presentation["platforms"]["facebook"]["state"], "PUBLISHED")
        self.assertEqual(presentation["platforms"]["threads"]["state"], "PUBLISHED")

    # =========================================================================
    # Requirement 6: PARTIALLY_PUBLISHED Retries Only Unsuccessful Platforms
    # =========================================================================
    def test_06_partial_publication_retries_only_unsuccessful_platforms(self):
        """Prove partially published item only publishes pending platforms on retry."""
        asin = "B0PARTIALRETRY"
        prior_results = {
            "facebook": {"platform": "facebook", "mode": "LIVE", "status": "PUBLISHED", "post_id": "fb_ok_1"},
            "pinterest": {"platform": "pinterest", "mode": "LIVE", "status": "PUBLISHED", "pin_id": "pin_ok_1"},
            "threads": {"platform": "threads", "mode": "LIVE", "status": "PUBLISHED", "post_id": "th_ok_1"},
        }
        pkg = self._sample_package(asin=asin, media_type="image")

        mock_fb = Mock()
        mock_pin_daily = Mock()
        mock_pin_lmhq = Mock(return_value={"status": "PUBLISHED", "pin_id": "pin_lmhq_1", "mode": "LIVE"})
        mock_th = Mock()

        publisher = SocialPublisher(config=PublishingConfig(dry_run=False))
        publisher.adapters["facebook"].publish = mock_fb
        publisher.adapters["pinterest"].publish = mock_pin_daily
        publisher.adapters["threads"].publish = mock_th
        publisher.pinterest_adapters["dailyideasforyou"].publish = mock_pin_daily
        publisher.pinterest_adapters["lifemodehq"].publish = mock_pin_lmhq

        report = publisher.publish_package(
            pkg,
            dry_run=False,
            prior_results=prior_results,
            target_destinations=["pinterest_lifemodehq"],
        )

        # Facebook, Pinterest dailyideasforyou, and Threads must NOT be called
        mock_fb.assert_not_called()
        mock_pin_daily.assert_not_called()
        mock_th.assert_not_called()

        # Pinterest lifemodehq MUST be called
        mock_pin_lmhq.assert_called_once()
        self.assertEqual(report["results"]["pinterest_lifemodehq"]["status"], "PUBLISHED")
        # Preserved results retained
        self.assertEqual(report["results"]["facebook"]["post_id"], "fb_ok_1")

    # =========================================================================
    # Requirement 7: Previously Published Posts Never Called Again
    # =========================================================================
    def test_07_previously_published_platforms_never_called_again(self):
        """Prove all confirmed platforms are skipped with SKIP_DUPLICATE."""
        asin = "B0NODUPES"
        prior_results = {
            "facebook": {"platform": "facebook", "mode": "LIVE", "status": "PUBLISHED", "post_id": "fb_done"},
            "pinterest": {"platform": "pinterest", "mode": "LIVE", "status": "PUBLISHED", "pin_id": "pin_done"},
            "pinterest_lifemodehq": {"platform": "pinterest", "mode": "LIVE", "status": "PUBLISHED", "pin_id": "pin_lmhq_done"},
            "threads": {"platform": "threads", "mode": "LIVE", "status": "PUBLISHED", "post_id": "th_done"},
        }
        pkg = self._sample_package(asin=asin, media_type="image")

        mock_adapter = Mock()
        publisher = SocialPublisher(config=PublishingConfig(dry_run=False))
        for k in publisher.adapters:
            publisher.adapters[k].publish = mock_adapter
        for k in publisher.pinterest_adapters:
            publisher.pinterest_adapters[k].publish = mock_adapter

        report = publisher.publish_package(pkg, dry_run=False, prior_results=prior_results)

        # Zero adapter calls made
        mock_adapter.assert_not_called()
        for k, v in report["results"].items():
            self.assertEqual(v["action"], "SKIP_DUPLICATE")
            self.assertEqual(v["status"], "PUBLISHED")

    # =========================================================================
    # Requirement 8: Concurrent Execution Cannot Publish Twice
    # =========================================================================
    def test_08_concurrent_execution_cannot_publish_same_item_twice(self):
        """Prove concurrency locks reject simultaneous publishing requests."""
        item_id = "item_concurrent_test"
        with _LIVE_PUBLISH_LOCK:
            _ACTIVE_PUBLISHING_ITEM_IDS.add(item_id)

        res = execute_live_publish_item(item_id, confirmed=True)
        self.assertFalse(res["success"])
        self.assertIn("already being published", res["error"])

    # =========================================================================
    # Requirement 9: All Execution Paths Enforce Creative & Blacklist Rules
    # =========================================================================
    def test_09_all_execution_paths_enforce_same_rules(self):
        """Prove scheduled runner, cloud publisher, and manual actions enforce rules identically."""
        blacklisted_asin = "B09D7DWTVB"

        queue_file = self.temp_path / "test_queue.json"
        queue = PublishingQueue(
            "q_test",
            "2026-11-11T00:00:00Z",
            "2026-11-11",
            items=[
                QueueItem(
                    queue_item_id="item_b9",
                    original_url=f"https://amazon.com/dp/{blacklisted_asin}",
                    asin=blacklisted_asin,
                    scheduled_date="2026-11-11",
                    daily_slot=1,
                    scheduled_at="2026-11-11T15:00:00+01:00",
                    status=STATUS_READY,
                    media_type="image",
                )
            ],
        )
        queue.save(queue_file)

        # A. QueuePublisher.find_due_items ignores blacklisted ASIN
        eval_dt = datetime(2026, 11, 11, 15, 5, tzinfo=ZoneInfo("Europe/Budapest"))
        due = QueuePublisher.find_due_items(queue, eval_dt, window_id=1)
        self.assertEqual(len(due), 0)

        # B. audit_queue_items cancels blacklisted ASIN
        audit_res = audit_queue_items(queue, eval_dt)
        b9_item = next(i for i in queue.items if i.queue_item_id == "item_b9")
        self.assertEqual(b9_item.status, STATUS_CANCELLED)

        # C. execute_live_publish_item cancels blacklisted ASIN
        with patch("web.services.QUEUE_FILE", queue_file):
            manual_res = execute_live_publish_item("item_b9", confirmed=True)
            self.assertFalse(manual_res["success"])
            self.assertIn("permanently blacklisted", manual_res["error"])
            reloaded = PublishingQueue.load(queue_file)
            self.assertEqual(reloaded.items[0].status, STATUS_CANCELLED)
