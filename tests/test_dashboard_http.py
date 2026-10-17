"""Focused regression check for the local dashboard Overview response."""

from concurrent.futures import ThreadPoolExecutor
from unittest import TestCase
from unittest.mock import patch

from web.app import create_app


class TestDashboardHttpResponse(TestCase):
    def test_overview_returns_promptly_without_external_services(self):
        app = create_app({"TESTING": True, "SECRET_KEY": "dashboard-http-test"})

        offline_ollama = {
            "online": False,
            "models": [],
            "target_model_available": False,
            "status_text": "OFFLINE",
            "base_url": "http://localhost:11434",
        }
        offline_tasks = {
            "supported": False,
            "tasks": {},
            "all_registered": False,
            "status_text": "NOT AVAILABLE",
        }
        sync_status = {
            "status": "READY",
            "label": "Ready to Sync",
            "description": "Test fixture",
            "badge_class": "badge-info",
            "is_synced": True,
        }

        with patch("web.services.check_ollama_status", return_value=offline_ollama), \
             patch("web.services.check_windows_tasks_status", return_value=offline_tasks), \
             patch("web.services.get_git_sync_status", return_value=sync_status):
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(lambda: app.test_client().get("/"))
                response = future.result(timeout=3)

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Next publishing slots", html)
        self.assertIn("Cloud publisher", html)
        self.assertIn("Publishing activity", html)
