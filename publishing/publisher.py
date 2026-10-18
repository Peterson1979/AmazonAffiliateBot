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


def _confirmed_live_result(result: Dict[str, Any]) -> bool:
    """Only live responses with platform evidence are safe to reuse as success."""
    return bool(
        isinstance(result, dict)
        and result.get("status") == STATUS_PUBLISHED
        and result.get("mode") == "LIVE"
        and any(result.get(key) for key in ("post_id", "pin_id", "photo_id", "permalink", "permalink_url", "pin_url"))
    )


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
        self.pinterest_adapters: Dict[str, PinterestAdapter] = {}
        for account_key, account_cfg in self.config.pinterest_accounts.items():
            credentials = self.config.credentials.get("pinterest_accounts", {}).get(account_key, {})
            self.pinterest_adapters[account_key] = PinterestAdapter(credentials=credentials)

    def publish_package(
        self,
        package: Dict[str, Any],
        dry_run: Optional[bool] = None,
        prior_results: Optional[Dict[str, Any]] = None,
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
        prior_results = prior_results or {}
        results: Dict[str, Any] = {}
        all_succeeded = True

        from publishing.images import is_valid_image_url, recover_verified_image_url

        targets = [
            ("facebook", "facebook", self.adapters.get("facebook"), self.config.accounts.get("facebook", {})),
            ("pinterest", "pinterest", self.pinterest_adapters.get("dailyideasforyou", self.adapters.get("pinterest")), self.config.pinterest_accounts.get("dailyideasforyou", self.config.accounts.get("pinterest", {}))),
            ("pinterest_lifemodehq", "pinterest", self.pinterest_adapters.get("lifemodehq"), self.config.pinterest_accounts.get("lifemodehq", {})),
            ("threads", "threads", self.adapters.get("threads"), self.config.accounts.get("threads", {})),
        ]

        for result_key, platform_name, adapter, account_cfg in targets:
            p_content = platforms_dict.get(platform_name)

            previous = prior_results.get(result_key) or {}
            if _confirmed_live_result(previous):
                duplicate_safe_result = dict(previous)
                duplicate_safe_result["action"] = "SKIP_DUPLICATE"
                results[result_key] = duplicate_safe_result
                continue

            if not adapter or not p_content:
                results[result_key] = {
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
                results[result_key] = {
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

            if platform_name in ("pinterest", "threads"):
                if not is_valid_image_url(p_content.get("image_url")):
                    results[result_key] = {
                        "platform": platform_name,
                        "account": account_cfg.get("account", ""),
                        "action": "PUBLISH",
                        "mode": mode,
                        "status": STATUS_FAILED,
                        "errors": [
                            f"Blocked: {platform_name.capitalize()} requires a verified HTTPS image URL from the product package or same-ASIN cache."
                        ],
                    }
                    all_succeeded = False
                    continue

            # Route to platform adapter
            account_cfg_to_use = dict(account_cfg)
            # Facebook supports a text-only Page feed post when no verified
            # image is available. Pinterest and Threads require an image.
            account_cfg_to_use["require_image"] = platform_name in ("pinterest", "threads")
            pub_res = adapter.publish(
                content=p_content,
                account_config=account_cfg_to_use,
                dry_run=is_dry_run
            )
            results[result_key] = pub_res

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

    def publish_pinterest_account(
        self,
        package: Dict[str, Any],
        account_key: str,
        dry_run: Optional[bool] = None,
        prior_result: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Publish only one configured Pinterest account, with retry-safe result tracking."""
        account_cfg = self.config.pinterest_accounts.get(account_key)
        adapter = self.pinterest_adapters.get(account_key)
        if not account_cfg or not adapter:
            return {"status": STATUS_FAILED, "errors": [f"Unknown Pinterest account '{account_key}'."]}
        if (prior_result or {}).get("status") == STATUS_PUBLISHED:
            result = dict(prior_result)
            result["action"] = "SKIP_DUPLICATE"
            return result
        if package.get("status") != "READY_FOR_REVIEW":
            return {"status": STATUS_FAILED, "errors": ["Content package is not READY_FOR_REVIEW."]}
        pin_content = package.get("platforms", {}).get("pinterest") or {}
        from publishing.images import is_valid_image_url, recover_verified_image_url
        image_url = pin_content.get("image_url") or recover_verified_image_url(package)
        if not is_valid_image_url(image_url):
            return {"status": STATUS_FAILED, "errors": ["Pinterest requires a verified HTTPS image URL."]}
        pin_content = dict(pin_content)
        pin_content["image_url"] = image_url
        return adapter.publish(pin_content, account_cfg, dry_run=self.config.dry_run if dry_run is None else dry_run)
