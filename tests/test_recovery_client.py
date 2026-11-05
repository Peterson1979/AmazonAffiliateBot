"""Focused tests for safe recovery response capture."""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.recovery_client import post_recovery


class DisposableResponse:
    def __init__(self, status_code, headers, body):
        self.status_code = status_code
        self.headers = headers
        self._body = body
        self.closed = False

    @property
    def text(self):
        if self.closed:
            raise RuntimeError("response body was disposed")
        return self._body

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.response


class TestRecoveryClient(unittest.TestCase):
    def test_captures_json_before_response_is_closed_and_does_not_redirect(self):
        response = DisposableResponse(200, {"Content-Type": "application/json"}, '{"success": false, "message": "safe error"}')
        session = FakeSession(response)

        result = post_recovery(session, "http://127.0.0.1:5000", "B0H4Z5W52B", "batch-test")

        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["json"]["message"], "safe error")
        self.assertTrue(response.closed)
        self.assertEqual(session.calls[0][1]["allow_redirects"], False)
        self.assertEqual(session.calls[0][1]["headers"]["Accept"], "application/json")

    def test_captures_non_json_error_status_and_body_before_close(self):
        response = DisposableResponse(500, {"Content-Type": "text/plain"}, "server error")
        session = FakeSession(response)

        result = post_recovery(session, "http://127.0.0.1:5000", "B0H4Z5W52B", "batch-test")

        self.assertEqual(result["status_code"], 500)
        self.assertEqual(result["body"], "server error")
        self.assertIsNone(result["json"])
        self.assertTrue(response.closed)


if __name__ == "__main__":
    unittest.main()
