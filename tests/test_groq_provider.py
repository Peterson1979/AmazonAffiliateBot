"""Focused Groq provider and selection regressions."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai.groq import GroqAPIError, GroqClient, classify_groq_error
from ai.ollama import OllamaClient, OllamaProviderError
from ai.provider import create_content_provider
from amazon.scraper import Product
from content.package import ContentPackage


class TestGroqProvider(unittest.TestCase):
    def test_default_provider_is_groq_without_exposing_key(self):
        with patch.dict(os.environ, {"GROQ_API_KEY": "test-secret"}, clear=False):
            provider = create_content_provider({"content_generation": {"provider": "groq"}})
        self.assertIsInstance(provider, GroqClient)
        self.assertEqual(provider.model, "openai/gpt-oss-120b")
        self.assertNotIn("test-secret", repr(provider))

    def test_proven_ollama_provider_settings_are_selected_explicitly(self):
        provider = create_content_provider({
            "content_generation": {"provider": "ollama"},
            "ollama": {
                "base_url": "http://localhost:11434",
                "model": "qwen2.5:7b",
                "timeout": 60,
                "max_attempts": 2,
                "num_predict": 90,
                "num_ctx": 1024,
            },
        })
        self.assertIsInstance(provider, OllamaClient)
        self.assertEqual(provider.base_url, "http://localhost:11434")
        self.assertEqual(provider.model, "qwen2.5:7b")
        self.assertEqual(provider.timeout, 60)
        self.assertEqual(provider.max_attempts, 2)
        self.assertEqual(provider.num_predict, 90)
        self.assertEqual(provider.num_ctx, 1024)

    def test_daily_quota_is_classified_and_never_retried(self):
        client = GroqClient(api_key="test-secret", max_attempts=2)
        response = type("Response", (), {
            "status_code": 429,
            "headers": {"retry-after": "1"},
            "json": lambda self: {"error": {"code": "rate_limit_exceeded", "message": "Daily quota exhausted"}},
        })()
        with patch("ai.groq.requests.post", return_value=response) as post:
            with self.assertRaises(GroqAPIError) as raised:
                client.generate_text("test")
        self.assertEqual(raised.exception.category, "daily_quota")
        self.assertIn("Daily quota exhausted", str(raised.exception))
        post.assert_called_once()

    def test_request_limit_uses_short_retry_after_once(self):
        client = GroqClient(api_key="test-secret", max_attempts=2)
        limited = type("Response", (), {
            "status_code": 429,
            "headers": {"retry-after": "2", "x-ratelimit-reset-requests": "2s"},
            "json": lambda self: {"error": {"code": "rate_limit_exceeded", "message": "Requests per minute exceeded"}},
        })()
        success = type("Response", (), {
            "status_code": 200,
            "headers": {},
            "json": lambda self: {"choices": [{"message": {"content": "ok"}}]},
            "raise_for_status": lambda self: None,
        })()
        with patch("ai.groq.requests.post", side_effect=[limited, success]) as post, patch("ai.groq.time.sleep") as sleep:
            self.assertEqual(client.generate_text("test"), "ok")
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(2.0)

    def test_failed_response_retains_safe_error_and_rate_metadata(self):
        client = GroqClient(api_key="test-secret", max_attempts=2)
        response = type("Response", (), {
            "status_code": 429,
            "headers": {
                "retry-after": "7",
                "x-ratelimit-remaining-requests": "0",
                "x-ratelimit-reset-requests": "9s",
            },
            "json": lambda self: {"error": {"type": "rate_limit_exceeded", "message": "Requests limited"}},
        })()
        with patch("ai.groq.requests.post", return_value=response) as post:
            with self.assertRaises(GroqAPIError) as raised:
                client.generate_text("test")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(raised.exception.response_metadata["http_status"], 429)
        self.assertEqual(raised.exception.response_metadata["error_code"], "rate_limit_exceeded")
        self.assertEqual(raised.exception.response_metadata["rate_limit_headers"]["retry-after"], "7")
        self.assertNotIn("test-secret", str(raised.exception))

    def test_max_tokens_reserves_room_for_reasoning_tokens(self):
        """The platform content budget must not be consumed by reasoning tokens
        (observed: 168 reasoning tokens inside an 180-token request truncated the
        Pinterest JSON)."""
        client = GroqClient(api_key="test-secret", num_predict=90)
        response = type("Response", (), {
            "status_code": 200,
            "headers": {},
            "json": lambda self: {
                "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
                "usage": {"completion_tokens_details": {"reasoning_tokens": 168}},
            },
        })()
        with patch("ai.groq.requests.post", return_value=response) as post:
            self.assertEqual(client.generate_text("test", num_predict=180), "ok")
        sent = post.call_args.kwargs["json"]
        headroom = sent["max_tokens"] - 180
        self.assertGreaterEqual(headroom, 168, "headroom must cover observed reasoning usage")
        self.assertEqual(client.num_predict, 90, "configured budget itself must be unchanged")

    def test_success_retains_finish_reason_usage_and_rate_metadata(self):
        client = GroqClient(api_key="test-secret", max_attempts=2)
        response = type("Response", (), {
            "status_code": 200,
            "headers": {"x-ratelimit-remaining-tokens": "1234", "x-ratelimit-reset-tokens": "1s"},
            "json": lambda self: {
                "id": "chatcmpl-safe",
                "model": "openai/gpt-oss-120b",
                "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            },
        })()
        with patch("ai.groq.requests.post", return_value=response):
            self.assertEqual(client.generate_text("test"), "ok")
        self.assertEqual(client.last_response_metadata["http_status"], 200)
        self.assertEqual(client.last_response_metadata["finish_reason"], "stop")
        self.assertEqual(client.last_response_metadata["usage"]["total_tokens"], 14)
        self.assertEqual(client.last_response_metadata["rate_limit_headers"]["x-ratelimit-remaining-tokens"], "1234")
        self.assertEqual(len(client.response_metadata_history), 1)

    def test_empty_success_is_distinct_from_rate_limit(self):
        client = GroqClient(api_key="test-secret", max_attempts=2)
        response = type("Response", (), {
            "status_code": 200,
            "headers": {},
            "json": lambda self: {
                "choices": [{"finish_reason": "length", "message": {"content": ""}}],
                "usage": {"total_tokens": 20},
            },
        })()
        with patch("ai.groq.requests.post", return_value=response) as post:
            with self.assertRaises(GroqAPIError) as raised:
                client.generate_text("test")
        self.assertEqual(post.call_count, 1)
        self.assertEqual(raised.exception.category, "empty_response")
        self.assertEqual(raised.exception.status_code, 200)
        self.assertEqual(raised.exception.response_metadata["finish_reason"], "length")
        self.assertIn("empty content", str(raised.exception))
        self.assertNotIn("rate limit", str(raised.exception).lower())

    def test_provider_diagnostics_are_persisted_in_new_packages(self):
        product = Product(
            title="Test product",
            features=["Verified feature"],
            affiliate_url="https://link.amazon/B0TESTDIAG1",
            original_affiliate_url="https://link.amazon/B0TESTDIAG1",
            asin="B0TESTDIAG1",
            image_url="https://m.media-amazon.com/images/I/test.jpg",
        )
        package = ContentPackage.build_package(
            product,
            {
                "platforms": {
                    "facebook": {"content": "A factual post."},
                    "pinterest": {"title": "Test", "description": "A factual description."},
                    "threads": {"content": "A factual thread."},
                },
                "provider_diagnostics": [{"http_status": 200, "finish_reason": "stop", "usage": {"total_tokens": 9}}],
            },
            model_name="openai/gpt-oss-120b",
        )
        self.assertEqual(package["provider_diagnostics"][0]["http_status"], 200)
        self.assertEqual(package["provider_diagnostics"][0]["usage"]["total_tokens"], 9)

    def test_missing_429_details_remain_unknown_and_do_not_retry(self):
        response = type("Response", (), {"status_code": 429, "headers": {}, "json": lambda self: {}})()
        error = classify_groq_error(response)
        self.assertEqual(error.category, "rate_limit_unknown")
        self.assertNotIn("test-secret", str(error))

    def test_timeout_is_bounded(self):
        client = GroqClient(api_key="test-secret", timeout=3, max_attempts=2)
        with patch("ai.groq.requests.post", side_effect=requests.exceptions.Timeout()):
            with self.assertRaises(OllamaProviderError) as raised:
                client.generate_text("test")
        self.assertIn("3 seconds", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
