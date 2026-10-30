"""Minimal Amazon Creators API GetItems integration.

Credentials are read only from the process environment.  This module never
logs tokens, client credentials, or response authorization data.
"""

from __future__ import annotations

import json
import logging
import os
import re
import socket
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

import requests


logger = logging.getLogger(__name__)


TOKEN_ENDPOINTS = {
    "3.1": "https://api.amazon.com/auth/o2/token",
    "3.2": "https://api.amazon.co.uk/auth/o2/token",
    "3.3": "https://api.amazon.co.jp/auth/o2/token",
}


def environment_status() -> Dict[str, Any]:
    """Return non-secret Creators API environment status for local diagnostics."""
    client_id = bool(os.environ.get("AMAZON_CREATORS_API_CLIENT_ID", "").strip())
    client_secret = bool(os.environ.get("AMAZON_CREATORS_API_CLIENT_SECRET", "").strip())
    version = os.environ.get("AMAZON_CREATORS_API_VERSION", "").strip()
    marketplace = os.environ.get("AMAZON_CREATORS_API_MARKETPLACE", "").strip().lower()
    return {
        "configured": bool(client_id and client_secret and version and marketplace),
        "client_id_present": client_id,
        "client_secret_present": client_secret,
        "version": version or None,
        "marketplace": marketplace or None,
    }


class CreatorsAPIError(RuntimeError):
    """Safe, non-secret Creators API failure."""


def _sanitize_exception_text(text: str, sensitive_values: List[str]) -> str:
    """Remove credentials and token-like values from transport diagnostics."""
    sanitized = str(text)
    for value in sensitive_values:
        if value:
            sanitized = sanitized.replace(value, "[REDACTED]")
    sanitized = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[REDACTED]", sanitized)
    sanitized = re.sub(
        r"(?i)(client[_-]?secret|access[_-]?token|authorization|password|token)"
        r"(\s*[:=]\s*)([^\s,;}]+)",
        r"\1\2[REDACTED]",
        sanitized,
    )
    sanitized = re.sub(r"(?i)(https?://)[^/@\s]+:[^/@\s]+@", r"\1[REDACTED]@", sanitized)
    return sanitized


def _transport_exception_details(exc: requests.exceptions.RequestException,
                                 sensitive_values: List[str]) -> Dict[str, Any]:
    """Return secret-free details for an exception and its explicit cause/context chain."""
    chain = []
    seen = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        chain.append({
            "exception_type": type(current).__name__,
            "message": _sanitize_exception_text(str(current), sensitive_values),
        })
        current = current.__cause__ or current.__context__
    return {
        "exception_type": type(exc).__name__,
        "message": _sanitize_exception_text(str(exc), sensitive_values),
        "chain": chain[1:],
    }


def marketplace_from_url(url: str, configured: str = "") -> Optional[str]:
    """Return the Amazon marketplace domain, or None for ambiguous short URLs."""
    if configured.strip():
        return configured.strip().lower()
    host = (urlparse(url).hostname or "").lower()
    if host == "amazon.com" or host.endswith(".amazon.com"):
        return "www.amazon.com"
    if host.startswith("www.") and ".amazon." in host:
        return host
    if host.startswith("amazon.") and host != "amazon.com":
        return f"www.{host}"
    return None


class CreatorsAPIClient:
    def __init__(self, client_id: str, client_secret: str, credential_version: str,
                 marketplace: str, partner_tag: str, timeout: float = 15.0,
                 diagnostic_mode: bool = False):
        if credential_version not in TOKEN_ENDPOINTS:
            raise ValueError("AMAZON_CREATORS_API_VERSION must be 3.1, 3.2, or 3.3")
        if not marketplace or not partner_tag:
            raise ValueError("Creators API marketplace and partner tag are required")
        self._client_id = client_id
        self._client_secret = client_secret
        self.credential_version = credential_version
        self.marketplace = marketplace
        self.partner_tag = partner_tag
        self.timeout = timeout
        self.diagnostic_mode = diagnostic_mode
        self._token: Optional[str] = None

    @classmethod
    def from_environment(cls, marketplace: str, partner_tag: str,
                         diagnostic_mode: bool = False) -> Optional["CreatorsAPIClient"]:
        client_id = os.environ.get("AMAZON_CREATORS_API_CLIENT_ID", "").strip()
        client_secret = os.environ.get("AMAZON_CREATORS_API_CLIENT_SECRET", "").strip()
        version = os.environ.get("AMAZON_CREATORS_API_VERSION", "").strip()
        if not (client_id and client_secret and version):
            return None
        return cls(client_id, client_secret, version, marketplace, partner_tag,
                   diagnostic_mode=diagnostic_mode)

    def _log_local_oauth_diagnostic(self) -> None:
        """Record local DNS and explicit-proxy state immediately before OAuth."""
        sensitive_values = [self._client_id, self._client_secret, self._token or ""]
        diagnostic: Dict[str, Any] = {
            "dns_outcome": "success",
            "dns_exception_type": None,
            "dns_message": None,
            "explicit_proxy_configured": False,
        }
        try:
            socket.getaddrinfo("api.amazon.com", 443)
        except Exception as exc:
            diagnostic["dns_outcome"] = "failed"
            diagnostic["dns_exception_type"] = type(exc).__name__
            diagnostic["dns_message"] = _sanitize_exception_text(str(exc), sensitive_values)
        logger.info("Creators API local OAuth diagnostic: %s", json.dumps(diagnostic, sort_keys=True))

    def _access_token(self) -> str:
        if self._token:
            return self._token
        if self.diagnostic_mode:
            self._log_local_oauth_diagnostic()
        try:
            response = requests.post(
                TOKEN_ENDPOINTS[self.credential_version],
                headers={"Content-Type": "application/json"},
                json={
                    "grant_type": "client_credentials",
                    "client_id": self._client_id,
                    "client_secret": self._client_secret,
                    "scope": "creatorsapi::default",
                },
                timeout=self.timeout,
            )
        except requests.exceptions.RequestException as exc:
            details = _transport_exception_details(
                exc,
                [self._client_id, self._client_secret, self._token or ""],
            )
            logger.error("Creators API OAuth transport failure: %s", json.dumps(details, sort_keys=True))
            raise
        if response.status_code >= 400:
            raise CreatorsAPIError(f"Creators API authentication failed (HTTP {response.status_code}).")
        try:
            token = response.json().get("access_token")
        except ValueError as exc:
            raise CreatorsAPIError("Creators API authentication returned invalid JSON.") from exc
        if not token:
            raise CreatorsAPIError("Creators API authentication returned no access token.")
        self._token = str(token)
        return self._token

    def get_item(self, asin: str) -> Dict[str, Any]:
        response = requests.post(
            "https://creatorsapi.amazon/catalog/v1/getItems",
            headers={
                "Authorization": f"Bearer {self._access_token()}",
                "Content-Type": "application/json",
                "x-marketplace": self.marketplace,
            },
            json={
                "itemIds": [asin],
                "itemIdType": "ASIN",
                "marketplace": self.marketplace,
                "partnerTag": self.partner_tag,
                "resources": [
                    "itemInfo.title",
                    "itemInfo.features",
                    "itemInfo.productInfo",
                    "images.primary.large",
                    "images.primary.medium",
                    "offersV2.listings.price",
                ],
            },
            timeout=self.timeout,
        )
        if response.status_code == 429:
            raise CreatorsAPIError("Creators API rate limit reached (HTTP 429); no retry was attempted.")
        if response.status_code >= 400:
            raise CreatorsAPIError(f"Creators API request failed (HTTP {response.status_code}).")
        try:
            body = response.json()
        except ValueError as exc:
            raise CreatorsAPIError("Creators API returned invalid JSON.") from exc
        items = ((body.get("itemsResult") or {}).get("items") or [])
        item = next((value for value in items if str(value.get("asin", "")).upper() == asin.upper()), None)
        if not item:
            raise CreatorsAPIError(f"Creators API returned no item for ASIN {asin.upper()}.")
        return item


def _display_value(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        value = value.get("displayValue")
    return str(value).strip() if value is not None and str(value).strip() else None


def _values(value: Any) -> List[str]:
    if isinstance(value, dict):
        value = value.get("displayValues", value.get("displayValue"))
    if not isinstance(value, list):
        value = [value]
    return [text for item in value if (text := _display_value(item))]


def _url_value(value: Any) -> Optional[str]:
    if isinstance(value, dict):
        value = value.get("url")
    return str(value).strip() if value is not None and str(value).strip() else None


def product_from_item(item: Dict[str, Any], original_url: str) -> "Product":
    from amazon.scraper import Product

    info = item.get("itemInfo") or {}
    title = _display_value(info.get("title")) or ""
    features = _values(info.get("features"))
    product_info = info.get("productInfo") or {}
    description = None
    for key in ("productDescription", "description"):
        description = _display_value(product_info.get(key))
        if description:
            break
    images = item.get("images") or {}
    primary = images.get("primary") or {}
    image_url = next((_url_value(primary.get(size)) for size in ("large", "medium", "small") if _url_value(primary.get(size))), None)
    listings = ((item.get("offersV2") or {}).get("listings") or [])
    price = None
    if listings:
        money = ((listings[0].get("price") or {}).get("money") or {})
        price = _display_value(money.get("displayAmount")) or _display_value(money.get("amount"))
    return Product(
        title=title,
        description=description,
        features=features,
        affiliate_url=original_url,
        original_affiliate_url=original_url,
        source_url=str(item.get("detailPageURL") or ""),
        price=price,
        asin=str(item.get("asin") or "").upper() or None,
        image_url=image_url,
    )
