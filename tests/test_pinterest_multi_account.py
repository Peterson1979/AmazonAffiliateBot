"""Focused offline coverage for independently routed Pinterest accounts."""

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from publishing.adapters.pinterest_adapter import PinterestAdapter
from publishing.publisher import SocialPublisher
from publishing.adapters import STATUS_PUBLISHED, STATUS_WOULD_PUBLISH


class TestPinterestMultiAccount(unittest.TestCase):
    def setUp(self):
        self.package = {
            "asin": "B0MULTIPIN1",
            "status": "READY_FOR_REVIEW",
            "platforms": {
                "facebook": {"status": "VALID", "content": "Facebook copy"},
                "pinterest": {
                    "status": "VALID",
                    "title": "Pin title",
                    "description": "Pin description",
                    "image_url": "https://m.media-amazon.com/images/I/multi.jpg",
                },
                "threads": {"status": "VALID", "content": "Threads copy"},
            },
        }

    def test_dry_run_routes_both_accounts_independently(self):
        publisher = SocialPublisher(dry_run=True)

        report = publisher.publish_package(self.package)

        self.assertEqual(report["results"]["pinterest"]["account"], "dailyideasforyou")
        self.assertEqual(report["results"]["pinterest"]["status"], STATUS_WOULD_PUBLISH)
        self.assertEqual(report["results"]["pinterest_lifemodehq"]["account"], "LifeModeHQ")
        self.assertEqual(report["results"]["pinterest_lifemodehq"]["status"], STATUS_WOULD_PUBLISH)

    def test_prior_daily_success_does_not_suppress_lifemode_account(self):
        publisher = SocialPublisher(dry_run=True)

        report = publisher.publish_package(
            self.package,
            prior_results={"pinterest": {"status": STATUS_PUBLISHED, "pin_id": "daily-pin"}},
        )

        self.assertEqual(report["results"]["pinterest"]["action"], "SKIP_DUPLICATE")
        self.assertEqual(report["results"]["pinterest_lifemodehq"]["status"], STATUS_WOULD_PUBLISH)

    def test_lifemode_duplicate_is_skipped_without_adapter_call(self):
        publisher = SocialPublisher(dry_run=False)
        publisher.pinterest_adapters["lifemodehq"].publish = MagicMock()

        result = publisher.publish_pinterest_account(
            self.package,
            "lifemodehq",
            dry_run=False,
            prior_result={"status": STATUS_PUBLISHED, "pin_id": "existing-life-pin"},
        )

        self.assertEqual(result["action"], "SKIP_DUPLICATE")
        publisher.pinterest_adapters["lifemodehq"].publish.assert_not_called()

    @patch("publishing.adapters.pinterest_adapter.requests.post")
    @patch("publishing.adapters.pinterest_adapter.requests.get")
    def test_lifemode_board_is_resolved_per_account_before_pin(self, mock_get, mock_post):
        board_response = MagicMock(status_code=200)
        board_response.json.return_value = {
            "items": [{"id": "life-board-123", "name": "Home & Everyday Living"}]
        }
        pin_response = MagicMock(status_code=201)
        pin_response.json.return_value = {"id": "life-pin-123"}
        mock_get.return_value = board_response
        mock_post.return_value = pin_response

        adapter = PinterestAdapter(credentials={"access_token": "life-token"})
        result = adapter._execute_live_publish(
            self.package["platforms"]["pinterest"],
            {"account": "LifeModeHQ", "board": "Home & Everyday Living"},
        )

        self.assertEqual(result["status"], STATUS_PUBLISHED)
        self.assertEqual(result["board_id"], "life-board-123")
        self.assertEqual(mock_get.call_count, 1)
        payload = mock_post.call_args.kwargs["json"]
        self.assertEqual(payload["board_id"], "life-board-123")


if __name__ == "__main__":
    unittest.main()
