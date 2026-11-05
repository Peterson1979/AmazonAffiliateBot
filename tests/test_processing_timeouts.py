"""Regression coverage for bounded local-model failures."""

import json
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai.groq import GroqAPIError
from ai.ollama import OllamaClient, OllamaEmptyResponse, OllamaGenerationTimeout
from amazon.scraper import Product
from content.generator import ContentGenerator, effective_num_predict, recommended_num_ctx
from content.package import ContentPackage, STATUS_FAILED_VALIDATION, STATUS_READY_FOR_REVIEW


FACEBOOK_TEXT = (
    "This laptop stand is made from lightweight aluminum with a loading capacity of 11 lbs.\n\n"
    "It offers up to 7 inches of height adjustment and fits laptops from 10 to 17.3 inches."
)
PINTEREST_JSON = json.dumps({
    "title": "Adjustable Aluminum Laptop Stand",
    "description": "Features 7 inches height adjustment and fits 10 to 17.3 inches laptops.",
})
THREADS_TEXT = (
    "Foldable aluminum laptop stand with up to 7 inches height adjustment. "
    "Fits laptops from 10 to 17.3 inches."
)


def sample_product() -> Product:
    return Product(
        title="Laptop Stand Ergonomic Adjustable - Sturdy Portable Desk Riser",
        features=[
            "Ergonomic Height Adjustment: up to 7 inches of height adjustment",
            "Sturdy and Durable Construction: lightweight aluminum with a loading capacity of 11 lbs (5 kgs)",
            "Universal Compatibility: accommodates tablets and laptops from 10 to 17.3 inches",
        ],
        affiliate_url="https://link.amazon/B0hwa4Nkm",
        source_url="https://www.amazon.co.uk/dp/B0GKGBWRTR",
        image_url="https://m.media-amazon.com/images/I/example.jpg",
        asin="B0GKGBWRTR",
    )


class TestProcessingTimeouts(unittest.TestCase):
    def test_configured_output_budget_is_not_overridden(self):
        self.assertEqual(effective_num_predict("facebook", 90), 90)
        self.assertEqual(effective_num_predict("threads", 120), 120)
        self.assertEqual(effective_num_predict("facebook", None), 320)

    def test_long_product_prompt_gets_bounded_context_headroom(self):
        prompt = "x" * 7419
        self.assertEqual(recommended_num_ctx(prompt, 320, 1024), 3072)

    def test_short_prompt_keeps_configured_context_floor(self):
        self.assertEqual(recommended_num_ctx("short prompt", 120, 1024), 1024)

    def test_context_sizing_is_bounded(self):
        self.assertEqual(recommended_num_ctx("x" * 30000, 320, 1024), 4096)

    def test_generation_retries_are_bounded(self):
        client = OllamaClient(timeout=7, max_attempts=2)
        with patch.object(client, "verify_ready"), patch("ai.ollama.requests.post", side_effect=requests.exceptions.Timeout()):
            with self.assertRaises(OllamaGenerationTimeout) as raised:
                client.generate_text("test")
        self.assertIn("7 seconds", str(raised.exception))
        self.assertEqual(client.max_attempts, 2)

    def test_provider_failure_stops_remaining_platforms(self):
        product = Product(
            title="Aluminum laptop stand",
            features=["Adjustable aluminum frame"],
            affiliate_url="https://link.amazon/example",
            asin="B0TIMEOUT1",
        )
        ollama = MagicMock()
        ollama.model = "qwen2.5:7b"
        ollama.generate_text.side_effect = OllamaGenerationTimeout("timed out")
        generator = ContentGenerator(ollama_client=ollama)

        result = generator.generate_all_platforms(product, "Disclosure")

        self.assertEqual(ollama.generate_text.call_count, 1)
        self.assertIn("timed out", result["platforms"]["facebook"]["error"])
        self.assertIn("Not attempted", result["platforms"]["pinterest"]["error"])
        self.assertIn("Not attempted", result["platforms"]["threads"]["error"])

    def test_empty_provider_payload_raises_explicit_empty_response(self):
        """A blank completion raises OllamaEmptyResponse explicitly, in exactly one call."""
        client = OllamaClient(timeout=7, max_attempts=2)
        response = MagicMock(status_code=200)
        response.json.return_value = {"response": "   \n"}
        with patch.object(client, "verify_ready"), patch(
            "ai.ollama.requests.post", return_value=response
        ) as post:
            with self.assertRaises(OllamaEmptyResponse) as raised:
                client.generate_text("test")
        self.assertEqual(post.call_count, 1)
        self.assertIn("empty response", str(raised.exception))

    def test_empty_first_response_consumes_the_single_regeneration(self):
        """An empty first completion is retried once (existing budget) and can recover."""
        mock_ollama = MagicMock()
        mock_ollama.model = "qwen2.5:7b"
        mock_ollama.generate_text.side_effect = [
            OllamaEmptyResponse("Ollama returned an empty response."),
            THREADS_TEXT,
        ]
        generator = ContentGenerator(ollama_client=mock_ollama)

        result = generator.generate_for_platform("threads", sample_product(), "Disclosure")

        self.assertEqual(mock_ollama.generate_text.call_count, 2)
        self.assertIn("17.3 inches", result["text"])
        metadata = generator.last_validation_metadata["threads"]
        self.assertEqual(metadata["status"], "VALID")
        self.assertEqual(metadata["retry_count"], 1)

    def test_persistently_empty_response_fails_closed_and_is_never_marked_valid(self):
        """Two empty completions fail validation explicitly after exactly 2 calls."""
        empty = GroqAPIError("Groq returned empty content (HTTP 200)", category="empty_response", status_code=200)
        mock_ollama = MagicMock()
        mock_ollama.model = "openai/gpt-oss-120b"
        mock_ollama.generate_text.side_effect = [empty, empty]
        generator = ContentGenerator(ollama_client=mock_ollama)
        product = sample_product()

        with self.assertRaises(RuntimeError) as raised:
            generator.generate_for_platform("threads", product, "Disclosure")

        self.assertEqual(mock_ollama.generate_text.call_count, 2)
        self.assertIn("empty response", str(raised.exception))
        metadata = generator.last_validation_metadata["threads"]
        self.assertEqual(metadata["status"], "FAILED_VALIDATION")
        self.assertTrue(all("empty response" in error for error in metadata["errors"]))

        # A full package run with a persistently empty provider still fails closed
        # on every platform instead of marking empty content valid.
        mock_ollama.generate_text.side_effect = [empty] * 6
        social = generator.generate_all_platforms(product, "Disclosure")
        package = ContentPackage.build_package(product, social, model_name=mock_ollama.model)
        self.assertEqual(package["status"], STATUS_FAILED_VALIDATION)
        self.assertEqual(
            package["platforms"]["threads"]["validation"]["status"],
            STATUS_FAILED_VALIDATION,
        )

    def test_empty_response_on_one_platform_keeps_remaining_platforms_generating(self):
        """A single empty completion must not abort the other platforms' generation."""
        mock_ollama = MagicMock()
        mock_ollama.model = "qwen2.5:7b"
        # facebook, pinterest, threads attempt 1 (empty), threads regeneration
        mock_ollama.generate_text.side_effect = [
            FACEBOOK_TEXT,
            PINTEREST_JSON,
            OllamaEmptyResponse("Ollama returned an empty response."),
            THREADS_TEXT,
        ]
        generator = ContentGenerator(ollama_client=mock_ollama)
        product = sample_product()

        social = generator.generate_all_platforms(product, "Disclosure")

        self.assertEqual(mock_ollama.generate_text.call_count, 4)
        for platform in ("facebook", "pinterest", "threads"):
            self.assertNotIn("error", social["platforms"][platform], platform)
            self.assertEqual(social["validation_metadata"][platform]["status"], "VALID")
        package = ContentPackage.build_package(product, social, model_name=mock_ollama.model)
        self.assertEqual(package["status"], STATUS_READY_FOR_REVIEW)

    def test_provider_failure_is_persistable_and_not_marked_ready(self):
        product = Product(
            title="Aluminum laptop stand",
            features=["Adjustable aluminum frame"],
            affiliate_url="https://link.amazon/example",
            asin="B0TIMEOUT1",
            image_url="https://m.media-amazon.com/images/I/example.jpg",
        )
        social = {
            "platforms": {
                "facebook": {"error": "Ollama generation timed out after 60 seconds (2 attempt(s))."},
                "pinterest": {"error": "Not attempted because Ollama provider failed."},
                "threads": {"error": "Not attempted because Ollama provider failed."},
            },
            "validation_metadata": {},
        }
        package = ContentPackage.build_package(product, social)
        self.assertEqual(package["status"], STATUS_FAILED_VALIDATION)
        self.assertIn("timed out", package["platforms"]["facebook"]["validation"]["errors"][0])


if __name__ == "__main__":
    unittest.main()
