"""Focused tests for everydayideaslife Pinterest OAuth scopes, verification, and persistence."""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from publishing.pinterest_oauth import (
    DAILYIDEASFORYOU_SCOPES,
    DAILYIDEASFORYOU_TOKEN_ENV,
    DEFAULT_REDIRECT_URI,
    LIFEMODEHQ_TOKEN_ENV,
    PINTEREST_APP_ID,
    _normalize_name,
    build_authorization_url,
    persist_dailyideasforyou_token_and_board,
    persist_lifemodehq_token_and_board,
    verify_account_and_board,
)
from scripts.authorize_pinterest_dailyideasforyou import (
    TARGET_BOARD,
    TARGET_BOARD_ID,
    TARGET_USERNAME,
)


class TestEverydayIdeasLifeOAuth(unittest.TestCase):
    def test_target_account_and_board_configuration(self):
        self.assertEqual(TARGET_USERNAME, "everydayideaslife")
        self.assertEqual(TARGET_BOARD, "Shopping ideas")
        self.assertEqual(TARGET_BOARD_ID, "1033646620665866548")

    def test_board_name_normalization_casing_and_whitespace(self):
        self.assertEqual(_normalize_name("Shopping Ideas"), "shopping ideas")
        self.assertEqual(_normalize_name("Shopping ideas"), "shopping ideas")
        self.assertEqual(_normalize_name("  Shopping \t Ideas\n"), "shopping ideas")
        self.assertEqual(_normalize_name("SHOPPING IDEAS"), "shopping ideas")
        self.assertEqual(
            _normalize_name("Shopping Ideas"),
            _normalize_name("Shopping ideas"),
        )
        self.assertEqual(
            _normalize_name("  shopping    ideas  "),
            _normalize_name("Shopping Ideas"),
        )

    def test_dailyideasforyou_scopes_include_boards_write(self):
        expected_scopes = (
            "pins:read",
            "pins:write",
            "boards:read",
            "boards:write",
            "user_accounts:read",
        )
        self.assertEqual(DAILYIDEASFORYOU_SCOPES, expected_scopes)
        self.assertIn("boards:write", DAILYIDEASFORYOU_SCOPES)

    def test_authorization_url_contains_exact_requested_scopes_and_redirect(self):
        url = build_authorization_url(
            state="test-state-456",
            redirect_uri=DEFAULT_REDIRECT_URI,
            client_id=PINTEREST_APP_ID,
            scopes=DAILYIDEASFORYOU_SCOPES,
        )
        self.assertIn("redirect_uri=http%3A%2F%2Flocalhost%3A8085%2Foauth%2Fcallback", url)
        query = parse_qs(urlparse(url).query)
        self.assertEqual(query["redirect_uri"], [DEFAULT_REDIRECT_URI])
        self.assertEqual(query["state"], ["test-state-456"])
        scopes = query["scope"][0].split(",")
        self.assertEqual(
            set(scopes),
            {"pins:read", "pins:write", "boards:read", "boards:write", "user_accounts:read"},
        )
        self.assertEqual(len(scopes), 5)
        self.assertIn("boards:write", scopes)

    @patch("publishing.pinterest_oauth._api_get")
    def test_verification_rejects_wrong_account_names(self, mock_get):
        for wrong_username in ("LifeModeHQ", "dailyideasforyou", "wrong_user"):
            mock_get.side_effect = [
                {"username": wrong_username},
            ]
            with self.assertRaisesRegex(RuntimeError, f"not '{TARGET_USERNAME}'"):
                verify_account_and_board(
                    "token-xyz",
                    expected_username=TARGET_USERNAME,
                    board_name=TARGET_BOARD,
                    expected_board_id=TARGET_BOARD_ID,
                )

    @patch("publishing.pinterest_oauth._api_get")
    def test_verification_prefers_and_confirms_configured_board_id_with_title_casing(self, mock_get):
        # Pinterest UI has "Shopping Ideas" (capital I)
        mock_get.side_effect = [
            {"username": "everydayideaslife"},
            {
                "id": TARGET_BOARD_ID,
                "name": "Shopping Ideas",
                "owner": {"username": "everydayideaslife"},
            },
        ]
        account, board = verify_account_and_board(
            "token-xyz",
            expected_username=TARGET_USERNAME,
            board_name=TARGET_BOARD,
            expected_board_id=TARGET_BOARD_ID,
        )
        self.assertEqual(account["username"], "everydayideaslife")
        self.assertEqual(board["id"], TARGET_BOARD_ID)
        self.assertEqual(board["name"], "Shopping Ideas")
        # Ensure only /user_account and /boards/1033646620665866548 were requested
        self.assertEqual(mock_get.call_count, 2)
        self.assertEqual(mock_get.call_args_list[0][0][0], "/user_account")
        self.assertEqual(mock_get.call_args_list[1][0][0], f"/boards/{TARGET_BOARD_ID}")

    @patch("publishing.pinterest_oauth._api_get")
    def test_verification_rejects_board_id_belonging_to_another_account(self, mock_get):
        mock_get.side_effect = [
            {"username": "everydayideaslife"},
            {
                "id": TARGET_BOARD_ID,
                "name": "Shopping Ideas",
                "owner": {"username": "some_other_account"},
            },
        ]
        with self.assertRaisesRegex(RuntimeError, "belongs to 'some_other_account', not 'everydayideaslife'"):
            verify_account_and_board(
                "token-xyz",
                expected_username=TARGET_USERNAME,
                board_name=TARGET_BOARD,
                expected_board_id=TARGET_BOARD_ID,
            )

    @patch("publishing.pinterest_oauth._api_get")
    def test_verification_falls_back_to_paginated_boards_if_direct_id_fails(self, mock_get):
        # Direct lookup fails with 404, then pagination retrieves 2 pages
        mock_get.side_effect = [
            {"username": "everydayideaslife"},
            RuntimeError("Pinterest API verification failed for /boards/1033646620665866548 with HTTP 404."),
            {
                "items": [{"id": "other-board-1", "name": "Recipes", "owner": {"username": "everydayideaslife"}}],
                "bookmark": "page-2-bookmark",
            },
            {
                "items": [
                    {"id": "other-board-2", "name": "Fashion"},
                    {
                        "id": "1033646620665866548",
                        "name": "Shopping Ideas",
                        "owner": {"username": "everydayideaslife"},
                    },
                ],
                "bookmark": None,
            },
        ]
        account, board = verify_account_and_board(
            "token-xyz",
            expected_username=TARGET_USERNAME,
            board_name=TARGET_BOARD,
            expected_board_id=TARGET_BOARD_ID,
        )
        self.assertEqual(account["username"], "everydayideaslife")
        self.assertEqual(board["id"], "1033646620665866548")
        self.assertEqual(board["name"], "Shopping Ideas")
        # Confirms pagination occurred
        self.assertEqual(mock_get.call_count, 4)

    @patch("publishing.pinterest_oauth._api_get")
    def test_verification_skips_foreign_boards_during_pagination(self, mock_get):
        mock_get.side_effect = [
            {"username": "everydayideaslife"},
            # /boards list without direct lookup
            {
                "items": [
                    {"id": "foreign-id", "name": "Shopping Ideas", "owner": {"username": "collaborator_user"}},
                    {"id": "my-id", "name": "Shopping Ideas", "owner": {"username": "everydayideaslife"}},
                ],
                "bookmark": None,
            },
        ]
        account, board = verify_account_and_board(
            "token-xyz",
            expected_username=TARGET_USERNAME,
            board_name=TARGET_BOARD,
            expected_board_id=None,
        )
        self.assertEqual(board["id"], "my-id")

    @patch("publishing.pinterest_oauth._api_get")
    def test_verification_fails_if_board_not_found_on_any_page(self, mock_get):
        mock_get.side_effect = [
            {"username": "everydayideaslife"},
            {"items": [{"id": "board-1", "name": "Kitchen Decor"}], "bookmark": "bm1"},
            {"items": [{"id": "board-2", "name": "Garden Ideas"}], "bookmark": None},
        ]
        with self.assertRaisesRegex(RuntimeError, "board 'Shopping ideas' was not found"):
            verify_account_and_board(
                "token-xyz",
                expected_username=TARGET_USERNAME,
                board_name=TARGET_BOARD,
                expected_board_id=None,
            )

    def test_persistence_updates_dailyideasforyou_and_preserves_lifemodehq_credentials(self):
        test_root = PROJECT_ROOT / ".test-tmp"
        test_root.mkdir(exist_ok=True)
        try:
            with tempfile.TemporaryDirectory(dir=test_root) as temp_dir:
                root = Path(temp_dir)
                env_path = root / ".env"
                config_path = root / "config.json"

                initial_env = (
                    "DRY_RUN=false\n"
                    "META_ACCESS_TOKEN=meta-secret-123\n"
                    f"{LIFEMODEHQ_TOKEN_ENV}=lifemode-secret-456\n"
                    f"{DAILYIDEASFORYOU_TOKEN_ENV}=old-dailyideas-secret\n"
                    "PINTEREST_APP_SECRET=app-secret-789\n"
                )
                env_path.write_text(initial_env, encoding="utf-8")

                initial_config = {
                    "publishing": {
                        "pinterest_accounts": {
                            "dailyideasforyou": {
                                "account": "dailyideasforyou",
                                "board": "Shopping ideas",
                                "board_id": "old-board-id",
                            },
                            "lifemodehq": {
                                "account": "LifeModeHQ",
                                "board": "Home & Everyday Living",
                                "board_id": "495607202683261042",
                            },
                        }
                    }
                }
                config_path.write_text(json.dumps(initial_config, indent=2), encoding="utf-8")

                persist_dailyideasforyou_token_and_board(
                    "new-dailyideas-token-999",
                    "1033646620665866548",
                    env_path=env_path,
                    config_path=config_path,
                )

                updated_env = env_path.read_text(encoding="utf-8")
                self.assertIn(f"{DAILYIDEASFORYOU_TOKEN_ENV}=new-dailyideas-token-999", updated_env)
                self.assertIn(f"{LIFEMODEHQ_TOKEN_ENV}=lifemode-secret-456", updated_env)
                self.assertIn("META_ACCESS_TOKEN=meta-secret-123", updated_env)
                self.assertIn("PINTEREST_APP_SECRET=app-secret-789", updated_env)
                self.assertIn("DRY_RUN=false", updated_env)
                self.assertNotIn("old-dailyideas-secret", updated_env)

                updated_config = json.loads(config_path.read_text(encoding="utf-8"))
                self.assertEqual(
                    updated_config["publishing"]["pinterest_accounts"]["dailyideasforyou"]["board_id"],
                    "1033646620665866548",
                )
                self.assertEqual(
                    updated_config["publishing"]["pinterest_accounts"]["lifemodehq"]["board_id"],
                    "495607202683261042",
                )

                # Test mutual isolation: updating lifemodehq does not alter dailyideasforyou token or board
                persist_lifemodehq_token_and_board(
                    "new-lifemode-token-777",
                    "495607202683261042",
                    env_path=env_path,
                    config_path=config_path,
                )
                re_updated_env = env_path.read_text(encoding="utf-8")
                self.assertIn(f"{DAILYIDEASFORYOU_TOKEN_ENV}=new-dailyideas-token-999", re_updated_env)
                self.assertIn(f"{LIFEMODEHQ_TOKEN_ENV}=new-lifemode-token-777", re_updated_env)
                self.assertNotIn("lifemode-secret-456", re_updated_env)
        finally:
            import shutil
            shutil.rmtree(test_root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
