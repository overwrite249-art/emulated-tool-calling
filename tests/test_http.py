"""Real-socket tests of inbound framing, config isolation and SSE transport."""
import http.client
import io
import json
import socket
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from emutools.core import Config
from emutools.server import Handler, Server
from emutools.wire import iter_sse, upstream_stream, UpstreamError


BODY = {"model": "test-model", "messages": [{"role": "user", "content": "hi"}]}


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = Config(model_big="isolated-pro", model_small="isolated-flash", max_request_bytes=1024,
                         client_timeout=1, upstream_key="", upstream_base="http://127.0.0.1:1", connect_retries=1)
        cls.server = Server(("127.0.0.1", 0), Handler, cfg=cls.cfg)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(3)

    def exchange(self, data, responses=1):
        with socket.create_connection(("127.0.0.1", self.port), timeout=3) as sock:
            sock.sendall(data)
            sock.shutdown(socket.SHUT_WR)
            with sock.makefile("rb") as f:
                out = []
                for _ in range(responses):
                    status_line = f.readline()
                    self.assertTrue(status_line.startswith(b"HTTP/1.1 "), status_line)
                    status = int(status_line.split()[1])
                    headers = {}
                    while True:
                        line = f.readline()
                        if line in (b"\r\n", b""):
                            break
                        k, v = line.decode().split(":", 1)
                        headers[k.lower()] = v.strip()
                    body = f.read(int(headers.get("content-length", 0)))
                    out.append((status, json.loads(body)))
                return out

    def post(self, body, path="/v1/messages/count_tokens"):
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        return self.exchange(("POST %s HTTP/1.1\r\nHost: localhost\r\nContent-Length: %d\r\n\r\n" % (path, len(raw))).encode() + raw)[0]

    def test_config_is_scoped_to_server(self):
        status, body = self.exchange(b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")[0]
        self.assertEqual(status, 200)
        self.assertEqual(body["model_big"], "isolated-pro")

    def test_models_advertise_configured_ids(self):
        _, body = self.exchange(b"GET /v1/models HTTP/1.1\r\nHost: localhost\r\n\r\n")[0]
        self.assertIn("isolated-pro", [v["id"] for v in body["data"]])

    def test_valid_count_tokens(self):
        status, body = self.post(BODY)
        self.assertEqual(status, 200)
        self.assertGreater(body["input_tokens"], 0)

    def test_invalid_token_limits_return_400_not_500(self):
        for name in ("max_tokens", "max_completion_tokens"):
            for value in ("oops", 0, -1, True, {}, 2.5):
                with self.subTest(name=name, value=value):
                    self.assertEqual(self.post(dict(BODY, **{name: value}))[0], 400)

    def test_invalid_stream_types_return_400(self):
        for value in ("false", 1, [], None):
            with self.subTest(value=value):
                self.assertEqual(self.post(dict(BODY, stream=value))[0], 400)

    def test_malformed_tools_and_messages_return_400(self):
        for changes in ({"tools": "bad"}, {"tools": [123]}, {"tools": [{"name": "x", "input_schema": []}]},
                        {"messages": [123]}, {"messages": {}}, {"messages": []},
                        {"tool_choice": {"type": "tool", "name": "missing"}}, {"temperature": "hot"}):
            with self.subTest(changes=changes):
                self.assertEqual(self.post(dict(BODY, **changes))[0], 400)

    def test_messages_alias_uses_anthropic_errors(self):
        status, body = self.post(b"{", "/messages")
        self.assertEqual(status, 400)
        self.assertEqual(body["type"], "error")

    def test_openai_error_shape_and_validation(self):
        status, body = self.post(dict(BODY, max_tokens="oops", stream=True), "/v1/chat/completions")
        self.assertEqual(status, 400)
        self.assertEqual(body["error"]["type"], "invalid_request_error")

    def test_invalid_utf8_rejected_instead_of_replaced(self):
        self.assertEqual(self.post(b'{"messages":[],"x":"\xff"}')[0], 400)

    def test_nan_is_not_valid_json(self):
        self.assertEqual(self.post(b'{"messages":[],"temperature":NaN}')[0], 400)

    def test_conflicting_length_and_chunked_is_rejected(self):
        raw = b"POST /messages HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\nTransfer-Encoding: chunked\r\n\r\n{}"
        self.assertEqual(self.exchange(raw)[0][0], 400)

    def test_duplicate_lengths_rejected(self):
        raw = b"POST /messages HTTP/1.1\r\nHost: localhost\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{}"
        self.assertEqual(self.exchange(raw)[0][0], 400)

    def test_invalid_lengths_rejected(self):
        for length in (b"-1", b"+2", b"oops"):
            with self.subTest(length=length):
                raw = b"POST /messages HTTP/1.1\r\nHost: localhost\r\nContent-Length: " + length + b"\r\n\r\n{}"
                self.assertEqual(self.exchange(raw)[0][0], 400)

    def test_truncated_length_rejected(self):
        raw = b"POST /messages HTTP/1.1\r\nHost: localhost\r\nContent-Length: 20\r\n\r\n{}"
        self.assertEqual(self.exchange(raw)[0][0], 400)

    def test_chunk_extensions_and_trailers_preserve_next_request(self):
        raw = json.dumps(BODY).encode()
        request = b"POST /messages/count_tokens HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n"
        request += ("%x;extension=yes\r\n" % len(raw)).encode() + raw + b"\r\n0\r\nX-Trailer: yes\r\nAnother: ok\r\n\r\n"
        request += b"GET /health HTTP/1.1\r\nHost: localhost\r\nConnection: close\r\n\r\n"
        results = self.exchange(request, responses=2)
        self.assertEqual([s for s, _ in results], [200, 200])
        self.assertEqual(results[1][1]["model_big"], "isolated-pro")

    def test_malformed_chunks_fail_closed(self):
        for data in (b"ZZ\r\n", b"-1\r\n", b"2\r\n{", b"2\r\n{}XX", b"0\r\nBroken trailer\r\n\r\n"):
            with self.subTest(data=data):
                raw = b"POST /messages HTTP/1.1\r\nHost: localhost\r\nTransfer-Encoding: chunked\r\n\r\n" + data
                self.assertEqual(self.exchange(raw)[0][0], 400)

    def test_length_and_chunked_share_size_limit(self):
        for headers, body in ((b"Content-Length: 1025", b""), (b"Transfer-Encoding: chunked", b"401\r\n")):
            with self.subTest(headers=headers):
                raw = b"POST /messages HTTP/1.1\r\nHost: localhost\r\n" + headers + b"\r\n\r\n" + body
                self.assertEqual(self.exchange(raw)[0][0], 413)

    def test_legacy_text_completions_not_misrepresented_as_chat(self):
        self.assertEqual(self.post({"prompt": "hi"}, "/v1/completions")[0], 404)


class TransportTests(unittest.TestCase):
    def test_first_sse_event_arrives_before_next_chunk(self):
        release = threading.Event()
        received = threading.Event()
        result = []
        class SlowStream(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                self.wfile.write(b'data: {"first":true}\n\n')
                self.wfile.flush()
                release.wait(3)
        server = ThreadingHTTPServer(("127.0.0.1", 0), SlowStream)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        def read_first():
            conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=4)
            try:
                conn.request("GET", "/")
                response = conn.getresponse()
                result.append(next(iter_sse(response)))
                response.close()
                received.set()
            finally:
                conn.close()
        reader = threading.Thread(target=read_first, daemon=True)
        reader.start()
        try:
            self.assertTrue(received.wait(1.5), "first token was buffered until more data or EOF")
            self.assertEqual(result, [{"first": True}])
        finally:
            release.set()
            reader.join(5)
            server.shutdown()
            server.server_close()
            thread.join(3)

    def test_silent_upstream_eof_is_an_error(self):
        raw = b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
        with patch("emutools.wire._request_with_retries", return_value=io.BytesIO(raw)):
            with self.assertRaisesRegex(UpstreamError, "finish_reason"):
                list(upstream_stream(Config(), {}))

    def test_malformed_sse_is_an_error(self):
        with self.assertRaises(UpstreamError):
            list(iter_sse(io.BytesIO(b'data: {broken}\n\n')))


if __name__ == "__main__":
    unittest.main()
