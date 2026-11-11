"""
Threads platform publishing adapter.
Target account: lifemodehq
Publishes posts via official Meta Threads API two-step container flow.
"""

from typing import Dict, Any, List, Tuple
import requests
import time
from content.package import is_valid_video_url
from .base import BasePlatformAdapter, STATUS_PUBLISHED, STATUS_FAILED

DEFAULT_VIDEO_POLL_ATTEMPTS = 30
DEFAULT_VIDEO_POLL_INTERVAL = 5.0


class ThreadsAdapter(BasePlatformAdapter):
    """Publishing adapter for Threads API (two-step container publishing)."""

    platform_name = "threads"

    def check_credentials(self) -> Tuple[bool, List[str]]:
        """Verify Threads API access token and User ID."""
        required = ["access_token", "user_id"]
        missing = [k for k in required if not self.credentials.get(k)]
        return len(missing) == 0, missing

    def _execute_live_video_publish(
        self,
        video_url: str,
        image_url: str,
        text: str,
        user_id: str,
        access_token: str,
        account_name: str,
        account_config: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Execute live video post creation via Threads API two-step container flow:
        1. Create VIDEO media container (media_type=VIDEO, video_url=...)
        2. Poll container status until FINISHED (transcoding complete)
        3. Publish media container only after FINISHED
        """
        if not is_valid_video_url(video_url):
            return {
                "platform": self.platform_name,
                "account": account_name,
                "user_id": user_id,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": [
                    f"Threads requires a publicly accessible HTTPS video URL. "
                    f"Invalid or local video reference '{video_url}' cannot be retrieved by Threads API."
                ],
                "message": f"Cannot publish video to Threads account '{account_name}': video_url must be a publicly accessible HTTPS URL.",
            }

        step1_url = f"https://graph.threads.net/v1.0/{user_id}/threads"
        step1_payload = {
            "media_type": "VIDEO",
            "video_url": video_url,
            "text": text,
            "access_token": access_token,
        }

        try:
            resp1 = requests.post(step1_url, data=step1_payload, timeout=60)
            if resp1.status_code != 200:
                err_msg = resp1.text
                try:
                    err_json = resp1.json().get("error", {})
                    err_msg = err_json.get("message") or resp1.text
                except Exception:
                    pass
                return {
                    "platform": self.platform_name,
                    "account": account_name,
                    "user_id": user_id,
                    "action": "PUBLISH",
                    "mode": "LIVE",
                    "status": STATUS_FAILED,
                    "http_status": resp1.status_code,
                    "errors": [f"Threads Step 1 (Video Container Creation) error (HTTP {resp1.status_code}): {err_msg}"],
                    "message": f"Threads video container creation rejected with HTTP {resp1.status_code}.",
                }

            resp1_data = resp1.json()
            creation_id = resp1_data.get("id", "")
            if not creation_id:
                return {
                    "platform": self.platform_name,
                    "account": account_name,
                    "user_id": user_id,
                    "action": "PUBLISH",
                    "mode": "LIVE",
                    "status": STATUS_FAILED,
                    "http_status": resp1.status_code,
                    "errors": ["Threads API returned HTTP 200 for video container creation but response missing container ID."],
                    "message": "Threads video container creation confirmation failed: missing container ID in response.",
                }
        except Exception as e:
            return {
                "platform": self.platform_name,
                "account": account_name,
                "user_id": user_id,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": [f"Network exception during Threads video container creation: {str(e)}"],
                "message": f"Threads video container creation request failed: {str(e)}",
            }

        # Poll container status until FINISHED before publishing
        status_url = f"https://graph.threads.net/v1.0/{creation_id}?fields=status,error_message&access_token={access_token}"
        max_attempts = max(1, int(account_config.get("video_poll_max_attempts", DEFAULT_VIDEO_POLL_ATTEMPTS)))
        poll_interval = float(account_config.get("video_poll_interval", DEFAULT_VIDEO_POLL_INTERVAL))

        container_finished = False
        last_status = None
        last_poll_error = None

        for _ in range(max_attempts):
            if poll_interval > 0:
                time.sleep(poll_interval)
            try:
                s_resp = requests.get(status_url, timeout=15)
                if s_resp.status_code == 200:
                    s_data = s_resp.json()
                    c_status = s_data.get("status")
                    last_status = c_status
                    if c_status == "FINISHED":
                        container_finished = True
                        break
                    if c_status in ("ERROR", "EXPIRED"):
                        err_detail = s_data.get("error_message") or f"Container status={c_status}"
                        return {
                            "platform": self.platform_name,
                            "account": account_name,
                            "user_id": user_id,
                            "creation_id": creation_id,
                            "action": "PUBLISH",
                            "mode": "LIVE",
                            "status": STATUS_FAILED,
                            "errors": [f"Threads video processing failed ({c_status}): {err_detail}"],
                            "message": f"Threads video container error: {err_detail}",
                        }
                else:
                    last_poll_error = f"HTTP {s_resp.status_code}"
            except Exception as e:
                last_poll_error = str(e)

        if not container_finished:
            detail = f"last_status={last_status}" if last_status else (last_poll_error or "no FINISHED status received")
            return {
                "platform": self.platform_name,
                "account": account_name,
                "user_id": user_id,
                "creation_id": creation_id,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": [
                    f"Threads video container '{creation_id}' did not reach FINISHED after {max_attempts} poll attempts ({detail})."
                ],
                "message": "Threads video transcoding timed out before reaching FINISHED; container was not published.",
            }

        # Step 2: Publish video container (only after FINISHED)
        step2_url = f"https://graph.threads.net/v1.0/{user_id}/threads_publish"
        step2_payload = {
            "creation_id": creation_id,
            "access_token": access_token,
        }

        try:
            resp2 = requests.post(step2_url, data=step2_payload, timeout=30)
            if resp2.status_code == 200:
                resp2_data = resp2.json()
                post_id = resp2_data.get("id", "")
                if post_id:
                    permalink = None
                    verified_media_type = None
                    try:
                        p_resp = requests.get(
                            f"https://graph.threads.net/v1.0/{user_id}/threads?fields=id,media_type,permalink&access_token={access_token}&limit=3",
                            timeout=10,
                        )
                        if p_resp.status_code == 200:
                            for t_item in p_resp.json().get("data", []):
                                if t_item.get("id") == post_id:
                                    permalink = t_item.get("permalink")
                                    verified_media_type = t_item.get("media_type")
                                    break
                    except Exception:
                        pass

                    if not permalink:
                        permalink = f"https://www.threads.com/@{account_name}/post/{post_id}"

                    return {
                        "platform": self.platform_name,
                        "account": account_name,
                        "user_id": user_id,
                        "creation_id": creation_id,
                        "post_id": post_id,
                        "permalink": permalink,
                        "video_url": video_url,
                        "image_url": image_url or None,
                        "media_type": "VIDEO",
                        "verified_media_type": verified_media_type or "VIDEO",
                        "action": "PUBLISH",
                        "mode": "LIVE",
                        "status": STATUS_PUBLISHED,
                        "http_status": resp2.status_code,
                        "api_response": resp2_data,
                        "message": f"Successfully published video post {post_id} to Threads account '{account_name}' ({user_id}).",
                    }
                return {
                    "platform": self.platform_name,
                    "account": account_name,
                    "user_id": user_id,
                    "creation_id": creation_id,
                    "action": "PUBLISH",
                    "mode": "LIVE",
                    "status": STATUS_FAILED,
                    "http_status": resp2.status_code,
                    "errors": ["Threads API returned HTTP 200 for video publishing but response missing post ID."],
                    "message": "Threads video post confirmation failed: missing post ID in publish response.",
                }

            err_msg = resp2.text
            err_json = None
            try:
                err_json = resp2.json()
                err_json = err_json.get("error", {}) if isinstance(err_json, dict) else {}
                err_msg = err_json.get("message") or resp2.text
            except Exception:
                pass
            return {
                "platform": self.platform_name,
                "account": account_name,
                "user_id": user_id,
                "creation_id": creation_id,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "http_status": resp2.status_code,
                "api_response": err_json,
                "errors": [f"Threads Step 2 (Video Container Publish) error (HTTP {resp2.status_code}): {err_msg}"],
                "message": f"Threads video container publishing rejected with HTTP {resp2.status_code}.",
            }
        except Exception as e:
            return {
                "platform": self.platform_name,
                "account": account_name,
                "user_id": user_id,
                "creation_id": creation_id,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": [f"Network exception during Threads video container publishing: {str(e)}"],
                "message": f"Threads video container publishing request failed: {str(e)}",
            }

    def _execute_live_publish(
        self,
        content: Dict[str, Any],
        account_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Execute live post creation via Threads API official two-step flow:
        1. Create media container (media_type=TEXT, IMAGE, or VIDEO)
        2. Publish media container
        Requires valid Threads User ID and Threads Access Token.
        """
        account_name = account_config.get("account", "lifemodehq")
        user_id = str(self.credentials.get("user_id") or account_config.get("user_id", "")).strip()
        access_token = str(self.credentials.get("access_token", "")).strip()

        # Permanent blacklist enforcement
        from publishing.blacklist import is_asin_blacklisted
        asin = (content.get("asin") or account_config.get("asin") or "").strip().upper()
        if is_asin_blacklisted(asin):
            return {
                "platform": self.platform_name,
                "account": account_name,
                "user_id": user_id,
                "action": "BLOCKED",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": [f"ASIN '{asin}' is permanently blacklisted. Publishing is prohibited."],
                "message": f"Cannot publish to Threads: ASIN '{asin}' is permanently blacklisted."
            }

        text = str(content.get("content") or content.get("text", "")).strip()
        if not text:
            return {
                "platform": self.platform_name,
                "account": account_name,
                "user_id": user_id,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": ["Missing content for Threads post creation."],
                "message": f"Cannot publish to Threads account '{account_name}': content is empty."
            }

        declared_media = (content.get("media_type") or account_config.get("media_type") or "").strip().lower()
        video_url = str(content.get("video_url") or account_config.get("video_url", "")).strip()
        image_url = str(content.get("image_url") or account_config.get("image_url", "")).strip()

        if declared_media == "image":
            # Strict enforcement: an image item must NEVER publish a video
            video_url = ""
        elif declared_media == "video":
            if not video_url:
                return {
                    "platform": self.platform_name,
                    "account": account_name,
                    "user_id": user_id,
                    "action": "PUBLISH",
                    "mode": "LIVE",
                    "status": STATUS_FAILED,
                    "errors": [f"Declared media_type is 'video' but video_url is missing for Threads. Image fallback prohibited."],
                    "message": f"Cannot publish to Threads: missing video_url for video publication."
                }
            return self._execute_live_video_publish(
                video_url=video_url,
                image_url=image_url,
                text=text,
                user_id=user_id,
                access_token=access_token,
                account_name=account_name,
                account_config=account_config,
            )

        if video_url:
            return self._execute_live_video_publish(
                video_url=video_url,
                image_url=image_url,
                text=text,
                user_id=user_id,
                access_token=access_token,
                account_name=account_name,
                account_config=account_config,
            )

        if image_url and not (image_url.startswith("https://") or image_url.startswith("http://")):
            return {
                "platform": self.platform_name,
                "account": account_name,
                "user_id": user_id,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": [
                    f"Threads requires a publicly accessible HTTPS image URL. "
                    f"Local relative path or file '{image_url}' cannot be retrieved by Threads API."
                ],
                "message": f"Cannot publish to Threads account '{account_name}': image_url must be a publicly accessible HTTPS URL."
            }

        if not image_url and account_config.get("require_image", False):
            return {
                "platform": self.platform_name,
                "account": account_name,
                "user_id": user_id,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": ["Missing required image_url for Threads image post creation. Text-only fallback is disabled."],
                "message": f"Cannot publish to Threads account '{account_name}': missing image_url."
            }

        # Step 1: Create media container
        step1_url = f"https://graph.threads.net/v1.0/{user_id}/threads"
        if image_url:
            step1_payload = {
                "media_type": "IMAGE",
                "image_url": image_url,
                "text": text,
                "access_token": access_token
            }
        else:
            step1_payload = {
                "media_type": "TEXT",
                "text": text,
                "access_token": access_token
            }

        try:
            resp1 = requests.post(step1_url, data=step1_payload, timeout=30)
            if resp1.status_code != 200:
                err_msg = resp1.text
                try:
                    err_json = resp1.json().get("error", {})
                    err_msg = err_json.get("message") or resp1.text
                except Exception:
                    pass
                return {
                    "platform": self.platform_name,
                    "account": account_name,
                    "user_id": user_id,
                    "action": "PUBLISH",
                    "mode": "LIVE",
                    "status": STATUS_FAILED,
                    "http_status": resp1.status_code,
                    "errors": [f"Threads Step 1 (Container Creation) error (HTTP {resp1.status_code}): {err_msg}"],
                    "message": f"Threads container creation rejected with HTTP {resp1.status_code}."
                }

            resp1_data = resp1.json()
            creation_id = resp1_data.get("id", "")
            if not creation_id:
                return {
                    "platform": self.platform_name,
                    "account": account_name,
                    "user_id": user_id,
                    "action": "PUBLISH",
                    "mode": "LIVE",
                    "status": STATUS_FAILED,
                    "http_status": resp1.status_code,
                    "errors": ["Threads API returned HTTP 200 for container creation but response missing container ID."],
                    "message": "Threads container creation confirmation failed: missing container ID in response."
                }
        except Exception as e:
            return {
                "platform": self.platform_name,
                "account": account_name,
                "user_id": user_id,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": [f"Network exception during Threads container creation: {str(e)}"],
                "message": f"Threads container creation request failed: {str(e)}"
            }

        # Wait for media container processing if image
        if image_url:
            status_url = f"https://graph.threads.net/v1.0/{creation_id}?fields=status,error_message&access_token={access_token}"
            for _ in range(5):
                time.sleep(2)
                try:
                    s_resp = requests.get(status_url, timeout=10)
                    if s_resp.status_code == 200:
                        c_status = s_resp.json().get("status")
                        if c_status == "FINISHED":
                            break
                        elif c_status == "ERROR":
                            err_detail = s_resp.json().get("error_message", "Container processing error")
                            return {
                                "platform": self.platform_name,
                                "account": account_name,
                                "user_id": user_id,
                                "creation_id": creation_id,
                                "action": "PUBLISH",
                                "mode": "LIVE",
                                "status": STATUS_FAILED,
                                "errors": [f"Threads media processing failed: {err_detail}"],
                                "message": f"Threads media container error: {err_detail}"
                            }
                except Exception:
                    pass

        # Step 2: Publish media container
        step2_url = f"https://graph.threads.net/v1.0/{user_id}/threads_publish"
        step2_payload = {
            "creation_id": creation_id,
            "access_token": access_token
        }

        try:
            resp2 = requests.post(step2_url, data=step2_payload, timeout=30)
            if resp2.status_code == 200:
                resp2_data = resp2.json()
                post_id = resp2_data.get("id", "")
                if post_id:
                    # Query user threads to resolve verified permalink
                    permalink = None
                    verified_media_type = None
                    try:
                        p_resp = requests.get(
                            f"https://graph.threads.net/v1.0/{user_id}/threads?fields=id,media_type,permalink&access_token={access_token}&limit=3",
                            timeout=10
                        )
                        if p_resp.status_code == 200:
                            for t_item in p_resp.json().get("data", []):
                                if t_item.get("id") == post_id:
                                    permalink = t_item.get("permalink")
                                    verified_media_type = t_item.get("media_type")
                                    break
                    except Exception:
                        pass

                    if not permalink:
                        permalink = f"https://www.threads.com/@{account_name}/post/{post_id}"

                    return {
                        "platform": self.platform_name,
                        "account": account_name,
                        "user_id": user_id,
                        "creation_id": creation_id,
                        "post_id": post_id,
                        "permalink": permalink,
                        "image_url": image_url,
                        "verified_media_type": verified_media_type or ("IMAGE" if image_url else "TEXT"),
                        "action": "PUBLISH",
                        "mode": "LIVE",
                        "status": STATUS_PUBLISHED,
                        "http_status": resp2.status_code,
                        "api_response": resp2_data,
                        "message": f"Successfully published {'image' if image_url else 'text'} post {post_id} to Threads account '{account_name}' ({user_id})."
                    }
                else:
                    return {
                        "platform": self.platform_name,
                        "account": account_name,
                        "user_id": user_id,
                        "creation_id": creation_id,
                        "action": "PUBLISH",
                        "mode": "LIVE",
                        "status": STATUS_FAILED,
                        "http_status": resp2.status_code,
                        "errors": ["Threads API returned HTTP 200 for publishing but response missing post ID."],
                        "message": "Threads post confirmation failed: missing post ID in publish response."
                    }
            else:
                err_msg = resp2.text
                err_json = None
                try:
                    err_json = resp2.json()
                    err_json = err_json.get("error", {}) if isinstance(err_json, dict) else {}
                    err_msg = err_json.get("message") or resp2.text
                except Exception:
                    pass
                return {
                    "platform": self.platform_name,
                    "account": account_name,
                    "user_id": user_id,
                    "creation_id": creation_id,
                    "action": "PUBLISH",
                    "mode": "LIVE",
                    "status": STATUS_FAILED,
                    "http_status": resp2.status_code,
                    "api_response": err_json,
                    "errors": [f"Threads Step 2 (Container Publish) error (HTTP {resp2.status_code}): {err_msg}"],
                    "message": f"Threads container publishing rejected with HTTP {resp2.status_code}."
                }
        except Exception as e:
            return {
                "platform": self.platform_name,
                "account": account_name,
                "user_id": user_id,
                "creation_id": creation_id,
                "action": "PUBLISH",
                "mode": "LIVE",
                "status": STATUS_FAILED,
                "errors": [f"Network exception during Threads container publishing: {str(e)}"],
                "message": f"Threads container publishing request failed: {str(e)}"
            }

