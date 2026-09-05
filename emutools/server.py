# --- generated header: build_single_file.py strips these blocks ---
from __future__ import annotations
from ._prelude import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .protocol import *  # noqa: F401,F403
from .wire import *  # noqa: F401,F403
from .engine import *  # noqa: F401,F403
# --- end generated header ---


class RequestError(ValueError):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def validate_request(body: Dict[str, Any], protocol: str) -> None:
    """Reject malformed client values before conversion or starting a 200 stream."""
    for name in ("max_tokens", "max_completion_tokens"):
        if name in body and (isinstance(body[name], bool) or not isinstance(body[name], int) or body[name] <= 0):
            raise RequestError(name + " must be a positive integer")
    for name in ("stream", "parallel_tool_calls"):
        if name in body and not isinstance(body[name], bool):
            raise RequestError(name + " must be a boolean")
    for name, upper in (("temperature", 2), ("top_p", 1)):
        value = body.get(name)
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= upper):
            raise RequestError("%s must be a number between 0 and %s" % (name, upper))
    if "model" in body and (not isinstance(body["model"], str) or not body["model"].strip()):
        raise RequestError("model must be a nonempty string")
    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        raise RequestError("messages must be a nonempty array")
    roles = ("user", "assistant") if protocol == "anthropic" else ("system", "developer", "user", "assistant", "tool", "function")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in roles:
            raise RequestError("each message must be an object with a valid role")
        if message.get("content") is not None and not isinstance(message["content"], (str, list)):
            raise RequestError("message content must be a string, array, or null")
        if "tool_calls" in message and not isinstance(message["tool_calls"], list):
            raise RequestError("message tool_calls must be an array")
    tool_names: List[str] = []
    for field_name in ("tools", "functions"):
        if field_name not in body:
            continue
        if not isinstance(body[field_name], list):
            raise RequestError(field_name + " must be an array")
        for raw in body[field_name]:
            if not isinstance(raw, dict):
                raise RequestError("each tool must be an object")
            tool = raw.get("function", raw)
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not tool["name"].strip():
                raise RequestError("each tool must have a nonempty name")
            for schema_key in ("parameters", "input_schema"):
                if schema_key in tool and not isinstance(tool[schema_key], dict):
                    raise RequestError("tool " + schema_key + " must be an object")
            if tool["name"] in tool_names:
                raise RequestError("tool names must be unique")
            tool_names.append(tool["name"])
    choice = body.get("tool_choice")
    forced = None
    required = False
    if isinstance(choice, dict):
        kind = choice.get("type")
        allowed = ("auto", "none", "any", "tool") if protocol == "anthropic" else ("function", "none", "any", "required")
        if kind not in allowed:
            raise RequestError("unsupported tool_choice type")
        if "disable_parallel_tool_use" in choice and not isinstance(choice["disable_parallel_tool_use"], bool):
            raise RequestError("disable_parallel_tool_use must be a boolean")
        if kind == "tool":
            forced = choice.get("name")
        elif kind == "function":
            fn = choice.get("function")
            forced = fn.get("name") if isinstance(fn, dict) else None
        if kind in ("tool", "function") and (not isinstance(forced, str) or forced not in tool_names):
            raise RequestError("tool_choice must name an available tool")
        required = kind in ("any", "required")
    elif choice is not None:
        if choice not in ("auto", "none", "required", "any"):
            raise RequestError("unsupported tool_choice")
        required = choice in ("required", "any")
    if required and not tool_names:
        raise RequestError("tool_choice requires at least one tool")
    opts = body.get("stream_options")
    if opts is not None and (not isinstance(opts, dict) or ("include_usage" in opts and not isinstance(opts["include_usage"], bool))):
        raise RequestError("stream_options.include_usage must be a boolean")
    for name in ("stop", "stop_sequences"):
        if name in body and body[name] is not None:
            stops = [body[name]] if isinstance(body[name], str) and name == "stop" else body[name]
            if not isinstance(stops, list) or any(not isinstance(x, str) or not x for x in stops):
                raise RequestError(name + " must contain nonempty strings")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "emutools/" + __version__
    sys_version = ""

    @property
    def cfg(self) -> Config:
        return self.server.cfg

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(self.cfg.client_timeout)

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

    def _read_body(self) -> Dict[str, Any]:
        lengths = self.headers.get_all("content-length", [])
        encodings = self.headers.get_all("transfer-encoding", [])
        if len(lengths) > 1 or (lengths and encodings):
            raise RequestError("ambiguous request body framing")
        limit = max(1, self.cfg.max_request_bytes)
        if encodings:
            if len(encodings) != 1 or encodings[0].strip().lower() != "chunked":
                raise RequestError("only chunked transfer encoding is supported")
            chunks: List[bytes] = []
            total = 0
            while True:
                line = self.rfile.readline(8193)
                if len(line) > 8192 or not line.endswith(b"\r\n"):
                    raise RequestError("invalid chunk header")
                size_text = line[:-2].split(b";", 1)[0]
                if not re.fullmatch(b"[0-9a-fA-F]+", size_text):
                    raise RequestError("invalid chunk size")
                size = int(size_text, 16)
                if size == 0:
                    trailer_size = 0
                    while True:
                        trailer = self.rfile.readline(8193)
                        trailer_size += len(trailer)
                        if len(trailer) > 8192 or trailer_size > 65536 or not trailer.endswith(b"\r\n"):
                            raise RequestError("invalid or oversized chunk trailers")
                        if trailer == b"\r\n":
                            break
                        if b":" not in trailer:
                            raise RequestError("invalid chunk trailer")
                    break
                total += size
                if total > limit:
                    raise RequestError("request body too large", 413)
                chunk = self.rfile.read(size)
                if len(chunk) != size or self.rfile.read(2) != b"\r\n":
                    raise RequestError("truncated or invalid chunk data")
                chunks.append(chunk)
            raw = b"".join(chunks)
        else:
            value = lengths[0] if lengths else "0"
            if not re.fullmatch(r"[0-9]+", value):
                raise RequestError("invalid content-length")
            length = int(value)
            if length > limit:
                raise RequestError("request body too large", 413)
            raw = self.rfile.read(length)
            if len(raw) != length:
                raise RequestError("truncated request body")
        try:
            def reject_constant(value: str) -> None:
                raise ValueError("non-finite JSON number")
            data = json.loads(raw.decode("utf-8"), parse_constant=reject_constant)
        except (ValueError, UnicodeError) as exc:
            raise RequestError("invalid JSON body") from exc
        if not isinstance(data, dict):
            raise RequestError("request body must be a JSON object")
        return data

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
                        for m in dict.fromkeys(ADVERTISED_MODELS + [self.cfg.model_big, self.cfg.model_small])
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
                    "upstream": self.cfg.upstream_base,
                    "model_big": self.cfg.model_big,
                    "model_small": self.cfg.model_small,
                    "parallel": self.cfg.parallel,
                    "max_tool_rounds": self.cfg.max_tool_rounds,
                    "max_repeat": self.cfg.max_repeat,
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
        protocol = "anthropic" if path.startswith(("/v1/messages", "/messages")) else "openai"
        try:
            body = self._read_body()
            if self.cfg.log_bodies:
                log_debug("REQ %s %s" % (path, truncate_middle(canon_json(body), 4000)))
            if path in ("/v1/messages/count_tokens", "/messages/count_tokens"):
                self._handle_count_tokens(body)
            elif path in ("/v1/messages", "/messages"):
                self._handle_messages(body)
            elif path in ("/v1/chat/completions", "/chat/completions"):
                self._handle_chat(body)
            else:
                self._error(protocol, 404, "unknown route %s" % path, "not_found_error")
        except RequestError as exc:
            self.close_connection = True  # never reuse unread/ambiguous framing
            self._error(protocol, exc.status, str(exc))
        except (socket.timeout, TimeoutError):
            self.close_connection = True
            self._error(protocol, 408, "request body timed out")
        except UpstreamError as exc:
            self._error(protocol, 502 if exc.status < 400 else exc.status, exc.message, "api_error")
        except (BrokenPipeError, ConnectionResetError):
            log_info("client disconnected")
        except Exception as exc:  # noqa: BLE001 - never kill the server
            log_error("unhandled error on %s: %s\n%s" % (path, exc, traceback.format_exc()))
            self._error(protocol, 500, "internal proxy error", "api_error")

    # ---------- handlers ----------

    def _handle_count_tokens(self, body: Dict[str, Any]) -> None:
        validate_request(body, "anthropic")
        req = anthropic_to_canon(body, self.cfg)
        payload = build_upstream_payload(req, self.cfg, [], allow_tools=True)
        self._send_json(200, {"input_tokens": _estimate_input_tokens(payload)})

    def _handle_messages(self, body: Dict[str, Any]) -> None:
        validate_request(body, "anthropic")
        req = anthropic_to_canon(body, self.cfg)
        if not req.messages:
            self._error("anthropic", 400, "messages must not be empty")
            return
        log_info(
            "anthropic %s model=%s -> %s tools=%d stream=%s"
            % (
                "stream" if req.stream else "sync",
                req.model or "?",
                self.cfg.resolve_model(req.model),
                len(req.tools),
                req.stream,
            )
        )
        if req.stream:
            if not self._start_stream():
                return
            for piece in anthropic_stream_bytes(req, self.cfg):
                if not self._write_chunk(piece):
                    return
            self._end_chunks()
            return
        res = run_turn(req, self.cfg)
        for note in res.notes:
            log_warn("note: " + note)
        self._send_json(200, anthropic_response(req, res))

    def _handle_chat(self, body: Dict[str, Any]) -> None:
        validate_request(body, "openai")
        req = openai_to_canon(body, self.cfg)
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
                self.cfg.resolve_model(req.model),
                len(req.tools),
            )
        )
        if req.stream:
            if not self._start_stream():
                return
            for piece in openai_stream_bytes(req, self.cfg, include_usage):
                if not self._write_chunk(piece):
                    return
            self._end_chunks()
            return
        res = run_turn(req, self.cfg)
        for note in res.notes:
            log_warn("note: " + note)
        self._send_json(200, openai_response(req, res))


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address, RequestHandlerClass=Handler, bind_and_activate=True, cfg=None):
        self.cfg = cfg if cfg is not None else CFG
        super().__init__(server_address, RequestHandlerClass, bind_and_activate=bind_and_activate)


def serve(cfg: Config) -> None:
    if not cfg.upstream_key:
        log_warn("EMU_UPSTREAM_API_KEY is not set - upstream calls will likely fail with 401")
    httpd = Server((cfg.host, cfg.port), Handler, cfg=cfg)
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
    "RequestError",
    "validate_request",
    "Handler",
    "Server",
    "serve",
]
# --- end generated header ---
