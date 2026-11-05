"""
tests/test_portable_package_paths.py - Focused tests for repository-relative
content package references stored on newly created queue entries.

Machine-specific absolute Windows paths must never reach a new queue entry:
GitHub Actions checks the repository out elsewhere, so only a path such as
``data/generated/asin_<ASIN>_content_package.json`` stays resolvable there.
Existing absolute references remain supported by the publisher's resolution
fallback (covered in tests/test_content_package_preflight.py).
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from amazon.scraper import Product
from publishing.queue import PublishingQueue
from web import services

PROJECT_ROOT = services.PROJECT_ROOT


class TestPortablePackageReferences(unittest.TestCase):
    def setUp(self):
        # Temporary workspace inside the repository root so relative paths can
        # be asserted against PROJECT_ROOT (mirrors tests/test_batch_management.py).
        self.root = Path(tempfile.mkdtemp(dir=PROJECT_ROOT))
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.queue_file = self.root / "queue.json"
        self.batches_file = self.root / "batches.json"
        self.products_dir = self.root / "products"
        self.generated_dir = self.root / "generated"

    # ---------- helpers (same fixture style as tests/test_batch_management.py) ----------

    def _product_for(self, url):
        asin = url.rsplit("/", 1)[-1].split("?", 1)[0]
        return Product(
            title=f"Product {asin}",
            features=["Verified feature"],
            affiliate_url=url,
            original_affiliate_url=url,
            asin=asin,
            image_url="https://m.media-amazon.com/images/I/test.jpg",
        )

    def _generate(self, product, disclosure):
        url = product.affiliate_url
        return {"platforms": {
            "facebook": {"content": f"Facebook {url}"},
            "threads": {"content": f"Threads {url}"},
            "pinterest": {"title": product.title, "description": f"Pinterest {url}"},
        }}

    def _import(self, urls):
        with patch.object(services, "QUEUE_FILE", self.queue_file), \
             patch.object(services, "BATCHES_FILE", self.batches_file), \
             patch.object(services, "PRODUCTS_DIR", self.products_dir), \
             patch.object(services, "GENERATED_DIR", self.generated_dir), \
             patch("web.services.resolve_product_for_url") as resolve, \
             patch("web.services.ContentGenerator.generate_all_platforms", side_effect=self._generate):
            resolve.side_effect = lambda url, **kwargs: (True, self._product_for(url),
                                                         f"asin_{url.rsplit('/', 1)[-1].split('?', 1)[0]}", [])
            return services.add_products_from_urls("\n".join(urls),
                                                    config={"affiliate": {"disclosure": "Ad"}})

    # ---------- tests ----------

    def test_helper_converts_repository_paths_to_posix_relative(self):
        ref = services.to_repository_relative_path(
            PROJECT_ROOT / "data" / "generated" / "asin_B0PORTABLE_content_package.json"
        )
        self.assertEqual(ref, "data/generated/asin_B0PORTABLE_content_package.json")
        self.assertFalse(Path(ref).is_absolute())
        self.assertNotIn("\\", ref)

    def test_paths_outside_the_repository_are_left_usable(self):
        outside = Path(tempfile.gettempdir()) / "elsewhere" / "pkg.json"
        ref = services.to_repository_relative_path(outside)
        self.assertEqual(ref, str(outside).replace("\\", "/"))
        self.assertFalse(ref.endswith("\\"))

    def test_new_queue_entries_store_repository_relative_package_paths(self):
        result = self._import(["https://amazon.test/dp/B0PORTABLE1"])
        self.assertEqual(result.get("accepted"), 1)

        queue = PublishingQueue.load(self.queue_file)
        self.assertEqual(len(queue.items), 1)

        ref = queue.items[0].package_reference
        self.assertTrue(ref, "queue entry must record a content package reference")
        self.assertFalse(Path(ref).is_absolute(),
                         f"new queue entries must be repository-relative, got {ref!r}")
        self.assertNotIn("\\", ref, "POSIX separators only")
        # Still resolvable from the repository root (the publisher's fallback).
        resolved = PROJECT_ROOT / ref
        self.assertTrue(resolved.exists(), f"{ref} must resolve inside the repository")
        self.assertEqual(resolved, self.generated_dir / "asin_B0PORTABLE1_content_package.json")
        self.assertTrue(ref.endswith("generated/asin_B0PORTABLE1_content_package.json"))

    def test_recovery_path_also_stores_a_portable_reference(self):
        """recover_failed_product must not write an absolute machine path either."""
        asin = "B0PORTREC01"
        product_file = self.products_dir / f"asin_{asin}.json"
        product_file.parent.mkdir(parents=True, exist_ok=True)
        url = f"https://amazon.test/dp/{asin}"
        product = self._product_for(url)
        product_file.write_text(json.dumps(product.to_dict()), encoding="utf-8")
        package_file = self.generated_dir / f"asin_{asin}_content_package.json"
        package_file.parent.mkdir(parents=True, exist_ok=True)
        package_file.write_text(
            json.dumps({
                "status": "READY_FOR_REVIEW",
                "asin": asin,
                "source": {"affiliate_url": url, "title": f"Product {asin}"},
            }),
            encoding="utf-8",
        )
        batch = {
            "batch_id": "batch_recovery_test",
            "created_at": "2026-10-30T10:00:00+01:00",
            "entries": [{
                "asin": asin,
                "url": url,
                "status": "CONTENT_GENERATION_FAILED",
                "batch_id": "batch_recovery_test",
            }],
        }
        batches_file = self.batches_file
        batches_file.write_text(json.dumps([batch]), encoding="utf-8")

        # Recovery refuses unless the queue is empty of this ASIN/URL and the
        # product's cached affiliate URL matches; regeneration itself is
        # stubbed out so no live provider call happens.
        with patch.object(services, "QUEUE_FILE", self.queue_file), \
             patch.object(services, "BATCHES_FILE", batches_file), \
             patch.object(services, "PRODUCTS_DIR", self.products_dir), \
             patch.object(services, "GENERATED_DIR", self.generated_dir), \
             patch("web.services.regenerate_content",
                   return_value={"success": True, "is_ready": True, "package_status": "READY_FOR_REVIEW"}):
            result = services.recover_failed_product(asin, batch_id="batch_recovery_test")

        self.assertTrue(result.get("success"), result.get("message"))
        queue = PublishingQueue.load(self.queue_file)
        ref = queue.items[0].package_reference
        self.assertFalse(Path(ref).is_absolute(), f"recovery must store a relative path, got {ref!r}")
        self.assertEqual(ref, f"{self.root.relative_to(PROJECT_ROOT).as_posix()}/generated/asin_{asin}_content_package.json")


if __name__ == "__main__":
    unittest.main()
