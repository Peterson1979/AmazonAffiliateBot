"""
Pinterest platform publishing adapter.
Target account: dailyideasforyou
Target board: Shopping ideas
"""

import re
from typing import Dict, Any, List, Tuple
import requests
from .base import BasePlatformAdapter, STATUS_PUBLISHED, STATUS_FAILED


class PinterestAdapter(BasePlatformAdapter):
    """Publishing adapter for Pinterest API v5 (Pins & Boards)."""

    platform_name = "pinterest"

    def check_credentials(self) -> Tuple[bool, List[str]]:
        """Verify Pinterest API v5 access token."""
        required = ["access_token"]
        missing = [k for k in required if not self.credentials.get(k)]
        return len(missing) == 0, missing

    def _execute_live_publish(
        self,
        content: Dict[str, Any],
        account_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Execute live pin creation via Pinterest API v5."""
        account_name = account_config.get("account", "dailyideasforyou")
        board_name = account_config.get("board", "Shopping ideas")
        board_id = str(account_config.get("board_id", "1033646620665866548"))
        access_token = self.credentials.get("access_token", "")

        title = str(content.get("title", ""))[:100]
        description = str(content.get("description", ""))[:800]
        image_url = content.get("image_url") or account_config.get("image_url", "")
        link = content.get("link") or account_config.get("link", "")

        if not link:
            url_match = re.search(r'https?://\S+', description)
            if url_match:
                link = url_match.group(0).rstrip(".,)")

        if not image_url:
            return {
                "platform": self.platform_name,
                "account": account_name,
                "board": board_name,
                "board_id": board_id,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": ["Missing required image_url for Pinterest Pin creation."],
                "message": f"Cannot create pin on board '{board_name}': missing image_url."
            }

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }

        payload: Dict[str, Any] = {
            "board_id": board_id,
            "media_source": {
                "source_type": "image_url",
                "url": image_url
            },
            "title": title,
            "description": description
        }
        if link:
            payload["link"] = link

        try:
            resp = requests.post(
                "https://api.pinterest.com/v5/pins",
                headers=headers,
                json=payload,
                timeout=30
            )

            if resp.status_code in (200, 201):
                pin_data = resp.json()
                pin_id = pin_data.get("id", "")
                return {
                    "platform": self.platform_name,
                    "account": account_name,
                    "board": board_name,
                    "board_id": board_id,
                    "pin_id": pin_id,
                    "pin_url": f"https://www.pinterest.com/pin/{pin_id}/",
                    "title": title,
                    "link": link,
                    "image_url": image_url,
                    "action": "PUBLISH",
                    "mode": "LIVE",
                    "status": STATUS_PUBLISHED,
                    "http_status": resp.status_code,
                    "message": f"Successfully created pin {pin_id} on Pinterest board '{board_name}' ({account_name})."
                }
            else:
                error_detail = resp.text
                try:
                    err_json = resp.json()
                    error_detail = err_json.get("message") or resp.text
                except Exception:
                    pass
                return {
                    "platform": self.platform_name,
                    "account": account_name,
                    "board": board_name,
                    "board_id": board_id,
                    "action": "PUBLISH",
                    "mode": "LIVE",
                    "status": STATUS_FAILED,
                    "http_status": resp.status_code,
                    "errors": [f"Pinterest API error (HTTP {resp.status_code}): {error_detail}"],
                    "message": f"Pinterest API rejected pin with HTTP {resp.status_code}."
                }
        except Exception as e:
            return {
                "platform": self.platform_name,
                "account": account_name,
                "board": board_name,
                "board_id": board_id,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": [f"Network exception calling Pinterest API: {str(e)}"],
                "message": f"Pinterest API request failed: {str(e)}"
            }

