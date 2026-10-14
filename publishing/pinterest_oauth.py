"""Secure local Pinterest OAuth helpers for account-specific authorization."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, Optional, Tuple
from urllib.parse import urlencode

import requests


PINTEREST_APP_ID = "1518240"
PINTEREST_AUTH_URL = "https://www.pinterest.com/oauth/"
PINTEREST_TOKEN_URL = "https://api.pinterest.com/v5/oauth/token"
PINTEREST_API_URL = "https://api.pinterest.com/v5"
DEFAULT_REDIRECT_URI = "http://localhost:8085/oauth/callback"
LIFEMODEHQ_TOKEN_ENV = "PINTEREST_LIFEMODEHQ_ACCESS_TOKEN"
REQUIRED_SCOPES = (
    "pins:read",
    "pins:write",
    "boards:read",
    "boards:write",
    "user_accounts:read",
)


def generate_oauth_state() -> str:
    """Generate an unpredictable, single-use OAuth state value."""
    return base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")


def validate_oauth_state(expected_state: str, received_state: str) -> bool:
    """Constant-time validation of the callback state parameter."""
    return bool(expected_state and received_state) and hmac.compare_digest(expected_state, received_state)


def build_authorization_url(
    state: str,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    client_id: str = PINTEREST_APP_ID,
    scopes: Iterable[str] = REQUIRED_SCOPES,
) -> str:
    """Build the Pinterest authorization URL without including any secret."""
    scope_value = ",".join(dict.fromkeys(scopes))
    return PINTEREST_AUTH_URL + "?" + urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope_value,
        "state": state,
    })


def exchange_authorization_code(
    code: str,
    client_secret: str,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    client_id: str = PINTEREST_APP_ID,
    session: Optional[requests.Session] = None,
) -> Dict[str, Any]:
    """Exchange a callback code for a token without logging credentials."""
    if not code or not client_secret:
        raise ValueError("Pinterest OAuth code and app secret are required.")
    http = session or requests.Session()
    response = http.post(
        PINTEREST_TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
        },
        auth=(client_id, client_secret),
        timeout=30,
    )
    if response.status_code not in (200, 201):
        raise RuntimeError(f"Pinterest OAuth token exchange failed with HTTP {response.status_code}.")
    payload = response.json()
    if not payload.get("access_token"):
        raise RuntimeError("Pinterest OAuth token exchange returned no access token.")
    return payload


def _api_get(path: str, access_token: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    response = requests.get(
        f"{PINTEREST_API_URL}{path}",
        headers={"Authorization": f"Bearer {access_token}"},
        params=params or {},
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Pinterest API verification failed for {path} with HTTP {response.status_code}.")
    return response.json()


def verify_account_and_board(
    access_token: str,
    expected_username: str = "LifeModeHQ",
    board_name: str = "Home & Everyday Living",
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Verify account identity and resolve the board ID for that account."""
    if not access_token:
        raise ValueError(f"{LIFEMODEHQ_TOKEN_ENV} is required for Pinterest verification.")
    account = _api_get("/user_account", access_token)
    actual_username = str(account.get("username", ""))
    if actual_username.casefold() != expected_username.casefold():
        raise RuntimeError(
            f"Pinterest token belongs to '{actual_username or 'unknown'}', not '{expected_username}'."
        )

    bookmark = None
    for _ in range(10):
        params: Dict[str, Any] = {"page_size": 100}
        if bookmark:
            params["bookmark"] = bookmark
        page = _api_get("/boards", access_token, params)
        for board in page.get("items", []):
            if board.get("name") == board_name:
                return account, board
        bookmark = (page.get("bookmark") or "").strip()
        if not bookmark:
            break
    raise RuntimeError(f"Pinterest board '{board_name}' was not found for account '{expected_username}'.")


def persist_lifemodehq_token_and_board(
    access_token: str,
    board_id: str,
    env_path: Path = Path(".env"),
    config_path: Path = Path("config.json"),
) -> None:
    """Persist only local ignored secrets and the verified account-specific board ID."""
    if not access_token or not board_id:
        raise ValueError("Verified LifeModeHQ access token and board ID are required.")

    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    replacement = f"{LIFEMODEHQ_TOKEN_ENV}={access_token}"
    found = False
    updated = []
    for line in lines:
        if line.strip().startswith(f"{LIFEMODEHQ_TOKEN_ENV}="):
            updated.append(replacement)
            found = True
        else:
            updated.append(line)
    if not found:
        updated.append(replacement)
    env_path.write_text("\n".join(updated).rstrip() + "\n", encoding="utf-8")

    config = json.loads(config_path.read_text(encoding="utf-8"))
    account_cfg = config.setdefault("publishing", {}).setdefault("pinterest_accounts", {}).setdefault("lifemodehq", {})
    account_cfg["board_id"] = str(board_id)
    config_path.write_text(json.dumps(config, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
