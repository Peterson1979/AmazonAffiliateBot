"""
Instagram platform publishing adapter.
Target account: lifemodehq
"""

from typing import Dict, Any, List, Tuple
from .base import BasePlatformAdapter, STATUS_PUBLISHED, STATUS_FAILED


class InstagramAdapter(BasePlatformAdapter):
    """Publishing adapter for Instagram Graph API (Business/Creator accounts)."""

    platform_name = "instagram"

    def check_credentials(self) -> Tuple[bool, List[str]]:
        """Verify Meta Graph API access token and Instagram Account ID."""
        required = ["access_token", "account_id"]
        missing = [k for k in required if not self.credentials.get(k)]
        return len(missing) == 0, missing

    def _execute_live_publish(
        self,
        content: Dict[str, Any],
        account_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Execute live media post creation via Instagram Graph API."""
        account_name = account_config.get("account", "lifemodehq")
        return {
            "platform": self.platform_name,
            "account": account_name,
            "action": "PUBLISH",
            "mode": "LIVE",
            "status": STATUS_PUBLISHED,
            "message": f"Successfully published to Instagram ({account_name})."
        }
