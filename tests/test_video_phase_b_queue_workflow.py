"""
Focused Phase B tests for connecting existing cached products to the publishing queue
and managing the complete dashboard publication workflow without re-importing URLs.
"""

from contextlib import ExitStack
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from amazon.scraper import Product
from content.package import ContentPackage, STATUS_FAILED_VALIDATION, STATUS_READY_FOR_REVIEW
from publishing.queue import (
    PublishingQueue,
    QueueItem,
    STATUS_FAILED,
    STATUS_PARTIALLY_PUBLISHED,
    STATUS_PUBLISHED,
    STATUS_READY,
    STATUS_SKIPPED,
)
from publishing.sync import merge_publishing_queues
from web import app as web_app_module
from web import services
from web.app import create_app


class TestVideoPhaseBQueueWorkflow(unittest.TestCase):
    ASIN_VIDEO = "B0PHASEBV1"
    AFF_URL_VIDEO = "https://www.amazon.com/dp/B0PHASEBV1?tag=lifemode-20&ref_=as_li_ss_tl"
    IMAGE_VIDEO = "https://m.media-amazon.com/images/I/71PhaseBVideoCover._AC_SL1500_.jpg"
    R2_VIDEO_URL = "https://pub-8fcd679c40fd4aaa851f6ee7cdd4d083.r2.dev/amazon-affiliate-bot/videos/B0PHASEBV1/demo.mp4"
    R2_VIDEO_KEY = "amazon-affiliate-bot/videos/B0PHASEBV1/demo.mp4"

    ASIN_IMAGE = "B0PHASEBI1"
    AFF_URL_IMAGE = "https://www.amazon.com/dp/B0PHASEBI1?tag=lifemode-20&ref_=as_li_ss_tl"
    IMAGE_STATIC = "https://m.media-amazon.com/images/I/71PhaseBStaticImg._AC_SL1500_.jpg"

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_video_phase_b_"))
        self.products_dir = self.temp_dir / "data" / "products"
        self.generated_dir = self.temp_dir / "data" / "generated"
        self.queue_dir = self.temp_dir / "data" / "queue"
        self.batches_dir = self.temp_dir / "data" / "batches"
        for directory in (self.products_dir, self.generated_dir, self.queue_dir, self.batches_dir):
            directory.mkdir(parents=True, exist_ok=True)

        self.queue_file = self.queue_dir / "publishing_queue.json"
        self.batches_file = self.batches_dir / "import_batches.json"

        # Seed cached product 1: unqueued product WITH attached R2 video
        self.product_video, self.copy_video, self.pkg_video_path = self._seed_cached_product(
            asin=self.ASIN_VIDEO,
            title="Cordless Portable Espresso Maker 20 Bar Travel Machine",
            affiliate_url=self.AFF_URL_VIDEO,
            image_url=self.IMAGE_VIDEO,
            attach_video=True,
        )

        # Seed cached product 2: unqueued product WITHOUT video (static image package)
        self.product_image, self.copy_image, self.pkg_image_path = self._seed_cached_product(
            asin=self.ASIN_IMAGE,
            title="Compact Digital Kitchen Scale Stainless Steel 11 lb",
            affiliate_url=self.AFF_URL_IMAGE,
            image_url=self.IMAGE_STATIC,
            attach_video=False,
        )

        # Start with an initialized empty publishing queue
        queue = PublishingQueue(
            queue_id="queue_phase_b",
            created_at="2026-11-05T10:00:00+01:00",
            start_date="2026-11-06",
            total_days=30,
            products_per_day=2,
            target_capacity=60,
            items=[],
        )
        queue.recalculate_counts()
        queue.save(self.queue_file)

        self.app = create_app({"TESTING": True})
        self.client = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _seed_cached_product(
        self,
        asin: str,
        title: str,
        affiliate_url: str,
        image_url: str,
        attach_video: bool = False,
    ):
        product = Product(
            title=title,
            asin=asin,
            price="$49.99",
            image_url=image_url,
            affiliate_url=affiliate_url,
            original_affiliate_url=affiliate_url,
            source_url=f"https://www.amazon.com/dp/{asin}",
            features=[
                "20 bar high-pressure extraction pump with 7500 mAh rechargeable USB-C battery",
                "Compatible with ground coffee and NS capsules for travel brewing",
                "304 stainless steel water chamber holds 80 ml",
            ],
        )
        (self.products_dir / f"asin_{asin}.json").write_text(
            json.dumps(product.to_dict(), indent=2),
            encoding="utf-8",
        )

        copy_bundle = {
            "facebook": (
                f"{title} features a 304 stainless steel chamber.\n\n"
                f"Link: {affiliate_url}\n\n"
                f"As an Amazon Associate I earn from qualifying purchases."
            ),
            "pinterest_title": title[:95],
            "pinterest_desc": (
                f"{title} with USB-C battery and stainless steel chamber. {affiliate_url}\n\n"
                f"As an Amazon Associate I earn from qualifying purchases."
            ),
            "threads": (
                f"{title} for travel use.\n\n"
                f"Link: {affiliate_url}\n\n"
                f"As an Amazon Associate I earn from qualifying purchases."
            ),
        }
        social_data = {
            "asin": asin,
            "affiliate_url": affiliate_url,
            "platforms": {
                "facebook": {"text": copy_bundle["facebook"]},
                "pinterest": {
                    "title": copy_bundle["pinterest_title"],
                    "description": copy_bundle["pinterest_desc"],
                },
                "threads": {"text": copy_bundle["threads"]},
            },
            "validation_metadata": {
                "facebook": {"status": "VALID", "retry_count": 0, "errors": []},
                "pinterest": {"status": "VALID", "retry_count": 0, "errors": []},
                "threads": {"status": "VALID", "retry_count": 0, "errors": []},
            },
        }
        pkg = ContentPackage.build_package(
            product=product,
            social_data=social_data,
            cache_file_path=f"data/products/asin_{asin}.json",
            model_name="test-model",
        )
        if attach_video:
            ContentPackage.attach_verified_video(
                pkg,
                video_url=self.R2_VIDEO_URL,
                object_key=self.R2_VIDEO_KEY,
                content_type="video/mp4",
                size_bytes=1048576,
                filename="demo.mp4",
            )
        pkg_file = ContentPackage.save_content_package(
            pkg,
            f"asin_{asin}",
            target_dir=self.generated_dir,
        )
        return product, copy_bundle, pkg_file

    def _enter_sandbox_patches(self, stack: ExitStack, historical_verified_asins=None):
        verified_set = {a.upper() for a in (historical_verified_asins or set())}

        def _fake_hist_report(asin: str, destination: str):
            if (asin or "").upper() in verified_set:
                return {"evidence_status": "platform_verified", "post_id": "hist_123"}
            return {"evidence_status": "no_evidence"}

        stack.enter_context(patch.object(services, "PROJECT_ROOT", self.temp_dir))
        stack.enter_context(patch.object(services, "PRODUCTS_DIR", self.products_dir))
        stack.enter_context(patch.object(services, "GENERATED_DIR", self.generated_dir))
        stack.enter_context(patch.object(services, "QUEUE_FILE", self.queue_file))
        stack.enter_context(patch.object(services, "BATCHES_FILE", self.batches_file))
        stack.enter_context(patch.object(web_app_module, "DEFAULT_QUEUE_FILE", self.queue_file))
        stack.enter_context(patch.object(services, "load_application_config", return_value={}))
        stack.enter_context(
            patch.object(services, "get_historical_destination_report", side_effect=_fake_hist_report)
        )

    def test_01_existing_product_selection_and_navigation(self):
        """Users can select an existing cached product on /add-product and navigate to its detail page without re-importing."""
        with ExitStack() as stack:
            self._enter_sandbox_patches(stack)

            resp = self.client.get("/add-product")
            self.assertEqual(resp.status_code, 200)
            html = resp.get_data(as_text=True)

            # Existing product selector lists both cached products and marks the video package
            self.assertIn("Open an Existing Product", html)
            self.assertIn(f'value="{self.ASIN_VIDEO}"', html)
            self.assertIn(f'value="{self.ASIN_IMAGE}"', html)
            self.assertIn("Video", html)

            # New-product single and batch URL import controls remain intact
            self.assertIn('name="url"', html)
            self.assertIn('name="video"', html)
            self.assertIn('name="urls"', html)

            # Selecting an existing product redirects straight to /products/<asin>
            nav_resp = self.client.get(f"/add-product?existing_asin={self.ASIN_VIDEO}")
            self.assertEqual(nav_resp.status_code, 302)
            self.assertTrue(nav_resp.headers["Location"].endswith(f"/products/{self.ASIN_VIDEO}"))

            # Unknown ASIN stays on /add-product with warning flash
            missing_resp = self.client.get("/add-product?existing_asin=B0UNKNOWN99", follow_redirects=True)
            self.assertEqual(missing_resp.status_code, 200)
            self.assertIn("was not found", missing_resp.get_data(as_text=True))

            # Products directory also links directly to both cached products
            dir_resp = self.client.get("/products")
            self.assertEqual(dir_resp.status_code, 200)
            dir_html = dir_resp.get_data(as_text=True)
            self.assertIn(f"/products/{self.ASIN_VIDEO}", dir_html)
            self.assertIn(f"/products/{self.ASIN_IMAGE}", dir_html)

    def test_02_enqueue_existing_product_with_attached_video(self):
        """Enqueueing an unqueued product with an attached R2 video preserves video_url, affiliate_url, and platform copy."""
        with ExitStack() as stack:
            self._enter_sandbox_patches(stack)

            pkg_before = json.loads(self.pkg_video_path.read_text(encoding="utf-8"))

            # Detail page before queueing shows Not Queued + Video Attached + Add to Publishing Queue action
            detail_before = self.client.get(f"/products/{self.ASIN_VIDEO}")
            self.assertEqual(detail_before.status_code, 200)
            html_before = detail_before.get_data(as_text=True)
            self.assertIn("Not Queued", html_before)
            self.assertIn("Video Attached", html_before)
            self.assertIn(f"/api/queue/{self.ASIN_VIDEO}/enqueue", html_before)
            self.assertIn("Add to Publishing Queue", html_before)

            # Enqueue via dashboard POST action
            enq_resp = self.client.post(
                f"/api/queue/{self.ASIN_VIDEO}/enqueue",
                follow_redirects=True,
            )
            self.assertEqual(enq_resp.status_code, 200)
            html_after = enq_resp.get_data(as_text=True)

            # Verify persisted QueueItem
            queue = PublishingQueue.load(self.queue_file)
            self.assertEqual(len(queue.items), 1)
            item = queue.items[0]
            self.assertEqual(item.asin, self.ASIN_VIDEO)
            self.assertEqual(item.original_url, self.AFF_URL_VIDEO)
            self.assertEqual(item.status, STATUS_READY)
            self.assertEqual(item.validation_status, STATUS_READY_FOR_REVIEW)
            self.assertEqual(item.media_type, "video")
            self.assertEqual(item.video_url, self.R2_VIDEO_URL)
            self.assertEqual(item.timezone, "Europe/Budapest")
            self.assertIn(item.daily_slot, (1, 2))
            self.assertTrue(bool(item.scheduled_date))
            self.assertTrue(bool(item.scheduled_at))
            self.assertTrue(item.queue_item_id.endswith(f"_{self.ASIN_VIDEO}"))

            # ContentPackage on disk is completely unchanged (affiliate URL, copy, cover image, R2 video intact)
            pkg_after = json.loads(self.pkg_video_path.read_text(encoding="utf-8"))
            self.assertEqual(pkg_before, pkg_after)
            self.assertEqual(pkg_after["source"]["affiliate_url"], self.AFF_URL_VIDEO)
            self.assertEqual(pkg_after["video_url"], self.R2_VIDEO_URL)
            self.assertEqual(pkg_after["platforms"]["facebook"]["content"], self.copy_video["facebook"])
            self.assertEqual(pkg_after["platforms"]["pinterest"]["title"], self.copy_video["pinterest_title"])
            self.assertEqual(pkg_after["platforms"]["pinterest"]["description"], self.copy_video["pinterest_desc"])
            self.assertEqual(pkg_after["platforms"]["threads"]["content"], self.copy_video["threads"])
            self.assertEqual(pkg_after["platforms"]["pinterest"]["image_url"], self.IMAGE_VIDEO)

            # Dashboard updates immediately with queue slot, Open Queue Entry link, and live publish form
            self.assertIn(item.queue_item_id, html_after)
            self.assertIn("Video Attached", html_after)
            self.assertIn(f"/queue?asin={self.ASIN_VIDEO}", html_after)
            self.assertIn(f"/queue/{item.queue_item_id}/publish", html_after)
            self.assertNotIn("Add to Publishing Queue", html_after)

    def test_03_enqueue_existing_product_without_video_static_image(self):
        """Enqueueing an unqueued product without a video creates a READY static-image QueueItem."""
        with ExitStack() as stack:
            self._enter_sandbox_patches(stack)

            detail_before = self.client.get(f"/products/{self.ASIN_IMAGE}")
            self.assertEqual(detail_before.status_code, 200)
            html_before = detail_before.get_data(as_text=True)
            self.assertIn("Not Queued", html_before)
            self.assertIn("Static Image", html_before)
            self.assertIn("Add to Publishing Queue", html_before)

            res = services.enqueue_existing_product_service(self.ASIN_IMAGE)
            self.assertTrue(res["success"])
            self.assertEqual(res["media_type"], "image")
            self.assertIsNone(res["video_url"])
            self.assertEqual(res["original_url"], self.AFF_URL_IMAGE)

            queue = PublishingQueue.load(self.queue_file)
            self.assertEqual(len(queue.items), 1)
            item = queue.items[0]
            self.assertEqual(item.asin, self.ASIN_IMAGE)
            self.assertEqual(item.original_url, self.AFF_URL_IMAGE)
            self.assertIsNone(item.media_type)
            self.assertIsNone(item.video_url)
            self.assertEqual(item.status, STATUS_READY)
            self.assertEqual(item.validation_status, STATUS_READY_FOR_REVIEW)

            detail_after = self.client.get(f"/products/{self.ASIN_IMAGE}")
            html_after = detail_after.get_data(as_text=True)
            self.assertIn("Static Image", html_after)
            self.assertIn(item.queue_item_id, html_after)
            self.assertIn(f"/queue/{item.queue_item_id}/publish", html_after)

    def test_04_duplicate_prevention_and_slot_allocation(self):
        """Sequential enqueues allocate distinct slots and block duplicate ASIN or affiliate URL entries."""
        with ExitStack() as stack:
            self._enter_sandbox_patches(stack)

            first = services.enqueue_existing_product_service(self.ASIN_VIDEO)
            second = services.enqueue_existing_product_service(self.ASIN_IMAGE)
            self.assertTrue(first["success"])
            self.assertTrue(second["success"])
            self.assertNotEqual(first["queue_item_id"], second["queue_item_id"])
            self.assertNotEqual(
                (first["scheduled_date"], first["daily_slot"]),
                (second["scheduled_date"], second["daily_slot"]),
            )

            # Duplicate enqueue via AJAX endpoint returns 400 and leaves queue unchanged
            dup_resp = self.client.post(
                f"/api/queue/{self.ASIN_VIDEO}/enqueue",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            self.assertEqual(dup_resp.status_code, 400)
            dup_json = dup_resp.get_json()
            self.assertFalse(dup_json["success"])
            self.assertIn("already in the publishing queue", dup_json["message"])

            # Another cached ASIN pointing to an already-queued affiliate_url is also blocked
            asin_clone = "B0CLONEURL1"
            self._seed_cached_product(
                asin=asin_clone,
                title="Duplicate Affiliate Link Product",
                affiliate_url=self.AFF_URL_VIDEO,
                image_url=self.IMAGE_STATIC,
                attach_video=False,
            )
            clone_res = services.enqueue_existing_product_service(asin_clone)
            self.assertFalse(clone_res["success"])

            queue = PublishingQueue.load(self.queue_file)
            self.assertEqual(len(queue.items), 2)

    def test_05_queue_asin_filter_and_existing_publish_confirmation_controls(self):
        """Product Detail links to /queue?asin=<ASIN> and reuses the existing two-factor live publish endpoint."""
        with ExitStack() as stack:
            self._enter_sandbox_patches(stack)

            enq_video = services.enqueue_existing_product_service(self.ASIN_VIDEO)
            services.enqueue_existing_product_service(self.ASIN_IMAGE)
            item_id = enq_video["queue_item_id"]

            # Filtered queue page shows only the requested ASIN
            q_resp = self.client.get(f"/queue?asin={self.ASIN_VIDEO}")
            self.assertEqual(q_resp.status_code, 200)
            q_html = q_resp.get_data(as_text=True)
            self.assertIn(f"Filtered to ASIN", q_html)
            self.assertIn(self.ASIN_VIDEO, q_html)
            self.assertNotIn(self.ASIN_IMAGE, q_html)

            with patch.object(web_app_module, "execute_live_publish_item", wraps=services.execute_live_publish_item) as spy_pub:
                # 1. Checkbox alone without publish_live='confirm' is rejected without calling adapters
                unconfirmed_resp = self.client.post(
                    f"/queue/{item_id}/publish",
                    data={"confirm_live": "yes"},
                )
                self.assertEqual(unconfirmed_resp.status_code, 302)
                spy_pub.assert_called_once_with(item_id, confirmed=False)
                queue_check = PublishingQueue.load(self.queue_file)
                matched = next(it for it in queue_check.items if it.queue_item_id == item_id)
                self.assertEqual(matched.status, STATUS_READY)

            # 2. Both confirm_live='yes' and publish_live='confirm' pass confirmation gate (mocked execution, zero live calls)
            with patch.object(
                web_app_module,
                "execute_live_publish_item",
                return_value={
                    "success": True,
                    "asin": self.ASIN_VIDEO,
                    "status": STATUS_PUBLISHED,
                    "confirmed_platforms": 4,
                    "skipped_platforms": 0,
                    "failed_platforms": 0,
                },
            ) as mock_exec:
                confirmed_resp = self.client.post(
                    f"/queue/{item_id}/publish",
                    data={"confirm_live": "yes", "publish_live": "confirm"},
                )
                self.assertEqual(confirmed_resp.status_code, 302)
                mock_exec.assert_called_once_with(item_id, confirmed=True)

    def test_06_protection_of_published_terminal_and_unvalidated_products(self):
        """Already-published, historically verified, terminal, or unvalidated products cannot be re-enqueued."""
        # Case A: Historical platform_verified evidence blocks enqueueing even when queue is empty
        with ExitStack() as stack:
            self._enter_sandbox_patches(stack, historical_verified_asins={self.ASIN_VIDEO})

            hist_res = services.enqueue_existing_product_service(self.ASIN_VIDEO)
            self.assertFalse(hist_res["success"])
            self.assertIn("verified publication history", hist_res["message"])

            detail_html = self.client.get(f"/products/{self.ASIN_VIDEO}").get_data(as_text=True)
            self.assertIn("Verified publication history already exists", detail_html)
            self.assertNotIn("Add to Publishing Queue", detail_html)

        # Case B: Terminal / published queue items block re-enqueueing
        with ExitStack() as stack:
            self._enter_sandbox_patches(stack)

            for terminal_status, expected_snippet in (
                (STATUS_PUBLISHED, "already been published"),
                (STATUS_PARTIALLY_PUBLISHED, "already been published"),
                (STATUS_FAILED, "terminal status"),
                (STATUS_SKIPPED, "terminal status"),
            ):
                q = PublishingQueue(
                    queue_id="queue_term",
                    created_at="2026-11-05T10:00:00+01:00",
                    start_date="2026-11-06",
                    total_days=30,
                    products_per_day=2,
                    target_capacity=60,
                    items=[
                        QueueItem(
                            queue_item_id=f"item_001_{self.ASIN_VIDEO}",
                            original_url=self.AFF_URL_VIDEO,
                            asin=self.ASIN_VIDEO,
                            product_name=self.product_video.title,
                            scheduled_date="2026-11-06",
                            daily_slot=1,
                            scheduled_at="2026-11-06T15:00:00+01:00",
                            timezone="Europe/Budapest",
                            package_reference=f"data/generated/asin_{self.ASIN_VIDEO}_content_package.json",
                            status=terminal_status,
                            validation_status=STATUS_READY_FOR_REVIEW,
                        )
                    ],
                )
                q.save(self.queue_file)
                blocked = services.enqueue_existing_product_service(self.ASIN_VIDEO)
                self.assertFalse(blocked["success"])
                self.assertIn(expected_snippet, blocked["message"])

            # Case C: Unvalidated ContentPackage cannot be queued
            pkg_img = json.loads(self.pkg_image_path.read_text(encoding="utf-8"))
            pkg_img["status"] = STATUS_FAILED_VALIDATION
            self.pkg_image_path.write_text(json.dumps(pkg_img, indent=2), encoding="utf-8")
            invalid_res = services.enqueue_existing_product_service(self.ASIN_IMAGE)
            self.assertFalse(invalid_res["success"])
            self.assertIn(STATUS_READY_FOR_REVIEW, invalid_res["message"])

    def test_07_synchronization_auto_sync_and_queue_merge(self):
        """Enqueueing triggers AUTO_SYNC_AFTER_IMPORT when live and merges cleanly with remote queues."""
        live_app = create_app({"TESTING": False})
        live_client = live_app.test_client()

        with ExitStack() as stack:
            self._enter_sandbox_patches(stack)
            stack.enter_context(patch.dict("os.environ", {"AUTO_SYNC_AFTER_IMPORT": "1"}))
            mock_sync = stack.enter_context(
                patch.object(
                    web_app_module,
                    "execute_sync_with_github",
                    return_value={"success": True, "message": "Synced queue update"},
                )
            )

            resp = live_client.post(
                f"/api/queue/{self.ASIN_VIDEO}/enqueue",
                headers={"X-Requested-With": "XMLHttpRequest"},
            )
            self.assertEqual(resp.status_code, 200)
            payload = resp.get_json()
            self.assertTrue(payload["success"])
            self.assertEqual(payload["sync"]["success"], True)
            mock_sync.assert_called_once()

            # Verify merge_publishing_queues preserves newly enqueued local video item alongside remote items
            local_queue = PublishingQueue.load(self.queue_file)
            remote_queue = PublishingQueue(
                queue_id="queue_phase_b",
                created_at="2026-11-05T10:00:00+01:00",
                start_date="2026-11-06",
                total_days=30,
                products_per_day=2,
                target_capacity=60,
                items=[
                    QueueItem(
                        queue_item_id="item_099_B0REMOTE01",
                        original_url="https://www.amazon.com/dp/B0REMOTE01?tag=lifemode-20",
                        asin="B0REMOTE01",
                        product_name="Remote Scheduled Item",
                        scheduled_date="2026-11-10",
                        daily_slot=1,
                        scheduled_at="2026-11-10T15:00:00+01:00",
                        timezone="Europe/Budapest",
                        package_reference="data/generated/asin_B0REMOTE01_content_package.json",
                        status=STATUS_READY,
                        validation_status=STATUS_READY_FOR_REVIEW,
                    )
                ],
            )
            merged, _report = merge_publishing_queues(local_queue.to_dict(), remote_queue.to_dict())
            by_asin = {it["asin"]: it for it in merged["items"]}
            self.assertIn(self.ASIN_VIDEO, by_asin)
            self.assertIn("B0REMOTE01", by_asin)
            self.assertEqual(by_asin[self.ASIN_VIDEO]["media_type"], "video")
            self.assertEqual(by_asin[self.ASIN_VIDEO]["video_url"], self.R2_VIDEO_URL)
            self.assertEqual(by_asin[self.ASIN_VIDEO]["original_url"], self.AFF_URL_VIDEO)


if __name__ == "__main__":
    unittest.main()
