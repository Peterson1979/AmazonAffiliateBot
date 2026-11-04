"""
Pinterest platform publishing adapter.
Supports independently configured Pinterest accounts and boards.
"""

import base64
from pathlib import Path
import re
from typing import Dict, Any, List, Optional, Tuple
import requests
from .base import BasePlatformAdapter, STATUS_PUBLISHED, STATUS_FAILED

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


class PinterestAdapter(BasePlatformAdapter):
    """Publishing adapter for Pinterest API v5 (Pins & Boards)."""

    platform_name = "pinterest"

    def check_credentials(self) -> Tuple[bool, List[str]]:
        """Verify Pinterest API v5 access token."""
        required = ["access_token"]
        missing = [k for k in required if not self.credentials.get(k)]
        return len(missing) == 0, missing

    def resolve_board_id(self, board_name: str) -> Tuple[Optional[str], Optional[str]]:
        """Resolve a board ID for this account; IDs are never shared across accounts."""
        access_token = self.credentials.get("access_token", "")
        if not access_token:
            return None, "Missing Pinterest access token."
        try:
            resp = requests.get(
                "https://api.pinterest.com/v5/boards",
                headers={"Authorization": f"Bearer {access_token}"},
                params={"page_size": 100},
                timeout=30,
            )
            if resp.status_code != 200:
                return None, f"Pinterest board lookup failed with HTTP {resp.status_code}."
            boards = resp.json().get("items", [])
            for board in boards:
                if board.get("name") == board_name:
                    return str(board.get("id")), None
            return None, f"Pinterest board '{board_name}' was not found for this account."
        except Exception as exc:
            return None, f"Pinterest board lookup failed: {exc}"

    def _execute_live_publish(
        self,
        content: Dict[str, Any],
        account_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Execute live pin creation via Pinterest API v5."""
        account_name = account_config.get("account", "dailyideasforyou")
        board_name = account_config.get("board", "Shopping ideas")
        configured_board_id = account_config.get("board_id")
        board_id = str(configured_board_id) if configured_board_id else ""
        access_token = self.credentials.get("access_token", "")

        title = str(content.get("title", ""))[:100]
        description = str(content.get("description", ""))[:800]
        image_url = str(content.get("image_url") or account_config.get("image_url", "")).strip()
        image_path = content.get("image_path")
        link = content.get("link") or account_config.get("link", "")

        if not link:
            url_match = re.search(r'https?://\S+', description)
            if url_match:
                link = url_match.group(0).rstrip(".,)")

        # Resolve local creative image file vs public HTTPS URL
        local_file: Optional[Path] = None
        if image_path:
            p = Path(image_path)
            if p.is_file():
                local_file = p
            elif (PROJECT_ROOT / image_path).is_file():
                local_file = PROJECT_ROOT / image_path
        if not local_file and image_url and not (image_url.startswith("http://") or image_url.startswith("https://")):
            clean_rel = image_url.lstrip("/\\")
            if clean_rel.startswith("creatives/"):
                cand = PROJECT_ROOT / "data" / clean_rel
                if cand.is_file():
                    local_file = cand
            if not local_file:
                cand = PROJECT_ROOT / clean_rel
                if cand.is_file():
                    local_file = cand

        media_source: Optional[Dict[str, Any]] = None
        if local_file and local_file.is_file():
            try:
                with open(local_file, "rb") as f:
                    b64_data = base64.b64encode(f.read()).decode("ascii")
                suffix = local_file.suffix.lower()
                content_type = "image/png" if suffix == ".png" else "image/jpeg"
                media_source = {
                    "source_type": "image_base64",
                    "content_type": content_type,
                    "data": b64_data,
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
                    "errors": [f"Failed to read local image file for Pinterest base64 upload: {e}"],
                    "message": f"Cannot create pin: local image read failed: {e}",
                }
        elif image_url and (image_url.startswith("https://") or image_url.startswith("http://")):
            media_source = {
                "source_type": "image_url",
                "url": image_url,
            }
        else:
            return {
                "platform": self.platform_name,
                "account": account_name,
                "board": board_name,
                "board_id": board_id,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": [
                    f"Pinterest requires a valid public HTTPS image URL or an accessible local image file for base64 upload. Received: '{image_url or image_path}'."
                ],
                "message": "Cannot create pin: invalid or missing image source for Pinterest.",
            }

        if not board_id:
            board_id, board_error = self.resolve_board_id(board_name)
            if not board_id:
                return {
                    "platform": self.platform_name,
                    "account": account_name,
                    "board": board_name,
                    "board_id": "",
                    "action": "PUBLISH",
                    "mode": "LIVE",
                    "status": STATUS_FAILED,
                    "errors": [board_error or f"Pinterest board '{board_name}' could not be resolved."],
                    "message": f"Cannot create pin: board '{board_name}' could not be resolved for this account."
                }

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json"
        }

        payload: Dict[str, Any] = {
            "board_id": board_id,
            "media_source": media_source,
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
                    "api_response": pin_data,
                    "message": f"Successfully created pin {pin_id} on Pinterest board '{board_name}' ({account_name})."
                }
            else:
                error_detail = resp.text
                err_json = None
                try:
                    err_json = resp.json()
                    error_detail = err_json.get("message") or resp.text
                except Exception:
                    pass
                if resp.status_code == 401:
                    return {
                        "platform": self.platform_name,
                        "account": account_name,
                        "board": board_name,
                        "board_id": board_id,
                        "action": "PUBLISH",
                        "mode": "LIVE",
                        "status": STATUS_FAILED,
                        "http_status": 401,
                        "api_response": err_json if isinstance(err_json, dict) else None,
                        "errors": [
                            f"Pinterest authentication failed (HTTP 401) for account '{account_name}'. "
                            "The OAuth access token is expired or invalid. Reauthorization is required."
                        ],
                        "message": f"Pinterest authentication failed for '{account_name}': token is expired or invalid. Reauthorization required.",
                    }
                return {
                    "platform": self.platform_name,
                    "account": account_name,
                    "board": board_name,
                    "board_id": board_id,
                    "action": "PUBLISH",
                    "mode": "LIVE",
                    "status": STATUS_FAILED,
                    "http_status": resp.status_code,
                    "api_response": err_json if isinstance(err_json, dict) else None,
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

