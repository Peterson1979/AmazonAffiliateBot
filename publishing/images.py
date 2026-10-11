"""Safe image provenance and validation helpers for publishing."""

from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from urllib.parse import urlparse
import json


def is_valid_image_url(value: Any) -> bool:
    """Accept only a non-empty HTTPS URL suitable for an image API."""
    if not isinstance(value, str) or not value.strip():
        return False
    parsed = urlparse(value.strip())
    return parsed.scheme == "https" and bool(parsed.netloc) and bool(parsed.path)


def _package_image_candidates(package: Dict[str, Any]) -> Iterable[str]:
    for platform_data in (package.get("platforms") or {}).values():
        if isinstance(platform_data, dict) and platform_data.get("image_url"):
            yield platform_data["image_url"]
    source = package.get("source") or {}
    if isinstance(source, dict) and source.get("image_url"):
        yield source["image_url"]
    if package.get("image_url"):
        yield package["image_url"]


def recover_verified_image_url(package: Dict[str, Any], products_dir: Path = Path("data/products")) -> Optional[str]:
    """Recover an image only from the same package or ASIN product cache."""
    for candidate in _package_image_candidates(package):
        if is_valid_image_url(candidate):
            return candidate.strip()
    asin = str(package.get("asin") or "").strip().upper()
    if not asin:
        return None
    cache_file = products_dir / f"asin_{asin}.json"
    if not cache_file.exists():
        return None
    try:
        with cache_file.open("r", encoding="utf-8") as handle:
            cached = json.load(handle)
    except (OSError, ValueError, TypeError):
        return None
    candidate = cached.get("image_url") if isinstance(cached, dict) else None
    return candidate.strip() if is_valid_image_url(candidate) else None
