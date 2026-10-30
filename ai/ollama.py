"""
Ollama Local AI Client for generating product recommendations and social content.
Interacts exclusively with the local Ollama instance and qwen2.5:7b model.
"""

from typing import Dict, Any, List, Optional
import json
import os
import requests
from amazon.scraper import Product

class OllamaProviderError(RuntimeError):
    """A bounded, actionable failure communicating with the local provider."""

class OllamaGenerationTimeout(OllamaProviderError):
    """The provider did not finish a generation within the configured limit."""

class OllamaEmptyResponse(OllamaProviderError):
    """The provider answered but returned no generated content.

    This is an explicit, distinguishable content failure: it is not a transport
    failure, so callers must not treat it as a provider-wide outage, and it must
    never be mistaken for valid content.
    """


class OllamaClient:
    """Client for interacting with local Ollama API for text and recommendation generation."""

    def __init__(
        self,
        base_url: str = "http://localhost:11434",
        model: str = "qwen2.5:7b",
        timeout: int = 60,
        num_predict: int = 120,
        num_ctx: int = 1024,
        max_attempts: int = 2,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.num_predict = num_predict
        self.num_ctx = num_ctx
        self.max_attempts = max(1, int(max_attempts))
        self.num_thread = min(os.cpu_count() or 4, 10)

    def check_connection(self) -> bool:
        """Check if local Ollama server is reachable."""
        try:
            res = requests.get(f"{self.base_url}/api/tags", timeout=5)
            return res.status_code == 200
        except Exception:
            return False

    def list_models(self) -> List[str]:
        """Fetch list of models available in local Ollama."""
        try:
            res = requests.get(f"{self.base_url}/api/tags", timeout=5)
            if res.status_code == 200:
                data = res.json()
                return [m.get("name", "") for m in data.get("models", [])]
        except Exception:
            pass
        return []

    def is_model_available(self, target_model: Optional[str] = None) -> bool:
        """Check if the specified model (or self.model) is installed in Ollama."""
        target = (target_model or self.model).lower()
        models = [m.lower() for m in self.list_models()]
        
        for m in models:
            if m == target or m.startswith(f"{target}:") or target.startswith(f"{m}:") or m.split(":")[0] == target.split(":")[0] and (":" in target and ":" in m and target == m):
                return True
            if m == target:
                return True
        return False

    def verify_ready(self) -> None:
        """
        Verify that Ollama is reachable and the required model is installed.
        Raises descriptive RuntimeError on failure.
        """
        if not self.check_connection():
            raise RuntimeError(f"Ollama is not reachable at {self.base_url}. Please ensure 'ollama serve' is running.")
        
        if not self.is_model_available(self.model):
            available = self.list_models()
            raise RuntimeError(
                f"Ollama model '{self.model}' is not installed.\n"
                f"Available models: {', '.join(available) if available else 'None'}\n"
                f"Run: 'ollama pull {self.model}' to install it."
            )

    def generate_text(
        self,
        prompt: str,
        num_predict: Optional[int] = None,
        num_ctx: Optional[int] = None,
        temperature: float = 0.7,
        json_format: bool = False
    ) -> str:
        """
        Generic prompt generation using local Ollama.

        Args:
            prompt: Text prompt to send to the model.
            num_predict: Max tokens to predict (defaults to self.num_predict).
            num_ctx: Context size window (defaults to self.num_ctx).
            temperature: Sampling temperature.
            json_format: If True, forces Ollama to output valid JSON.

        Returns:
            str: Generated text response from Ollama.
        """
        self.verify_ready()

        payload: Dict[str, Any] = {
            "model": self.model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": temperature,
                "top_p": 0.9,
                "num_ctx": num_ctx or self.num_ctx,
                "num_predict": num_predict or self.num_predict,
                "num_thread": self.num_thread,
            }
        }

        if json_format:
            payload["format"] = "json"

        import time
        last_error = None

        for attempt in range(self.max_attempts):
            try:
                response = requests.post(
                    f"{self.base_url}/api/generate",
                    json=payload,
                    timeout=self.timeout
                )
                if response.status_code >= 500:
                    if attempt < self.max_attempts - 1:
                        time.sleep(1.0)
                        continue
                    response.raise_for_status()

                response.raise_for_status()
                data = response.json()
                generated_text = data.get("response", "").strip()

                if not generated_text:
                    # Explicit signal (not a generic error) so generation code can
                    # retry this platform within its existing regeneration budget
                    # instead of aborting every remaining platform.
                    raise OllamaEmptyResponse("Ollama returned an empty response.")

                return generated_text

            except requests.exceptions.ConnectionError as e:
                last_error = e
                if attempt < self.max_attempts - 1:
                    time.sleep(1.0)
                    continue
                raise OllamaProviderError(f"Ollama connection error: {e}") from e
            except requests.exceptions.Timeout as e:
                last_error = e
                if attempt < self.max_attempts - 1:
                    time.sleep(1.0)
                    continue
                raise OllamaGenerationTimeout(
                    f"Ollama generation timed out after {self.timeout} seconds "
                    f"({self.max_attempts} attempt(s))."
                ) from e
            except requests.exceptions.HTTPError as e:
                last_error = e
                if attempt < self.max_attempts - 1:
                    time.sleep(1.0)
                    continue
                raise OllamaProviderError(f"Ollama generation failed: {e}") from e
            except OllamaProviderError:
                # Preserve the explicit failure type (e.g. OllamaEmptyResponse).
                raise
            except Exception as e:
                raise OllamaProviderError(f"Ollama generation failed: {e}") from e

        raise OllamaProviderError(f"Ollama generation failed after {self.max_attempts} attempts: {last_error}")

    def generate_recommendation(
        self,
        product: Product,
        disclosure: str = "This post contains affiliate links. As an Amazon Associate I earn from qualifying purchases."
    ) -> str:
        """
        Generate a natural product recommendation using local Ollama qwen2.5:7b (V1 method).

        Args:
            product: Normalized Product dataclass.
            disclosure: Configurable affiliate disclosure text.

        Returns:
            str: Generated recommendation copy including the affiliate URL and disclosure.
        """
        if product.features:
            features_text = "\n".join(f"- {f}" for f in product.features[:3])
        else:
            features_text = "- Ergonomic design for daily use"

        prompt = f"""You are a product curator writing a concise, authentic 50-word product recommendation.

PRODUCT: {product.title}
KEY FEATURES:
{features_text}

LINK: {product.affiliate_url}

INSTRUCTIONS:
1. Highlight 1-2 practical benefits strictly from the features above.
2. Include the affiliate link: {product.affiliate_url}
3. End with the disclosure:
{disclosure}

Recommendation:"""

        generated_text = self.generate_text(
            prompt=prompt,
            num_predict=self.num_predict,
            num_ctx=self.num_ctx
        )

        if product.affiliate_url and product.affiliate_url not in generated_text:
            generated_text += f"\n\nLink: {product.affiliate_url}"

        if disclosure and disclosure.strip() not in generated_text:
            generated_text += f"\n\n{disclosure.strip()}"

        return generated_text
