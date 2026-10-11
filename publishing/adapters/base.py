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

        if dry_run:
            result = {
                "platform": self.platform_name,
                "account": account_name,
                "action": "PUBLISH",
                "mode": mode,
                "status": STATUS_WOULD_PUBLISH,
                "message": f"Simulated publish to {self.platform_name} ({account_name}). No real request sent."
            }
            if self.platform_name == "pinterest":
                result["board"] = account_config.get("board", "")
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
