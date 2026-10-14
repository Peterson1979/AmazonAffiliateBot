"""Offline tests for Pinterest OAuth state, scopes, and account separation."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from publishing.pinterest_oauth import (
    DEFAULT_REDIRECT_URI,
    LIFEMODEHQ_TOKEN_ENV,
    REQUIRED_SCOPES,
    build_authorization_url,
    persist_lifemodehq_token_and_board,
    validate_oauth_state,
    verify_account_and_board,
)


class TestPinterestOAuth(unittest.TestCase):
    def test_state_validation_is_exact_and_constant_time_safe(self):
        self.assertTrue(validate_oauth_state("state-123", "state-123"))
        self.assertFalse(validate_oauth_state("state-123", "state-124"))
        self.assertFalse(validate_oauth_state("", "state-123"))

    def test_authorization_url_requests_required_scopes_and_registered_redirect(self):
        url = build_authorization_url("state-123")
        self.assertIn("redirect_uri=http%3A%2F%2Flocalhost%3A8085%2Foauth%2Fcallback", url)
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["redirect_uri"], [DEFAULT_REDIRECT_URI])
        self.assertEqual(
            set(query["scope"][0].split(",")),
            {
                "pins:read",
                "pins:write",
                "boards:read",
                "boards:write",
                "user_accounts:read",
            },
        )
        self.assertEqual(len(query["scope"][0].split(",")), 5)
        self.assertEqual(query["scope"][0].split(","), list(REQUIRED_SCOPES))

    @patch("publishing.pinterest_oauth._api_get")
    def test_account_verification_does_not_accept_another_account_board(self, mock_get):
        mock_get.side_effect = [
            {"username": "DailyIdeasForYou"},
            {"items": [{"id": "daily-board", "name": "Home & Everyday Living"}]},
        ]
        with self.assertRaisesRegex(RuntimeError, "not 'LifeModeHQ'"):
            verify_account_and_board("life-token")

    def test_persistence_uses_lifemode_token_and_account_specific_board(self):
        test_root = PROJECT_ROOT / ".test-tmp"
        test_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=test_root) as temp_dir:
            root = Path(temp_dir)
            env_path = root / ".env"
            config_path = root / "config.json"
            config_path.write_text(json.dumps({"publishing": {"pinterest_accounts": {"lifemodehq": {}}}}), encoding="utf-8")
            persist_lifemodehq_token_and_board("secret-token", "life-board", env_path, config_path)
            self.assertIn(f"{LIFEMODEHQ_TOKEN_ENV}=secret-token", env_path.read_text(encoding="utf-8"))
            self.assertEqual(
                json.loads(config_path.read_text(encoding="utf-8"))["publishing"]["pinterest_accounts"]["lifemodehq"]["board_id"],
                "life-board",
            )


if __name__ == "__main__":
    unittest.main()
