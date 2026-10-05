"""Regressions for the bug-hunt fixes (encoding, request quirks, upstream safety)."""
import io
import json
import os
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from emutools.core import Config, ToolCall, ToolDef, CanonRequest, CanonMessage, utf8, _env_bool
from emutools.server import validate_request
from emutools.wire import anthropic_to_canon, openai_to_canon, iter_sse, upstream_complete, UpstreamError
import emutools.engine as engine

_SAVED_ENV = {}


def setUpModule():
    for key in [k for k in os.environ if k.startswith(("EMU_", "DEEPSEEK_"))]:
        _SAVED_ENV[key] = os.environ.pop(key)


def tearDownModule():
    os.environ.update(_SAVED_ENV)
    _SAVED_ENV.clear()


class _Resp(io.BytesIO):
    def read1(self, n=-1):
        return self.read(min(n, 7) if n and n > 0 else n)


class BugHuntTests(unittest.TestCase):
    def test_lone_surrogate_encodes(self):
        self.assertEqual(utf8(json.loads('"a\\ud83d"')), "a\ufffd".encode())
        self.assertEqual(utf8(json.loads('"\\ud83d\\ude00"')), "\U0001F600".encode())
        engine.sse("x", {"t": json.loads('"\\udc00"')})  # must not raise

    def test_anthropic_tool_choice_any_string(self):
        body = {"model": "m", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"name": "Read", "input_schema": {"type": "object"}}], "tool_choice": "any"}
        self.assertEqual(anthropic_to_canon(body, Config()).tool_choice, "required")

    def test_openai_nulls_mean_absent(self):
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}],
                "stream": None, "max_tokens": None, "tools": None, "temperature": None}
        validate_request(body, "openai")
        self.assertNotIn("stream", body)
        openai_to_canon(body, Config())

    def test_iter_sse_large_event_is_linear_and_correct(self):
        payload = json.dumps({"x": "y" * 300000})
        events = list(iter_sse(_Resp(("data: %s\n\n" % payload).encode())))
        self.assertEqual(len(events[0]["x"]), 300000)

    def test_non_finite_args_rejected(self):
        tool = ToolDef(name="T", description="", schema={"type": "object"})
        req = CanonRequest(model="m", messages=[], tools=[tool])
        issues = engine._call_issues(ToolCall(id="1", name="T", args={"a": [float("nan")]}), req, {"T": tool})
        self.assertTrue(issues)

    def test_env_bool_unknown_keeps_default(self):
        os.environ["EMU_X_TEST"] = "maybe"
        try:
            self.assertTrue(_env_bool("EMU_X_TEST", True))
        finally:
            del os.environ["EMU_X_TEST"]

    def test_upstream_redirect_not_followed(self):
        hits = []

        class H(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                hits.append(self.path)
                self.rfile.read(int(self.headers.get("content-length") or 0))
                self.send_response(307)
                self.send_header("location", "http://127.0.0.1:%d/stolen" % self.server.server_address[1])
                self.send_header("content-length", "0")
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            cfg = Config()
            cfg.upstream_base = "http://127.0.0.1:%d" % srv.server_address[1]
            cfg.upstream_key = "secret"
            cfg.connect_retries = 1
            with self.assertRaises(UpstreamError) as ctx:
                upstream_complete(cfg, {"model": "m", "messages": []})
            self.assertEqual(ctx.exception.status, 502)
            self.assertNotIn("/stolen", hits)
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    unittest.main()
