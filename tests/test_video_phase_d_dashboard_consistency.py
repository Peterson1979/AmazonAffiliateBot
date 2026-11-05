"""
Focused Phase D tests for dashboard UI consistency across Content Preview,
Products Directory, Publishing Queue, and Product Detail in the video publishing workflow.
"""

from contextlib import ExitStack
import html as html_lib
import json
from pathlib import Path
import re
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from amazon.scraper import Product
from content.package import ContentPackage, STATUS_READY_FOR_REVIEW
from content.pinterest_creative import generate_flow_creatives
from publishing.queue import (
    PublishingQueue,
    QueueItem,
    STATUS_READY,
)
from web import app as web_app_module
from web import services
from web.app import create_app


class TestVideoPhaseDDashboardConsistency(unittest.TestCase):
    ASIN_VIDEO = "B0PHASEDV1"
    AFF_URL_VIDEO = "https://www.amazon.com/dp/B0PHASEDV1?tag=lifemode-20&ref_=phase_d_vid"
    COVER_IMG_VIDEO = "https://m.media-amazon.com/images/I/71PhaseDVideoCover._AC_SL1500_.jpg"
    R2_VIDEO_URL = "https://pub-8fcd679c40fd4aaa851f6ee7cdd4d083.r2.dev/amazon-affiliate-bot/videos/B0PHASEDV1/product_reel.mp4"
    R2_VIDEO_KEY = "amazon-affiliate-bot/videos/B0PHASEDV1/product_reel.mp4"

    ASIN_IMAGE = "B0PHASEDI1"
    AFF_URL_IMAGE = "https://www.amazon.com/dp/B0PHASEDI1?tag=lifemode-20&ref_=phase_d_img"
    STATIC_IMG_URL = "https://m.media-amazon.com/images/I/71PhaseDStaticImg._AC_SL1500_.jpg"

    ASIN_NOMEDIA = "B0PHASEDN1"
    AFF_URL_NOMEDIA = "https://www.amazon.com/dp/B0PHASEDN1?tag=lifemode-20&ref_=phase_d_none"

    def setUp(self):
        self.temp_dir = Path(tempfile.mkdtemp(prefix="test_video_phase_d_"))
        self.products_dir = self.temp_dir / "data" / "products"
        self.generated_dir = self.temp_dir / "data" / "generated"
        self.queue_dir = self.temp_dir / "data" / "queue"
        self.batches_dir = self.temp_dir / "data" / "batches"
        self.creatives_dir = self.temp_dir / "data" / "creatives"
        for d in (
            self.products_dir,
            self.generated_dir,
            self.queue_dir,
            self.batches_dir,
            self.creatives_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)

        self.queue_file = self.queue_dir / "publishing_queue.json"
        self.batches_file = self.batches_dir / "import_batches.json"

        # 1. Video-attached product
        self.prod_video, self.copy_video, self.pkg_video_path = self._seed_product(
            asin=self.ASIN_VIDEO,
            title="Magnetic Gooseneck Electric Pour-Over Kettle 0.9L",
            affiliate_url=self.AFF_URL_VIDEO,
            image_url=self.COVER_IMG_VIDEO,
            attach_video=True,
        )

        # 2. Static-image-only product
        self.prod_image, self.copy_image, self.pkg_image_path = self._seed_product(
            asin=self.ASIN_IMAGE,
            title="Bamboo Expandable Drawer Utensil Organizer 8 Slot",
            affiliate_url=self.AFF_URL_IMAGE,
            image_url=self.STATIC_IMG_URL,
            attach_video=False,
        )

        # 3. Media-less product (neither video nor static image URL)
        self.prod_nomedia, self.copy_nomedia, self.pkg_nomedia_path = self._seed_product(
            asin=self.ASIN_NOMEDIA,
            title="Minimalist Desk Cable Clips Pack of 6",
            affiliate_url=self.AFF_URL_NOMEDIA,
            image_url="",
            attach_video=False,
        )

        # Queue all three items so Queue, Products, Preview, and Detail can all be verified
        queue = PublishingQueue(
            queue_id="queue_phase_d",
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
                    product_name=self.prod_video.title,
                    scheduled_date="2026-11-08",
                    daily_slot=2,
                    scheduled_at="2026-11-08T21:00:00+01:00",
                    timezone="Europe/Budapest",
                    package_reference=f"data/generated/asin_{self.ASIN_VIDEO}_content_package.json",
                    status=STATUS_READY,
                    validation_status=STATUS_READY_FOR_REVIEW,
                    media_type="video",
                    video_url=self.R2_VIDEO_URL,
                ),
                QueueItem(
                    queue_item_id=f"item_002_{self.ASIN_IMAGE}",
                    original_url=self.AFF_URL_IMAGE,
                    asin=self.ASIN_IMAGE,
                    product_name=self.prod_image.title,
                    scheduled_date="2026-11-07",
                    daily_slot=1,
                    scheduled_at="2026-11-07T15:00:00+01:00",
                    timezone="Europe/Budapest",
                    package_reference=f"data/generated/asin_{self.ASIN_IMAGE}_content_package.json",
                    status=STATUS_READY,
                    validation_status=STATUS_READY_FOR_REVIEW,
                ),
                QueueItem(
                    queue_item_id=f"item_003_{self.ASIN_NOMEDIA}",
                    original_url=self.AFF_URL_NOMEDIA,
                    asin=self.ASIN_NOMEDIA,
                    product_name=self.prod_nomedia.title,
                    scheduled_date="2026-11-07",
                    daily_slot=2,
                    scheduled_at="2026-11-07T21:00:00+01:00",
                    timezone="Europe/Budapest",
                    package_reference=f"data/generated/asin_{self.ASIN_NOMEDIA}_content_package.json",
                    status=STATUS_READY,
                    validation_status=STATUS_READY_FOR_REVIEW,
                ),
            ],
        )
        queue.recalculate_counts()
        queue.save(self.queue_file)

        self.app = create_app({"TESTING": True, "SECRET_KEY": "phase-d-test"})
        self.client = self.app.test_client()

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _seed_product(
        self,
        asin: str,
        title: str,
        affiliate_url: str,
        image_url: str,
        attach_video: bool,
    ):
        product = Product(
            title=title,
            asin=asin,
            price="$39.99",
            image_url=image_url,
            affiliate_url=affiliate_url,
            original_affiliate_url=affiliate_url,
            source_url=f"https://www.amazon.com/dp/{asin}",
            features=[
                "Precision 304 stainless steel spout with 1200W rapid heating element",
                "Built-in LCD temperature display and 60-minute hold timer",
                "Ergonomic cool-touch handle for balanced pour control",
            ],
        )
        (self.products_dir / f"asin_{asin}.json").write_text(
            json.dumps(product.to_dict(), indent=2),
            encoding="utf-8",
        )

        copy_bundle = {
            "facebook": (
                f"{title} includes a 304 stainless steel spout and LCD display.\n\n"
                f"Link: {affiliate_url}\n\n"
                f"As an Amazon Associate I earn from qualifying purchases."
            ),
            "pinterest_title": title[:95],
            "pinterest_desc": (
                f"{title} with 304 stainless steel spout and 60-minute hold timer. {affiliate_url}\n\n"
                f"As an Amazon Associate I earn from qualifying purchases."
            ),
            "threads": (
                f"{title} with LCD display and cool-touch handle.\n\n"
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
        if not image_url:
            for p_name in ("facebook", "pinterest", "threads"):
                pkg["platforms"][p_name]["image_url"] = ""
        if attach_video:
            ContentPackage.attach_verified_video(
                pkg,
                video_url=self.R2_VIDEO_URL,
                object_key=self.R2_VIDEO_KEY,
                content_type="video/mp4",
                size_bytes=2097152,
                filename="product_reel.mp4",
            )
        pkg_file = ContentPackage.save_content_package(
            pkg,
            f"asin_{asin}",
            target_dir=self.generated_dir,
        )
        return product, copy_bundle, pkg_file

    def _enter_sandbox(self, stack: ExitStack):
        stack.enter_context(patch.object(services, "PROJECT_ROOT", self.temp_dir))
        stack.enter_context(patch.object(services, "PRODUCTS_DIR", self.products_dir))
        stack.enter_context(patch.object(services, "GENERATED_DIR", self.generated_dir))
        stack.enter_context(patch.object(services, "QUEUE_FILE", self.queue_file))
        stack.enter_context(patch.object(services, "BATCHES_FILE", self.batches_file))
        stack.enter_context(patch.object(web_app_module, "DEFAULT_QUEUE_FILE", self.queue_file))
        stack.enter_context(patch.object(services, "load_application_config", return_value={}))
        stack.enter_context(
            patch.object(
                services,
                "get_historical_destination_report",
                return_value={"evidence_status": "no_evidence"},
            )
        )
        stack.enter_context(
            patch(
                "web.services.load_product_creatives",
                side_effect=lambda asin: load_creatives_sandboxed(asin, self.creatives_dir),
            )
        )
        stack.enter_context(
            patch(
                "web.services.get_product_flow_creative_status",
                side_effect=lambda asin: services_flow_status_sandboxed(asin, self.creatives_dir),
            )
        )

    def test_01_video_based_content_preview_rendering(self):
        """Video-based packages render an HTML5 <video> player, cover poster, title, ASIN, affiliate URL, and video mode."""
        with ExitStack() as stack:
            self._enter_sandbox(stack)

            resp = self.client.get(f"/content-preview/{self.ASIN_VIDEO}")
            self.assertEqual(resp.status_code, 200)
            html = resp.get_data(as_text=True)

            self.assertIn("Video Publication Mode", html)
            self.assertIn(self.ASIN_VIDEO, html)
            self.assertIn(self.prod_video.title, html)
            self.assertIn(self.AFF_URL_VIDEO, html_lib.unescape(html))

            # HTML5 video player with R2 source and verified cover image poster
            self.assertIn("<video", html)
            self.assertIn(f'src="{self.R2_VIDEO_URL}"', html)
            self.assertIn('type="video/mp4"', html)
            self.assertIn(f'poster="{self.COVER_IMG_VIDEO}"', html)
            self.assertNotIn('alt="Product"', html)

            # Platform mode indicators reflect video publishing
            self.assertIn("Video Pin", html)
            self.assertIn("Native Video", html)

    def test_02_image_based_and_no_media_content_preview_rendering(self):
        """Image-based packages render the verified static image; media-less packages render a neutral notice."""
        with ExitStack() as stack:
            self._enter_sandbox(stack)

            resp_img = self.client.get(f"/content-preview/{self.ASIN_IMAGE}")
            self.assertEqual(resp_img.status_code, 200)
            html_img = resp_img.get_data(as_text=True)

            self.assertIn("Image Publication Mode", html_img)
            self.assertIn(self.ASIN_IMAGE, html_img)
            self.assertIn(self.prod_image.title, html_img)
            self.assertIn(self.AFF_URL_IMAGE, html_lib.unescape(html_img))
            self.assertNotIn("<video", html_img)
            self.assertIn(f'<img src="{self.STATIC_IMG_URL}" alt="Product"', html_img)
            self.assertIn("Image Pin", html_img)

            resp_none = self.client.get(f"/content-preview/{self.ASIN_NOMEDIA}")
            self.assertEqual(resp_none.status_code, 200)
            html_none = resp_none.get_data(as_text=True)
            self.assertIn("No Media Attached", html_none)
            self.assertNotIn("<video", html_none)
            self.assertNotIn('alt="Product"', html_none)

    def test_03_all_four_publishing_destinations_displayed_consistently(self):
        """Content Preview, Products, Queue, and Product Detail all display both Pinterest accounts, Facebook, and Threads."""
        with ExitStack() as stack:
            self._enter_sandbox(stack)

            preview_html = self.client.get(f"/content-preview/{self.ASIN_VIDEO}").get_data(as_text=True)
            self.assertIn("Pinterest — dailyideasforyou", preview_html)
            self.assertIn("Pinterest — LifeModeHQ", preview_html)
            self.assertIn("LifeModeHQ", preview_html)
            self.assertIn("lifemodehq", preview_html)
            self.assertIn("Shopping ideas", preview_html)
            self.assertIn("Home &amp; Everyday Living", preview_html)

            for route in ("/products", "/queue", f"/products/{self.ASIN_VIDEO}"):
                page_html = self.client.get(route).get_data(as_text=True)
                self.assertIn("Facebook (LifeModeHQ)", page_html, f"Missing Facebook label on {route}")
                self.assertIn("Pinterest (dailyideasforyou)", page_html, f"Missing dailyideasforyou label on {route}")
                self.assertIn("Pinterest (LifeModeHQ)", page_html, f"Missing Pinterest LifeModeHQ label on {route}")
                self.assertIn("Threads (lifemodehq)", page_html, f"Missing Threads label on {route}")

    def test_04_products_directory_media_indicators(self):
        """Products directory displays 🎬 Video, 🖼️ Image, and — No Media per product row."""
        with ExitStack() as stack:
            self._enter_sandbox(stack)

            html = self.client.get("/products").get_data(as_text=True)

            row_video = self._extract_table_row(html, self.ASIN_VIDEO)
            row_image = self._extract_table_row(html, self.ASIN_IMAGE)
            row_none = self._extract_table_row(html, self.ASIN_NOMEDIA)

            self.assertIn("🎬 Video", row_video)
            self.assertIn("Video Pin Active", row_video)
            self.assertNotIn("No Flow image", row_video)

            self.assertIn("🖼️ Image", row_image)
            self.assertIn("— No Media", row_none)

    def test_05_publishing_queue_media_indicators_and_lightweight_preview(self):
        """Publishing queue displays media indicators and a lightweight thumbnail/preview link without inline <video> bloat."""
        with ExitStack() as stack:
            self._enter_sandbox(stack)

            html = self.client.get("/queue").get_data(as_text=True)
            self.assertNotIn("<video", html)

            row_video = self._extract_table_row(html, self.ASIN_VIDEO)
            row_image = self._extract_table_row(html, self.ASIN_IMAGE)
            row_none = self._extract_table_row(html, self.ASIN_NOMEDIA)

            self.assertIn("🎬 Video", row_video)
            self.assertIn(f'src="{self.COVER_IMG_VIDEO}"', row_video)
            self.assertIn('loading="lazy"', row_video)
            self.assertIn(f"/content-preview/{self.ASIN_VIDEO}", row_video)
            self.assertIn(self.R2_VIDEO_URL, row_video)

            self.assertIn("🖼️ Image", row_image)
            self.assertIn("— No Media", row_none)

    def test_06_product_detail_video_creative_status_removes_outdated_static_prompt(self):
        """Product Detail shows Active Video Pin Mode when a video is attached and never asks to upload a static image."""
        with ExitStack() as stack:
            self._enter_sandbox(stack)

            # Seed Flow prompt manifest for the video product as well to test slot badge non-contradiction
            with patch("content.pinterest_creative.get_or_download_reference_image", return_value=None):
                generate_flow_creatives(self.prod_video, creatives_dir=self.creatives_dir)

            html_video = self.client.get(f"/products/{self.ASIN_VIDEO}").get_data(as_text=True)
            self.assertIn("Active Video Pin Mode", html_video)
            self.assertIn("dailyideasforyou", html_video)
            self.assertIn("LifeModeHQ", html_video)
            self.assertNotIn("No creative is currently selected for Pinterest publishing", html_video)
            self.assertNotIn("Upload the generated Google Flow image (PNG, JPG, or WEBP):", html_video)

            # Image-only product still displays the standard Google Flow static creative banner
            html_image = self.client.get(f"/products/{self.ASIN_IMAGE}").get_data(as_text=True)
            self.assertIn("No creative is currently selected for Pinterest publishing", html_image)
            self.assertNotIn("Active Video Pin Mode", html_image)

    def test_07_preservation_of_platform_copy_affiliate_urls_and_video_metadata(self):
        """Rendering Preview, Products, Queue, and Detail leaves copy, affiliate URLs, and R2 video metadata untouched."""
        with ExitStack() as stack:
            self._enter_sandbox(stack)

            pkg_video_before = self.pkg_video_path.read_text(encoding="utf-8")
            pkg_image_before = self.pkg_image_path.read_text(encoding="utf-8")
            queue_before = self.queue_file.read_text(encoding="utf-8")

            preview_video_html = self.client.get(f"/content-preview/{self.ASIN_VIDEO}").get_data(as_text=True)
            detail_video_html = self.client.get(f"/products/{self.ASIN_VIDEO}").get_data(as_text=True)
            self.client.get("/products")
            self.client.get("/queue")

            for page_html in (preview_video_html, detail_video_html):
                unescaped = html_lib.unescape(page_html)
                self.assertIn(self.copy_video["facebook"], unescaped)
                self.assertIn(self.copy_video["pinterest_title"], unescaped)
                self.assertIn(self.copy_video["pinterest_desc"], unescaped)
                self.assertIn(self.copy_video["threads"], unescaped)
                self.assertIn(self.AFF_URL_VIDEO, unescaped)

            self.assertEqual(self.pkg_video_path.read_text(encoding="utf-8"), pkg_video_before)
            self.assertEqual(self.pkg_image_path.read_text(encoding="utf-8"), pkg_image_before)
            self.assertEqual(self.queue_file.read_text(encoding="utf-8"), queue_before)

    def _extract_table_row(self, html: str, asin: str) -> str:
        for match in re.finditer(r"<tr\b[^>]*>(.*?)</tr>", html, flags=re.DOTALL):
            row_html = match.group(1)
            if asin in row_html:
                return row_html
        self.fail(f"Could not find <tr> containing ASIN {asin}")


def load_creatives_sandboxed(asin: str, creatives_dir: Path):
    from content.pinterest_creative import load_product_creatives

    return load_product_creatives(asin, base_dir=creatives_dir)


def services_flow_status_sandboxed(asin: str, creatives_dir: Path):
    from content.pinterest_creative import get_product_flow_creative_status

    return get_product_flow_creative_status(asin, creatives_dir=creatives_dir)


if __name__ == "__main__":
    unittest.main()
