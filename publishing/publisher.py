"""
Social media publisher module for AmazonAffiliateBot (V4).
Consumes validated V3/V4 Content Packages and orchestrates publishing across all target accounts.
Supports DRY_RUN mode by default without requiring live credentials or network calls.
Active supported platforms: Facebook, Pinterest, and Threads.
"""

from typing import Dict, Any, Optional, List
from datetime import datetime, timezone
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
        and any(result.get(key) for key in ("post_id", "video_id", "pin_id", "photo_id", "permalink", "permalink_url", "pin_url"))
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
        target_destinations: Optional[List[str]] = None,
        declared_media_type: Optional[str] = None,
    ) -> Dict[str, Any]:
        """
        Publish a validated Content Package across all configured platforms (or targeted destinations).

        Args:
            package: Validated V3/V4 Content Package dictionary.
            dry_run: Optional override for DRY_RUN mode.
            prior_results: Optional previously confirmed results dictionary to avoid duplicates.
            target_destinations: Optional list of destination keys to publish in this pass
                                 (e.g. ['facebook', 'threads', 'pinterest'] or ['pinterest_lifemodehq']).
                                 If None, all configured destinations are evaluated.
            declared_media_type: Declared authoritative media type ('image' or 'video').
                                 Overrides any package-level media_type.

        Returns:
            Dict[str, Any]: Comprehensive publishing report.

        Raises:
            ValueError: If package status is not 'READY_FOR_REVIEW'.
        """
        is_dry_run = self.config.dry_run if dry_run is None else dry_run
        mode = "DRY_RUN" if is_dry_run else "LIVE"

        asin = (package.get("asin") or "").strip().upper()
        from publishing.blacklist import is_asin_blacklisted
        if is_asin_blacklisted(asin):
            target_keys = target_destinations or ["facebook", "pinterest", "pinterest_lifemodehq", "threads"]
            return {
                "asin": asin,
                "title": package.get("title", "Unknown"),
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "mode": mode,
                "status": STATUS_FAILED,
                "errors": [f"ASIN '{asin}' is permanently blacklisted. Publishing is prohibited."],
                "results": {
                    k: {
                        "platform": k,
                        "action": "BLOCKED",
                        "mode": mode,
                        "status": STATUS_FAILED,
                        "errors": [f"ASIN '{asin}' is permanently blacklisted."],
                        "message": f"Cannot publish to {k}: ASIN '{asin}' is permanently blacklisted."
                    }
                    for k in target_keys
                }
            }

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

        from pathlib import Path
        from publishing.images import is_valid_image_url, recover_verified_image_url
        from content.package import is_valid_video_url
        from content.pinterest_creative import get_selected_creative, has_creative_context

        project_root = Path(__file__).resolve().parent.parent

        effective_media_type = (
            (declared_media_type or package.get("media_type") or "").strip().lower()
        )

        raw_pkg_video = package.get("video_url")
        pkg_video_url = raw_pkg_video.strip() if is_valid_video_url(raw_pkg_video) else None

        if effective_media_type == "image":
            # An image item must never carry a video_url into platforms
            pkg_video_url = None
        elif effective_media_type == "video":
            # Video integrity check: verify video does not belong to a different ASIN
            video_meta_asin = (package.get("video") or {}).get("asin")
            if video_meta_asin and str(video_meta_asin).strip().upper() != asin:
                raise ValueError(
                    f"Video metadata ASIN '{video_meta_asin}' does not match package ASIN '{asin}'. Cross-ASIN video prohibited."
                )

        product_has_creative_context = bool(asin and has_creative_context(asin))
        selected_creative_info: Optional[Dict[str, Any]] = None
        creative_resolution_error: Optional[str] = None

        if product_has_creative_context:
            selected_creative_info = get_selected_creative(asin)
            if not selected_creative_info or not selected_creative_info.get("selected_image_path"):
                creative_resolution_error = (
                    f"No Pinterest creative selected for publishing for ASIN {asin}. "
                    "Please select either Gift Inspiration or Everyday Pick in the dashboard before publishing."
                )
            else:
                sel_path = selected_creative_info.get("selected_image_path")
                p = Path(sel_path)
                if not p.is_file():
                    p = project_root / sel_path
                if not p.is_file():
                    creative_resolution_error = (
                        f"Selected Flow creative image file '{sel_path}' for ASIN {asin} does not exist on disk."
                    )
                else:
                    selected_creative_info["resolved_image_path"] = str(p)

        targets = [
            ("facebook", "facebook", self.adapters.get("facebook"), self.config.accounts.get("facebook", {})),
            ("pinterest", "pinterest", self.pinterest_adapters.get("dailyideasforyou", self.adapters.get("pinterest")), self.config.pinterest_accounts.get("dailyideasforyou", self.config.accounts.get("pinterest", {}))),
            ("pinterest_lifemodehq", "pinterest", self.pinterest_adapters.get("lifemodehq"), self.config.pinterest_accounts.get("lifemodehq", {})),
            ("threads", "threads", self.adapters.get("threads"), self.config.accounts.get("threads", {})),
        ]

        if target_destinations is not None:
            allowed_destinations = set(target_destinations)
            # Preserve prior results for destinations not being targeted in this pass
            for res_key, _, _, _ in targets:
                if res_key not in allowed_destinations and res_key in prior_results:
                    results[res_key] = prior_results[res_key]
            targets = [t for t in targets if t[0] in allowed_destinations]

        for result_key, platform_name, adapter, account_cfg in targets:
            p_content = platforms_dict.get(result_key)
            if not p_content:
                base_content = platforms_dict.get(platform_name)
                if base_content:
                    acc_name = (account_cfg.get("account") or "").lower()
                    acc_override = (base_content.get("accounts") or {}).get(acc_name)
                    if acc_override:
                        p_content = dict(base_content)
                        p_content.update(acc_override)
                    elif "lifemodehq" in (base_content.get("accounts") or {}) and "lifemodehq" in result_key:
                        p_content = dict(base_content)
                        p_content.update(base_content["accounts"]["lifemodehq"])
                    else:
                        p_content = base_content

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

            account_cfg_to_use = dict(account_cfg)
            account_cfg_to_use["asin"] = asin

            if effective_media_type == "image":
                target_video_url = None
                p_content = dict(p_content)
                p_content.pop("video_url", None)
                p_content["media_type"] = "image"
                account_cfg_to_use["media_type"] = "image"
                account_cfg_to_use.pop("video_url", None)
            else:
                raw_plat_video = p_content.get("video_url")
                target_video_url = (
                    raw_plat_video.strip()
                    if is_valid_video_url(raw_plat_video)
                    else pkg_video_url
                )
                if not target_video_url and effective_media_type == "video":
                    results[result_key] = {
                        "platform": platform_name,
                        "account": account_cfg.get("account", ""),
                        "action": "PUBLISH",
                        "mode": mode,
                        "status": STATUS_FAILED,
                        "errors": [
                            f"Declared media_type is 'video' but no valid video URL exists for {platform_name}. Image fallback prohibited."
                        ],
                    }
                    all_succeeded = False
                    continue

            if target_video_url:
                # Video publishing path: route public HTTPS R2 video_url to each platform
                # while preserving existing product image_url as the Pinterest video cover.
                p_content = dict(p_content)
                p_content["video_url"] = target_video_url
                p_content["media_type"] = "video"
                account_cfg_to_use["media_type"] = "video"
                account_cfg_to_use["video_url"] = target_video_url

                variants = p_content.get("variants") or {}
                vid_var = variants.get("video") or {}
                if platform_name == "pinterest":
                    if vid_var.get("description"):
                        p_content["description"] = vid_var["description"]
                    if vid_var.get("title"):
                        p_content["title"] = vid_var["title"]
                else:
                    if vid_var.get("content") or vid_var.get("text"):
                        p_content["content"] = vid_var.get("content") or vid_var.get("text")
                        p_content["text"] = p_content["content"]

                cover_candidate = p_content.get("image_url")
                if not is_valid_image_url(cover_candidate):
                    recovered_img = recover_verified_image_url(package)
                    if recovered_img:
                        cover_candidate = recovered_img
                        p_content["image_url"] = recovered_img

                if platform_name == "pinterest":
                    if not is_valid_image_url(cover_candidate):
                        results[result_key] = {
                            "platform": platform_name,
                            "account": account_cfg.get("account", ""),
                            "action": "PUBLISH",
                            "mode": mode,
                            "status": STATUS_FAILED,
                            "errors": [
                                "Blocked: Pinterest video Pin requires a verified HTTPS cover image URL (image_url) from the product package or same-ASIN cache."
                            ],
                        }
                        all_succeeded = False
                        continue
                    p_content["cover_image_url"] = cover_candidate.strip()

                account_cfg_to_use["require_image"] = (platform_name == "pinterest")
            else:
                # Existing image/text publishing path: strip any invalid/empty video_url
                # so adapters remain 100% on the image/text workflow.
                if "video_url" in p_content:
                    p_content = dict(p_content)
                    p_content.pop("video_url", None)

                variants = p_content.get("variants") or {}
                img_var = variants.get("image") or {}
                if platform_name == "pinterest":
                    if img_var.get("description"):
                        p_content["description"] = img_var["description"]
                    if img_var.get("title"):
                        p_content["title"] = img_var["title"]
                else:
                    if img_var.get("content") or img_var.get("text"):
                        p_content["content"] = img_var.get("content") or img_var.get("text")
                        p_content["text"] = p_content["content"]

                if product_has_creative_context:
                    if creative_resolution_error:
                        results[result_key] = {
                            "platform": platform_name,
                            "account": account_cfg.get("account", ""),
                            "action": "PUBLISH",
                            "mode": mode,
                            "status": STATUS_FAILED,
                            "errors": [creative_resolution_error],
                        }
                        all_succeeded = False
                        continue

                    p_content = dict(p_content)
                    p_content["selected_creative"] = selected_creative_info
                    p_content["image_path"] = selected_creative_info.get("resolved_image_path") or selected_creative_info.get("selected_image_path")
                    p_content["image_url"] = selected_creative_info.get("uploaded_image_url") or selected_creative_info.get("selected_image_path")

                    if platform_name == "threads":
                        img_val = p_content.get("image_url")
                        if not is_valid_image_url(img_val):
                            local_img = p_content.get("image_path")
                            if local_img and Path(local_img).is_file():
                                from publishing.r2_storage import upload_creative_to_r2
                                r2_res = upload_creative_to_r2(
                                    asin=asin,
                                    local_image_path=local_img,
                                    dry_run=is_dry_run,
                                )
                                if r2_res.get("success") and r2_res.get("public_url"):
                                    p_content["image_url"] = r2_res["public_url"]
                                    img_val = p_content["image_url"]

                        if not is_valid_image_url(img_val):
                            results[result_key] = {
                                "platform": platform_name,
                                "account": account_cfg.get("account", ""),
                                "action": "PUBLISH",
                                "mode": mode,
                                "status": STATUS_FAILED,
                                "errors": [
                                    "Threads requires a publicly accessible HTTPS image URL for creative image publishing. "
                                    f"Local creative path '{img_val}' cannot be fetched by Threads API."
                                ],
                            }
                            all_succeeded = False
                            continue
                else:
                    # Resolve only from this package or its same-ASIN product cache.
                    if not p_content.get("image_url"):
                        img_candidate = recover_verified_image_url(package)
                        if img_candidate:
                            p_content = dict(p_content)
                            p_content["image_url"] = img_candidate

                    if platform_name in ("pinterest", "threads"):
                        img_val = p_content.get("image_url")
                        if not is_valid_image_url(img_val):
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

                # Facebook supports a text-only Page feed post when no verified
                # image is available. Pinterest and Threads require an image.
                account_cfg_to_use["require_image"] = platform_name in ("pinterest", "threads")

            # Route to platform adapter with independent failure isolation
            try:
                pub_res = adapter.publish(
                    content=p_content,
                    account_config=account_cfg_to_use,
                    dry_run=is_dry_run
                )
            except Exception as exc:
                pub_res = {
                    "platform": platform_name,
                    "account": account_cfg.get("account", ""),
                    "action": "PUBLISH",
                    "mode": mode,
                    "status": STATUS_FAILED,
                    "errors": [f"Unexpected adapter exception for '{result_key}': {exc}"],
                    "message": f"Publishing failed for '{result_key}' due to unexpected adapter error: {exc}",
                }
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
        asin = package.get("asin")

        from content.package import is_valid_video_url
        from publishing.images import is_valid_image_url, recover_verified_image_url

        raw_pin_vid = pin_content.get("video_url")
        raw_pkg_vid = package.get("video_url")
        target_video_url = (
            raw_pin_vid.strip()
            if is_valid_video_url(raw_pin_vid)
            else (raw_pkg_vid.strip() if is_valid_video_url(raw_pkg_vid) else None)
        )

        if target_video_url:
            cover_url = pin_content.get("image_url")
            if not is_valid_image_url(cover_url):
                cover_url = recover_verified_image_url(package)
            if not is_valid_image_url(cover_url):
                return {
                    "status": STATUS_FAILED,
                    "errors": ["Pinterest video Pin requires a verified HTTPS cover image URL."],
                }
            pin_content = dict(pin_content)
            pin_content["video_url"] = target_video_url
            pin_content["media_type"] = "video"
            pin_content["image_url"] = cover_url.strip()
            pin_content["cover_image_url"] = cover_url.strip()
            return adapter.publish(pin_content, account_cfg, dry_run=self.config.dry_run if dry_run is None else dry_run)

        if "video_url" in pin_content:
            pin_content = dict(pin_content)
            pin_content.pop("video_url", None)

        from content.pinterest_creative import get_selected_creative, has_creative_context
        if asin and has_creative_context(asin):
            selected_info = get_selected_creative(asin)
            if not selected_info or not selected_info.get("selected_image_path"):
                return {
                    "status": STATUS_FAILED,
                    "errors": [
                        f"No Pinterest creative selected for publishing for ASIN {asin}. "
                        "Please select either Gift Inspiration or Everyday Pick in the dashboard before publishing."
                    ],
                }
            pin_content = dict(pin_content)
            pin_content["selected_creative"] = selected_info
            pin_content["image_path"] = selected_info["selected_image_path"]
            if selected_info.get("uploaded_image_url"):
                pin_content["image_url"] = selected_info["uploaded_image_url"]
            elif selected_info.get("selected_image_path"):
                pin_content["image_url"] = selected_info["selected_image_path"]

        image_url = pin_content.get("image_url") or recover_verified_image_url(package)
        has_valid_creative_image = bool(
            pin_content.get("image_path")
            or (isinstance(image_url, str) and (image_url.startswith("/creatives/") or "creatives" in image_url))
        )
        if not (is_valid_image_url(image_url) or has_valid_creative_image):
            return {"status": STATUS_FAILED, "errors": ["Pinterest requires a verified HTTPS image URL."]}
        pin_content = dict(pin_content)
        pin_content["image_url"] = image_url
        return adapter.publish(pin_content, account_cfg, dry_run=self.config.dry_run if dry_run is None else dry_run)
