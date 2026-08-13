# --- generated header: build_single_file.py strips these blocks ---
from __future__ import annotations
from ._prelude import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .protocol import *  # noqa: F401,F403
from .wire import *  # noqa: F401,F403
from .engine import *  # noqa: F401,F403
# --- end generated header ---


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "emutools/" + __version__
    sys_version = ""

    # ---------- low-level helpers ----------

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        log_debug("%s %s" % (self.address_string(), fmt % args))

    def _cors(self) -> None:
        self.send_header("access-control-allow-origin", "*")
        self.send_header("access-control-allow-headers", "*")
        self.send_header("access-control-allow-methods", "GET,POST,OPTIONS")

    def _send_json(self, status: int, obj: Dict[str, Any]) -> None:
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(raw)))
            self._cors()
            self.end_headers()
            self.wfile.write(raw)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            log_debug("client disconnected before response")

    def _start_stream(self) -> bool:
        try:
            self.send_response(200)
            self.send_header("content-type", "text/event-stream; charset=utf-8")
            self.send_header("cache-control", "no-cache, no-store")
            self.send_header("connection", "keep-alive")
            self.send_header("x-accel-buffering", "no")
            self.send_header("transfer-encoding", "chunked")
            self._cors()
            self.end_headers()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def _write_chunk(self, data: bytes) -> bool:
        if not data:
            return True
        try:
            self.wfile.write(("%x\r\n" % len(data)).encode("ascii"))
            self.wfile.write(data)
            self.wfile.write(b"\r\n")
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            log_info("client disconnected mid-stream")
            return False

    def _end_chunks(self) -> None:
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_body(self) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        try:
            length = int(self.headers.get("content-length") or 0)
        except (TypeError, ValueError):
            length = 0
        if length <= 0:
            if (self.headers.get("transfer-encoding") or "").lower() == "chunked":
                buf = b""
                while True:
                    line = self.rfile.readline().strip()
                    if not line:
                        break
                    try:
                        size = int(line, 16)
                    except ValueError:
                        break
                    if size == 0:
                        self.rfile.readline()
                        break
                    buf += self.rfile.read(size)
                    self.rfile.readline()
                raw = buf
            else:
                return {}, None
        else:
            if length > 200 * 1024 * 1024:
                return None, "request body too large"
            raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8", "replace") or "{}")
        except ValueError as exc:
            return None, "invalid JSON body: %s" % exc
        if not isinstance(data, dict):
            return None, "request body must be a JSON object"
        return data, None

    def _error(self, protocol: str, status: int, message: str, etype: str = "invalid_request_error") -> None:
        log_warn("HTTP %d %s: %s" % (status, self.path, message))
        if protocol == "anthropic":
            self._send_json(status, {"type": "error", "error": {"type": etype, "message": message}})
        else:
            self._send_json(
                status,
                {"error": {"message": message, "type": etype, "param": None, "code": None}},
            )

    # ---------- routes ----------

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.send_header("content-length", "0")
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        if path in ("/v1/models", "/models"):
            self._send_json(
                200,
                {
                    "object": "list",
                    "data": [
                        {
                            "id": m,
                            "object": "model",
                            "created": 1700000000,
                            "owned_by": "emutools",
                        }
                        for m in ADVERTISED_MODELS
                    ],
                },
            )
            return
        if path in ("/health", "/healthz"):
            self._send_json(
                200,
                {
                    "status": "ok",
                    "version": __version__,
                    "upstream": CFG.upstream_base,
                    "model_big": CFG.model_big,
                    "model_small": CFG.model_small,
                    "parallel": CFG.parallel,
                    "max_tool_rounds": CFG.max_tool_rounds,
                    "max_repeat": CFG.max_repeat,
                },
            )
            return
        if path == "/":
            self._send_json(
                200,
                {
                    "name": "emutools",
                    "version": __version__,
                    "description": "Emulated tool-calling proxy (Anthropic + OpenAI wire formats)",
                    "endpoints": [
                        "POST /v1/messages",
                        "POST /v1/messages/count_tokens",
                        "POST /v1/chat/completions",
                        "GET /v1/models",
                        "GET /health",
                    ],
                },
            )
            return
        self._error("openai", 404, "unknown route %s" % path, "not_found_error")

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        protocol = "anthropic" if path.startswith("/v1/messages") else "openai"

        body, err = self._read_body()
        if err is not None or body is None:
            self._error(protocol, 400, err or "invalid body")
            return
        if CFG.log_bodies:
            log_debug("REQ %s %s" % (path, truncate_middle(canon_json(body), 4000)))

        try:
            if path == "/v1/messages/count_tokens":
                self._handle_count_tokens(body)
            elif path in ("/v1/messages", "/messages"):
                self._handle_messages(body)
            elif path in ("/v1/chat/completions", "/chat/completions", "/v1/completions"):
                self._handle_chat(body)
            else:
                self._error(protocol, 404, "unknown route %s" % path, "not_found_error")
        except UpstreamError as exc:
            self._error(protocol, 502 if exc.status < 400 else exc.status, exc.message, "api_error")
        except (BrokenPipeError, ConnectionResetError):
            log_info("client disconnected")
        except Exception as exc:  # noqa: BLE001 - never kill the server
            log_error("unhandled error on %s: %s\n%s" % (path, exc, traceback.format_exc()))
            self._error(protocol, 500, "internal proxy error: %s" % exc, "api_error")

    # ---------- handlers ----------

    def _handle_count_tokens(self, body: Dict[str, Any]) -> None:
        req = anthropic_to_canon(body, CFG)
        payload = build_upstream_payload(req, CFG, [], allow_tools=True)
        self._send_json(200, {"input_tokens": _estimate_input_tokens(payload)})

    def _handle_messages(self, body: Dict[str, Any]) -> None:
        req = anthropic_to_canon(body, CFG)
        if not req.messages:
            self._error("anthropic", 400, "messages must not be empty")
            return
        log_info(
            "anthropic %s model=%s -> %s tools=%d stream=%s"
            % (
                "stream" if req.stream else "sync",
                req.model or "?",
                CFG.resolve_model(req.model),
                len(req.tools),
                req.stream,
            )
        )
        if req.stream:
            if not self._start_stream():
                return
            for piece in anthropic_stream_bytes(req, CFG):
                if not self._write_chunk(piece):
                    return
            self._end_chunks()
            return
        res = run_turn(req, CFG)
        for note in res.notes:
            log_warn("note: " + note)
        self._send_json(200, anthropic_response(req, res))

    def _handle_chat(self, body: Dict[str, Any]) -> None:
        req = openai_to_canon(body, CFG)
        if not req.messages:
            self._error("openai", 400, "messages must not be empty")
            return
        opts = body.get("stream_options")
        include_usage = bool(isinstance(opts, dict) and opts.get("include_usage"))
        log_info(
            "openai %s model=%s -> %s tools=%d"
            % (
                "stream" if req.stream else "sync",
                req.model or "?",
                CFG.resolve_model(req.model),
                len(req.tools),
            )
        )
        if req.stream:
            if not self._start_stream():
                return
            for piece in openai_stream_bytes(req, CFG, include_usage):
                if not self._write_chunk(piece):
                    return
            self._end_chunks()
            return
        res = run_turn(req, CFG)
        for note in res.notes:
            log_warn("note: " + note)
        self._send_json(200, openai_response(req, res))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def serve(cfg: Config) -> None:
    if not cfg.upstream_key:
        log_warn("EMU_UPSTREAM_API_KEY is not set - upstream calls will likely fail with 401")
    httpd = Server((cfg.host, cfg.port), Handler)
    log_info("emutools %s listening on http://%s:%d" % (__version__, cfg.host, cfg.port))
    log_info("upstream %s  big=%s  small=%s" % (_upstream_url(cfg), cfg.model_big, cfg.model_small))
    log_info("Claude Code : ANTHROPIC_BASE_URL=http://%s:%d ANTHROPIC_AUTH_TOKEN=dummy claude" % (cfg.host, cfg.port))
    log_info("OpenAI apps : OPENAI_BASE_URL=http://%s:%d/v1 OPENAI_API_KEY=dummy" % (cfg.host, cfg.port))
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        log_info("shutting down")
    finally:
        httpd.server_close()


# --- generated header: build_single_file.py strips these blocks ---
__all__ = [
    "Handler",
    "Server",
    "serve",
]
# --- end generated header ---
