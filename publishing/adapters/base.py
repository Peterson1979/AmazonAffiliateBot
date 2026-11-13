"""
Base platform adapter interface for social media publishing.
"""

from typing import Dict, Any, List, Tuple, Optional
from abc import ABC, abstractmethod


STATUS_WOULD_PUBLISH = "WOULD_PUBLISH"
STATUS_PUBLISHED = "PUBLISHED"
STATUS_FAILED = "FAILED"
STATUS_SKIPPED = "SKIPPED"


class BasePlatformAdapter(ABC):
    """Abstract base class for social media platform publishing adapters."""

    platform_name: str = ""

    def __init__(self, credentials: Optional[Dict[str, Any]] = None):
        self.credentials = credentials or {}

    @abstractmethod
    def check_credentials(self) -> Tuple[bool, List[str]]:
        """Verify if required API credentials exist for live publishing."""
        pass

    def publish(
        self,
        content: Dict[str, Any],
        account_config: Dict[str, Any],
        dry_run: bool = True
    ) -> Dict[str, Any]:
        """
        Publish content to the target platform or simulate in DRY_RUN mode.

        Args:
            content: Validated platform content dictionary.
            account_config: Target account configuration (e.g. account username, board).
            dry_run: If True, simulate without making any network or browser actions.

        Returns:
            Dict[str, Any]: Structured publishing result.
        """
        account_name = account_config.get("account", "")
        mode = "DRY_RUN" if dry_run else "LIVE"

        # 1. Permanent blacklist enforcement at final platform boundary
        from publishing.blacklist import is_asin_blacklisted
        asin = (content.get("asin") or account_config.get("asin") or "").strip().upper()
        if is_asin_blacklisted(asin):
            return {
                "platform": self.platform_name,
                "account": account_name,
                "action": "BLOCKED",
                "mode": mode,
                "status": STATUS_FAILED,
                "errors": [f"ASIN '{asin}' is permanently blacklisted. Publishing is prohibited."],
                "message": f"Cannot publish to {self.platform_name}: ASIN '{asin}' is permanently blacklisted."
            }

        # 2. Strict media type validation
        declared_media = (content.get("media_type") or account_config.get("media_type") or "").strip().lower()
        if declared_media == "image":
            # An image item must never carry a video_url into platform adapters
            if "video_url" in content:
                content = dict(content)
                content.pop("video_url", None)
            content["media_type"] = "image"
        elif declared_media == "video":
            video_url = str(content.get("video_url") or account_config.get("video_url") or "").strip()
            if not video_url:
                return {
                    "platform": self.platform_name,
                    "account": account_name,
                    "action": "PUBLISH",
                    "mode": mode,
                    "status": STATUS_FAILED,
                    "errors": [f"Declared media_type is 'video' but video_url is missing for {self.platform_name}. Fallback prohibited."],
                    "message": f"Cannot publish to {self.platform_name}: missing video_url for video publication."
                }

        # 3. Threads image URL validation (fails closed in both dry-run and live)
        if self.platform_name == "threads":
            target_image_url = str(content.get("image_url") or account_config.get("image_url") or "").strip()
            if target_image_url and not (target_image_url.startswith("https://") or target_image_url.startswith("http://")):
                return {
                    "platform": self.platform_name,
                    "account": account_name,
                    "action": "PUBLISH",
                    "mode": mode,
                    "status": STATUS_FAILED,
                    "errors": [
                        f"Threads requires a publicly accessible HTTPS image URL. "
                        f"Local relative path or file '{target_image_url}' cannot be retrieved by Threads API."
                    ],
                    "message": f"Cannot publish to Threads account '{account_name}': image_url must be a publicly accessible HTTPS URL.",
                }

        if dry_run:
            result = {
                "platform": self.platform_name,
                "account": account_name,
                "action": "PUBLISH",
                "mode": mode,
                "status": STATUS_WOULD_PUBLISH,
                "message": f"Simulated publish to {self.platform_name} ({account_name}). No real request sent."
            }
            if declared_media == "image":
                result["media_type"] = "IMAGE"
                if content.get("image_url"):
                    result["image_url"] = content.get("image_url")
                if content.get("image_path"):
                    result["image_path"] = content.get("image_path")
            elif content.get("video_url"):
                result["video_url"] = content.get("video_url")
                result["media_type"] = "VIDEO"
            else:
                result["media_type"] = "IMAGE"
            if self.platform_name == "pinterest":
                result["board"] = account_config.get("board", "")
                result["title"] = content.get("title", "")
                result["link"] = content.get("link", "")
                if content.get("image_path"):
                    result["image_path"] = content.get("image_path")
                if content.get("image_url"):
                    result["image_url"] = content.get("image_url")
                if content.get("cover_image_url"):
                    result["cover_image_url"] = content.get("cover_image_url")
            return result

        # Live publishing: verify credentials first
        has_creds, missing = self.check_credentials()
        if not has_creds:
            return {
                "platform": self.platform_name,
                "account": account_name,
                "action": "PUBLISH",
                "mode": mode,
                "status": STATUS_FAILED,
                "errors": [f"Missing required credentials for {self.platform_name}: {', '.join(missing)}"],
                "message": f"Cannot publish to {self.platform_name}: missing credentials."
            }

        return self._execute_live_publish(content, account_config)

    @abstractmethod
    def _execute_live_publish(
        self,
        content: Dict[str, Any],
        account_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Execute the actual API call to publish content live (when credentials are present)."""
        pass
