"""
Social media publisher module for AmazonAffiliateBot (V4).
Consumes validated V3/V4 Content Packages and orchestrates publishing across all target accounts.
Supports DRY_RUN mode by default without requiring live credentials or network calls.
Active supported platforms: Facebook, Pinterest, and Threads.
"""

from typing import Dict, Any, Optional, List
from .config import PublishingConfig, DEFAULT_TARGET_ACCOUNTS
from .adapters import (
    FacebookAdapter,
    PinterestAdapter,
    ThreadsAdapter,
    BasePlatformAdapter,
    STATUS_WOULD_PUBLISH,
    STATUS_PUBLISHED,
    STATUS_FAILED,
    STATUS_SKIPPED
)

SUPPORTED_PLATFORMS = ("facebook", "pinterest", "threads")


class SocialPublisher:
    """
    Publishing orchestrator that consumes validated V3/V4 Content Packages
    and routes them to target platform adapters.
    """

    def __init__(
        self,
        config: Optional[PublishingConfig] = None,
        dry_run: Optional[bool] = None
    ):
        self.config = config or PublishingConfig()
        if dry_run is not None:
            self.config.dry_run = dry_run

        # Instantiate adapters with platform-specific credentials
        self.adapters: Dict[str, BasePlatformAdapter] = {
            "facebook": FacebookAdapter(credentials=self.config.credentials.get("facebook")),
            "pinterest": PinterestAdapter(credentials=self.config.credentials.get("pinterest")),
            "threads": ThreadsAdapter(credentials=self.config.credentials.get("threads")),
        }

    def publish_package(
        self,
        package: Dict[str, Any],
        dry_run: Optional[bool] = None
    ) -> Dict[str, Any]:
        """
        Publish a validated Content Package across all configured platforms.

        Args:
            package: Validated V3/V4 Content Package dictionary.
            dry_run: Optional override for DRY_RUN mode.

        Returns:
            Dict[str, Any]: Comprehensive publishing report.

        Raises:
            ValueError: If package status is not 'READY_FOR_REVIEW'.
        """
        is_dry_run = self.config.dry_run if dry_run is None else dry_run
        mode = "DRY_RUN" if is_dry_run else "LIVE"

        package_status = package.get("status")
        if package_status != "READY_FOR_REVIEW":
            raise ValueError(
                f"Publisher rejected content package: status is '{package_status}'. "
                "Package must be 'READY_FOR_REVIEW' to publish."
            )

        platforms_dict = package.get("platforms", {})
        results: Dict[str, Any] = {}
        all_succeeded = True

        # Fail closed before routing anything: a missing Pinterest image must
        # not result in a partial live publication on another platform.
        from publishing.images import is_valid_image_url, recover_verified_image_url
        pinterest_content = platforms_dict.get("pinterest") or {}
        if not pinterest_content.get("image_url"):
            recovered = recover_verified_image_url(package)
            if recovered:
                pinterest_content["image_url"] = recovered
        if not is_valid_image_url(pinterest_content.get("image_url")):
            reason = "Blocked: Pinterest requires a verified HTTPS image URL from the product package or same-ASIN cache."
            for platform_name in SUPPORTED_PLATFORMS:
                account_cfg = self.config.accounts.get(platform_name, {})
                results[platform_name] = {
                    "platform": platform_name,
                    "account": account_cfg.get("account", ""),
                    "action": "PUBLISH",
                    "mode": mode,
                    "status": STATUS_FAILED if platform_name == "pinterest" else STATUS_SKIPPED,
                    "errors": [reason] if platform_name == "pinterest" else ["Skipped because item image preflight was blocked."],
                }
            return {
                "asin": package.get("asin", ""),
                "package_status": package_status,
                "mode": mode,
                "status": STATUS_FAILED,
                "results": results,
            }

        for platform_name in SUPPORTED_PLATFORMS:
            adapter = self.adapters.get(platform_name)
            p_content = platforms_dict.get(platform_name)
            account_cfg = self.config.accounts.get(platform_name, {})

            if not adapter or not p_content:
                results[platform_name] = {
                    "platform": platform_name,
                    "account": account_cfg.get("account", ""),
                    "action": "PUBLISH",
                    "mode": mode,
                    "status": STATUS_FAILED,
                    "errors": [f"Missing platform content or adapter for '{platform_name}'."]
                }
                all_succeeded = False
                continue

            # Verify that individual platform is marked VALID in package
            if p_content.get("status") != "VALID":
                results[platform_name] = {
                    "platform": platform_name,
                    "account": account_cfg.get("account", ""),
                    "action": "PUBLISH",
                    "mode": mode,
                    "status": STATUS_FAILED,
                    "errors": [f"Platform '{platform_name}' status is not 'VALID' in content package."]
                }
                all_succeeded = False
                continue

            # Resolve only from this package or its same-ASIN product cache.
            if not p_content.get("image_url"):
                img_candidate = recover_verified_image_url(package)
                if img_candidate:
                    p_content["image_url"] = img_candidate

            if platform_name == "pinterest":
                from publishing.images import is_valid_image_url
                if not is_valid_image_url(p_content.get("image_url")):
                    results[platform_name] = {
                        "platform": platform_name,
                        "account": account_cfg.get("account", ""),
                        "action": "PUBLISH",
                        "mode": mode,
                        "status": STATUS_FAILED,
                        "errors": ["Blocked: Pinterest requires a verified HTTPS image URL from the product package or same-ASIN cache."],
                    }
                    all_succeeded = False
                    continue

            # Route to platform adapter
            account_cfg_to_use = dict(account_cfg)
            account_cfg_to_use["require_image"] = True
            pub_res = adapter.publish(
                content=p_content,
                account_config=account_cfg_to_use,
                dry_run=is_dry_run
            )
            results[platform_name] = pub_res

            if pub_res.get("status") not in (STATUS_WOULD_PUBLISH, STATUS_PUBLISHED):
                all_succeeded = False

        overall_status = (
            (STATUS_WOULD_PUBLISH if is_dry_run else STATUS_PUBLISHED)
            if all_succeeded
            else STATUS_FAILED
        )

        return {
            "asin": package.get("asin", ""),
            "package_status": package_status,
            "mode": mode,
            "status": overall_status,
            "results": results
        }
