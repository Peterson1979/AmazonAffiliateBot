"""
Focused unit tests verifying that FacebookAdapter targets Graph API v27.0
across all publishing, photo upload, video upload, and post-verification endpoints.
"""

import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path
import tempfile

from publishing.adapters.facebook_adapter import (
    FacebookAdapter,
    GRAPH_API_VERSION,
)
from publishing.adapters.base import STATUS_PUBLISHED, STATUS_FAILED


class TestFacebookAdapterGraphAPIVersion(unittest.TestCase):
    """Test that all Facebook Graph API endpoints strictly use v27.0."""

    def setUp(self):
        self.page_id = "226949230493910"
        self.access_token = "test_fb_token_v27"
        self.adapter = FacebookAdapter(
            credentials={
                "access_token": self.access_token,
                "page_id": self.page_id,
            }
        )

    def test_module_and_class_api_version_constant(self):
        """Module constant and adapter attribute must expose v27.0."""
        self.assertEqual(GRAPH_API_VERSION, "v27.0")
        self.assertEqual(FacebookAdapter.api_version, "v27.0")
        self.assertEqual(self.adapter.api_version, "v27.0")

    @patch("publishing.adapters.facebook_adapter.requests.post")
    def test_feed_publish_endpoint_uses_v27(self, mock_post):
        """Text-only feed post must target /v27.0/{page_id}/feed."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"id": f"{self.page_id}_111222"}
        mock_post.return_value = mock_resp

        res = self.adapter._execute_live_publish(
            content={"content": "Feed text post"},
            account_config={"account": "LifeModeHQ"},
        )

        self.assertEqual(res["status"], STATUS_PUBLISHED)
        mock_post.assert_called_once()
        called_url = mock_post.call_args[0][0]
        expected_url = f"https://graph.facebook.com/v27.0/{self.page_id}/feed"
        self.assertEqual(called_url, expected_url)
        self.assertNotIn("v19.0", called_url)
        self.assertNotIn("v26.0", called_url)

    @patch("publishing.adapters.facebook_adapter.requests.get")
    @patch("publishing.adapters.facebook_adapter.requests.post")
    def test_photo_remote_url_and_verification_endpoints_use_v27(self, mock_post, mock_get):
        """Photo upload via URL and photo verification GET must use v27.0."""
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.json.return_value = {
            "id": "photo_333444",
            "post_id": f"{self.page_id}_555666",
        }
        mock_post.return_value = mock_post_resp

        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = {
            "id": "photo_333444",
            "link": "https://www.facebook.com/photo/333444",
            "images": [{"source": "https://example.com/img.jpg"}],
        }
        mock_get.return_value = mock_get_resp

        res = self.adapter._execute_live_publish(
            content={
                "content": "Photo post via URL",
                "image_url": "https://example.com/photo.jpg",
            },
            account_config={"account": "LifeModeHQ"},
        )

        self.assertEqual(res["status"], STATUS_PUBLISHED)
        # Verify POST endpoint
        mock_post.assert_called_once()
        called_post_url = mock_post.call_args[0][0]
        expected_post_url = f"https://graph.facebook.com/v27.0/{self.page_id}/photos"
        self.assertEqual(called_post_url, expected_post_url)
        self.assertNotIn("v19.0", called_post_url)
        self.assertNotIn("v26.0", called_post_url)

        # Verify GET verification endpoint
        mock_get.assert_called_once()
        called_get_url = mock_get.call_args[0][0]
        self.assertTrue(called_get_url.startswith("https://graph.facebook.com/v27.0/photo_333444"))
        self.assertNotIn("v19.0", called_get_url)
        self.assertNotIn("v26.0", called_get_url)

    @patch("publishing.adapters.facebook_adapter.requests.get")
    @patch("publishing.adapters.facebook_adapter.requests.post")
    def test_photo_local_file_upload_endpoint_uses_v27(self, mock_post, mock_get):
        """Photo multipart file upload must target /v27.0/{page_id}/photos."""
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            f.write(b"fake_image_bytes")
            temp_path = f.name

        try:
            mock_post_resp = MagicMock()
            mock_post_resp.status_code = 200
            mock_post_resp.json.return_value = {
                "id": "photo_local_777",
                "post_id": f"{self.page_id}_888999",
            }
            mock_post.return_value = mock_post_resp

            mock_get_resp = MagicMock()
            mock_get_resp.status_code = 200
            mock_get_resp.json.return_value = {
                "id": "photo_local_777",
                "link": "https://www.facebook.com/photo/local_777",
                "images": [{"source": "https://example.com/local.jpg"}],
            }
            mock_get.return_value = mock_get_resp

            res = self.adapter._execute_live_publish(
                content={
                    "content": "Photo post local file",
                    "image_path": temp_path,
                },
                account_config={"account": "LifeModeHQ"},
            )

            self.assertEqual(res["status"], STATUS_PUBLISHED)
            mock_post.assert_called_once()
            called_post_url = mock_post.call_args[0][0]
            expected_post_url = f"https://graph.facebook.com/v27.0/{self.page_id}/photos"
            self.assertEqual(called_post_url, expected_post_url)
            self.assertNotIn("v19.0", called_post_url)
            self.assertNotIn("v26.0", called_post_url)
        finally:
            Path(temp_path).unlink(missing_ok=True)

    @patch("publishing.adapters.facebook_adapter.requests.get")
    @patch("publishing.adapters.facebook_adapter.requests.post")
    def test_video_publish_and_verification_endpoints_use_v27(self, mock_post, mock_get):
        """Video publish must target graph-video.facebook.com/v27.0 and verification must target graph.facebook.com/v27.0."""
        mock_post_resp = MagicMock()
        mock_post_resp.status_code = 200
        mock_post_resp.json.return_value = {
            "id": "video_vid_999",
            "post_id": f"{self.page_id}_video_post_1000",
        }
        mock_post.return_value = mock_post_resp

        mock_get_resp = MagicMock()
        mock_get_resp.status_code = 200
        mock_get_resp.json.return_value = {
            "id": "video_vid_999",
            "permalink_url": "/LifeModeHQ/videos/video_vid_999/",
            "status": {"video_status": "ready"},
        }
        mock_get.return_value = mock_get_resp

        res = self.adapter._execute_live_publish(
            content={
                "content": "Video post caption",
                "video_url": "https://pub-r2.dev/video.mp4",
            },
            account_config={"account": "LifeModeHQ"},
        )

        self.assertEqual(res["status"], STATUS_PUBLISHED)

        # Video POST upload URL
        mock_post.assert_called_once()
        called_post_url = mock_post.call_args[0][0]
        expected_video_url = f"https://graph-video.facebook.com/v27.0/{self.page_id}/videos"
        self.assertEqual(called_post_url, expected_video_url)
        self.assertNotIn("v19.0", called_post_url)
        self.assertNotIn("v26.0", called_post_url)

        # Video GET verification URL
        mock_get.assert_called_once()
        called_get_url = mock_get.call_args[0][0]
        self.assertTrue(called_get_url.startswith("https://graph.facebook.com/v27.0/video_vid_999"))
        self.assertNotIn("v19.0", called_get_url)
        self.assertNotIn("v26.0", called_get_url)


class TestFacebookVerificationScript(unittest.TestCase):
    """Test the safe authentication-only verification script logic."""

    @patch("scripts.verify_facebook_token.safe_api_get")
    def test_run_verification_success(self, mock_get):
        from scripts.verify_facebook_token import run_verification

        # Mock /me
        # Mock /{page_id}
        # Mock /me/permissions
        mock_get.side_effect = [
            (200, {"id": "226949230493910", "name": "LifeModeHQ"}),
            (200, {"id": "226949230493910", "name": "LifeModeHQ", "tasks": ["CREATE_CONTENT", "MANAGE"]}),
            (200, {"data": [
                {"permission": "pages_manage_posts", "status": "granted"},
                {"permission": "pages_read_engagement", "status": "granted"},
                {"permission": "pages_show_list", "status": "granted"},
                {"permission": "publish_video", "status": "granted"},
            ]}),
        ]

        result = run_verification(token="valid_token_xyz", page_id="226949230493910")
        self.assertTrue(result)
        self.assertEqual(mock_get.call_count, 3)

    @patch("scripts.verify_facebook_token.safe_api_get")
    def test_run_verification_expired_token(self, mock_get):
        from scripts.verify_facebook_token import run_verification

        mock_get.return_value = (400, {
            "error": {
                "message": "Invalid OAuth access token - Cannot parse access token",
                "code": 190,
                "type": "OAuthException",
            }
        })

        result = run_verification(token="expired_token_xyz", page_id="226949230493910")
        self.assertFalse(result)

    def test_run_verification_missing_token(self):
        from scripts.verify_facebook_token import run_verification
        result = run_verification(token="", page_id="226949230493910")
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
