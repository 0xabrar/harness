from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from harness_app_server import AppServerError, JsonRpcConnection  # noqa: E402


class BuildRequestMessageTests(unittest.TestCase):
    """Tests for JsonRpcConnection._build_request."""

    def test_build_request_message(self) -> None:
        # We need a dummy proc for __init__; use a simple object with .stdout = None
        # so the reader thread exits immediately.
        conn = _make_connection()

        line = conn._build_request("initialize", {"clientInfo": {"name": "test"}})
        msg = json.loads(line)

        self.assertEqual(msg["id"], 1)
        self.assertEqual(msg["method"], "initialize")
        self.assertEqual(msg["params"], {"clientInfo": {"name": "test"}})

    def test_build_request_auto_increments_id(self) -> None:
        conn = _make_connection()

        msg1 = json.loads(conn._build_request("initialize", {}))
        msg2 = json.loads(conn._build_request("thread/start", {}))

        self.assertEqual(msg1["id"], 1)
        self.assertEqual(msg2["id"], 2)

    def test_build_request_omits_params_when_none(self) -> None:
        conn = _make_connection()

        msg = json.loads(conn._build_request("ping"))

        self.assertNotIn("params", msg)
        self.assertEqual(msg["method"], "ping")


class BuildNotificationMessageTests(unittest.TestCase):
    """Tests for JsonRpcConnection._build_notification."""

    def test_build_notification_message(self) -> None:
        conn = _make_connection()

        line = conn._build_notification("initialized", {})
        msg = json.loads(line)

        self.assertNotIn("id", msg)
        self.assertEqual(msg["method"], "initialized")
        self.assertEqual(msg["params"], {})

    def test_build_notification_omits_params_when_none(self) -> None:
        conn = _make_connection()

        msg = json.loads(conn._build_notification("initialized"))

        self.assertNotIn("id", msg)
        self.assertNotIn("params", msg)
        self.assertEqual(msg["method"], "initialized")


class ParseResponseTests(unittest.TestCase):
    """Tests for JsonRpcConnection._parse_line — response messages."""

    def test_parse_response(self) -> None:
        line = json.dumps({"id": 1, "result": {"serverInfo": {"name": "codex"}}})
        parsed = JsonRpcConnection._parse_line(line)

        self.assertEqual(parsed["type"], "response")
        self.assertEqual(parsed["id"], 1)
        self.assertEqual(parsed["result"]["serverInfo"]["name"], "codex")

    def test_parse_response_without_result(self) -> None:
        line = json.dumps({"id": 42})
        parsed = JsonRpcConnection._parse_line(line)

        self.assertEqual(parsed["type"], "response")
        self.assertEqual(parsed["id"], 42)
        self.assertEqual(parsed["result"], {})


class ParseNotificationTests(unittest.TestCase):
    """Tests for JsonRpcConnection._parse_line — notification messages."""

    def test_parse_notification(self) -> None:
        line = json.dumps({"method": "turn/completed", "params": {"threadId": "t1"}})
        parsed = JsonRpcConnection._parse_line(line)

        self.assertEqual(parsed["type"], "notification")
        self.assertEqual(parsed["method"], "turn/completed")
        self.assertEqual(parsed["params"]["threadId"], "t1")

    def test_parse_notification_without_params(self) -> None:
        line = json.dumps({"method": "heartbeat"})
        parsed = JsonRpcConnection._parse_line(line)

        self.assertEqual(parsed["type"], "notification")
        self.assertEqual(parsed["method"], "heartbeat")
        self.assertEqual(parsed["params"], {})


class ParseErrorResponseTests(unittest.TestCase):
    """Tests for JsonRpcConnection._parse_line — error responses."""

    def test_parse_error_response(self) -> None:
        line = json.dumps({
            "id": 3,
            "error": {"code": -32600, "message": "Invalid request"},
        })
        parsed = JsonRpcConnection._parse_line(line)

        self.assertEqual(parsed["type"], "error")
        self.assertEqual(parsed["id"], 3)
        self.assertEqual(parsed["error"]["code"], -32600)
        self.assertEqual(parsed["error"]["message"], "Invalid request")

    def test_parse_error_response_with_data(self) -> None:
        line = json.dumps({
            "id": 5,
            "error": {"code": -32001, "message": "Busy", "data": {"retry": True}},
        })
        parsed = JsonRpcConnection._parse_line(line)

        self.assertEqual(parsed["type"], "error")
        self.assertEqual(parsed["error"]["data"]["retry"], True)


class ParseServerRequestTests(unittest.TestCase):
    """Tests for JsonRpcConnection._parse_line — server-initiated requests."""

    def test_parse_server_request(self) -> None:
        line = json.dumps({"id": 10, "method": "confirm/action", "params": {"prompt": "Allow?"}})
        parsed = JsonRpcConnection._parse_line(line)

        self.assertEqual(parsed["type"], "server_request")
        self.assertEqual(parsed["id"], 10)
        self.assertEqual(parsed["method"], "confirm/action")
        self.assertEqual(parsed["params"]["prompt"], "Allow?")


class ParseEdgeCaseTests(unittest.TestCase):
    """Edge cases for _parse_line."""

    def test_empty_line_returns_empty_type(self) -> None:
        parsed = JsonRpcConnection._parse_line("")
        self.assertEqual(parsed["type"], "empty")

    def test_whitespace_only_returns_empty_type(self) -> None:
        parsed = JsonRpcConnection._parse_line("   \t  ")
        self.assertEqual(parsed["type"], "empty")

    def test_invalid_json_raises(self) -> None:
        with self.assertRaises(json.JSONDecodeError):
            JsonRpcConnection._parse_line("not json at all")


class AppServerErrorTests(unittest.TestCase):
    """Tests for AppServerError exception."""

    def test_error_with_code_and_data(self) -> None:
        err = AppServerError("fail", code=-32600, data={"detail": "bad"})
        self.assertEqual(str(err), "fail")
        self.assertEqual(err.code, -32600)
        self.assertEqual(err.data, {"detail": "bad"})

    def test_error_defaults(self) -> None:
        err = AppServerError("plain error")
        self.assertIsNone(err.code)
        self.assertIsNone(err.data)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeProc:
    """Minimal stand-in for subprocess.Popen so JsonRpcConnection.__init__
    starts its reader thread (which exits immediately on stdout=None).
    """
    def __init__(self) -> None:
        self.stdout = None
        self.stdin = None

    def poll(self) -> int | None:
        return 0

    def terminate(self) -> None:
        pass

    def kill(self) -> None:
        pass

    def wait(self, timeout: float | None = None) -> int:
        return 0


def _make_connection() -> JsonRpcConnection:
    return JsonRpcConnection(_FakeProc())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
