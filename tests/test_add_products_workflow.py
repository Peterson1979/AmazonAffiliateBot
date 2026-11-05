"""Focused end-to-end tests for the dashboard **Add Products** workflow.

Covers the shared path (not one specific ASIN):
  pasted URLs -> cache/ASIN resolution (no network providers needed) -> Groq
  generation -> package persistence -> import/queue integration -> publisher
  pickup with duplicate-post protection.

No live social APIs, no live model calls, and no real queue/batch data are used.
"""

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from amazon.scraper import Product
from content.package import STATUS_FAILED_VALIDATION, STATUS_READY_FOR_REVIEW, STATUS_VALID
from publishing.adapters import STATUS_PUBLISHED as PLATFORM_STATUS_PUBLISHED
from publishing.adapters import STATUS_WOULD_PUBLISH
from publishing.queue import (
    PublishingQueue,
    QueuePublisher,
    STATUS_PUBLISHED,
    STATUS_READY,
    resolve_product_for_url,
)
from web import app as web_app
from web import services

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DISCLOSURE = "This post contains affiliate links. As an Amazon Associate I earn from qualifying purchases."
DESTINATIONS = ("facebook", "pinterest", "pinterest_lifemodehq", "threads")

A1 = "https://www.amazon.co.uk/dp/B0WFTEST01?tag=homeideas4a01-20&ref=first"       # exact cached URL
A1_VARIANT = "https://www.amazon.co.uk/dp/B0WFTEST01?tag=homeideas4a01-20&srctok=renamed"  # same ASIN, new tracking
A2 = "https://www.amazon.de/dp/B0WFTEST02?tag=homeideas4a01-20&x=2"                 # same ASIN, different cached URL
A2_CACHED = "https://www.amazon.de/dp/B0WFTEST02?tag=oldtag-20"
A3_STALE = "https://www.amazon.co.uk/dp/B0WFTEST03?tag=homeideas4a01-20"             # cached but insufficient


class StubPublisher:
    """Deterministic publisher double: never touches a social network."""

    def __init__(self):
        self.config = SimpleNamespace(dry_run=False)
        self.calls = []

    def publish_package(self, package, dry_run=True, prior_results=None):
        self.calls.append({
            "asin": package.get("asin"),
            "dry_run": dry_run,
            "prior_results": prior_results,
        })
        if dry_run:
            return {
                "mode": "DRY_RUN",
                "status": STATUS_WOULD_PUBLISH,
                "results": {d: {"status": STATUS_WOULD_PUBLISH, "mode": "DRY_RUN"} for d in DESTINATIONS},
            }
        return {
            "mode": "LIVE",
            "status": PLATFORM_STATUS_PUBLISHED,
            "results": {
                d: {"status": PLATFORM_STATUS_PUBLISHED, "mode": "LIVE", "post_id": f"post-{d}"}
                for d in DESTINATIONS
            },
        }


class TestAddProductsWorkflow(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(dir=PROJECT_ROOT / ".test-tmp"))
        self.queue_file = self.root / "queue.json"
        self.batches_file = self.root / "batches.json"
        self.products_dir = self.root / "products"
        self.generated_dir = self.root / "generated"
        self.products_dir.mkdir(parents=True, exist_ok=True)
        self.generated_dir.mkdir(parents=True, exist_ok=True)
        self.config = json.loads((PROJECT_ROOT / "config.json").read_text(encoding="utf-8"))
        self._write_fixture_cache()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    # ---------- fixtures ----------

    def _write_product(self, asin, stored_url, title, features, image_url="https://m.media-amazon.com/images/I/test.jpg"):
        product = Product(
            title=title,
            features=list(features),
            affiliate_url=stored_url,
            original_affiliate_url=stored_url,
            source_url=stored_url,
            asin=asin,
            image_url=image_url,
        )
        (self.products_dir / f"asin_{asin}.json").write_text(
            json.dumps(product.to_dict(), indent=2), encoding="utf-8"
        )

    def _write_fixture_cache(self):
        # B0WFTEST01: cache holds the exact pasted URL (previously rejected as DUPLICATE)
        self._write_product("B0WFTEST01", A1, "Adjustable Laptop Stand",
                            ["Folds flat to 10 x 8.7 x 1.8 inches", "Holds up to 11 lbs"])
        # B0WFTEST02: cache holds a different tracking URL (pasted URL differs -> ASIN fallback)
        self._write_product("B0WFTEST02", A2_CACHED, "Stainless Steel Water Bottle",
                            ["Double wall vacuum insulation", "Leak-proof lid with carry loop"])
        # B0WFTEST03: cached but insufficient (no features) -> must fail closed
        self._write_product("B0WFTEST03", A3_STALE, "Broken Listing", [], image_url=None)

    @staticmethod
    def _generate_success(product, disclosure):
        """Successful generation double whose payloads embed the exact source URL."""
        url = product.affiliate_url
        return {
            "platforms": {
                "facebook": {"text": f"Facebook copy grounded in facts.\n\nLink: {url}"},
                "pinterest": {"title": "Grounded Pin Title", "description": f"Grounded pin description. {url}"},
                "threads": {"text": f"Short grounded thread.\n\nLink: {url}"},
            },
            "validation_metadata": {
                "facebook": {"status": "VALID", "retry_count": 0, "errors": []},
                "pinterest": {"status": "VALID", "retry_count": 0, "errors": []},
                "threads": {"status": "VALID", "retry_count": 0, "errors": []},
            },
        }

    @staticmethod
    def _generate_cg_failed(product, disclosure):
        """Content generation failed with Groq rate-limit errors across all platforms."""
        return {
            "platforms": {
                "facebook": {"error": "Groq returned empty content (HTTP 200)", "status": "FAILED"},
                "pinterest": {"error": "Not attempted because groq failed at facebook: empty response", "status": "FAILED"},
                "threads": {"error": "Not attempted because groq failed at facebook: empty response", "status": "FAILED"},
            },
            "validation_metadata": {},
        }

    @staticmethod
    def _generate_failing_generation(product, disclosure):
        """Failing generation (used by test_failed_generation_is_recorded_accurately_and_not_queued)."""
        return {
            "platforms": {
                "facebook": {"error": "Groq returned empty content (HTTP 200)", "status": "FAILED"},
                "pinterest": {"error": "Not attempted because groq failed at facebook: empty response", "status": "FAILED"},
                "threads": {"error": "Not attempted because groq failed at facebook: empty response", "status": "FAILED"},
            },
            "validation_metadata": {},
        }

    def _import(self, urls_text, generate=None):
        with patch.object(services, "QUEUE_FILE", self.queue_file), \
             patch.object(services, "BATCHES_FILE", self.batches_file), \
             patch.object(services, "PRODUCTS_DIR", self.products_dir), \
             patch.object(services, "GENERATED_DIR", self.generated_dir), \
             patch("amazon.scraper.scrape_amazon_product", side_effect=AssertionError("browser scrape must not run")), \
             patch("web.services.ContentGenerator.generate_all_platforms",
                   side_effect=generate or self._generate_success):
            return services.add_products_from_urls(urls_text, config=self.config)

    def _queue(self):
        return PublishingQueue.load(self.queue_file)

    # ---------- 1/2. multiple URLs, persistence, exact URL ----------

    def test_multiple_pasted_urls_are_generated_persisted_and_queued(self):
        result = self._import("\n".join([A1, A2]))

        self.assertEqual(result["total"], 2)
        self.assertEqual(result["accepted"], 2)
        self.assertEqual(result["rejected"], 0)

        queue = self._queue()
        self.assertEqual(len(queue.items), 2)
        by_asin = {item.asin: item for item in queue.items}
        self.assertEqual(set(by_asin), {"B0WFTEST01", "B0WFTEST02"})
        # byte-for-byte URL preservation and correct dashboard/publishing states
        self.assertEqual(by_asin["B0WFTEST01"].original_url, A1)
        self.assertEqual(by_asin["B0WFTEST02"].original_url, A2)
        for item in queue.items:
            self.assertEqual(item.status, STATUS_READY)
            self.assertEqual(item.validation_status, STATUS_READY_FOR_REVIEW)
            self.assertTrue(item.scheduled_at)
            self.assertTrue(Path(item.package_reference).exists())

        # persistence: accurate status + provider metadata + payloads carry the URL
        provider_model = self.config["groq"]["model"]
        for asin, url in (("B0WFTEST01", A1), ("B0WFTEST02", A2)):
            package = json.loads(
                (self.generated_dir / f"asin_{asin}_content_package.json").read_text(encoding="utf-8")
            )
            self.assertEqual(package["status"], STATUS_READY_FOR_REVIEW)
            self.assertEqual(package["model"], provider_model)
            self.assertEqual(package["source"]["affiliate_url"], url)
            self.assertIn(url, package["platforms"]["facebook"]["content"])
            self.assertIn(url, package["platforms"]["threads"]["content"])
            self.assertIn(url, package["platforms"]["pinterest"]["description"])
            self.assertEqual(package["platforms"]["pinterest"]["link"], url)
            for platform in ("facebook", "pinterest", "threads"):
                self.assertEqual(package["platforms"][platform]["status"], STATUS_VALID)
                self.assertEqual(package["platforms"][platform]["validation"]["status"], STATUS_VALID)

        # batch ledger reflects the run
        batch = services.get_import_batch(result["batch_id"], self.batches_file)
        self.assertEqual(batch["submitted_count"], 2)
        self.assertEqual(batch["accepted_count"], 2)

    # ---------- duplicate protection ----------

    def test_duplicates_never_create_extra_queue_entries(self):
        self.assertEqual(self._import("\n".join([A1, A2]))["accepted"], 2)

        # same URLs again -> queue-authoritative duplicates
        again = self._import("\n".join([A1, A2, A1]))
        self.assertEqual(again["accepted"], 0)
        statuses = [row["status"] for row in again["results"]]
        self.assertEqual(statuses, ["DUPLICATE", "DUPLICATE", "DUPLICATE"])
        self.assertIn("publishing queue", again["results"][0]["reason"])
        # third line is a repeat within the same batch
        self.assertIn("more than once in this batch", again["results"][2]["reason"])

        # different tracking URL for an already queued ASIN -> no second queue entry
        variant = self._import(A1_VARIANT)
        self.assertEqual(variant["accepted"], 0)
        self.assertEqual(variant["results"][0]["status"], "DUPLICATE")

        self.assertEqual(len(self._queue().items), 2)

    # ---------- 5. validation / empty-response failures ----------

    def test_failed_generation_is_recorded_accurately_and_not_queued(self):
        # Pass the failing generator into _import: a nested patch() of the same
        # target would be shadowed by _import's own patch and never take effect.
        result = self._import(A1, generate=self._generate_failing_generation)

        self.assertEqual(result["accepted"], 0)
        self.assertEqual(result["rejected"], 1)
        self.assertEqual(result["results"][0]["status"], "CONTENT_GENERATION_FAILED")
        self.assertEqual(len(self._queue().items), 0)

        package = json.loads(
            (self.generated_dir / "asin_B0WFTEST01_content_package.json").read_text(encoding="utf-8")
        )
        self.assertEqual(package["status"], STATUS_FAILED_VALIDATION)
        self.assertEqual(
            package["platforms"]["facebook"]["validation"]["status"], STATUS_FAILED_VALIDATION
        )
        self.assertTrue(package["platforms"]["facebook"]["validation"]["errors"])

    # ---------- 3. resolution without network providers ----------

    def test_resolution_uses_local_cache_before_apis_or_scraping(self):
        creators = MagicMock()
        creators.get_item.side_effect = RuntimeError("Creators API HTTP 403")

        with patch("amazon.scraper.scrape_amazon_product") as scrape:
            # exact cached URL
            ok, product, key, errors = resolve_product_for_url(
                A1, products_dir=self.products_dir, allow_scrape=True, creators_api_client=creators
            )
            self.assertTrue(ok, errors)
            self.assertEqual(product.affiliate_url, A1)
            self.assertEqual(key, "asin_B0WFTEST01")

            # re-pasted link with different tracking parameters, same ASIN
            ok, product, key, errors = resolve_product_for_url(
                A2, products_dir=self.products_dir, allow_scrape=True, creators_api_client=creators
            )
            self.assertTrue(ok, errors)
            self.assertEqual(product.asin, "B0WFTEST02")
            self.assertEqual(product.affiliate_url, A2)  # pasted URL wins byte-for-byte
            self.assertEqual(product.original_affiliate_url, A2)
            self.assertEqual(key, "asin_B0WFTEST02")

        creators.get_item.assert_not_called()   # 403 path never reached: fallback data sufficed
        scrape.assert_not_called()

    def test_insufficient_cached_data_still_fails_closed(self):
        ok, _, _, errors = resolve_product_for_url(
            A3_STALE, products_dir=self.products_dir, allow_scrape=True,
            creators_api_client=MagicMock(),
        )
        self.assertFalse(ok)
        self.assertTrue(errors)

    # ---------- 7/8. publisher pickup + duplicate-post protection ----------

    def test_queued_products_are_published_once_by_the_pipeline(self):
        self.assertEqual(self._import("\n".join([A1, A2]))["accepted"], 2)
        # Align scheduled items to the evaluation date so same-day overdue policy treats them as due
        queue = self._queue()
        for idx, item in enumerate(queue.items):
            item.scheduled_at = f"2027-06-01T1{3 if idx == 0 else 9}:00:00+02:00"
            item.scheduled_date = "2027-06-01"
            item.daily_slot = idx + 1
        queue.save(self.queue_file)
        future = "2027-06-01T20:00:00+02:00"
        log_file = self.root / "publisher.log"

        # dry run respects settings: nothing is marked published
        dry_stub = StubPublisher()
        QueuePublisher.publish_due_items(
            self.queue_file, now_override=future, publisher=dry_stub, dry_run=True, log_file=log_file
        )
        self.assertEqual(len(dry_stub.calls), 2)
        self.assertTrue(all(call["dry_run"] for call in dry_stub.calls))
        self.assertEqual({item.status for item in self._queue().items}, {STATUS_READY})

        # live (stubbed, no real APIs): both items publish and the package is found
        live_stub = StubPublisher()
        report = QueuePublisher.publish_due_items(
            self.queue_file, now_override=future, publisher=live_stub, dry_run=False, log_file=log_file
        )
        self.assertEqual(report["published_count"], 2)
        self.assertEqual({item.status for item in self._queue().items}, {STATUS_PUBLISHED})
        for call in live_stub.calls:
            self.assertIsNotNone(call["asin"])

        # duplicate protection: a second run publishes nothing
        QueuePublisher.publish_due_items(
            self.queue_file, now_override=future, publisher=live_stub, dry_run=False, log_file=log_file
        )
        self.assertEqual(len(live_stub.calls), 2)

        # retry path: prior results are forwarded so already-published platforms are skipped
        queue = self._queue()
        queue.items[0].status = STATUS_READY
        queue.save(self.queue_file)
        QueuePublisher.publish_due_items(
            self.queue_file, now_override=future, publisher=live_stub, dry_run=False, log_file=log_file
        )
        self.assertEqual(len(live_stub.calls), 3)
        retry_call = live_stub.calls[-1]
        self.assertIsNotNone(retry_call["prior_results"])
        self.assertEqual(
            retry_call["prior_results"]["facebook"]["status"], PLATFORM_STATUS_PUBLISHED
        )

    # ---------- 1. dashboard accepts multiple pasted URLs ----------

    def test_dashboard_form_forwards_multiple_urls_byte_for_byte(self):
        application = web_app.create_app({"TESTING": True, "SECRET_KEY": "test-key"})
        payload = f"{A1}\n{A2}"
        with patch("web.app.add_products_from_urls") as add:
            add.return_value = {
                "success": True, "total": 2, "accepted": 2, "rejected": 0, "results": [],
            }
            response = application.test_client().post("/add-product", data={"urls": payload})

        self.assertEqual(response.status_code, 200)
        forwarded = add.call_args.args[0] if add.call_args.args else add.call_args.kwargs["urls_text"]
        self.assertEqual(forwarded, payload)

    # ---------- GET /add-product ----------

    def test_get_add_product_does_not_return_500(self):
        """GET /add-product renders the blank form; it must not touch POST-only state."""
        # No TESTING flag on purpose: an unhandled exception must surface as a
        # real HTTP 500 here, which this test then fails on.
        application = web_app.create_app({"SECRET_KEY": "test-key"})
        response = application.test_client().get("/add-product")

        self.assertNotEqual(response.status_code, 500)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Add Amazon Products", html)
        self.assertNotIn("Ingestion Summary", html)  # GET renders no run results

    # ---------- A. Missing ASIN followed by valid URL ----------

    def test_missing_asin_then_valid_url_same_post(self):
        """Missing ASIN should not cause HTTP 500; should be rejected in batch; valid URL proceeds."""
        from web import services
        from publishing.queue import PublishingQueue
        from pathlib import Path

        root = Path(tempfile.mkdtemp(dir=PROJECT_ROOT / ".test-tmp"))
        queue_file = root / "queue.json"
        batches_file = root / "batches.json"
        products_dir = root / "products"
        generated_dir = root / "generated"
        products_dir.mkdir(parents=True, exist_ok=True)
        generated_dir.mkdir(parents=True, exist_ok=True)

        # Write a product with no ASIN (simulating unresolvable ASIN)
        from amazon.scraper import Product
        no_asin_product = Product(
            title="Unresolvable Product",
            features=["Some feature"],
            affiliate_url="https://www.amazon.co.uk/dp/B0TEST01?tag=homeideas4a01-20",
            original_affiliate_url="https://www.amazon.co.uk/dp/B0TEST01?tag=homeideas4a01-20",
            source_url="https://www.amazon.co.uk/dp/B0TEST01?tag=homeideas4a01-20",
            asin=None,  # <-- No ASIN
            image_url="https://m.media-amazon.com/images/I/test.jpg",
        )
        (products_dir / "asin_B0TEST01.json").write_text(
            json.dumps(no_asin_product.to_dict(), indent=2), encoding="utf-8"
        )

        # Valid URL fixture: cached product so resolution needs no network.
        valid_url = "https://www.amazon.co.uk/dp/B0WFTEST02?tag=homeideas4a01-20"
        valid_product = Product(
            title="Valid Grounded Product",
            features=["Folds flat to 10 x 8.7 x 1.8 inches", "Holds up to 11 lbs"],
            affiliate_url=valid_url,
            original_affiliate_url=valid_url,
            source_url=valid_url,
            asin="B0WFTEST02",
            image_url="https://m.media-amazon.com/images/I/test.jpg",
        )
        (products_dir / "asin_B0WFTEST02.json").write_text(
            json.dumps(valid_product.to_dict(), indent=2), encoding="utf-8"
        )

        missing_url = "https://www.amazon.co.uk/dp/B0TEST01?tag=homeideas4a01-20"
        application = web_app.create_app({"TESTING": True, "SECRET_KEY": "test-key"})

        with patch.object(services, "QUEUE_FILE", queue_file), \
             patch.object(services, "BATCHES_FILE", batches_file), \
             patch.object(services, "PRODUCTS_DIR", products_dir), \
             patch.object(services, "GENERATED_DIR", generated_dir), \
             patch("amazon.scraper.scrape_amazon_product",
                   side_effect=AssertionError("browser scrape must not run")), \
             patch("web.services.ContentGenerator.generate_all_platforms", side_effect=self._generate_success):
            # A.1: one POST carrying the missing-ASIN URL first, then the valid URL
            response = application.test_client().post(
                "/add-product", data={"urls": f"{missing_url}\n{valid_url}"}
            )

        # A.1: the route handles the mixed batch without HTTP 500
        self.assertNotEqual(response.status_code, 500)
        self.assertEqual(response.status_code, 200)

        # A.2: Missing-ASIN URL is recorded in batch with REJECTED status and clear reason
        latest_batch = services.get_import_batch(None, batches_file)
        entries = latest_batch.get("entries", [])
        self.assertEqual(len(entries), 2, "One outcome per submitted URL")
        missing_asin_entry = [e for e in entries if "B0TEST01" in e.get("url", "")]
        self.assertTrue(missing_asin_entry, "Missing-ASIN URL should have a batch entry")
        self.assertEqual(missing_asin_entry[0].get("status"), "REJECTED")
        self.assertIn("unresolvable ASIN", missing_asin_entry[0].get("reason", ""))

        # A.3: No queue item is created for the missing-ASIN URL
        queue = PublishingQueue.load(queue_file)
        missing_items = [
            item for item in queue.items
            if (item.original_url or "") == missing_url or not item.asin
        ]
        self.assertEqual(
            missing_items, [], "No queue item may be created for the missing-ASIN URL"
        )

        # A.4: The subsequent valid URL is processed: accepted and queued
        valid_entry = [e for e in entries if e.get("url") == valid_url]
        self.assertTrue(valid_entry, "Valid URL should have a batch entry")
        self.assertEqual(valid_entry[0].get("status"), "ACCEPTED")
        self.assertEqual(len(queue.items), 1, "Exactly the valid URL should be queued")
        self.assertEqual(queue.items[0].asin, "B0WFTEST02")
        self.assertEqual(queue.items[0].original_url, valid_url)

        shutil.rmtree(root, ignore_errors=True)

    # ---------- B. Six-URL mixed outcome ----------

    def test_six_url_mixed_outcomes(self):
        """Six distinct URLs -> one outcome per URL; counts sum to 6; 2 accepted create queue items."""
        import shutil
        from publishing.queue import PublishingQueue

        root = Path(tempfile.mkdtemp(dir=PROJECT_ROOT / ".test-tmp"))
        queue_file = root / "queue.json"
        batches_file = root / "batches.json"
        products_dir = root / "products"
        generated_dir = root / "generated"
        products_dir.mkdir(parents=True, exist_ok=True)
        generated_dir.mkdir(parents=True, exist_ok=True)

        config = json.loads((PROJECT_ROOT / "config.json").read_text(encoding="utf-8"))

        # Two valid cached URLs that will be accepted
        accepted_url1 = "https://www.amazon.co.uk/dp/B0WFTEST01?tag=homeideas4a01-20"
        accepted_url2 = "https://www.amazon.de/dp/B0WFTEST02?tag=homeideas4a01-20"

        # One duplicate URL (same as accepted_url1)
        duplicate_url = accepted_url1

        # One validation-failed URL (insufficient cached data)
        validation_failed_url = "https://www.amazon.co.uk/dp/B0WFTEST03?tag=homeideas4a01-20"

        # One rejected URL (scrape failure / insufficient data / no ASIN)
        rejected_url = "https://www.amazon.co.uk/dp/B0TEST01?tag=homeideas4a01-20"

        # One content-generation-failed URL (Groq rate limit / empty response across platforms)
        cg_failed_url = "https://www.amazon.co.uk/dp/B0RATELIMIT?tag=homeideas4a01-20"

        # Create product cache for B0WFTEST01
        p1 = Product(
            title="Test Product 1",
            features=["Feature one", "Feature two"],
            affiliate_url=accepted_url1,
            original_affiliate_url=accepted_url1,
            source_url=accepted_url1,
            asin="B0WFTEST01",
            image_url="https://m.media-amazon.com/images/I/test.jpg",
        )
        (products_dir / "asin_B0WFTEST01.json").write_text(
            json.dumps(p1.to_dict(), indent=2), encoding="utf-8"
        )

        # Create product cache for B0WFTEST02
        p2 = Product(
            title="Test Product 2",
            features=["Feature three", "Feature four"],
            affiliate_url=accepted_url2,
            original_affiliate_url=accepted_url2,
            source_url=accepted_url2,
            asin="B0WFTEST02",
            image_url="https://m.media-amazon.com/images/I/test.jpg",
        )
        (products_dir / "asin_B0WFTEST02.json").write_text(
            json.dumps(p2.to_dict(), indent=2), encoding="utf-8"
        )

        # Create insufficient-cache product for validation-failed
        p3 = Product(
            title="Broken Listing",
            features=[],  # no features -> insufficient facts
            affiliate_url=validation_failed_url,
            original_affiliate_url=validation_failed_url,
            source_url=validation_failed_url,
            asin="B0WFTEST03",
            image_url=None,
        )
        (products_dir / "asin_B0WFTEST03.json").write_text(
            json.dumps(p3.to_dict(), indent=2), encoding="utf-8"
        )

        # Create product for rejected URL (no ASIN extractable)
        p4 = Product(
            title="No ASIN Product",
            features=["Some feature"],
            affiliate_url=rejected_url,
            original_affiliate_url=rejected_url,
            source_url=rejected_url,
            asin=None,  # no ASIN
            image_url="https://m.media-amazon.com/images/I/test.jpg",
        )
        (products_dir / "asin_B0TEST01.json").write_text(
            json.dumps(p4.to_dict(), indent=2), encoding="utf-8"
        )

        # Create product for CG-failed URL
        p5 = Product(
            title="Rate-Limited Product",
            features=["Feature one"],
            affiliate_url=cg_failed_url,
            original_affiliate_url=cg_failed_url,
            source_url=cg_failed_url,
            asin="B0RATELIMIT",
            image_url="https://m.media-amazon.com/images/I/test.jpg",
        )
        (products_dir / "asin_B0RATELIMIT.json").write_text(
            json.dumps(p5.to_dict(), indent=2), encoding="utf-8"
        )

        # Mock generation side effect - dispatched on the grounded source URL
        def generate_all_platforms_side_effect(product, disclosure):
            url = getattr(product, "affiliate_url", "") or ""
            # First two accepted URLs get success generation
            if "B0WFTEST01" in url or "B0WFTEST02" in url:
                return self._generate_success(product, disclosure)
            # CG-failed URL gets Groq rate-limit simulation
            if "B0RATELIMIT" in url:
                return self._generate_cg_failed(product, disclosure)
            # Validation-failed URL: copy is present but fails claim grounding
            # (no provider/rate-limit markers, so the workflow must report
            # VALIDATION_FAILED rather than CONTENT_GENERATION_FAILED)
            if "B0WFTEST03" in url:
                return {
                    "platforms": {
                        "facebook": {"text": f"Ungrounded claim copy. Link: {url}"},
                        "pinterest": {"title": "Ungrounded Pin Title", "description": f"Ungrounded pin description. {url}"},
                        "threads": {"text": f"Ungrounded thread. Link: {url}"},
                    },
                    "validation_metadata": {
                        "facebook": {"status": "FAILED_VALIDATION", "retry_count": 0, "errors": ["Claim is not supported by the source facts."]},
                        "pinterest": {"status": "FAILED_VALIDATION", "retry_count": 0, "errors": ["Claim is not supported by the source facts."]},
                        "threads": {"status": "FAILED_VALIDATION", "retry_count": 0, "errors": ["Claim is not supported by the source facts."]},
                    },
                }
            # Rejected URLs never reach generation (resolution fails first)
            return None

        # Mock resolve_product_for_url side effect: per-ASIN cache hits, no network
        def resolve_product_for_url_side_effect(url, *a, **kw):
            affiliate_url = url
            asin = None
            if "B0WFTEST01" in affiliate_url:
                asin = "B0WFTEST01"
            elif "B0WFTEST02" in affiliate_url:
                asin = "B0WFTEST02"
            elif "B0RATELIMIT" in affiliate_url:
                asin = "B0RATELIMIT"
            elif "B0WFTEST03" in affiliate_url:
                asin = "B0WFTEST03"

            # rejected URL: no ASIN
            if not asin:
                return (False, None, None, ["Could not extract ASIN"])

            # Accepted + validation-failed URLs resolve to a cached product
            product = Product(
                title=f"Resolved Product {asin}",
                features=["Folds flat to 10 x 8.7 x 1.8 inches", "Holds up to 11 lbs"],
                affiliate_url=affiliate_url,
                original_affiliate_url=affiliate_url,
                source_url=affiliate_url,
                asin=asin,
                image_url="https://m.media-amazon.com/images/I/test.jpg",
            )
            return (True, product, f"asin_{asin}", [])

        with patch.object(services, "QUEUE_FILE", queue_file), \
             patch.object(services, "BATCHES_FILE", batches_file), \
             patch.object(services, "PRODUCTS_DIR", products_dir), \
             patch.object(services, "GENERATED_DIR", generated_dir), \
             patch("amazon.scraper.scrape_amazon_product",
                   side_effect=AssertionError("browser scrape must not run")), \
             patch("web.services.ContentGenerator.generate_all_platforms", side_effect=generate_all_platforms_side_effect), \
             patch("web.services.resolve_product_for_url", side_effect=resolve_product_for_url_side_effect):
            result = services.add_products_from_urls(
                "\n".join([accepted_url1, accepted_url2, duplicate_url, validation_failed_url, rejected_url, cg_failed_url]),
                config=config
            )

        # B.1: Six distinct submitted URLs, one outcome per URL, counts sum to 6
        latest_batch = services.get_import_batch(result["batch_id"], batches_file)
        entries = latest_batch.get("entries", [])
        self.assertEqual(len(entries), 6, f"Expected 6 batch entries, got {len(entries)}")

        outcome_counts = {
            "accepted": sum(1 for e in entries if e.get("status") == "ACCEPTED"),
            "duplicate": sum(1 for e in entries if e.get("status") == "DUPLICATE"),
            "validation_failed": sum(1 for e in entries if e.get("status") == "VALIDATION_FAILED"),
            "rejected": sum(1 for e in entries if e.get("status") == "REJECTED"),
            "content_generation_failed": sum(1 for e in entries if e.get("status") == "CONTENT_GENERATION_FAILED"),
        }

        # B.2: Counts are exactly 2 accepted, 1 duplicate, 1 validation failed, 1 rejected, 1 content-generation failed
        self.assertEqual(outcome_counts["accepted"], 2)
        self.assertEqual(outcome_counts["duplicate"], 1)
        self.assertEqual(outcome_counts["validation_failed"], 1)
        self.assertEqual(outcome_counts["rejected"], 1)
        self.assertEqual(outcome_counts["content_generation_failed"], 1)

        # B.3: The six URL-level outcomes are mutually exclusive and sum to six
        total = sum(outcome_counts.values())
        self.assertEqual(total, 6, f"Outcome counts {outcome_counts} sum to {total}, expected 6")

        # B.5: Exactly two new queue items correspond to the two accepted URLs
        queue = PublishingQueue.load(queue_file)
        self.assertEqual(
            len(queue.items), 2, f"Expected 2 accepted queue items, got {len(queue.items)}"
        )
        self.assertEqual(
            {item.original_url for item in queue.items},
            {accepted_url1, accepted_url2},
            "Queue items must correspond exactly to the two accepted URLs",
        )
        self.assertEqual({item.asin for item in queue.items}, {"B0WFTEST01", "B0WFTEST02"})
        self.assertTrue(all(item.status == STATUS_READY for item in queue.items))

        shutil.rmtree(root, ignore_errors=True)

    # ---------- C. Duplicate POST protection (render behavior preserves results) ----------

    def test_duplicate_post_renders_correct_run(self):
        """Verify that re-posting the same URLs re-processes and displays the correct run.

        PRG is not implemented for this route: each POST renders its own run
        directly, so there is no redirect Location to follow (and therefore no
        resubmission on following it). The first run's summary must remain
        visible in its response, and the re-posted run must render its own
        correct summary (already-queued URLs as duplicates) without crashing
        or double-queueing.
        """
        application = web_app.create_app({"TESTING": True, "SECRET_KEY": "test-key"})
        payload = f"{A1}\n{A2}"

        with patch.object(services, "QUEUE_FILE", self.queue_file), \
             patch.object(services, "BATCHES_FILE", self.batches_file), \
             patch.object(services, "PRODUCTS_DIR", self.products_dir), \
             patch.object(services, "GENERATED_DIR", self.generated_dir), \
             patch("amazon.scraper.scrape_amazon_product",
                   side_effect=AssertionError("browser scrape must not run")), \
             patch("web.services.ContentGenerator.generate_all_platforms", side_effect=self._generate_success):
            # First POST
            response1 = application.test_client().post("/add-product", data={"urls": payload})
            # Second POST with same URLs (should re-process, not crash)
            response2 = application.test_client().post("/add-product", data={"urls": payload})

        # No PRG: neither POST redirects, so nothing is resubmitted via a Location
        self.assertEqual(response1.status_code, 200)
        self.assertEqual(response2.status_code, 200)
        self.assertNotIn("Location", response1.headers)
        html1 = response1.get_data(as_text=True)
        html2 = response2.get_data(as_text=True)

        # Both should render successfully (no HTTP 500)
        self.assertIn("Add Amazon Products", html1)
        self.assertIn("Add Amazon Products", html2)

        # The first run's summary remains available in its own response
        self.assertIn("ACCEPTED", html1)
        self.assertIn("Accepted:</strong> 2", html1)

        # The second run renders its own correct summary: already-queued URLs are duplicates
        self.assertIn("DUPLICATE", html2)
        self.assertIn("Accepted:</strong> 0", html2)
        self.assertIn("Total:</strong> 2", html2)
        self.assertEqual(
            len(self._queue().items), 2, "Re-posting must not create extra queue items"
        )
        self.assertNotIn("HTTP 500", html1 + html2)


if __name__ == "__main__":
    unittest.main()