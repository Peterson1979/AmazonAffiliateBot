"""Bounded Groq chat-completions client compatible with ContentGenerator."""

import re
from typing import Any, Dict, List, Optional
import os
import time

import requests

from amazon.scraper import Product
from ai.ollama import OllamaProviderError, OllamaGenerationTimeout


class GroqAPIError(OllamaProviderError):
    """A sanitized, classifiable Groq API failure."""

    def __init__(
        self,
        message: str,
        *,
        category: str,
        status_code: Optional[int] = None,
        retry_after: Optional[float] = None,
        reset_hint: Optional[str] = None,
        response_metadata: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(message)
        self.category = category
        self.status_code = status_code
        self.retry_after = retry_after
        self.reset_hint = reset_hint
        self.response_metadata = response_metadata or {}


_MAX_RETRY_AFTER_SECONDS = 5.0
# Reasoning models spend part of max_tokens thinking (observed: 108-182 tokens at
# effort "low"). Reserve that headroom so the platform's content budget is not
# consumed by reasoning, which otherwise yields empty or truncated content.
_REASONING_HEADROOM = 200
_SENSITIVE_VALUE_RE = re.compile(r"(?i)(bearer\s+|api[_-]?key\s*[=:]\s*)([^\s,;]+)")
_RATE_LIMIT_HEADERS = (
    "retry-after",
    "x-ratelimit-limit-requests",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-reset-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-tokens",
)


def _safe_text(value: Any, secrets: tuple[str, ...] = ()) -> str:
    """Keep provider details useful without allowing credentials into errors/logs."""
    text = str(value or "")
    text = _SENSITIVE_VALUE_RE.sub(r"\1[REDACTED]", text)
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text[:500]


def _header(headers: Any, *names: str) -> Optional[str]:
    if not headers:
        return None
    lowered = {str(key).lower(): value for key, value in headers.items()}
    for name in names:
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return _safe_text(value)
    return None


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    try:
        parsed = float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and parsed >= 0 else None


def _response_metadata(
    response: Any,
    data: Optional[Dict[str, Any]] = None,
    *,
    attempt: Optional[int] = None,
    secret: Optional[str] = None,
    category: Optional[str] = None,
) -> Dict[str, Any]:
    """Extract only bounded, non-secret diagnostics from one HTTP response."""
    headers = getattr(response, "headers", {}) or {}
    metadata: Dict[str, Any] = {
        "http_status": getattr(response, "status_code", None),
    }
    if attempt is not None:
        metadata["attempt"] = attempt
    if category:
        metadata["category"] = category
    rate_headers = {}
    for header_name in _RATE_LIMIT_HEADERS:
        value = _header(headers, header_name)
        if value is not None:
            rate_headers[header_name] = _safe_text(value, (secret,) if secret else ())
    if rate_headers:
        metadata["rate_limit_headers"] = rate_headers

    if not isinstance(data, dict):
        return metadata
    for key in ("id", "object", "model"):
        value = data.get(key)
        if value not in (None, ""):
            metadata[key] = _safe_text(value, (secret,) if secret else ())
    choices = data.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        finish_reason = choices[0].get("finish_reason")
        if finish_reason not in (None, ""):
            metadata["finish_reason"] = _safe_text(finish_reason, (secret,) if secret else ())
    usage = data.get("usage")
    if isinstance(usage, dict):
        safe_usage = {
            key: value for key, value in usage.items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        }
        if safe_usage:
            metadata["usage"] = safe_usage
    return metadata


def _metadata_suffix(metadata: Dict[str, Any]) -> str:
    parts = []
    if metadata.get("finish_reason"):
        parts.append(f"finish_reason={metadata['finish_reason']}")
    if metadata.get("usage"):
        parts.append(f"usage={metadata['usage']}")
    if metadata.get("rate_limit_headers"):
        parts.append(f"rate_headers={metadata['rate_limit_headers']}")
    return f"; {', '.join(parts)}" if parts else ""


def classify_groq_error(
    response: Any,
    secret: Optional[str] = None,
    attempt: Optional[int] = None,
) -> GroqAPIError:
    """Classify a Groq HTTP failure from its status, body, and rate-limit headers."""
    status = getattr(response, "status_code", None)
    headers = getattr(response, "headers", {}) or {}
    body: Any = {}
    try:
        body = response.json()
    except (AttributeError, ValueError, TypeError):
        body = {}
    error = body.get("error", {}) if isinstance(body, dict) else {}
    if not isinstance(error, dict):
        error = {}
    secrets = (secret,) if secret else ()
    code = _safe_text(error.get("code") or error.get("type"), secrets)
    detail = _safe_text(error.get("message") or body.get("message") if isinstance(body, dict) else "", secrets)
    evidence = f"{code} {detail}".lower()
    retry_after_header = _header(headers, "retry-after")
    retry_after = _parse_retry_after(retry_after_header)
    reset_hint = _header(
        headers,
        "x-ratelimit-reset-requests",
        "x-ratelimit-reset-tokens",
        "x-ratelimit-reset",
    )

    if status in (401, 403):
        category = "authentication_or_permission"
    elif status == 429:
        if any(marker in evidence for marker in ("daily", "per day", "tpd", "tokens/day", "day limit", "quota", "billing", "credits")):
            category = "daily_quota"
        elif any(marker in evidence for marker in ("token", "tokens", "tpm", "tokens per minute")):
            category = "token_limit"
        elif any(marker in evidence for marker in ("request", "requests", "rpm", "rate limit", "rate_limit")):
            category = "request_limit"
        elif _header(headers, "x-ratelimit-remaining-requests", "x-ratelimit-reset-requests"):
            category = "request_limit"
        elif _header(headers, "x-ratelimit-remaining-tokens", "x-ratelimit-reset-tokens"):
            category = "token_limit"
        else:
            category = "rate_limit_unknown"
    elif status is not None and status >= 500:
        category = "provider_server_error"
    elif status == 400:
        category = "invalid_request"
    else:
        category = "api_error"

    metadata = _response_metadata(response, body, attempt=attempt, secret=secret, category=category)
    if code:
        metadata["error_code"] = code
    if detail:
        metadata["error_message"] = detail
    parts = [f"Groq {category.replace('_', ' ')}"]
    if status is not None:
        parts.append(f"(HTTP {status})")
    if code:
        parts.append(f"code={code}")
    if detail:
        parts.append(f"detail={detail}")
    if reset_hint:
        parts.append(f"reset={reset_hint}")
    metadata_details = _metadata_suffix(metadata)
    if metadata_details:
        parts.append(metadata_details.lstrip("; "))
    return GroqAPIError(
        "; ".join(parts),
        category=category,
        status_code=status,
        retry_after=retry_after,
        reset_hint=reset_hint,
        response_metadata=metadata,
    )


class GroqClient:
    """Minimal OpenAI-compatible Groq client; never falls back to Ollama."""

    provider_name = "groq"

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: str = "https://api.groq.com/openai/v1",
        model: str = "openai/gpt-oss-120b",
        timeout: int = 30,
        max_attempts: int = 2,
        num_predict: int = 120,
        reasoning_effort: str = "low",
    ):
        self.api_key = api_key or os.getenv("GROQ_API_KEY", "")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout = max(1, int(timeout))
        self.max_attempts = max(1, int(max_attempts))
        self.num_predict = max(1, int(num_predict))
        self.reasoning_effort = reasoning_effort
        self.last_response_metadata: Dict[str, Any] = {}
        self.response_metadata_history: List[Dict[str, Any]] = []

    def _record_response_metadata(self, metadata: Dict[str, Any]) -> None:
        self.last_response_metadata = dict(metadata)
        self.response_metadata_history.append(dict(metadata))
        del self.response_metadata_history[:-20]

    def verify_ready(self) -> None:
        if not self.api_key.strip():
            raise OllamaProviderError("Groq API key is missing. Set the configured GROQ_API_KEY environment variable.")

    def generate_text(
        self,
        prompt: str,
        num_predict: Optional[int] = None,
        num_ctx: Optional[int] = None,
        temperature: float = 0.0,
        json_format: bool = False,
    ) -> str:
        self.verify_ready()
        payload: Dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": temperature,
            "max_tokens": (num_predict or self.num_predict) + _REASONING_HEADROOM,
            "reasoning_effort": self.reasoning_effort,
        }
        if json_format:
            payload["response_format"] = {"type": "json_object"}

        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        last_error: Optional[Exception] = None
        for attempt in range(self.max_attempts):
            try:
                response = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers,
                    json=payload,
                    timeout=self.timeout,
                )
                if response.status_code >= 400:
                    error = classify_groq_error(response, secret=self.api_key, attempt=attempt + 1)
                    self._record_response_metadata(error.response_metadata)
                    if response.status_code >= 500 and attempt < self.max_attempts - 1:
                        time.sleep(1.0)
                        continue
                    retryable = error.category in {"request_limit", "token_limit"}
                    if retryable and error.retry_after is not None and error.retry_after <= _MAX_RETRY_AFTER_SECONDS:
                        if attempt < self.max_attempts - 1:
                            time.sleep(error.retry_after)
                            continue
                    raise error
                data = response.json()
                metadata = _response_metadata(
                    response,
                    data,
                    attempt=attempt + 1,
                    secret=self.api_key,
                    category="success",
                )
                self._record_response_metadata(metadata)
                text = (((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "").strip()
                if not text:
                    raise GroqAPIError(
                        f"Groq returned empty content (HTTP {response.status_code}){_metadata_suffix(metadata)}",
                        category="empty_response",
                        status_code=response.status_code,
                        response_metadata=metadata,
                    )
                return text
            except requests.exceptions.Timeout as exc:
                last_error = exc
                self._record_response_metadata({"category": "timeout", "attempt": attempt + 1})
                if attempt == self.max_attempts - 1:
                    raise OllamaGenerationTimeout(
                        f"Groq generation timed out after {self.timeout} seconds ({self.max_attempts} attempt(s))."
                    ) from exc
            except OllamaProviderError:
                raise
            except requests.exceptions.RequestException as exc:
                last_error = exc
                self._record_response_metadata({
                    "category": "transport_error",
                    "attempt": attempt + 1,
                    "error_message": _safe_text(exc, (self.api_key,)),
                })
                if attempt == self.max_attempts - 1:
                    raise OllamaProviderError(f"Groq request failed: {exc}") from exc
            except (ValueError, KeyError, IndexError) as exc:
                raise OllamaProviderError(f"Groq returned an invalid response: {exc}") from exc
        raise OllamaProviderError(f"Groq generation failed after {self.max_attempts} attempts: {last_error}")

    def generate_recommendation(self, product: Product, disclosure: str) -> str:
        return self.generate_text(
            f"Write a concise factual recommendation using only these product facts:\n{product.title}\n"
            f"{chr(10).join(product.features)}\nLink: {product.affiliate_url}\n{disclosure}",
            num_predict=self.num_predict,
        )
