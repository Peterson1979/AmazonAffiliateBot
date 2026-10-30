"""
Content Package module for AmazonAffiliateBot (V3/V4).
Defines deterministic Content Package structures and serialization for manual-review-ready handoff.
Active supported platforms: Facebook, Pinterest, Threads. Historical X entries are not regenerated.
"""

from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, timezone
from pathlib import Path
import json

from amazon.scraper import Product, is_valid_image_url


SUPPORTED_PLATFORMS = ("facebook", "pinterest", "threads")

STATUS_READY_FOR_REVIEW = "READY_FOR_REVIEW"
STATUS_FAILED_VALIDATION = "FAILED_VALIDATION"
STATUS_VALID = "VALID"


class ContentPackage:
    """
    Deterministic Content Package builder and serializer for V3/V4.
    Structures validated social copy for Facebook, Pinterest, and Threads
    into a single machine-readable package for manual review and publishing.
    """

    @staticmethod
    def build_package(
        product: Product,
        social_data: Dict[str, Any],
        cache_file_path: Optional[str] = None,
        model_name: Optional[str] = None,
        platform_validation_metadata: Optional[Dict[str, Dict[str, Any]]] = None,
        timestamp: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Build a deterministic V3/V4 Content Package dictionary.

        Args:
            product: Normalized Product instance.
            social_data: Multi-platform generation dictionary (containing 'platforms', etc.)
            cache_file_path: Path string to the cached product JSON (e.g. 'data/products/asin_B0GKGBWRTR.json').
            model_name: Name of the LLM used (defaults to social_data['generated_with'] or 'qwen2.5:7b').
            platform_validation_metadata: Optional validation metadata per platform (status, retry_count, errors).
            timestamp: ISO 8601 generation timestamp (defaults to current UTC time).

        Returns:
            Dict[str, Any]: Formatted content package.
        """
        asin = product.asin or social_data.get("asin", "")
        model = model_name or social_data.get("generated_with", "qwen2.5:7b")
        generated_at = timestamp or datetime.now(timezone.utc).isoformat()

        if cache_file_path is None:
            if asin:
                cache_file_path = f"data/products/asin_{asin.upper()}.json"
            else:
                cache_file_path = "data/products/product_cache.json"

        # Format relative path cleanly with forward slashes
        cache_file_path = str(cache_file_path).replace("\\", "/")

        platforms_input = social_data.get("platforms", {})
        validation_meta = platform_validation_metadata or social_data.get("validation_metadata", {})
        packaged_platforms: Dict[str, Any] = {}
        all_platforms_valid = True
        product_image_url = getattr(product, "image_url", None)

        for p_name in SUPPORTED_PLATFORMS:
            p_data = platforms_input.get(p_name)
            p_meta = validation_meta.get(p_name, {})

            if not p_data:
                all_platforms_valid = False
                packaged_platforms[p_name] = {
                    "status": STATUS_FAILED_VALIDATION,
                    "validation": {
                        "status": STATUS_FAILED_VALIDATION,
                        "retry_count": p_meta.get("retry_count", 0),
                        "errors": p_meta.get("errors", [f"Missing content for {p_name}."])
                    }
                }
                continue

            # Check if platform returned an error dict
            has_error_payload = isinstance(p_data, dict) and "error" in p_data
            p_data_status = p_data.get("status") if isinstance(p_data, dict) else None
            p_val_status = p_meta.get("status", STATUS_VALID)
            p_errors = list(p_meta.get("errors", []))
            if has_error_payload and not p_errors:
                p_errors.append(str(p_data["error"]))
            p_retries = p_meta.get("retry_count", 0)

            # Check validity
            if has_error_payload or p_val_status != STATUS_VALID or p_data_status == STATUS_FAILED_VALIDATION or p_errors:
                all_platforms_valid = False
                p_status = STATUS_FAILED_VALIDATION
            else:
                # Content presence check
                if p_name == "pinterest":
                    has_content = bool(p_data.get("title", "").strip() and p_data.get("description", "").strip())
                    verified_image_url = product_image_url
                    if not is_valid_image_url(verified_image_url):
                        verified_image_url = p_data.get("image_url")
                    if not is_valid_image_url(verified_image_url):
                        p_errors.append("Pinterest requires a verified product image URL.")
                        has_content = False
                else:
                    content_text = (p_data.get("content") or p_data.get("text", "")).strip()
                    has_content = bool(content_text)
                
                if not has_content:
                    all_platforms_valid = False
                    p_status = STATUS_FAILED_VALIDATION
                    if not p_errors:
                        p_errors.append(f"Empty content for {p_name}.")
                else:
                    p_status = STATUS_VALID

            platform_entry: Dict[str, Any] = {
                "status": p_status
            }

            if p_name == "pinterest":
                platform_entry["title"] = p_data.get("title", "")
                platform_entry["description"] = p_data.get("description", "")
                if is_valid_image_url(product_image_url):
                    platform_entry["image_url"] = product_image_url
                elif is_valid_image_url(p_data.get("image_url")):
                    platform_entry["image_url"] = p_data["image_url"]
                if getattr(product, "affiliate_url", None):
                    platform_entry["link"] = product.affiliate_url
            else:
                content_text = p_data.get("content") or p_data.get("text", "")
                platform_entry["content"] = content_text
                if getattr(product, "image_url", None):
                    platform_entry["image_url"] = product.image_url

            platform_entry["validation"] = {
                "status": p_status,
                "retry_count": p_retries,
                "errors": p_errors
            }

            packaged_platforms[p_name] = platform_entry

        # Package is READY_FOR_REVIEW strictly if all supported platforms exist and are valid
        top_status = (
            STATUS_READY_FOR_REVIEW
            if (all_platforms_valid and len(packaged_platforms) == len(SUPPORTED_PLATFORMS))
            else STATUS_FAILED_VALIDATION
        )

        package = {
            "asin": asin,
            "status": top_status,
            "generated_at": generated_at,
            "model": model,
            "source": {
                "product_cache": cache_file_path,
                # Immutable user-supplied affiliate URL. source_url remains
                # available in the product cache for the resolved page URL.
                "affiliate_url": getattr(product, "original_affiliate_url", "") or product.affiliate_url or ""
            },
            "platforms": packaged_platforms
        }
        provider_diagnostics = social_data.get("provider_diagnostics")
        if isinstance(provider_diagnostics, list) and provider_diagnostics:
            package["provider_diagnostics"] = provider_diagnostics[-20:]

        return package

    @staticmethod
    def save_content_package(
        package: Dict[str, Any],
        cache_key: str,
        target_dir: Optional[Path] = None
    ) -> Path:
        """
        Save the Content Package to a JSON file in data/generated/.
        """
        if target_dir is None:
            target_dir = Path(__file__).resolve().parent.parent / "data" / "generated"

        target_dir.mkdir(parents=True, exist_ok=True)
        out_path = target_dir / f"{cache_key}_content_package.json"

        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(package, f, indent=2, ensure_ascii=False)

        return out_path

    @staticmethod
    def attach_verified_image(package: Dict[str, Any], image_url: str) -> bool:
        """Attach one verified image to every active platform entry in a package."""
        if not is_valid_image_url(image_url):
            return False
        package["image_url"] = image_url.strip()
        for platform_name in SUPPORTED_PLATFORMS:
            platform_entry = package.get("platforms", {}).get(platform_name)
            if isinstance(platform_entry, dict):
                platform_entry["image_url"] = image_url.strip()
        return True
