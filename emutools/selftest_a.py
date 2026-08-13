# --- generated header: build_single_file.py strips these blocks ---
from __future__ import annotations
from ._prelude import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .protocol import *  # noqa: F401,F403
from .wire import *  # noqa: F401,F403
from .engine import *  # noqa: F401,F403
from .server import *  # noqa: F401,F403
# --- end generated header ---


# ======================================================================================
# Self-test suite (no external network required)
# ======================================================================================


class _MockUpstream:
    """Scriptable OpenAI-compatible upstream used by the tests."""

    def __init__(self) -> None:
        self.responses: List[Any] = []
        self.requests: List[Dict[str, Any]] = []
        self.chunk_size_range = (1, 9)
        self.default = "ok"
        self._lock = threading.Lock()
        self.httpd: Optional[ThreadingHTTPServer] = None
        self.port = 0

    def script(self, *responses: Any) -> None:
        with self._lock:
            self.responses = list(responses)
            self.requests = []

    def _next(self) -> Any:
        with self._lock:
            if self.responses:
                return self.responses.pop(0)
            return self.default

    def last_request(self) -> Dict[str, Any]:
        with self._lock:
            return self.requests[-1] if self.requests else {}

    def system_prompt(self) -> str:
        req = self.last_request()
        for m in req.get("messages") or []:
            if m.get("role") == "system":
                return safe_str(m.get("content"))
        return ""

    def start(self) -> int:
        outer = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a: Any) -> None:  # noqa: A003
                pass

            def do_POST(self) -> None:  # noqa: N802
                n = int(self.headers.get("content-length") or 0)
                raw = self.rfile.read(n)
                try:
                    body = json.loads(raw.decode("utf-8"))
                except ValueError:
                    body = {}
                with outer._lock:
                    outer.requests.append(body)
                spec = outer._next()

                if isinstance(spec, dict) and "status" in spec:
                    payload = json.dumps(spec.get("body", {"error": "boom"})).encode()
                    self.send_response(int(spec["status"]))
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                if isinstance(spec, dict) and spec.get("raw") is not None:
                    payload = str(spec["raw"]).encode()
                    self.send_response(200)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return

                content = spec if isinstance(spec, str) else safe_str(spec)
                stops = body.get("stop") or []
                for s in stops:
                    idx = content.find(s)
                    if idx >= 0:
                        content = content[:idx]  # emulate real stop-sequence behaviour
                        break

                if body.get("stream"):
                    self.send_response(200)
                    self.send_header("content-type", "text/event-stream")
                    self.send_header("transfer-encoding", "chunked")
                    self.end_headers()

                    def wr(data: bytes) -> None:
                        self.wfile.write(("%x\r\n" % len(data)).encode())
                        self.wfile.write(data)
                        self.wfile.write(b"\r\n")
                        self.wfile.flush()

                    lo, hi = outer.chunk_size_range
                    i = 0
                    while i < len(content):
                        size = random.randint(lo, hi)
                        piece = content[i : i + size]
                        i += size
                        obj = {
                            "id": "x",
                            "object": "chat.completion.chunk",
                            "choices": [{"index": 0, "delta": {"content": piece}}],
                        }
                        wr(("data: %s\n\n" % json.dumps(obj)).encode())
                    final = {
                        "id": "x",
                        "object": "chat.completion.chunk",
                        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 11, "completion_tokens": 22},
                    }
                    wr(("data: %s\n\n" % json.dumps(final)).encode())
                    wr(b"data: [DONE]\n\n")
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                    return

                obj = {
                    "id": "cmpl",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": content},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 11, "completion_tokens": 22},
                }
                payload = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        return self.port

    def stop(self) -> None:
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()


class _Runner:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0
        self.failures: List[str] = []
        self.group = ""

    def section(self, name: str) -> None:
        self.group = name
        print("\n\033[1m%s\033[0m" % name)

    def check(self, name: str, cond: bool, detail: str = "") -> None:
        if cond:
            self.passed += 1
            print("  \033[32mPASS\033[0m %s" % name)
        else:
            self.failed += 1
            self.failures.append("[%s] %s :: %s" % (self.group, name, detail))
            print("  \033[31mFAIL\033[0m %s\n        %s" % (name, detail))

    def eq(self, name: str, got: Any, want: Any) -> None:
        self.check(name, got == want, "got %r want %r" % (got, want))


DEMO_TOOLS = [
    ToolDef(
        name="Read",
        description="Read a file from disk.",
        schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path"},
                "limit": {"type": "integer", "description": "Max lines"},
            },
            "required": ["file_path"],
        },
    ),
    ToolDef(
        name="Write",
        description="Write a file to disk.",
        schema={
            "type": "object",
            "properties": {
                "file_path": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["file_path", "content"],
        },
    ),
    ToolDef(
        name="Bash",
        description="Run a shell command.",
        schema={
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "number"},
                "background": {"type": "boolean"},
            },
            "required": ["command"],
        },
    ),
    ToolDef(
        name="Grep",
        description="Search files.",
        schema={
            "type": "object",
            "properties": {
                "pattern": {"type": "string"},
                "mode": {"type": "string", "enum": ["files", "content"]},
            },
            "required": ["pattern"],
        },
    ),
]
DEMO_BY_NAME = {t.name: t for t in DEMO_TOOLS}


def _anthropic_tools() -> List[Dict[str, Any]]:
    return [
        {"name": t.name, "description": t.description, "input_schema": t.schema} for t in DEMO_TOOLS
    ]


def _openai_tools() -> List[Dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {"name": t.name, "description": t.description, "parameters": t.schema},
        }
        for t in DEMO_TOOLS
    ]


def _http(port: int, path: str, body: Optional[Dict[str, Any]] = None, method: str = "POST"):
    url = "http://127.0.0.1:%d%s" % (port, path)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("content-type", "application/json")
    try:
        resp = urllib.request.urlopen(req, timeout=30)
        raw = resp.read()
        return resp.status, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _parse_sse(text: str) -> List[Tuple[str, Dict[str, Any]]]:
    events: List[Tuple[str, Dict[str, Any]]] = []
    ev = ""
    for block in text.split("\n\n"):
        ev = ""
        data = ""
        for line in block.split("\n"):
            if line.startswith("event:"):
                ev = line[6:].strip()
            elif line.startswith("data:"):
                data = line[5:].strip()
        if not data:
            continue
        if data == "[DONE]":
            events.append(("[DONE]", {}))
            continue
        try:
            events.append((ev or "data", json.loads(data)))
        except ValueError:
            pass
    return events


def _selftest_part3(r: "_Runner", mock: "_MockUpstream", pport: int, saved: Config, proxy) -> int:  # noqa: C901
    try:
        # --- multi-turn: tool result flows back and the model finishes
        r.section("9. Multi-turn agent loop")

        mock.script("The file contains the number 42.")
        status, body = _http(
            pport,
            "/v1/messages",
            {
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 512,
                "messages": [
                    {"role": "user", "content": "what is in /a?"},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "text", "text": "Reading."},
                            {
                                "type": "tool_use",
                                "id": "toolu_01",
                                "name": "Read",
                                "input": {"file_path": "/a"},
                            },
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "toolu_01",
                                "content": "42",
                            }
                        ],
                    },
                ],
                "tools": _anthropic_tools(),
            },
        )
        data = json.loads(body)
        r.eq("multi-turn final stop_reason", data.get("stop_reason"), "end_turn")
        r.check(
            "multi-turn answer text",
            "42" in (data["content"][0].get("text") or ""),
            repr(data.get("content")),
        )
        msgs = mock.last_request().get("messages") or []
        transcript = "\n".join(safe_str(m.get("content")) for m in msgs)
        r.check("prior tool call replayed as text", CALL_OPEN in transcript, transcript[:300])
        r.check("tool result replayed as text", "<tool_result" in transcript, transcript[:300])
        r.check("result payload present", "42" in transcript, transcript[:300])
        roles = [m.get("role") for m in msgs]
        r.check("roles alternate after merge", all(a != b for a, b in zip(roles, roles[1:])), repr(roles))

        # --- OpenAI tool role round trip
        mock.script("Found 3 TODOs.")
        status, body = _http(
            pport,
            "/v1/chat/completions",
            {
                "model": "gpt-4o",
                "messages": [
                    {"role": "system", "content": "be terse"},
                    {"role": "user", "content": "find TODOs"},
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "Grep",
                                    "arguments": '{"pattern":"TODO"}',
                                },
                            }
                        ],
                    },
                    {"role": "tool", "tool_call_id": "call_1", "content": "a.py:1\nb.py:9\nc.py:3"},
                ],
                "tools": _openai_tools(),
            },
        )
        data = json.loads(body)
        r.eq("openai tool-role finish", (data["choices"][0]).get("finish_reason"), "stop")
        transcript = "\n".join(safe_str(m.get("content")) for m in mock.last_request().get("messages") or [])
        r.check("openai tool result replayed", "b.py:9" in transcript, transcript[:300])
        r.check("openai system preserved", "be terse" in transcript, transcript[:300])

        # --- loop protection over the wire
        r.section("10. Loop protection end to end")

        repeat_call = '<tool_call>{"name":"Read","arguments":{"file_path":"/loop"}}</tool_call>'

        def looping_history(n: int) -> List[Dict[str, Any]]:
            msgs: List[Dict[str, Any]] = [{"role": "user", "content": "go"}]
            for i in range(n):
                msgs.append(
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": "toolu_%d" % i,
                                "name": "Read",
                                "input": {"file_path": "/loop"},
                            }
                        ],
                    }
                )
                msgs.append(
                    {
                        "role": "user",
                        "content": [
                            {"type": "tool_result", "tool_use_id": "toolu_%d" % i, "content": "same"}
                        ],
                    }
                )
            return msgs

        # 2 prior identical calls -> still allowed (max_repeat = 3)
        mock.script(repeat_call)
        status, body = _http(
            pport,
            "/v1/messages",
            {
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 512,
                "messages": looping_history(2),
                "tools": _anthropic_tools(),
            },
        )
        data = json.loads(body)
        r.eq("below repeat limit still calls", data.get("stop_reason"), "tool_use")
        r.check("warning nudge injected", "already called" in mock.system_prompt(), "no nudge")

        # 3 prior identical calls -> blocked, and the retry also loops -> text answer
        mock.script(repeat_call, repeat_call, repeat_call)
        status, body = _http(
            pport,
            "/v1/messages",
            {
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 512,
                "messages": looping_history(3),
                "tools": _anthropic_tools(),
            },
        )
        data = json.loads(body)
        r.eq("repeat limit blocks the call", data.get("stop_reason"), "end_turn")
        blocks = data.get("content") or []
        r.check(
            "loop explained to client",
            any("repeat" in (b.get("text") or "").lower() for b in blocks),
            repr(blocks),
        )
        r.check("escalation was attempted", "CRITICAL" in mock.system_prompt(), "no escalation")

        # loop guard recovers when the model changes its mind on retry
        mock.script(repeat_call, '<tool_call>{"name":"Read","arguments":{"file_path":"/other"}}</tool_call>')
        status, body = _http(
            pport,
            "/v1/messages",
            {
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 512,
                "messages": looping_history(3),
                "tools": _anthropic_tools(),
            },
        )
        data = json.loads(body)
        tu = [b for b in (data.get("content") or []) if b.get("type") == "tool_use"]
        r.eq("retry recovers with new args", tu[0]["input"] if tu else None, {"file_path": "/other"})

        # round budget exhausted -> tools stripped from prompt, forced answer
        mock.script(repeat_call, "Here is my final answer without tools.")
        status, body = _http(
            pport,
            "/v1/messages",
            {
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 512,
                "messages": [
                    {"role": "user", "content": "go"},
                ]
                + sum(
                    (
                        [
                            {
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "tool_use",
                                        "id": "toolu_b%d" % i,
                                        "name": "Read",
                                        "input": {"file_path": "/f%d" % i},
                                    }
                                ],
                            },
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "tool_result",
                                        "tool_use_id": "toolu_b%d" % i,
                                        "content": "r%d" % i,
                                    }
                                ],
                            },
                        ]
                        for i in range(8)
                    ),
                    [],
                ),
                "tools": _anthropic_tools(),
            },
        )
        data = json.loads(body)
        r.eq("budget exhausted -> end_turn", data.get("stop_reason"), "end_turn")
        sysp = mock.system_prompt()
        r.check("tool schemas removed at budget", "### Read" not in sysp, sysp[:200])
        r.check("budget message injected", "maximum number of tool calls" in sysp, sysp[:300])

        # streaming loop guard
        mock.script(repeat_call)
        status, body = _http(
            pport,
            "/v1/messages",
            {
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 512,
                "messages": looping_history(3),
                "tools": _anthropic_tools(),
                "stream": True,
            },
        )
        evs = _parse_sse(body)
        starts = [e[1] for e in evs if e[0] == "content_block_start"]
        tool_starts = [s for s in starts if s["content_block"]["type"] == "tool_use"]
        r.eq("streaming loop guard blocks call", len(tool_starts), 0)
        deltas = [e[1] for e in evs if e[0] == "content_block_delta"]
        txt = "".join(d["delta"].get("text", "") for d in deltas)
        r.check("streaming loop guard explains", "loop guard" in txt, repr(txt))

        # --- error handling
        r.section("11. Error handling and resilience")

        mock.script({"status": 500, "body": {"error": {"message": "upstream exploded"}}})
        cfg_retries = CFG.connect_retries
        CFG.connect_retries = 1
        status, body = _http(
            pport,
            "/v1/messages",
            {
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        r.check("upstream 500 -> error status", status >= 400, str(status))
        r.check("anthropic error envelope", json.loads(body).get("type") == "error", body[:200])

        mock.script({"status": 401, "body": {"error": {"message": "bad key"}}})
        status, body = _http(
            pport,
            "/v1/chat/completions",
            {"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
        )
        r.check("openai error envelope", "error" in json.loads(body), body[:200])
        CFG.connect_retries = cfg_retries

        mock.script({"raw": "this is not json at all"})
        status, body = _http(
            pport,
            "/v1/messages",
            {
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        r.check("non-JSON upstream handled", status >= 400, str(status))

        mock.script("", "", "")
        status, body = _http(
            pport,
            "/v1/messages",
            {
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        data = json.loads(body)
        r.eq("empty upstream -> 200", status, 200)
        r.check("empty upstream -> non-empty content", len(data.get("content") or []) >= 1, body[:200])
        r.check(
            "empty content block has text",
            bool((data["content"][0].get("text") or "").strip()),
            body[:200],
        )

        mock.script("hello")
        status, body = _http(pport, "/v1/messages", {"model": "x", "max_tokens": 10, "messages": []})
        r.eq("empty messages rejected", status, 400)

        req = urllib.request.Request(
            "http://127.0.0.1:%d/v1/messages" % pport, data=b"{not json", method="POST"
        )
        req.add_header("content-type", "application/json")
        try:
            urllib.request.urlopen(req, timeout=10)
            bad_status = 200
        except urllib.error.HTTPError as exc:
            bad_status = exc.code
        r.eq("malformed JSON body -> 400", bad_status, 400)

        status, body = _http(pport, "/v1/nope", {}, method="POST")
        r.eq("unknown route -> 404", status, 404)

        # unknown tool from the model
        mock.script('<tool_call>{"name":"Teleport","arguments":{"x":1}}</tool_call>', "Sorry, I cannot.")
        status, body = _http(
            pport,
            "/v1/messages",
            {
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "teleport"}],
                "tools": _anthropic_tools(),
            },
        )
        data = json.loads(body)
        tu = [b for b in (data.get("content") or []) if b.get("type") == "tool_use"]
        r.eq("unknown tool not forwarded", len(tu), 0)

        # invalid args trigger a repair round trip
        mock.script(
            '<tool_call>{"name":"Read","arguments":{}}</tool_call>',
            '<tool_call>{"name":"Read","arguments":{"file_path":"/fixed"}}</tool_call>',
        )
        status, body = _http(
            pport,
            "/v1/messages",
            {
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "read"}],
                "tools": _anthropic_tools(),
            },
        )
        data = json.loads(body)
        tu = [b for b in (data.get("content") or []) if b.get("type") == "tool_use"]
        r.eq("invalid args repaired on retry", tu[0]["input"] if tu else None, {"file_path": "/fixed"})
        r.check("repair instruction sent", "rejected" in mock.system_prompt(), "no repair prompt")

        # --- misc surfaces
        r.section("12. Endpoints, options and edge cases")

        status, body = _http(pport, "/v1/models", method="GET")
        r.eq("models status", status, 200)
        r.check("models list shape", len(json.loads(body).get("data") or []) > 0, body[:200])

        status, body = _http(pport, "/health", method="GET")
        r.eq("health status", status, 200)
        r.eq("health ok", json.loads(body).get("status"), "ok")

        status, body = _http(
            pport,
            "/v1/messages/count_tokens",
            {
                "model": "claude-sonnet-4-5-20250929",
                "messages": [{"role": "user", "content": "hello world " * 50}],
                "tools": _anthropic_tools(),
            },
        )
        data = json.loads(body)
        r.eq("count_tokens status", status, 200)
        r.check("count_tokens positive", data.get("input_tokens", 0) > 0, body[:200])

        # tool_choice = none
        mock.script("No tools, just prose.")
        _http(
            pport,
            "/v1/messages",
            {
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "hi"}],
                "tools": _anthropic_tools(),
                "tool_choice": {"type": "none"},
            },
        )
        sysp = mock.system_prompt()
        r.check("tool_choice=none hides schemas", "### Read" not in sysp, sysp[:200])
        r.check("tool_choice=none states disabled", "Tools are disabled" in sysp, sysp[:200])
        r.check("no stop sequence when tools off", not mock.last_request().get("stop"), repr(mock.last_request().get("stop")))

        # tool_choice = any
        mock.script('<tool_call>{"name":"Read","arguments":{"file_path":"/x"}}</tool_call>')
        _http(
            pport,
            "/v1/messages",
            {
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "hi"}],
                "tools": _anthropic_tools(),
                "tool_choice": {"type": "any"},
            },
        )
        r.check("tool_choice=any forces a call", "MUST call a tool" in mock.system_prompt(), "missing")

        # tool_choice = specific tool
        mock.script('<tool_call>{"name":"Bash","arguments":{"command":"ls"}}</tool_call>')
        _http(
            pport,
            "/v1/messages",
            {
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 128,
                "messages": [{"role": "user", "content": "hi"}],
                "tools": _anthropic_tools(),
                "tool_choice": {"type": "tool", "name": "Bash"},
            },
        )
        r.check("named tool_choice honoured", "MUST call the tool `Bash`" in mock.system_prompt(), "missing")

        # images and cache_control tolerated
        mock.script("I cannot see images.")
        status, body = _http(
            pport,
            "/v1/messages",
            {
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 128,
                "system": [
                    {"type": "text", "text": "sys A", "cache_control": {"type": "ephemeral"}},
                    {"type": "text", "text": "sys B"},
                ],
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "look"},
                            {
                                "type": "image",
                                "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"},
                            },
                        ],
                    }
                ],
                "metadata": {"user_id": "abc"},
            },
        )
        r.eq("image request still 200", status, 200)
        sysp = mock.system_prompt()
        r.check("system block array joined", "sys A" in sysp and "sys B" in sysp, sysp[:200])
        transcript = "\n".join(safe_str(m.get("content")) for m in mock.last_request().get("messages") or [])
        r.check("image replaced by placeholder", "image omitted" in transcript, transcript[:200])

        # thinking blocks dropped
        mock.script("done")
        _http(
            pport,
            "/v1/messages",
            {
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 128,
                "messages": [
                    {"role": "user", "content": "hi"},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "thinking", "thinking": "secret chain of thought"},
                            {"type": "text", "text": "visible"},
                        ],
                    },
                    {"role": "user", "content": "continue"},
                ],
            },
        )
        transcript = "\n".join(safe_str(m.get("content")) for m in mock.last_request().get("messages") or [])
        r.check("thinking block dropped", "secret chain" not in transcript, transcript[:200])
        r.check("visible assistant text kept", "visible" in transcript, transcript[:200])

        # very large tool result is truncated, not dropped
        mock.script("ok")
        big = "X" * 200000
        _http(
            pport,
            "/v1/messages",
            {
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 128,
                "messages": [
                    {"role": "user", "content": "go"},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "/big"}}
                        ],
                    },
                    {
                        "role": "user",
                        "content": [{"type": "tool_result", "tool_use_id": "t1", "content": big}],
                    },
                ],
                "tools": _anthropic_tools(),
            },
        )
        transcript = "\n".join(safe_str(m.get("content")) for m in mock.last_request().get("messages") or [])
        r.check("huge result truncated", len(transcript) < 120000, "len=%d" % len(transcript))
        r.check("truncation is signposted", "characters omitted" in transcript, transcript[:200])

        # tool result marked as error
        mock.script("I will fix it.")
        _http(
            pport,
            "/v1/messages",
            {
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 128,
                "messages": [
                    {"role": "user", "content": "go"},
                    {
                        "role": "assistant",
                        "content": [
                            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "nope"}}
                        ],
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": "t1",
                                "content": "command not found",
                                "is_error": True,
                            }
                        ],
                    },
                ],
                "tools": _anthropic_tools(),
            },
        )
        transcript = "\n".join(safe_str(m.get("content")) for m in mock.last_request().get("messages") or [])
        r.check('error result flagged', 'status="error"' in transcript, transcript[:400])

        # concurrency
        r.section("13. Concurrency")
        mock.script(*(["parallel ok"] * 24))
        errors: List[str] = []
        results: List[int] = []
        lock = threading.Lock()

        def worker(i: int) -> None:
            try:
                st, bd = _http(
                    pport,
                    "/v1/chat/completions",
                    {"model": "gpt-4o", "messages": [{"role": "user", "content": "n=%d" % i}]},
                )
                with lock:
                    results.append(st)
            except Exception as exc:  # noqa: BLE001
                with lock:
                    errors.append(repr(exc))

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        r.eq("16 concurrent requests, no exceptions", errors, [])
        r.eq("all concurrent requests 200", sorted(set(results)), [200])

        # --- regressions built from bytes a real upstream actually returned
        r.section("14. Real captured output (deepseek-v4-flash, live API)")

        # (a) canonical protocol, truncated by our own stop sequence
        real1 = '<tool_call>\n{"name": "Bash", "arguments": {"command": "wc -l /etc/hosts"}}\n'
        txt, calls = extract_tool_calls(real1, DEMO_BY_NAME)
        r.eq("real: canonical call parsed", len(calls), 1)
        r.eq("real: canonical name", calls[0].name if calls else None, "Bash")
        r.eq(
            "real: canonical args",
            calls[0].args if calls else None,
            {"command": "wc -l /etc/hosts"},
        )
        r.eq("real: no leftover text", txt.strip(), "")

        # (b) the model's OWN native markup leaking into the text channel
        real2 = (
            "<\uff5c\uff5cDSML\uff5c\uff5ctool_calls>\n"
            '<\uff5c\uff5cDSML\uff5c\uff5cinvoke name="Read">\n'
            '<\uff5c\uff5cDSML\uff5c\uff5cparameter name="file_path" string="true">'
            "/etc/hosts</\uff5c\uff5cDSML\uff5c\uff5cparameter>\n"
            "</\uff5c\uff5cDSML\uff5c\uff5cinvoke>\n"
            "</\uff5c\uff5cDSML\uff5c\uff5ctool_calls>"
        )
        txt, calls = extract_tool_calls(real2, DEMO_BY_NAME)
        r.eq("real: DSML dialect parsed", len(calls), 1)
        r.eq("real: DSML tool name", calls[0].name if calls else None, "Read")
        r.eq("real: DSML args", calls[0].args if calls else None, {"file_path": "/etc/hosts"})
        r.check("real: no sentinel leak", "DSML" not in txt, repr(txt))
        r.eq("real: DSML leaves no visible text", txt.strip(), "")

        # (c) streamed back in the EXACT deltas the API sent over SSE
        real_deltas = [
            "<", "\uff5c\uff5cDSML\uff5c\uff5c", "tool", "_c", "alls", ">\n",
            "<", "\uff5c\uff5cDSML\uff5c\uff5c", "inv", "oke", " name", '="', "Read", '">\n',
            "<", "\uff5c\uff5cDSML\uff5c\uff5c", "parameter", " name", '="', "file",
            "_path", '"', " string", '="', "true", '">',
            "/", "etc", "/h", "osts",
            "</", "\uff5c\uff5cDSML\uff5c\uff5c", "parameter", ">\n",
            "</", "\uff5c\uff5cDSML\uff5c\uff5c", "inv", "oke", ">\n",
            "</", "\uff5c\uff5cDSML\uff5c\uff5c", "tool", "_c", "alls", ">",
        ]
        p = StreamToolParser(DEMO_BY_NAME)
        seen: List[str] = []
        for d in real_deltas:
            seen.extend(p.feed(d))
        tail_txt, calls = p.finish()
        seen.extend(tail_txt)
        streamed = "".join(seen)
        r.eq("real: DSML streamed parsed", len(calls), 1)
        r.eq(
            "real: DSML streamed args",
            calls[0].args if calls else None,
            {"file_path": "/etc/hosts"},
        )
        r.check("real: DSML streamed no sentinel leak", "DSML" not in streamed, repr(streamed))
        r.check("real: DSML streamed no tag leak", "<" not in streamed, repr(streamed))
        r.eq("real: DSML streamed no visible text", streamed.strip(), "")

        # (d) worst case: one character per SSE chunk
        p = StreamToolParser(DEMO_BY_NAME)
        seen = []
        for chx in real2:
            seen.extend(p.feed(chx))
        tail_txt, calls = p.finish()
        seen.extend(tail_txt)
        streamed = "".join(seen)
        r.eq("real: DSML char-split parsed", len(calls), 1)
        r.eq(
            "real: DSML char-split args",
            calls[0].args if calls else None,
            {"file_path": "/etc/hosts"},
        )
        r.check("real: DSML char-split no leak", "DSML" not in streamed, repr(streamed))
        r.eq("real: DSML char-split no visible text", streamed.strip(), "")

        # (e) reasoning_content must never reach the client
        real3 = {
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": "There are **7 lines** in `/etc/hosts`.",
                        "reasoning_content": "The command returned 7, so there are 7 lines.",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 588, "completion_tokens": 30, "total_tokens": 618},
        }
        content, finish_r, usage = extract_completion_text(real3)
        r.eq("real: content extracted", content, "There are **7 lines** in `/etc/hosts`.")
        r.check("real: reasoning_content not leaked", "returned 7" not in content, content)
        r.eq("real: finish reason", finish_r, "stop")
        r.eq("real: usage passthrough", usage.get("total_tokens"), 618)

        # (f) a delta with content=None (reasoning phase) must not crash the parser
        p = StreamToolParser(DEMO_BY_NAME)
        r.eq("real: empty delta is a no-op", p.feed(""), [])

        # --- many independent conversations in flight at the same time
        r.section("15. Multi-conversation isolation")

        # Every upstream reply is byte-identical, so ANY difference in what a client
        # receives can only have come from that conversation's own history. If loop
        # state leaked between conversations, fresh ones would get blocked or
        # saturated ones would be allowed to repeat.
        mock.script()
        saved_default = mock.default
        mock.default = '<tool_call>\n{"name": "Read", "arguments": {"file_path": "/shared"}}\n</tool_call>'

        def _fresh_convo(i: int) -> Dict[str, Any]:
            return {
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 512,
                "messages": [{"role": "user", "content": "fresh %d" % i}],
                "tools": _anthropic_tools(),
            }

        def _looping_convo(i: int) -> Dict[str, Any]:
            msgs: List[Dict[str, Any]] = [{"role": "user", "content": "looper %d" % i}]
            for k in range(CFG.max_repeat):
                tid = "toolu_%d_%d" % (i, k)
                msgs.append(
                    {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "tool_use",
                                "id": tid,
                                "name": "Read",
                                "input": {"file_path": "/shared"},
                            }
                        ],
                    }
                )
                msgs.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": tid,
                                "content": "same bytes every time",
                            }
                        ],
                    }
                )
            return {
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 512,
                "messages": msgs,
                "tools": _anthropic_tools(),
            }

        outcomes: Dict[int, Tuple[str, Dict[str, Any]]] = {}
        conv_errors: List[str] = []
        clock = threading.Lock()

        def convo_worker(i: int) -> None:
            try:
                is_fresh = i % 2 == 0
                payload = _fresh_convo(i) if is_fresh else _looping_convo(i)
                st_code, bd = _http(pport, "/v1/messages", payload)
                data = json.loads(bd)
                blocks = data.get("content") or []
                uses = [b for b in blocks if b.get("type") == "tool_use"]
                info = {
                    "status": st_code,
                    "stop": data.get("stop_reason"),
                    "kinds": [b.get("type") for b in blocks],
                    "text": " ".join(
                        b.get("text") or "" for b in blocks if b.get("type") == "text"
                    ),
                    "inputs": [u.get("input") for u in uses],
                }
                with clock:
                    outcomes[i] = ("fresh" if is_fresh else "looper", info)
            except Exception as exc:  # noqa: BLE001
                with clock:
                    conv_errors.append("%d: %r" % (i, exc))

        cthreads = [threading.Thread(target=convo_worker, args=(i,)) for i in range(16)]
        for t in cthreads:
            t.start()
        for t in cthreads:
            t.join(timeout=90)

        r.eq("16 interleaved conversations, no exceptions", conv_errors, [])
        r.eq("every conversation answered", len(outcomes), 16)

        fresh_out = [v for _k, (kind, v) in outcomes.items() if kind == "fresh"]
        loop_out = [v for _k, (kind, v) in outcomes.items() if kind == "looper"]
        r.eq("8 fresh + 8 saturated", (len(fresh_out), len(loop_out)), (8, 8))
        r.eq(
            "all conversations 200",
            sorted({v["status"] for v in fresh_out + loop_out}),
            [200],
        )
        r.eq(
            "every fresh conversation got its tool call",
            sorted({v["stop"] for v in fresh_out}),
            ["tool_use"],
        )
        r.check(
            "tool args uncorrupted under concurrent parsing",
            all(v["inputs"] == [{"file_path": "/shared"}] for v in fresh_out),
            repr([v["inputs"] for v in fresh_out]),
        )
        r.check(
            "no saturated conversation was allowed to repeat",
            all("tool_use" not in v["kinds"] for v in loop_out),
            repr([v["kinds"] for v in loop_out]),
        )
        r.check(
            "every blocked conversation still returned usable text",
            all(v["text"].strip() for v in loop_out),
            repr([v["text"] for v in loop_out]),
        )
        r.check(
            "the block is explained, not a stub",
            all(len(v["text"].strip()) > 20 for v in loop_out),
            repr([v["text"][:160] for v in loop_out]),
        )
        r.check(
            "blocked conversations never end in tool_use",
            all(v["stop"] != "tool_use" for v in loop_out),
            repr([v["stop"] for v in loop_out]),
        )
        mock.default = saved_default

    finally:
        CFG.__dict__.update(saved.__dict__)
        proxy.shutdown()
        proxy.server_close()
        mock.stop()

    print("\n" + "=" * 72)
    total = r.passed + r.failed
    if r.failed:
        print("\033[31m%d/%d passed, %d FAILED\033[0m" % (r.passed, total, r.failed))
        print("\nFailures:")
        for f in r.failures:
            print("  - " + f)
        return 1
    print("\033[32mAll %d checks passed.\033[0m" % total)
    return 0


# --- generated header: build_single_file.py strips these blocks ---
__all__ = [
    "_MockUpstream",
    "_Runner",
    "DEMO_TOOLS",
    "DEMO_BY_NAME",
    "_anthropic_tools",
    "_openai_tools",
    "_http",
    "_parse_sse",
    "_selftest_part3",
]
# --- end generated header ---
