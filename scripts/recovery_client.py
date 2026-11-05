"""Small, non-redirecting HTTP client for the explicit recovery endpoint.

This module is intentionally not called by the application automatically. It
exists so a future operator-triggered recovery can capture an error response
before the HTTP response object is closed.
"""

import argparse
import json
from typing import Any, Dict, Optional

import requests


def post_recovery(
    session: Any,
    base_url: str,
    asin: str,
    batch_id: str,
    timeout: int = 120,
) -> Dict[str, Any]:
    """POST once and capture response data before closing the response."""
    url = f"{base_url.rstrip('/')}/api/recover-failed/{asin}?batch_id={batch_id}"
    response = session.post(
        url,
        headers={"Accept": "application/json"},
        allow_redirects=False,
        timeout=timeout,
    )
    try:
        # Read all response fields while the response is still alive. This is
        # the important lifetime boundary for clients that dispose response
        # content as soon as the request context exits.
        status_code = response.status_code
        headers = dict(response.headers or {})
        body = response.text
        parsed: Optional[Dict[str, Any]] = None
        try:
            value = json.loads(body)
            if isinstance(value, dict):
                parsed = value
        except (TypeError, ValueError):
            pass
        return {
            "status_code": status_code,
            "headers": headers,
            "body": body,
            "json": parsed,
        }
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture one explicit recovery response without following redirects.")
    parser.add_argument("--base-url", default="http://127.0.0.1:5000")
    parser.add_argument("--asin", required=True)
    parser.add_argument("--batch-id", required=True)
    args = parser.parse_args()
    result = post_recovery(requests.Session(), args.base_url, args.asin, args.batch_id)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status_code"] < 400 else 1


if __name__ == "__main__":
    raise SystemExit(main())
