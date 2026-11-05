"""
Facebook platform publishing adapter.
Target account: LifeModeHQ
Publishes posts to Facebook Page feed via Meta Graph API v19.0.
"""

from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple
import logging
import requests
from content.package import is_valid_video_url
from .base import BasePlatformAdapter, STATUS_PUBLISHED, STATUS_FAILED

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

LOGGER = logging.getLogger(__name__)


def _safe_graph_error_message(value: Any, access_token: str = "") -> str:
    """Return a bounded, single-line Graph error message with secrets redacted."""
    message = " ".join(str(value or "").split())
    if access_token:
        message = message.replace(access_token, "[REDACTED]")
    return message[:500]


def _redact_graph_dict(payload: Any, access_token: str = "") -> Optional[Dict[str, Any]]:
    """Return a shallow-sanitized Graph API dictionary with access_token redacted."""
    if not isinstance(payload, dict):
        return None
    if not access_token:
        return payload
    import json
    try:
        sanitized = json.dumps(payload).replace(access_token, "[REDACTED]")
        return json.loads(sanitized)
    except Exception:
        return None


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

    def _execute_live_video_publish(
        self,
        video_url: str,
        text: str,
        page_id: str,
        access_token: str,
        account_name: str,
    ) -> Dict[str, Any]:
        """
        Execute live Facebook Page video publication via Meta Graph Video API
        using a verified public HTTPS R2 video URL.
        """
        if not is_valid_video_url(video_url):
            return {
                "platform": self.platform_name,
                "account": account_name,
                "page_id": page_id,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": [
                    f"Facebook Page video publishing requires a publicly accessible HTTPS video URL. "
                    f"Invalid or local video reference '{video_url}'."
                ],
                "message": f"Cannot publish video to Facebook Page '{account_name}': video_url must be a valid HTTPS URL.",
            }

        url = f"https://graph-video.facebook.com/v19.0/{page_id}/videos"
        payload = {
            "file_url": video_url,
            "description": text,
            "published": True,
            "access_token": access_token,
        }

        try:
            resp = requests.post(url, data=payload, timeout=120)
            if 200 <= resp.status_code < 300:
                resp_data = resp.json()
                if not isinstance(resp_data, dict):
                    raise ValueError("Facebook Video API returned a non-object JSON response.")
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
                        "api_response": _redact_graph_dict(resp_data, access_token),
                        "errors": [
                            f"Facebook Graph Video API error (HTTP {resp.status_code}): "
                            f"{_safe_graph_error_message(error_detail, access_token)}"
                        ],
                        "message": "Facebook Graph Video API returned an error response.",
                    }

                video_id = str(resp_data.get("id") or "").strip()
                post_id = str(resp_data.get("post_id") or video_id).strip()
                if not video_id:
                    return {
                        "platform": self.platform_name,
                        "account": account_name,
                        "page_id": page_id,
                        "action": "PUBLISH",
                        "mode": "LIVE",
                        "status": STATUS_FAILED,
                        "http_status": resp.status_code,
                        "api_response": resp_data,
                        "errors": [f"Facebook Video API returned HTTP {resp.status_code} but response missing video ID."],
                        "message": "Facebook video confirmation failed: missing video ID in response.",
                    }

                permalink_url = None
                verified_video = False
                try:
                    v_url = f"https://graph.facebook.com/v19.0/{video_id}?fields=id,permalink_url,status&access_token={access_token}"
                    v_resp = requests.get(v_url, timeout=10)
                    if v_resp.status_code == 200:
                        v_json = v_resp.json()
                        if isinstance(v_json, dict):
                            v_status_obj = v_json.get("status")
                            if isinstance(v_status_obj, dict) and v_status_obj.get("video_status") == "error":
                                return {
                                    "platform": self.platform_name,
                                    "account": account_name,
                                    "page_id": page_id,
                                    "video_id": video_id,
                                    "action": "PUBLISH",
                                    "mode": "LIVE",
                                    "status": STATUS_FAILED,
                                    "http_status": resp.status_code,
                                    "api_response": resp_data,
                                    "errors": [f"Facebook video '{video_id}' reported processing error status."],
                                    "message": "Facebook video confirmation failed: video status reported an error.",
                                }
                            verified_video = bool(v_json.get("id"))
                            raw_permalink = str(v_json.get("permalink_url") or "").strip()
                            if raw_permalink:
                                permalink_url = (
                                    f"https://www.facebook.com{raw_permalink}"
                                    if raw_permalink.startswith("/")
                                    else raw_permalink
                                )
                except Exception:
                    pass

                if not permalink_url:
                    permalink_url = f"https://www.facebook.com/{page_id}/videos/{video_id}"

                return {
                    "platform": self.platform_name,
                    "account": account_name,
                    "page_id": page_id,
                    "video_id": video_id,
                    "post_id": post_id,
                    "permalink_url": permalink_url,
                    "video_url": video_url,
                    "media_type": "VIDEO",
                    "verified_video_read": verified_video,
                    "action": "PUBLISH",
                    "mode": "LIVE",
                    "status": STATUS_PUBLISHED,
                    "http_status": resp.status_code,
                    "api_response": resp_data,
                    "message": f"Successfully published video {video_id} to Facebook Page '{account_name}' ({page_id}).",
                }

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
                "api_response": _redact_graph_dict(response_json, access_token),
                "errors": [
                    f"Facebook Graph Video API error (HTTP {resp.status_code}): "
                    f"{_safe_graph_error_message(error_detail, access_token)}"
                ],
                "message": f"Facebook Graph Video API rejected video with HTTP {resp.status_code}.",
            }
        except Exception as e:
            return {
                "platform": self.platform_name,
                "account": account_name,
                "page_id": page_id,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": [f"Network exception calling Facebook Graph Video API: {_safe_graph_error_message(e, access_token)}"],
                "message": f"Facebook Graph Video API request failed: {_safe_graph_error_message(e, access_token)}",
            }

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

        video_url = str(content.get("video_url") or account_config.get("video_url", "")).strip()
        if video_url:
            return self._execute_live_video_publish(
                video_url=video_url,
                text=text,
                page_id=page_id,
                access_token=access_token,
                account_name=account_name,
            )

        image_url = str(content.get("image_url") or account_config.get("image_url", "")).strip()
        image_path = content.get("image_path")

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

        has_image = bool(local_file or image_url)
        if not has_image and account_config.get("require_image", False):
            return {
                "platform": self.platform_name,
                "account": account_name,
                "page_id": page_id,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": ["Missing required image for Facebook photo post creation."],
                "message": f"Cannot publish photo to Facebook Page '{account_name}': missing image."
            }

        if local_file and local_file.is_file():
            url = f"https://graph.facebook.com/v19.0/{page_id}/photos"
            payload = {
                "caption": text,
                "published": True,
                "access_token": access_token
            }
        elif image_url and (image_url.startswith("https://") or image_url.startswith("http://")):
            url = f"https://graph.facebook.com/v19.0/{page_id}/photos"
            payload = {
                "url": image_url,
                "caption": text,
                "published": True,
                "access_token": access_token
            }
        elif image_url and not (image_url.startswith("https://") or image_url.startswith("http://")):
            return {
                "platform": self.platform_name,
                "account": account_name,
                "page_id": page_id,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": [f"Facebook photo upload failed: local creative file not found on disk for '{image_url}'."],
                "message": f"Cannot publish photo to Facebook Page '{account_name}': local image file not found on disk."
            }
        else:
            url = f"https://graph.facebook.com/v19.0/{page_id}/feed"
            payload = {
                "message": text,
                "access_token": access_token
            }

        try:
            if local_file and local_file.is_file():
                with open(local_file, "rb") as f_img:
                    resp = requests.post(url, data=payload, files={"source": f_img}, timeout=60)
            else:
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
                        "errors": [f"Facebook Graph API error (HTTP {resp.status_code}): {_safe_graph_error_message(error_detail, access_token)}"],
                        "message": "Facebook Graph API returned an error response."
                    }

                is_photo_req = bool(local_file or image_url)
                photo_id = resp_data.get("id") if is_photo_req else ""
                post_id = resp_data.get("post_id") or (resp_data.get("id") if not is_photo_req else "")

                # A photo ID alone is not sufficient confirmation that the
                # Page post exists. Persist success only with a returned post ID.
                if isinstance(post_id, str) and post_id:
                    # Authenticated verification read from Graph API when photo is published
                    photo_link = None
                    verified_image = False
                    if is_photo_req and photo_id:
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
                        "api_response": resp_data,
                        "photo_id": photo_id if image_url else None,
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
                    "errors": [f"Facebook Graph API error (HTTP {resp.status_code}): {_safe_graph_error_message(error_detail, access_token)}"],
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
                "errors": [f"Network exception calling Facebook Graph API: {_safe_graph_error_message(e, access_token)}"],
                "message": f"Facebook Graph API request failed: {_safe_graph_error_message(e, access_token)}"
            }

