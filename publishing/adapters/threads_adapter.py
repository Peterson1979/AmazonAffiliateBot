"""
Threads platform publishing adapter.
Target account: lifemodehq
Publishes posts via official Meta Threads API two-step container flow.
"""

from typing import Dict, Any, List, Tuple
import requests
import time
from .base import BasePlatformAdapter, STATUS_PUBLISHED, STATUS_FAILED


class ThreadsAdapter(BasePlatformAdapter):
    """Publishing adapter for Threads API (two-step container publishing)."""

    platform_name = "threads"

    def check_credentials(self) -> Tuple[bool, List[str]]:
        """Verify Threads API access token and User ID."""
        required = ["access_token", "user_id"]
        missing = [k for k in required if not self.credentials.get(k)]
        return len(missing) == 0, missing

    def _execute_live_publish(
        self,
        content: Dict[str, Any],
        account_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Execute live post creation via Threads API official two-step flow:
        1. Create media container (media_type=TEXT)
        2. Publish media container
        Requires valid Threads User ID and Threads Access Token.
        """
        account_name = account_config.get("account", "lifemodehq")
        user_id = str(self.credentials.get("user_id") or account_config.get("user_id", "")).strip()
        access_token = str(self.credentials.get("access_token", "")).strip()

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

        image_url = str(content.get("image_url") or account_config.get("image_url", "")).strip()

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
                try:
                    err_json = resp2.json().get("error", {})
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

