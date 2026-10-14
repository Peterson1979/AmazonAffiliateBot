"""Authorize LifeModeHQ for Pinterest v5 and verify its target board.

This local helper never creates Pins and never prints tokens or app secrets.
"""

from __future__ import annotations

import os
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Allow the documented repository-root invocation to import project packages.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from publishing.pinterest_oauth import (
    DEFAULT_REDIRECT_URI,
    LIFEMODEHQ_TOKEN_ENV,
    PINTEREST_APP_ID,
    REQUIRED_SCOPES,
    build_authorization_url,
    exchange_authorization_code,
    generate_oauth_state,
    persist_lifemodehq_token_and_board,
    validate_oauth_state,
    verify_account_and_board,
)


class _CallbackHandler(BaseHTTPRequestHandler):
    result = {}

    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path != urlparse(DEFAULT_REDIRECT_URI).path:
            self.send_error(404)
            return
        values = parse_qs(parsed.query)
        _CallbackHandler.result = {key: values.get(key, [""])[0] for key in ("code", "state", "error")}
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"Pinterest authorization received. You may close this window.")

    def log_message(self, format, *args):  # noqa: A002
        return


def main() -> int:
    client_secret = os.environ.get("PINTEREST_APP_SECRET", "")
    if not client_secret:
        print("Missing PINTEREST_APP_SECRET in the local environment.")
        return 1

    redirect_uri = os.environ.get("PINTEREST_OAUTH_REDIRECT_URI", DEFAULT_REDIRECT_URI)
    parsed_redirect = urlparse(redirect_uri)
    if parsed_redirect.hostname not in ("localhost", "127.0.0.1") or not parsed_redirect.port:
        print("PINTEREST_OAUTH_REDIRECT_URI must be a registered localhost callback for this helper.")
        return 1

    state = generate_oauth_state()
    url = build_authorization_url(state, redirect_uri=redirect_uri, client_id=PINTEREST_APP_ID)
    server = HTTPServer((parsed_redirect.hostname, parsed_redirect.port), _CallbackHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    print(f"Open this URL to authorize LifeModeHQ: {url}")
    webbrowser.open(url)
    thread.join(timeout=300)
    server.server_close()

    callback = _CallbackHandler.result
    if callback.get("error"):
        print("Pinterest authorization was denied or cancelled.")
        return 1
    if not validate_oauth_state(state, callback.get("state", "")):
        print("Pinterest OAuth state validation failed.")
        return 1

    token_payload = exchange_authorization_code(callback.get("code", ""), client_secret, redirect_uri)
    account, board = verify_account_and_board(token_payload["access_token"])
    persist_lifemodehq_token_and_board(token_payload["access_token"], str(board["id"]))
    print(f"Authorized Pinterest account '{account.get('username')}' and verified board '{board.get('name')}'.")
    print(f"Stored the token in the ignored local .env file as {LIFEMODEHQ_TOKEN_ENV}.")
    print("Required scopes: " + ", ".join(REQUIRED_SCOPES))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
