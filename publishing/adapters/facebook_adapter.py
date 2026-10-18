"""
Facebook platform publishing adapter.
Target account: LifeModeHQ
Publishes posts to Facebook Page feed via Meta Graph API v19.0.
"""

from typing import Dict, Any, List, Tuple
import logging
import requests
from .base import BasePlatformAdapter, STATUS_PUBLISHED, STATUS_FAILED

LOGGER = logging.getLogger(__name__)


def _safe_graph_error_message(value: Any, access_token: str = "") -> str:
    """Return a bounded, single-line Graph error message with secrets redacted."""
    message = " ".join(str(value or "").split())
    if access_token:
        message = message.replace(access_token, "[REDACTED]")
    return message[:500]


def _log_graph_failure(http_status: int, error: Any, access_token: str = "") -> None:
    """Log safe Facebook diagnostics without tokens or request headers."""
    if isinstance(error, dict):
        code = error.get("code")
        subcode = error.get("error_subcode", error.get("subcode"))
        message = error.get("message", "")
    else:
        code = subcode = None
        message = error
    LOGGER.error(
        "Facebook Graph API failure: http_status=%s code=%s subcode=%s message=%s",
        http_status,
        code,
        subcode,
        _safe_graph_error_message(message, access_token),
    )


class FacebookAdapter(BasePlatformAdapter):
    """Publishing adapter for Facebook Graph API (Pages)."""

    platform_name = "facebook"

    def check_credentials(self) -> Tuple[bool, List[str]]:
        """Verify Meta Graph API Page access token and Page ID."""
        required = ["access_token", "page_id"]
        missing = [k for k in required if not self.credentials.get(k)]
        return len(missing) == 0, missing

    def _execute_live_publish(
        self,
        content: Dict[str, Any],
        account_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Execute live Facebook Page feed post creation via Meta Graph API.
        Requires a valid Page ID and Page Access Token.
        """
        account_name = account_config.get("account", "LifeModeHQ")
        page_id = str(self.credentials.get("page_id") or account_config.get("page_id", "")).strip()
        access_token = str(self.credentials.get("access_token", "")).strip()

        text = str(content.get("content") or content.get("text", "")).strip()
        if not text:
            return {
                "platform": self.platform_name,
                "account": account_name,
                "page_id": page_id,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": ["Missing content for Facebook post creation."],
                "message": f"Cannot publish to Facebook Page '{account_name}': content is empty."
            }

        image_url = str(content.get("image_url") or account_config.get("image_url", "")).strip()

        if not image_url and account_config.get("require_image", False):
            return {
                "platform": self.platform_name,
                "account": account_name,
                "page_id": page_id,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": ["Missing required image_url for Facebook photo post creation."],
                "message": f"Cannot publish photo to Facebook Page '{account_name}': missing image_url."
            }

        if image_url:
            url = f"https://graph.facebook.com/v19.0/{page_id}/photos"
            payload = {
                "url": image_url,
                "caption": text,
                "published": True,
                "access_token": access_token
            }
        else:
            url = f"https://graph.facebook.com/v19.0/{page_id}/feed"
            payload = {
                "message": text,
                "access_token": access_token
            }

        try:
            resp = requests.post(url, data=payload, timeout=30)
            if 200 <= resp.status_code < 300:
                resp_data = resp.json()
                if not isinstance(resp_data, dict):
                    raise ValueError("Facebook API returned a non-object JSON response.")
                if resp_data.get("error"):
                    graph_error = resp_data["error"]
                    _log_graph_failure(resp.status_code, graph_error, access_token)
                    error_detail = graph_error.get("message") if isinstance(graph_error, dict) else str(graph_error)
                    return {
                        "platform": self.platform_name,
                        "account": account_name,
                        "page_id": page_id,
                        "action": "PUBLISH",
                        "mode": "LIVE",
                        "status": STATUS_FAILED,
                        "http_status": resp.status_code,
                        "api_response": resp_data,
                        "errors": [f"Facebook Graph API error (HTTP {resp.status_code}): {error_detail}"],
                        "message": "Facebook Graph API returned an error response."
                    }

                photo_id = resp_data.get("id") if image_url else ""
                post_id = resp_data.get("post_id") or (resp_data.get("id") if not image_url else "")

                if isinstance(photo_id, str) and isinstance(post_id, str) and (photo_id or post_id):
                    # Authenticated verification read from Graph API when photo is published
                    photo_link = None
                    verified_image = False
                    if image_url and photo_id:
                        try:
                            v_url = f"https://graph.facebook.com/v19.0/{photo_id}?fields=id,link,images,created_time&access_token={access_token}"
                            v_resp = requests.get(v_url, timeout=10)
                            if v_resp.status_code == 200:
                                v_json = v_resp.json()
                                photo_link = v_json.get("link")
                                verified_image = bool(v_json.get("images"))
                        except Exception:
                            pass

                    if not photo_link and post_id:
                        photo_link = f"https://www.facebook.com/{page_id}/posts/{post_id.split('_')[-1]}"

                    return {
                        "platform": self.platform_name,
                        "account": account_name,
                        "page_id": page_id,
                        "photo_id": photo_id if image_url else None,
                        "post_id": post_id,
                        "permalink_url": photo_link,
                        "image_url": image_url if image_url else None,
                        "verified_image_read": verified_image if image_url else None,
                        "action": "PUBLISH",
                        "mode": "LIVE",
                        "status": STATUS_PUBLISHED,
                        "http_status": resp.status_code,
                        "api_response": resp_data,
                        "message": f"Successfully published {'photo' if image_url else 'post'} {photo_id or post_id} to Facebook Page '{account_name}' ({page_id})."
                    }
                else:
                    return {
                        "platform": self.platform_name,
                        "account": account_name,
                        "page_id": page_id,
                        "action": "PUBLISH",
                        "mode": "LIVE",
                        "status": STATUS_FAILED,
                        "http_status": resp.status_code,
                        "errors": [f"Facebook API returned HTTP {resp.status_code} but response missing post ID."],
                        "message": "Facebook post confirmation failed: missing post ID in response."
                    }
            else:
                error_detail = resp.text
                graph_error = None
                response_json = None
                try:
                    response_json = resp.json()
                    graph_error = response_json.get("error", {}) if isinstance(response_json, dict) else {}
                    error_detail = graph_error.get("message") or resp.text
                except Exception:
                    pass
                _log_graph_failure(resp.status_code, graph_error or error_detail, access_token)
                return {
                    "platform": self.platform_name,
                    "account": account_name,
                    "page_id": page_id,
                    "action": "PUBLISH",
                    "mode": "LIVE",
                    "status": STATUS_FAILED,
                    "http_status": resp.status_code,
                    "api_response": response_json if isinstance(response_json, dict) else None,
                    "errors": [f"Facebook Graph API error (HTTP {resp.status_code}): {error_detail}"],
                    "message": f"Facebook Graph API rejected post with HTTP {resp.status_code}."
                }
        except Exception as e:
            return {
                "platform": self.platform_name,
                "account": account_name,
                "page_id": page_id,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": [f"Network exception calling Facebook Graph API: {str(e)}"],
                "message": f"Facebook Graph API request failed: {str(e)}"
            }

