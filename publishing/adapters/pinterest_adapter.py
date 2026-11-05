"""
Pinterest platform publishing adapter.
Supports independently configured Pinterest accounts and boards.
"""

import base64
from pathlib import Path
import re
import time
from typing import Dict, Any, List, Optional, Tuple
from urllib.parse import urlparse
import requests
from amazon.scraper import is_valid_image_url
from content.package import is_valid_video_url
from .base import BasePlatformAdapter, STATUS_PUBLISHED, STATUS_FAILED

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

DEFAULT_VIDEO_POLL_ATTEMPTS = 30
DEFAULT_VIDEO_POLL_INTERVAL = 5.0


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

    def _auth_failed_result(
        self,
        account_name: str,
        board_name: str,
        board_id: str,
        err_json: Any = None,
    ) -> Dict[str, Any]:
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

    def _execute_live_video_pin(
        self,
        content: Dict[str, Any],
        account_config: Dict[str, Any],
        account_name: str,
        board_name: str,
        board_id: str,
        access_token: str,
        title: str,
        description: str,
        link: str,
        video_url: str,
    ) -> Dict[str, Any]:
        """
        Execute live video Pin creation via Pinterest API v5:
        a. Register video upload via POST /v5/media
        b. Retrieve media_id, upload_url, and upload_parameters
        c. Upload video binary to Pinterest's upload endpoint
        d. Poll GET /v5/media/{media_id} until status == 'succeeded'
        e. Create Pin via POST /v5/pins with source_type='video_id' and cover_image_url
        """
        if not is_valid_video_url(video_url):
            return {
                "platform": self.platform_name,
                "account": account_name,
                "board": board_name,
                "board_id": board_id,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": [
                    f"Pinterest video Pin requires a valid public HTTPS video URL. Received: '{video_url}'."
                ],
                "message": "Cannot create video pin: invalid or non-HTTPS video_url.",
            }

        cover_image_url = str(
            content.get("cover_image_url")
            or content.get("image_url")
            or account_config.get("image_url", "")
        ).strip()
        if not is_valid_image_url(cover_image_url):
            return {
                "platform": self.platform_name,
                "account": account_name,
                "board": board_name,
                "board_id": board_id,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": [
                    f"Pinterest video Pin requires a valid public HTTPS cover_image_url (image_url). Received: '{cover_image_url}'."
                ],
                "message": "Cannot create video pin: missing or invalid cover_image_url.",
            }

        if not board_id:
            resolved_board_id, board_error = self.resolve_board_id(board_name)
            if not resolved_board_id:
                return {
                    "platform": self.platform_name,
                    "account": account_name,
                    "board": board_name,
                    "board_id": "",
                    "action": "PUBLISH",
                    "mode": "LIVE",
                    "status": STATUS_FAILED,
                    "errors": [board_error or f"Pinterest board '{board_name}' could not be resolved."],
                    "message": f"Cannot create video pin: board '{board_name}' could not be resolved for this account.",
                }
            board_id = resolved_board_id

        # Fetch public R2 video bytes for multipart upload to Pinterest media storage
        try:
            dl_resp = requests.get(video_url, timeout=120)
            if dl_resp.status_code != 200 or not dl_resp.content:
                return {
                    "platform": self.platform_name,
                    "account": account_name,
                    "board": board_name,
                    "board_id": board_id,
                    "action": "PUBLISH",
                    "mode": "LIVE",
                    "status": STATUS_FAILED,
                    "http_status": dl_resp.status_code,
                    "errors": [
                        f"Failed to retrieve video binary from R2 URL '{video_url}' (HTTP {dl_resp.status_code})."
                    ],
                    "message": f"Cannot create video pin: video download returned HTTP {dl_resp.status_code}.",
                }
            video_bytes = dl_resp.content
        except Exception as e:
            return {
                "platform": self.platform_name,
                "account": account_name,
                "board": board_name,
                "board_id": board_id,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": [f"Network exception downloading video from R2 '{video_url}': {e}"],
                "message": f"Cannot create video pin: failed to download video from R2: {e}",
            }

        parsed_path = Path(urlparse(video_url).path).name or "video.mp4"
        video_mime = "video/quicktime" if parsed_path.lower().endswith(".mov") else "video/mp4"

        auth_json_headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }

        # Step a & b: Register video upload via /v5/media
        try:
            reg_resp = requests.post(
                "https://api.pinterest.com/v5/media",
                headers=auth_json_headers,
                json={"media_type": "video"},
                timeout=30,
            )
            if reg_resp.status_code == 401:
                err_json = None
                try:
                    err_json = reg_resp.json()
                except Exception:
                    pass
                return self._auth_failed_result(account_name, board_name, board_id, err_json)

            if reg_resp.status_code not in (200, 201):
                err_detail = reg_resp.text
                err_json = None
                try:
                    err_json = reg_resp.json()
                    err_detail = err_json.get("message") or reg_resp.text
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
                    "http_status": reg_resp.status_code,
                    "api_response": err_json if isinstance(err_json, dict) else None,
                    "errors": [f"Pinterest media registration error (HTTP {reg_resp.status_code}): {err_detail}"],
                    "message": f"Pinterest media registration failed with HTTP {reg_resp.status_code}.",
                }

            reg_data = reg_resp.json()
            media_id = str(reg_data.get("media_id") or "").strip()
            upload_url = str(reg_data.get("upload_url") or "").strip()
            upload_parameters = reg_data.get("upload_parameters")
            if not media_id or not upload_url or not isinstance(upload_parameters, dict):
                return {
                    "platform": self.platform_name,
                    "account": account_name,
                    "board": board_name,
                    "board_id": board_id,
                    "action": "PUBLISH",
                    "mode": "LIVE",
                    "status": STATUS_FAILED,
                    "http_status": reg_resp.status_code,
                    "api_response": reg_data if isinstance(reg_data, dict) else None,
                    "errors": [
                        "Pinterest /v5/media registration response missing required media_id, upload_url, or upload_parameters."
                    ],
                    "message": "Pinterest media registration returned incomplete upload parameters.",
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
                "errors": [f"Network exception during Pinterest media registration: {e}"],
                "message": f"Pinterest media registration request failed: {e}",
            }

        # Step c: Upload video file to Pinterest's pre-signed upload endpoint (no Bearer header)
        try:
            up_resp = requests.post(
                upload_url,
                data=upload_parameters,
                files={"file": (parsed_path, video_bytes, video_mime)},
                timeout=300,
            )
            if up_resp.status_code not in (200, 201, 204):
                return {
                    "platform": self.platform_name,
                    "account": account_name,
                    "board": board_name,
                    "board_id": board_id,
                    "media_id": media_id,
                    "action": "PUBLISH",
                    "mode": "LIVE",
                    "status": STATUS_FAILED,
                    "http_status": up_resp.status_code,
                    "errors": [
                        f"Pinterest video upload to storage endpoint failed (HTTP {up_resp.status_code}): {up_resp.text[:300]}"
                    ],
                    "message": f"Pinterest video file upload rejected with HTTP {up_resp.status_code}.",
                }
        except Exception as e:
            return {
                "platform": self.platform_name,
                "account": account_name,
                "board": board_name,
                "board_id": board_id,
                "media_id": media_id,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": [f"Network exception uploading video file to Pinterest storage: {e}"],
                "message": f"Pinterest video file upload failed: {e}",
            }

        # Step d: Poll /v5/media/{media_id} until status == 'succeeded'
        max_attempts = max(1, int(account_config.get("video_poll_max_attempts", DEFAULT_VIDEO_POLL_ATTEMPTS)))
        poll_interval = float(account_config.get("video_poll_interval", DEFAULT_VIDEO_POLL_INTERVAL))
        media_status_url = f"https://api.pinterest.com/v5/media/{media_id}"
        poll_headers = {"Authorization": f"Bearer {access_token}"}

        media_succeeded = False
        last_media_status = None

        for _ in range(max_attempts):
            if poll_interval > 0:
                time.sleep(poll_interval)
            try:
                poll_resp = requests.get(media_status_url, headers=poll_headers, timeout=30)
                if poll_resp.status_code == 401:
                    return self._auth_failed_result(account_name, board_name, board_id)
                if poll_resp.status_code != 200:
                    return {
                        "platform": self.platform_name,
                        "account": account_name,
                        "board": board_name,
                        "board_id": board_id,
                        "media_id": media_id,
                        "action": "PUBLISH",
                        "mode": "LIVE",
                        "status": STATUS_FAILED,
                        "http_status": poll_resp.status_code,
                        "errors": [
                            f"Pinterest media status polling failed for '{media_id}' (HTTP {poll_resp.status_code}): {poll_resp.text[:300]}"
                        ],
                        "message": f"Pinterest media status check failed with HTTP {poll_resp.status_code}.",
                    }
                poll_data = poll_resp.json()
                m_status = str(poll_data.get("status") or "").strip().lower()
                last_media_status = m_status
                if m_status == "succeeded":
                    media_succeeded = True
                    break
                if m_status == "failed":
                    return {
                        "platform": self.platform_name,
                        "account": account_name,
                        "board": board_name,
                        "board_id": board_id,
                        "media_id": media_id,
                        "action": "PUBLISH",
                        "mode": "LIVE",
                        "status": STATUS_FAILED,
                        "api_response": poll_data if isinstance(poll_data, dict) else None,
                        "errors": [f"Pinterest video processing failed for media_id '{media_id}'."],
                        "message": f"Pinterest reported failed video transcoding for media '{media_id}'.",
                    }
            except Exception as e:
                return {
                    "platform": self.platform_name,
                    "account": account_name,
                    "board": board_name,
                    "board_id": board_id,
                    "media_id": media_id,
                    "action": "PUBLISH",
                    "mode": "LIVE",
                    "status": STATUS_FAILED,
                    "errors": [f"Network exception polling Pinterest media '{media_id}': {e}"],
                    "message": f"Pinterest media status polling failed: {e}",
                }

        if not media_succeeded:
            return {
                "platform": self.platform_name,
                "account": account_name,
                "board": board_name,
                "board_id": board_id,
                "media_id": media_id,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": [
                    f"Pinterest video media '{media_id}' did not reach 'succeeded' after {max_attempts} poll attempts (last_status={last_media_status})."
                ],
                "message": "Pinterest video processing timed out before reaching 'succeeded'; Pin was not created.",
            }

        # Step e: Create video Pin via POST /v5/pins
        media_source: Dict[str, Any] = {
            "source_type": "video_id",
            "media_id": media_id,
            "cover_image_url": cover_image_url,
        }
        payload: Dict[str, Any] = {
            "board_id": board_id,
            "media_source": media_source,
            "title": title,
            "description": description,
        }
        if link:
            payload["link"] = link

        try:
            resp = requests.post(
                "https://api.pinterest.com/v5/pins",
                headers=auth_json_headers,
                json=payload,
                timeout=30,
            )
            if resp.status_code in (200, 201):
                pin_data = resp.json()
                pin_id = str(pin_data.get("id") or "").strip()
                if not pin_id:
                    return {
                        "platform": self.platform_name,
                        "account": account_name,
                        "board": board_name,
                        "board_id": board_id,
                        "media_id": media_id,
                        "action": "PUBLISH",
                        "mode": "LIVE",
                        "status": STATUS_FAILED,
                        "http_status": resp.status_code,
                        "api_response": pin_data if isinstance(pin_data, dict) else None,
                        "errors": ["Pinterest /v5/pins returned HTTP 2xx for video Pin but response missing Pin ID."],
                        "message": "Pinterest video Pin confirmation failed: missing Pin ID in response.",
                    }
                return {
                    "platform": self.platform_name,
                    "account": account_name,
                    "board": board_name,
                    "board_id": board_id,
                    "pin_id": pin_id,
                    "pin_url": f"https://www.pinterest.com/pin/{pin_id}/",
                    "media_id": media_id,
                    "video_url": video_url,
                    "cover_image_url": cover_image_url,
                    "image_url": cover_image_url,
                    "media_type": "VIDEO",
                    "title": title,
                    "link": link,
                    "action": "PUBLISH",
                    "mode": "LIVE",
                    "status": STATUS_PUBLISHED,
                    "http_status": resp.status_code,
                    "api_response": pin_data,
                    "message": f"Successfully created video pin {pin_id} on Pinterest board '{board_name}' ({account_name}).",
                }

            error_detail = resp.text
            err_json = None
            try:
                err_json = resp.json()
                error_detail = err_json.get("message") or resp.text
            except Exception:
                pass
            if resp.status_code == 401:
                return self._auth_failed_result(account_name, board_name, board_id, err_json)
            return {
                "platform": self.platform_name,
                "account": account_name,
                "board": board_name,
                "board_id": board_id,
                "media_id": media_id,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "http_status": resp.status_code,
                "api_response": err_json if isinstance(err_json, dict) else None,
                "errors": [f"Pinterest API error creating video Pin (HTTP {resp.status_code}): {error_detail}"],
                "message": f"Pinterest API rejected video Pin with HTTP {resp.status_code}.",
            }
        except Exception as e:
            return {
                "platform": self.platform_name,
                "account": account_name,
                "board": board_name,
                "board_id": board_id,
                "media_id": media_id,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": [f"Network exception calling Pinterest API for video Pin: {str(e)}"],
                "message": f"Pinterest video Pin request failed: {str(e)}",
            }

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
        video_url = str(content.get("video_url") or account_config.get("video_url", "")).strip()
        image_url = str(content.get("image_url") or account_config.get("image_url", "")).strip()
        image_path = content.get("image_path")
        link = content.get("link") or account_config.get("link", "")

        if not link:
            url_match = re.search(r'https?://\S+', description)
            if url_match:
                link = url_match.group(0).rstrip(".,)")

        if video_url:
            return self._execute_live_video_pin(
                content=content,
                account_config=account_config,
                account_name=account_name,
                board_name=board_name,
                board_id=board_id,
                access_token=access_token,
                title=title,
                description=description,
                link=link,
                video_url=video_url,
            )

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

