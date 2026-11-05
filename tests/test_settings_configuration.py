"""Focused settings regressions for Groq selection and discovery tag defaults."""

import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from web.app import create_app


class TestSettingsConfiguration(unittest.TestCase):
    def setUp(self):
        self.config = {
            "content_generation": {"provider": "groq"},
            "groq": {"model": "openai/gpt-oss-120b"},
            "ollama": {"model": "qwen2.5:7b", "base_url": "http://localhost:11434"},
            "affiliate": {"disclosure": "Disclosure"},
            "discovery": {
                "tracking_tag": "homeideas4a01-20",
                "target_future_capacity": 28,
                "min_future_capacity": 14,
                "queries": ["home goods"],
            },
            "publishing": {"dry_run": True},
        }
        self.app = create_app({"TESTING": True, "SECRET_KEY": "test"})
        self.client = self.app.test_client()

    def test_settings_displays_active_groq_configuration_and_tracking_tag(self):
        with patch("web.app.load_application_config", return_value=self.config), \
             patch("web.app.get_platform_statuses", return_value={}):
            response = self.client.get("/settings")
        html = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("GROQ", html)
        self.assertIn("openai/gpt-oss-120b", html)
        self.assertIn("homeideas4a01-20", html)
        self.assertNotIn("Ollama Model", html)

    def test_settings_save_cannot_switch_provider_back_to_ollama(self):
        saved = []

        def capture(config):
            saved.append(copy.deepcopy(config))
            return True

        with patch("web.app.load_application_config", return_value=copy.deepcopy(self.config)), \
             patch("web.app.save_application_config", side_effect=capture), \
             patch("web.app.get_platform_statuses", return_value={}):
            response = self.client.post("/settings", data={
                "disclosure": "Disclosure",
                "ollama_model": "qwen2.5:7b",
                "ollama_url": "http://localhost:11434",
                "tracking_tag": "homeideas4a01-20",
                "target_capacity": "28",
                "min_capacity": "14",
                "queries": "home goods",
            })
        self.assertEqual(response.status_code, 302)
        self.assertEqual(saved[0]["content_generation"]["provider"], "groq")
        self.assertEqual(saved[0]["groq"]["model"], "openai/gpt-oss-120b")
        self.assertEqual(saved[0]["discovery"]["tracking_tag"], "homeideas4a01-20")


if __name__ == "__main__":
    unittest.main()
