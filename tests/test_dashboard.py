"""
Unit and Integration Tests for AmazonAffiliateBot Management Dashboard.
Verifies all web dashboard endpoints, service integrations, security guarantees,
no secrets leakage, product workflows, and safety requirements.
"""

import unittest
import json
import os
import tempfile
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock

from web.app import create_app
from web.services import (
    CONFIG_PATH,
    load_application_config,
    save_application_config,
    get_dashboard_summary,
    get_all_products,
    get_product_detail,
    add_products_from_urls,
    revalidate_content,
    regenerate_content,
    execute_discovery_run,
    execute_publisher_run,
    get_log_content,
    check_windows_tasks_status,
    get_platform_statuses
)
from amazon.scraper import Product
from publishing.queue import PublishingQueue, QueueItem, STATUS_READY, STATUS_PUBLISHED
from content.facts import FactExtractor
from content.package import ContentPackage, STATUS_READY_FOR_REVIEW, STATUS_FAILED_VALIDATION


class TestDashboard(unittest.TestCase):
    """Test suite for the Web Dashboard and management layer."""

    def setUp(self):
        """Set up test Flask client and temporary test data directory."""
        self.temp_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)

        if CONFIG_PATH.exists():
            self._original_config_data = CONFIG_PATH.read_text(encoding="utf-8")
        else:
            self._original_config_data = None

        self.app = create_app({"TESTING": True, "SECRET_KEY": "test-key"})
        self.client = self.app.test_client()

        # Set fake environment variables to verify secrets are NEVER leaked in HTML
        os.environ["SECRET_TEST_TOKEN"] = "SUPER_SECRET_12345_TOKEN"
        os.environ["META_ACCESS_TOKEN"] = "META_SECRET_ACCESS_TOKEN_888"
        os.environ["PINTEREST_ACCESS_TOKEN"] = "PINTEREST_SECRET_TOKEN_777"
        os.environ["THREADS_ACCESS_TOKEN"] = "THREADS_SECRET_TOKEN_666"

    def tearDown(self):
        """Clean up environment variables and restore config.json."""
        for key in [
            "SECRET_TEST_TOKEN",
            "META_ACCESS_TOKEN",
            "PINTEREST_ACCESS_TOKEN",
            "THREADS_ACCESS_TOKEN"
        ]:
            os.environ.pop(key, None)

        if hasattr(self, "_original_config_data"):
            if self._original_config_data is not None:
                CONFIG_PATH.write_text(self._original_config_data, encoding="utf-8")
            elif CONFIG_PATH.exists():
                CONFIG_PATH.unlink()

    # -------------------------------------------------------------------------
    # 1. Main Page Status Checks (200 OK)
    # -------------------------------------------------------------------------
    def test_01_dashboard_overview_page_200(self):
        """Dashboard overview '/' returns 200 OK and shows system status."""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Operations & Management Dashboard", html)
        self.assertIn("Europe/Budapest", html)
        self.assertIn("DRY RUN", html)

    def test_02_products_page_200(self):
        """Products directory '/products' returns 200 OK."""
        response = self.client.get("/products")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Products Directory", html)

    def test_03_add_product_page_200(self):
        """Add products page '/add-product' returns 200 OK."""
        response = self.client.get("/add-product")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Add Amazon Products", html)
        self.assertIn("Enter Amazon Affiliate Links", html)

    def test_04_discovery_page_200(self):
        """Discovery page '/discovery' returns 200 OK."""
        response = self.client.get("/discovery")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Amazon Discovery & Replenishment", html)
        self.assertIn("Configured Search Queries", html)

    def test_05_queue_page_200(self):
        """Queue schedule page '/queue' returns 200 OK."""
        response = self.client.get("/queue")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Publishing Queue Schedule", html)
        self.assertIn("Slot 1", html)

    def test_06_content_preview_page_200(self):
        """Content preview page '/content-preview' returns 200 OK and shows DRY RUN banner."""
        response = self.client.get("/content-preview")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Multi-Platform Content Preview", html)
        self.assertIn("DRY RUN — NOTHING WILL BE PUBLISHED", html)

    def test_07_automation_page_200(self):
        """Automation page '/automation' returns 200 OK and shows setup command."""
        response = self.client.get("/automation")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Automation & Windows Task Scheduler", html)
        self.assertIn("scripts\\setup_windows_tasks.ps1", html)
        self.assertIn("08:30", html)
        self.assertIn("12:00", html)
        self.assertIn("19:00", html)

    def test_08_logs_page_200(self):
        """Logs page '/logs' returns 200 OK for publisher and maintenance logs."""
        response = self.client.get("/logs?type=publisher")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Operational Logs", html)
        self.assertIn("daily_publisher.log", html)

        response_maint = self.client.get("/logs?type=maintenance")
        self.assertEqual(response_maint.status_code, 200)
        html_m = response_maint.get_data(as_text=True)
        self.assertIn("queue_maintenance.log", html_m)

    def test_09_settings_page_200(self):
        """Settings page '/settings' returns 200 OK and shows non-secret configuration."""
        response = self.client.get("/settings")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Settings & Configuration", html)
        self.assertIn("Affiliate FTC Disclosure Notice", html)
        self.assertIn("Platform Credential Verification", html)

    # -------------------------------------------------------------------------
    # 2. Security & No Secrets Leakage
    # -------------------------------------------------------------------------
    def test_10_no_secrets_leaked_in_any_response(self):
        """Verify that secret tokens and API keys are never rendered in HTML."""
        secret_tokens = [
            "SUPER_SECRET_12345_TOKEN",
            "X_SECRET_API_KEY_999",
            "META_SECRET_ACCESS_TOKEN_888",
            "PINTEREST_SECRET_TOKEN_777",
            "THREADS_SECRET_TOKEN_666"
        ]
        routes = ["/", "/products", "/add-product", "/discovery", "/queue", "/content-preview", "/automation", "/logs", "/settings"]
        
        for route in routes:
            resp = self.client.get(route)
            html = resp.get_data(as_text=True)
            for secret in secret_tokens:
                self.assertNotIn(
                    secret,
                    html,
                    f"Security violation: Secret '{secret}' found rendered in HTML of route '{route}'!"
                )

    def test_11_instagram_strictly_excluded_from_active_platforms(self):
        """Instagram must NOT appear as an active platform in UI platform statuses."""
        resp = self.client.get("/")
        html = resp.get_data(as_text=True)
        # Verify active platform headers
        self.assertIn("Facebook", html)
        self.assertIn("Pinterest", html)
        self.assertIn("Threads", html)
        
        # Check platform statuses dict directly
        cfg = load_application_config()
        platforms = get_platform_statuses(cfg)
        self.assertIn("facebook", platforms)
        self.assertIn("pinterest", platforms)
        self.assertIn("threads", platforms)
        self.assertNotIn("instagram", platforms)

    # -------------------------------------------------------------------------
    # 3. Product Detail & Content Inspection
    # -------------------------------------------------------------------------
    def test_12_product_detail_view_with_valid_asin(self):
        """Product detail page loads structured canonical facts and platform copy."""
        # Check an existing ASIN from test products
        resp = self.client.get("/products/B0GNGTN7TV")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("B0GNGTN7TV", html)
        self.assertIn("Structured Canonical Facts", html)

    def test_13_product_detail_not_found_redirects(self):
        """Navigating to non-existent ASIN redirects with flash warning."""
        resp = self.client.get("/products/NONEXISTENT999", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("was not found", html)

    # -------------------------------------------------------------------------
    # 4. Manual Product Addition & Validation
    # -------------------------------------------------------------------------
    def test_14_add_product_empty_input(self):
        """Submitting empty URL displays error flash message."""
        resp = self.client.post("/add-product", data={"url": "", "urls": ""}, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Please enter at least one Amazon product", html)

    @patch("web.services.scrape_amazon_product")
    @patch("web.services.ContentGenerator.generate_all_platforms")
    def test_15_add_valid_product_flow(self, mock_gen, mock_scrape):
        """Test complete manual URL processing flow."""
        fake_prod = Product(
            title="Professional Ergonomic Aluminum Laptop Stand",
            price="$39.99",
            asin="B0TESTADD1",
            features=["17.3-inch laptop support", "Aluminum alloy structure", "Adjustable height"],
            affiliate_url="https://www.amazon.com/dp/B0TESTADD1?tag=lifemode-20"
        )
        mock_scrape.return_value = fake_prod
        mock_gen.return_value = {
            "asin": "B0TESTADD1",
            "product_title": fake_prod.title,
            "generated_with": "qwen2.5:7b",
            "platforms": {
                "x": {"content": "Check out this ergonomic stand. https://www.amazon.com/dp/B0TESTADD1?tag=lifemode-20\n\nAd"},
                "facebook": {"content": "Aluminum alloy structure.\n\nAdjustable height.\n\nhttps://www.amazon.com/dp/B0TESTADD1?tag=lifemode-20\n\nAd"},
                "pinterest": {"title": "Ergonomic Laptop Stand", "description": "17.3-inch laptop support. https://www.amazon.com/dp/B0TESTADD1?tag=lifemode-20\n\nAd"},
                "threads": {"content": "Adjustable height stand. https://www.amazon.com/dp/B0TESTADD1?tag=lifemode-20\n\nAd"}
            }
        }

        resp = self.client.post(
            "/add-product",
            data={"url": "https://www.amazon.com/dp/B0TESTADD1?tag=lifemode-20", "urls": ""},
            follow_redirects=True
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("B0TESTADD1", html)

    # -------------------------------------------------------------------------
    # 5. Settings Save Flow
    # -------------------------------------------------------------------------
    def test_16_settings_save_safe_fields(self):
        """Saving safe settings updates configuration without storing secrets."""
        resp = self.client.post(
            "/settings",
            data={
                "disclosure": "Custom test disclosure string.",
                "ollama_url": "http://localhost:11434",
                "ollama_model": "qwen2.5:7b",
                "target_capacity": "30",
                "min_capacity": "15",
                "tracking_tag": "testtag-20",
                "queries": "kitchen gadgets\noffice setup"
            },
            follow_redirects=True
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Configuration updated successfully", html)

        cfg = load_application_config()
        self.assertEqual(cfg["affiliate"]["disclosure"], "Custom test disclosure string.")
        self.assertEqual(cfg["discovery"]["tracking_tag"], "testtag-20")

    # -------------------------------------------------------------------------
    # 6. Discovery & Publisher Controls (DRY RUN)
    # -------------------------------------------------------------------------
    @patch("web.services.QueueReplenisher.maintain_queue")
    def test_17_discovery_run_invokes_replenisher(self, mock_maint):
        """Triggering discovery calls QueueReplenisher in DRY RUN mode."""
        mock_maint.return_value = {
            "replenished": True,
            "reason": "REPLENISHED",
            "initial_future_capacity": 2,
            "final_future_capacity": 4,
            "target_capacity": 28,
            "items_added_count": 2,
            "items_added": [],
            "candidates_evaluated": 5,
            "candidates_rejected": 3,
            "rejection_summary": {}
        }

        resp = self.client.post("/discovery/run", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        mock_maint.assert_called_once()
        self.assertTrue(mock_maint.call_args[1].get("dry_run"))

    @patch("web.services.QueuePublisher.publish_due_items")
    def test_18_publisher_run_invokes_publisher_in_dry_run(self, mock_pub):
        """Triggering publisher calls QueuePublisher in DRY RUN mode."""
        mock_pub.return_value = {
            "evaluated_at": "2026-10-06T12:00:00+02:00",
            "due_count": 0,
            "published_count": 0,
            "failed_count": 0,
            "skipped_count": 0,
            "items_published": []
        }

        resp = self.client.post("/publisher/run", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        mock_pub.assert_called_once()
        self.assertTrue(mock_pub.call_args[1].get("dry_run"))

    # -------------------------------------------------------------------------
    # 7. Additional Interactive Workflows & Provenance Tests
    # -------------------------------------------------------------------------
    def test_19_revalidate_endpoint_and_fail_closed_reporting(self):
        """Re-validate endpoint processes package validation and reports fail-closed state."""
        # Test standard HTML redirect flow
        resp = self.client.post("/api/validate/B0GKGBWRTR", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("B0GKGBWRTR", html)

        # Test AJAX JSON flow
        resp_json = self.client.post(
            "/api/validate/B0GKGBWRTR",
            headers={"X-Requested-With": "XMLHttpRequest"}
        )
        self.assertEqual(resp_json.status_code, 200)
        data = resp_json.get_json()
        self.assertTrue(data.get("success"))
        self.assertIn("is_valid", data)

    @patch("web.services.ContentGenerator.generate_all_platforms")
    def test_20_regenerate_endpoint(self, mock_gen):
        """Regenerate endpoint generates new platform copy and executes validation."""
        mock_gen.return_value = {
            "asin": "B0GKGBWRTR",
            "product_title": "17.3-inch Laptop Stand Aluminum Adjustable",
            "generated_with": "qwen2.5:7b",
            "platforms": {
                "x": {"content": "17.3-inch Laptop Stand supports 10-17.3 inches with 7 inches height adjustment. https://link.amazon/B0hwa4Nkm\n\nThis post contains affiliate links. As an Amazon Associate I earn from qualifying purchases."},
                "facebook": {"content": "17.3-inch Laptop Stand constructed from aluminum.\n\nSupports up to 11 lbs with 7 inches height adjustment.\n\nhttps://link.amazon/B0hwa4Nkm\n\nThis post contains affiliate links. As an Amazon Associate I earn from qualifying purchases."},
                "pinterest": {"title": "17.3-inch Laptop Stand", "description": "Adjustable aluminum laptop stand supporting up to 11 lbs. https://link.amazon/B0hwa4Nkm\n\nThis post contains affiliate links. As an Amazon Associate I earn from qualifying purchases."},
                "threads": {"content": "17.3-inch Laptop Stand with 7 inches height adjustment. https://link.amazon/B0hwa4Nkm\n\nThis post contains affiliate links. As an Amazon Associate I earn from qualifying purchases."}
            }
        }
        resp = self.client.post("/api/regenerate/B0GKGBWRTR", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("B0GKGBWRTR", html)

    def test_21_discovery_save_endpoint(self):
        """Discovery configuration save updates search queries and capacity thresholds."""
        resp = self.client.post(
            "/discovery/save",
            data={
                "queries": "ergonomic office accessories\nwfh setup gadgets",
                "target_capacity": "32",
                "min_capacity": "16",
                "tracking_tag": "customtag-20"
            },
            follow_redirects=True
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Discovery settings saved successfully", html)
        self.assertIn("customtag-20", html)

    @patch("web.services.QueueReplenisher.maintain_queue")
    def test_22_queue_maintain_endpoint(self, mock_maint):
        """Queue maintenance endpoint triggers dry-run replenisher."""
        mock_maint.return_value = {
            "replenished": True,
            "reason": "REPLENISHED",
            "initial_future_capacity": 2,
            "final_future_capacity": 4,
            "target_capacity": 28,
            "items_added_count": 2,
            "items_added": []
        }
        resp = self.client.post("/queue/maintain", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Queue maintenance executed (DRY RUN)", html)
        mock_maint.assert_called_once()
        self.assertTrue(mock_maint.call_args[1].get("dry_run"))

    def test_23_queue_status_filters(self):
        """Queue page supports filtering by READY, PUBLISHED, and FAILED status."""
        for filter_val in ["READY", "PUBLISHED", "FAILED", "ALL"]:
            resp = self.client.get(f"/queue?status={filter_val}")
            self.assertEqual(resp.status_code, 200)
            html = resp.get_data(as_text=True)
            self.assertIn("Publishing Queue Schedule", html)

    def test_24_content_preview_with_specific_asin(self):
        """Content preview for specific ASIN renders all active platform preview cards."""
        resp = self.client.get("/content-preview/B0GKGBWRTR")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Facebook", html)
        self.assertIn("Pinterest Pin", html)
        self.assertIn("Threads Post", html)
        self.assertIn("https://link.amazon/B0hwa4Nkm", html)

    def test_25_affiliate_disclosure_exact_text_consistency(self):
        """Required Amazon affiliate disclosure is present and unaltered."""
        expected_disclosure = "This post contains affiliate links. As an Amazon Associate I earn from qualifying purchases."
        resp = self.client.get("/content-preview/B0GKGBWRTR")
        html = resp.get_data(as_text=True)
        self.assertIn(expected_disclosure, html)

    def test_26_affiliate_url_provenance_in_preview_and_detail(self):
        """Original affiliate URLs are preserved and match across product detail and preview."""
        resp_detail = self.client.get("/products/B0GKGBWRTR")
        html_detail = resp_detail.get_data(as_text=True)
        self.assertIn("https://link.amazon/B0hwa4Nkm", html_detail)

        resp_preview = self.client.get("/content-preview/B0GKGBWRTR")
        html_preview = resp_preview.get_data(as_text=True)
        self.assertIn("https://link.amazon/B0hwa4Nkm", html_preview)

    def test_27_mobile_responsive_meta_and_css(self):
        """Templates include mobile responsive viewport and CSS media queries."""
        resp = self.client.get("/")
        html = resp.get_data(as_text=True)
        self.assertIn('name="viewport"', html)
        self.assertIn('content="width=device-width, initial-scale=1.0"', html)

        # Check CSS file
        resp_css = self.client.get("/static/css/style.css")
        self.assertEqual(resp_css.status_code, 200)
        css_text = resp_css.get_data(as_text=True)
        self.assertIn("@media (max-width: 900px)", css_text)
        self.assertIn("@media (max-width: 640px)", css_text)

    def test_28_windows_task_scheduler_status_shows_unregistered_safely(self):
        """Automation page displays task status without executing privileged registration."""
        resp = self.client.get("/automation")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("Task Scheduler Registration Status", html)
        self.assertIn("powershell -ExecutionPolicy Bypass -File scripts\\setup_windows_tasks.ps1", html)


if __name__ == "__main__":
    unittest.main()

