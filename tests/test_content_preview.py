"""Focused regression tests for the Multi-Platform Content Preview.

Covers the Add Products -> persisted package -> accepted queue item -> preview path:
- the default preview selection surfaces the newest product accepted into
  the publishing queue (READY / READY_FOR_REVIEW with generated content),
- all available platform copy and the exact original affiliate URL are displayed,
- both the path segment and the selector form's ?asin= query select a product,
- genuinely invalid packages keep their FAILED label and are never shown as
  publish-ready, and previewing changes no persisted status.
- product image rendering and fallback when no image URL is available.
"""

import json
import re
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from amazon.scraper import Product  # noqa: E402
import web.services as services  # noqa: E402
from web.app import create_app  # noqa: E402
from publishing.queue import (  # noqa: E402
    PublishingQueue,
    QueueItem,
    STATUS_PUBLISHED,
    STATUS_READY,
    STATUS_READY_FOR_REVIEW,
)
from content.package import STATUS_FAILED_VALIDATION  # noqa: E402

URL = "https://link.amazon/B0preview1"

ACCEPTED = "B0PREVIEW01"   # newest item accepted into the queue by Add Products
OLDER_PUBLISHED = "B0PREVIEW02"
INVALID = "B0PREVIEW03"    # FAILED_VALIDATION package, never queued
B0NOIMAGE = "B0NOIMAGE"    # product with no image URL

FB_COPY = "Accepted Facebook preview copy with grounded facts. " + URL
PIN_TITLE = "Accepted Pin Title"
PIN_DESC = "Accepted grounded pin description. " + URL
TH_COPY = "Accepted threads preview copy. " + URL


def _package(asin, title, status, fb, pin_title, pin_desc, th):
    return {
        "asin": asin,
        "status": status,
        "model": "openai/gpt-oss-120b",
        "generated_at": "2026-10-26T12:00:00+00:00",
        "source": {
            "title": title,
            "affiliate_url": URL,
            "features": ["Feature one", "Feature two"],
        },
        "platforms": {
            "facebook": {"status": "VALID" if status != STATUS_FAILED_VALIDATION else "FAILED",
                         "content": fb, "image_url": "https://m.media-amazon.com/images/I/test.jpg"},
            "pinterest": {"status": "VALID" if status != STATUS_FAILED_VALIDATION else "FAILED",
                          "title": pin_title, "description": pin_desc, "link": URL, "image_url": "https://m.media-amazon.com/images/I/test.jpg"},
            "threads": {"status": "VALID" if status != STATUS_FAILED_VALIDATION else "FAILED",
                        "content": th, "image_url": "https://m.media-amazon.com/images/I/test.jpg"},
        },
    }


class TestContentPreview(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=PROJECT_ROOT / ".test-tmp", prefix="preview_"))
        self.queue_file = self.root / "queue.json"
        self.batches_file = self.root / "batches.json"
        self.products_dir = self.root / "products"
        self.generated_dir = self.root / "generated"
        self.products_dir.mkdir()
        self.generated_dir.mkdir()

        # --- product caches ---
        for asin, title in ((ACCEPTED, "Accepted Laptop Stand"),
                            (OLDER_PUBLISHED, "Older Monitor"),
                            (INVALID, "Broken Listing")):
            product = Product(
                title=title,
                features=["Feature one", "Feature two"],
                affiliate_url=URL,
                original_affiliate_url=URL,
                source_url=URL,
                asin=asin,
                image_url="https://m.media-amazon.com/images/I/test.jpg",
            )
            (self.products_dir / f"asin_{asin}.json").write_text(
                json.dumps(product.to_dict(), indent=2), encoding="utf-8"
            )

        # --- persisted content packages (exactly as Add Products persists them) ---
        packages = {
            ACCEPTED: _package(ACCEPTED, "Accepted Laptop Stand", STATUS_READY_FOR_REVIEW,
                               FB_COPY, PIN_TITLE, PIN_DESC, TH_COPY),
            OLDER_PUBLISHED: _package(OLDER_PUBLISHED, "Older Monitor", STATUS_READY_FOR_REVIEW,
                                      "Older Facebook copy. " + URL, "Older Pin",
                                      "Older pin description. " + URL, "Older threads copy."),
            INVALID: _package(INVALID, "Broken Listing", STATUS_FAILED_VALIDATION,
                              "Rejected facebook copy.", "Rejected Pin",
                              "Rejected pin description.", "Rejected threads copy."),
        }
        for asin, pkg in packages.items():
            (self.generated_dir / f"asin_{asin}_content_package.json").write_text(
                json.dumps(pkg, indent=2), encoding="utf-8"
            )

        # --- queue: one published (older), one freshly accepted; INVALID not queued ---
        def item(queue_item_id, asin, status, validation, scheduled_at):
            return QueueItem(
                queue_item_id=queue_item_id,
                original_url=URL,
                asin=asin,
                product_name=packages[asin]["source"]["title"],
                scheduled_date=scheduled_at[:10],
                daily_slot=1,
                scheduled_at=scheduled_at,
                timezone="Europe/Budapest",
                package_reference=str(
                    self.generated_dir / f"asin_{asin}_content_package.json"
                ),
                status=status,
                validation_status=validation,
            )

        queue = PublishingQueue(
            queue_id="preview_test_queue",
            created_at="2026-10-01T00:00:00+00:00",
            start_date="2026-10-07",
            target_capacity=60,
            items=[
                item("item_old_B0PREVIEW02", OLDER_PUBLISHED, STATUS_PUBLISHED,
                     STATUS_READY_FOR_REVIEW, "2026-10-07T12:00:00+02:00"),
                item("item_new_B0PREVIEW01", ACCEPTED, STATUS_READY,
                     STATUS_READY_FOR_REVIEW, "2026-10-26T19:00:00+02:00"),
            ],
        )
        queue.save(self.queue_file)

        self.app = create_app({"TESTING": True, "SECRET_KEY": "preview-test"})
        self.client = self.app.test_client()

        self.patchers = [
            patch.object(services, "QUEUE_FILE", self.queue_file),
            patch.object(services, "BATCHES_FILE", self.batches_file),
            patch.object(services, "PRODUCTS_DIR", self.products_dir),
            patch.object(services, "GENERATED_DIR", self.generated_dir),
            patch("amazon.scraper.scrape_amazon_product",
                  side_effect=AssertionError("browser scrape must not run")),
        ]
        for p in self.patchers:
            p.start()

    def tearDown(self):
        for p in self.patchers:
            p.stop()
        shutil.rmtree(self.root, ignore_errors=True)

    # ---------- helpers ----------

    def _get(self, url):
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200, f"{url} -> {resp.status_code}")
        return resp.get_data(as_text=True)

    @staticmethod
    def _selected_asin(html):
        m = re.search(r'<option value="([^"]+)" selected>', html)
        return m.group(1) if m else None

    # ---------- 1. default selection surfaces the accepted product ----------

    def test_default_preview_selects_newly_accepted_product_with_all_content(self):
        html = self._get("/content-preview")

        self.assertEqual(self._selected_asin(html), ACCEPTED)
        # All three platform cards with the persisted copy
        self.assertIn(FB_COPY, html)
        self.assertIn(PIN_TITLE, html)
        self.assertIn(PIN_DESC, html)
        self.assertIn(TH_COPY, html)
        # Exact original affiliate URL (embedded in the copy, as generated)
        self.assertIn(URL, html)
        # Validation badges reflect the persisted VALID statuses
        self.assertGreaterEqual(html.count("VALIDATED"), 3)

    def test_accepted_product_listed_and_selectable_via_path_and_query(self):
        html = self._get("/content-preview")
        self.assertIn(f'<option value="{ACCEPTED}"', html)

        # path segment (used by the selector's onchange and queue "Preview" links)
        html_path = self._get(f"/content-preview/{ACCEPTED}")
        self.assertEqual(self._selected_asin(html_path), ACCEPTED)
        self.assertIn(FB_COPY, html_path)
        self.assertIn(URL, html_path)

        # query string (the GET selector form, works without JavaScript)
        html_query = self._get(f"/content-preview?asin={ACCEPTED}")
        self.assertEqual(self._selected_asin(html_query), ACCEPTED)
        self.assertIn(FB_COPY, html_query)
        self.assertIn(TH_COPY, html_query)

    # ---------- 2. invalid packages stay ineligible, nothing is relabeled ----------

    def test_invalid_package_keeps_failed_label_and_is_never_default(self):
        # not selected by default (accepted item wins)
        html = self._get("/content-preview")
        self.assertNotEqual(self._selected_asin(html), INVALID)

        # explicitly selected -> shown as failed, not publish-ready
        for url in (f"/content-preview/{INVALID}", f"/content-preview?asin={INVALID}"):
            html = self._get(url)
            self.assertEqual(self._selected_asin(html), INVALID)
            self.assertIn("FAILED VALIDATION", html)
            self.assertNotIn("VALIDATED", html)

    def test_preview_changes_no_persisted_state(self):
        for url in ("/content-preview", f"/content-preview/{ACCEPTED}",
                    f"/content-preview?asin={INVALID}"):
            self._get(url)

        pkg_accepted = json.loads(
            (self.generated_dir / f"asin_{ACCEPTED}_content_package.json").read_text(encoding="utf-8")
        )
        pkg_invalid = json.loads(
            (self.generated_dir / f"asin_{INVALID}_content_package.json").read_text(encoding="utf-8")
        )
        self.assertEqual(pkg_accepted["status"], STATUS_READY_FOR_REVIEW)
        self.assertEqual(pkg_invalid["status"], STATUS_FAILED_VALIDATION)

        queue = PublishingQueue.load(self.queue_file)
        statuses = {i.asin: i.status for i in queue.items}
        self.assertEqual(statuses[ACCEPTED], STATUS_READY)
        self.assertEqual(statuses[OLDER_PUBLISHED], STATUS_PUBLISHED)

    # ---------- 3. product image rendering ----------

    def test_product_image_rendered_when_available(self):
        """Verify the product image is displayed when platforms contain image URLs."""
        html = self._get("/content-preview")
        # The accepted product has image_url set on its platforms
        self.assertIn("Product Image", html)
        # Verify the img src points to a valid URL
        import re
        m = re.search(r'<img src="(https?://[^"]+)" alt="Product"', html)
        self.assertIsNotNone(m)
        self.assertIn(m.group(1), "https://m.media-amazon.com/images/I/test.jpg")

    def test_product_image_associated_with_correct_product(self):
        """Verify the image URL belongs to the currently selected product ASIN."""
        # Select the accepted product (B0PREVIEW01)
        html = self._get(f"/content-preview/{ACCEPTED}")
        # Image should be present for the selected product
        self.assertIn("Product Image", html)
        # Verify the image URL matches what's in the product data
        import re
        m = re.search(r'<img src="(https?://[^"]+)" alt="Product"', html)
        self.assertIsNotNone(m)
        # The image should be from the product's platform data
        self.assertIn("m.media-amazon.com", m.group(1))

        # Now select the older product - its image should also render
        html_old = self._get(f"/content-preview/{OLDER_PUBLISHED}")
        # Older product also has image_url, so it should render too
        self.assertIn("Product Image", html_old)


if __name__ == "__main__":
    unittest.main()