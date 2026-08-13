# --- generated header: build_single_file.py strips these blocks ---
from __future__ import annotations
from ._prelude import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .protocol import *  # noqa: F401,F403
# --- end generated header ---


# ======================================================================================
# Loop / runaway protection
# ======================================================================================


@dataclass
class LoopState:
    rounds: int = 0                      # assistant turns that contained >=1 tool call
    seq: List[str] = field(default_factory=list)   # fingerprints, in order
    counts: Dict[str, int] = field(default_factory=dict)
    names: Dict[str, str] = field(default_factory=dict)
    last_results: List[str] = field(default_factory=list)
    budget_exhausted: bool = False
    nudges: List[str] = field(default_factory=list)
    oscillating: bool = False
    saturated: List[str] = field(default_factory=list)  # fingerprints at/over the cap


def analyze_history(messages: List[CanonMessage], cfg: Config) -> LoopState:
    """Reconstruct tool-call history from the (stateless) client transcript."""
    st = LoopState()
    for msg in messages:
        if msg.role == "assistant" and msg.tool_calls:
            st.rounds += 1
            for tc in msg.tool_calls:
                fp = tc.fp()
                st.seq.append(fp)
                st.counts[fp] = st.counts.get(fp, 0) + 1
                st.names[fp] = tc.name
        for _tid, _name, content, _err in msg.tool_results:
            st.last_results.append(content or "")

    st.budget_exhausted = st.rounds >= cfg.max_tool_rounds
    st.saturated = [fp for fp, c in st.counts.items() if c >= cfg.max_repeat]
    # Warn one step before the hard block so the model can self-correct cheaply.
    warn_at = max(2, cfg.max_repeat - 1)
    repeated = [fp for fp, c in st.counts.items() if c >= warn_at]

    # A/B/A/B oscillation over the recent window.
    tail = st.seq[-6:]
    if len(tail) >= 4:
        a, b = tail[-4], tail[-3]
        if a != b and tail[-2] == a and tail[-1] == b:
            st.oscillating = True
    if len(tail) >= 3 and tail[-1] == tail[-2] == tail[-3]:
        st.oscillating = True

    for fp in repeated:
        st.nudges.append(
            "You have already called `%s` %d times with identical arguments in this "
            "conversation. The result will not change. Do NOT call it again - use the "
            "result you already have, try a materially different approach, or give your "
            "final answer now." % (st.names.get(fp, "a tool"), st.counts[fp])
        )
    if st.oscillating:
        st.nudges.append(
            "You are alternating between the same tool calls without making progress. "
            "Stop looping: state what you have learned and answer the user directly."
        )
    if not st.budget_exhausted and st.rounds >= max(1, int(cfg.max_tool_rounds * 0.8)):
        st.nudges.append(
            "You have used %d of your %d tool calls. Wrap up quickly and produce a final "
            "answer." % (st.rounds, cfg.max_tool_rounds)
        )
    if len(st.last_results) >= 3 and len(set(st.last_results[-3:])) == 1:
        st.nudges.append(
            "The last three tool results were byte-for-byte identical. Repeating the call "
            "will not help. Change strategy or answer now."
        )
    return st


BUDGET_MESSAGE = (
    "You have reached the maximum number of tool calls for this conversation. "
    "Do not emit any <tool_call> block. Answer the user now, in prose, using only what "
    "you already know. If the task is incomplete, say plainly what is missing."
)


def filter_calls_for_loops(
    calls: List[ToolCall], st: LoopState, cfg: Config
) -> Tuple[List[ToolCall], List[str]]:
    """Drop calls that would continue a loop. Returns (kept, blocked reasons)."""
    kept: List[ToolCall] = []
    blocked: List[str] = []
    seen_this_turn: Dict[str, int] = {}

    for tc in calls:
        fp = tc.fp()
        prior = st.counts.get(fp, 0)
        here = seen_this_turn.get(fp, 0)

        if here >= 1:
            blocked.append(
                "Dropped a duplicate `%s` call emitted twice in the same reply." % tc.name
            )
            continue
        if prior >= cfg.max_repeat:
            blocked.append(
                "Blocked `%s`: identical arguments were already used %d times "
                "(limit %d). Loop protection stopped the repeat."
                % (tc.name, prior, cfg.max_repeat)
            )
            continue
        if len(kept) >= cfg.max_calls_per_turn:
            blocked.append(
                "Dropped extra `%s` call: more than %d tool calls in one reply."
                % (tc.name, cfg.max_calls_per_turn)
            )
            continue

        seen_this_turn[fp] = here + 1
        kept.append(tc)

    return kept, blocked


# ======================================================================================
# Upstream (OpenAI-compatible) client
# ======================================================================================


class UpstreamError(Exception):
    def __init__(self, message: str, status: int = 502, body: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.body = body


_RETRY_STATUS = (408, 409, 425, 429, 500, 502, 503, 504, 529)


def _upstream_url(cfg: Config) -> str:
    base = cfg.upstream_base.rstrip("/")
    path = cfg.upstream_path
    if not path.startswith("/"):
        path = "/" + path
    if base.endswith("/v1") and path.startswith("/v1/"):
        path = path[3:]
    return base + path


def _do_request(cfg: Config, payload: Dict[str, Any], stream: bool):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(_upstream_url(cfg), data=body, method="POST")
    req.add_header("content-type", "application/json")
    req.add_header("accept", "text/event-stream" if stream else "application/json")
    req.add_header("user-agent", "emutools/%s" % __version__)
    if cfg.upstream_key:
        req.add_header("authorization", "Bearer " + cfg.upstream_key)
    return urllib.request.urlopen(req, timeout=cfg.timeout)


def _request_with_retries(cfg: Config, payload: Dict[str, Any], stream: bool):
    last: Optional[Exception] = None
    attempts = max(1, cfg.connect_retries)
    for attempt in range(attempts):
        try:
            return _do_request(cfg, payload, stream)
        except urllib.error.HTTPError as exc:
            raw = b""
            try:
                raw = exc.read()
            except Exception:  # noqa: BLE001
                pass
            text = raw.decode("utf-8", "replace")
            if exc.code in _RETRY_STATUS and attempt < attempts - 1:
                delay = min(8.0, (2 ** attempt) * 0.7) + random.random() * 0.4
                log_warn(
                    "upstream %s (attempt %d/%d), retrying in %.1fs"
                    % (exc.code, attempt + 1, attempts, delay)
                )
                time.sleep(delay)
                last = UpstreamError("upstream %s" % exc.code, exc.code, text)
                continue
            raise UpstreamError(
                "upstream returned HTTP %s" % exc.code, exc.code, truncate_middle(text, 2000)
            )
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            last = exc
            if attempt < attempts - 1:
                delay = min(8.0, (2 ** attempt) * 0.7) + random.random() * 0.4
                log_warn(
                    "upstream connection error %r (attempt %d/%d), retrying in %.1fs"
                    % (exc, attempt + 1, attempts, delay)
                )
                time.sleep(delay)
                continue
            raise UpstreamError("cannot reach upstream: %s" % exc, 502)
    raise UpstreamError("cannot reach upstream: %s" % last, 502)


def upstream_complete(cfg: Config, payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(payload)
    payload["stream"] = False
    resp = _request_with_retries(cfg, payload, stream=False)
    try:
        raw = resp.read()
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass
    try:
        data = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        raise UpstreamError(
            "upstream returned non-JSON body", 502, truncate_middle(raw.decode("utf-8", "replace"), 1000)
        )
    if not isinstance(data, dict):
        raise UpstreamError("upstream returned unexpected JSON", 502)
    if "error" in data and "choices" not in data:
        err = data.get("error")
        msg = err.get("message") if isinstance(err, dict) else safe_str(err)
        raise UpstreamError("upstream error: %s" % msg, 502, canon_json(err))
    return data


def iter_sse(resp) -> Iterator[Dict[str, Any]]:
    """Yield parsed `data:` payloads from an SSE response."""
    buf = ""
    while True:
        chunk = resp.read(1024)
        if not chunk:
            break
        buf += chunk.decode("utf-8", "replace")
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            line = line.rstrip("\r")
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                yield json.loads(data)
            except ValueError:
                log_debug("skipping unparseable SSE line: %s" % data[:200])
    tail = buf.strip()
    if tail.startswith("data:"):
        data = tail[5:].strip()
        if data and data != "[DONE]":
            try:
                yield json.loads(data)
            except ValueError:
                pass


def upstream_stream(cfg: Config, payload: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """Yield {'text':..} / {'reasoning':..} / {'usage':..} / {'finish':..} events."""
    payload = dict(payload)
    payload["stream"] = True
    resp = _request_with_retries(cfg, payload, stream=True)
    try:
        for obj in iter_sse(resp):
            if not isinstance(obj, dict):
                continue
            if obj.get("error"):
                err = obj["error"]
                msg = err.get("message") if isinstance(err, dict) else safe_str(err)
                raise UpstreamError("upstream stream error: %s" % msg, 502)
            usage = obj.get("usage")
            if isinstance(usage, dict):
                yield {"usage": usage}
            choices = obj.get("choices")
            if not isinstance(choices, list) or not choices:
                continue
            ch = choices[0]
            if not isinstance(ch, dict):
                continue
            delta = ch.get("delta")
            if isinstance(delta, dict):
                rc = delta.get("reasoning_content") or delta.get("reasoning")
                if isinstance(rc, str) and rc:
                    yield {"reasoning": rc}
                content = delta.get("content")
                if isinstance(content, str) and content:
                    yield {"text": content}
                elif isinstance(content, list):
                    for part in content:
                        if isinstance(part, dict) and isinstance(part.get("text"), str):
                            yield {"text": part["text"]}
            msgobj = ch.get("message")
            if isinstance(msgobj, dict) and isinstance(msgobj.get("content"), str):
                yield {"text": msgobj["content"]}
            fr = ch.get("finish_reason")
            if fr:
                yield {"finish": fr}
    finally:
        try:
            resp.close()
        except Exception:  # noqa: BLE001
            pass


def extract_completion_text(data: Dict[str, Any]) -> Tuple[str, str, Dict[str, Any]]:
    """Return (content, finish_reason, usage) from a non-streaming completion."""
    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return "", "stop", usage
    ch = choices[0] if isinstance(choices[0], dict) else {}
    msg = ch.get("message") if isinstance(ch.get("message"), dict) else {}
    content = msg.get("content")
    if isinstance(content, list):
        content = "".join(
            p.get("text", "") for p in content if isinstance(p, dict)
        )
    if not isinstance(content, str):
        content = ""
    # Some upstreams still return native tool_calls; fold them into our text form.
    native = msg.get("tool_calls")
    if isinstance(native, list) and native:
        for nc in native:
            if not isinstance(nc, dict):
                continue
            fn = nc.get("function") if isinstance(nc.get("function"), dict) else {}
            nm = safe_str(fn.get("name"))
            raw_args = fn.get("arguments")
            parsed, _ = loads_tolerant(raw_args if isinstance(raw_args, str) else canon_json(raw_args))
            content += "\n" + CALL_OPEN + "\n" + json.dumps(
                {"name": nm, "arguments": parsed if isinstance(parsed, dict) else {}},
                ensure_ascii=False,
            ) + "\n" + CALL_CLOSE
    return content, safe_str(ch.get("finish_reason")) or "stop", usage


# ======================================================================================
# Protocol -> canonical request
# ======================================================================================


def _blocks_to_text(content: Any) -> str:
    """Flatten Anthropic/OpenAI content blocks into plain text."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        content = [content]
    if not isinstance(content, list):
        return safe_str(content)
    out: List[str] = []
    for block in content:
        if isinstance(block, str):
            out.append(block)
            continue
        if not isinstance(block, dict):
            out.append(safe_str(block))
            continue
        btype = block.get("type")
        if btype == "text" or (btype is None and "text" in block):
            out.append(safe_str(block.get("text")))
        elif btype == "image" or btype == "image_url":
            out.append("[image omitted: this model is text-only]")
        elif btype == "document":
            out.append("[document omitted: this model is text-only]")
        elif btype == "thinking" or btype == "redacted_thinking":
            continue
        elif btype == "input_audio":
            out.append("[audio omitted: this model is text-only]")
        elif "text" in block:
            out.append(safe_str(block.get("text")))
    return "\n".join(p for p in out if p)


def anthropic_to_canon(body: Dict[str, Any], cfg: Config) -> CanonRequest:
    messages: List[CanonMessage] = []
    id_to_name: Dict[str, str] = {}

    raw_msgs = body.get("messages")
    if not isinstance(raw_msgs, list):
        raw_msgs = []

    for raw in raw_msgs:
        if not isinstance(raw, dict):
            continue
        role = raw.get("role") or "user"
        content = raw.get("content")
        blocks = content if isinstance(content, list) else ([content] if content is not None else [])

        text_parts: List[str] = []
        calls: List[ToolCall] = []
        results: List[Tuple[str, str, str, bool]] = []

        for block in blocks:
            if isinstance(block, str):
                text_parts.append(block)
                continue
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                tid = safe_str(block.get("id")) or new_tool_use_id()
                nm = safe_str(block.get("name"))
                inp = block.get("input")
                if not isinstance(inp, dict):
                    parsed, _ = loads_tolerant(safe_str(inp))
                    inp = parsed if isinstance(parsed, dict) else {}
                id_to_name[tid] = nm
                calls.append(ToolCall(name=nm, args=inp, id=tid))
            elif btype == "tool_result":
                tid = safe_str(block.get("tool_use_id"))
                results.append(
                    (
                        tid,
                        id_to_name.get(tid, "tool"),
                        _blocks_to_text(block.get("content")),
                        bool(block.get("is_error")),
                    )
                )
            else:
                piece = _blocks_to_text([block])
                if piece:
                    text_parts.append(piece)

        messages.append(
            CanonMessage(
                role="assistant" if role == "assistant" else "user",
                text="\n".join(text_parts).strip(),
                tool_calls=calls,
                tool_results=results,
            )
        )

    tools: List[ToolDef] = []
    for raw in body.get("tools") or []:
        if not isinstance(raw, dict):
            continue
        nm = safe_str(raw.get("name"))
        if not nm:
            continue
        schema = raw.get("input_schema")
        if not isinstance(schema, dict):
            schema = raw.get("parameters") if isinstance(raw.get("parameters"), dict) else {}
        tools.append(ToolDef(name=nm, description=safe_str(raw.get("description")), schema=schema))

    choice = "auto"
    tc = body.get("tool_choice")
    if isinstance(tc, dict):
        ttype = tc.get("type")
        if ttype == "any":
            choice = "required"
        elif ttype == "none":
            choice = "none"
        elif ttype == "tool":
            choice = safe_str(tc.get("name")) or "required"
    elif isinstance(tc, str):
        choice = tc

    stops = body.get("stop_sequences")
    if not isinstance(stops, list):
        stops = []

    return CanonRequest(
        model=safe_str(body.get("model")),
        messages=messages,
        system=_blocks_to_text(body.get("system")),
        tools=tools,
        tool_choice=choice,
        max_tokens=int(body.get("max_tokens") or 4096),
        temperature=body.get("temperature"),
        top_p=body.get("top_p"),
        stop=[s for s in stops if isinstance(s, str)],
        stream=bool(body.get("stream")),
        protocol="anthropic",
    )


def openai_to_canon(body: Dict[str, Any], cfg: Config) -> CanonRequest:
    messages: List[CanonMessage] = []
    system_parts: List[str] = []
    id_to_name: Dict[str, str] = {}

    raw_msgs = body.get("messages")
    if not isinstance(raw_msgs, list):
        raw_msgs = []

    for raw in raw_msgs:
        if not isinstance(raw, dict):
            continue
        role = raw.get("role") or "user"
        if role in ("system", "developer"):
            system_parts.append(_blocks_to_text(raw.get("content")))
            continue
        if role == "tool" or role == "function":
            tid = safe_str(raw.get("tool_call_id")) or safe_str(raw.get("name"))
            messages.append(
                CanonMessage(
                    role="user",
                    tool_results=[
                        (
                            tid,
                            id_to_name.get(tid, safe_str(raw.get("name")) or "tool"),
                            _blocks_to_text(raw.get("content")),
                            False,
                        )
                    ],
                )
            )
            continue

        calls: List[ToolCall] = []
        for nc in raw.get("tool_calls") or []:
            if not isinstance(nc, dict):
                continue
            fn = nc.get("function") if isinstance(nc.get("function"), dict) else {}
            nm = safe_str(fn.get("name"))
            args_raw = fn.get("arguments")
            parsed, _ = loads_tolerant(args_raw if isinstance(args_raw, str) else canon_json(args_raw))
            tid = safe_str(nc.get("id")) or new_openai_call_id()
            id_to_name[tid] = nm
            calls.append(ToolCall(name=nm, args=parsed if isinstance(parsed, dict) else {}, id=tid))

        messages.append(
            CanonMessage(
                role="assistant" if role == "assistant" else "user",
                text=_blocks_to_text(raw.get("content")).strip(),
                tool_calls=calls,
            )
        )

    tools: List[ToolDef] = []
    for raw in body.get("tools") or []:
        if not isinstance(raw, dict):
            continue
        fn = raw.get("function") if isinstance(raw.get("function"), dict) else raw
        nm = safe_str(fn.get("name"))
        if not nm:
            continue
        schema = fn.get("parameters")
        if not isinstance(schema, dict):
            schema = fn.get("input_schema") if isinstance(fn.get("input_schema"), dict) else {}
        tools.append(ToolDef(name=nm, description=safe_str(fn.get("description")), schema=schema))

    # legacy `functions`
    for raw in body.get("functions") or []:
        if isinstance(raw, dict) and safe_str(raw.get("name")):
            tools.append(
                ToolDef(
                    name=safe_str(raw.get("name")),
                    description=safe_str(raw.get("description")),
                    schema=raw.get("parameters") if isinstance(raw.get("parameters"), dict) else {},
                )
            )

    choice = "auto"
    tc = body.get("tool_choice")
    if isinstance(tc, str):
        choice = tc if tc in ("auto", "none", "required") else "auto"
    elif isinstance(tc, dict):
        if tc.get("type") == "function":
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else {}
            choice = safe_str(fn.get("name")) or "required"
        elif tc.get("type") == "none":
            choice = "none"
        elif tc.get("type") in ("any", "required"):
            choice = "required"

    stops = body.get("stop")
    if isinstance(stops, str):
        stops = [stops]
    if not isinstance(stops, list):
        stops = []

    max_tokens = body.get("max_completion_tokens") or body.get("max_tokens") or 4096

    return CanonRequest(
        model=safe_str(body.get("model")),
        messages=messages,
        system="\n\n".join(p for p in system_parts if p),
        tools=tools,
        tool_choice=choice,
        max_tokens=int(max_tokens),
        temperature=body.get("temperature"),
        top_p=body.get("top_p"),
        stop=[s for s in stops if isinstance(s, str)],
        stream=bool(body.get("stream")),
        protocol="openai",
    )


# ======================================================================================
# Canonical request -> upstream payload
# ======================================================================================


def build_upstream_messages(req: CanonRequest, cfg: Config, extra_system: List[str]) -> List[Dict[str, str]]:
    system_chunks: List[str] = []
    if req.system.strip():
        system_chunks.append(req.system.strip())

    tools_active = bool(req.tools) and req.tool_choice != "none"
    if tools_active:
        system_chunks.append(build_tool_prompt(req.tools, cfg.parallel))
        if req.tool_choice == "required":
            system_chunks.append(
                "For this turn you MUST call a tool. Emit exactly one <tool_call> block and "
                "no prose."
            )
        elif req.tool_choice not in ("auto", "none"):
            system_chunks.append(
                "For this turn you MUST call the tool `%s`. Emit exactly one <tool_call> "
                "block for it and no prose." % req.tool_choice
            )
    elif req.tools and req.tool_choice == "none":
        system_chunks.append(
            "Tools are disabled for this turn. Answer directly and do not emit any "
            "<tool_call> block."
        )

    for note in extra_system:
        if note:
            system_chunks.append(note)

    out: List[Dict[str, str]] = []
    if system_chunks:
        out.append({"role": "system", "content": "\n\n".join(system_chunks)})

    for msg in req.messages:
        if msg.role == "assistant":
            parts: List[str] = []
            if msg.text:
                parts.append(msg.text)
            for tc in msg.tool_calls:
                parts.append(render_tool_call_text(tc))
            body = "\n\n".join(p for p in parts if p).strip()
            out.append({"role": "assistant", "content": body or "(no output)"})
        else:
            parts = []
            for _tid, name, content, is_err in msg.tool_results:
                parts.append(
                    render_tool_result_text(name, content, is_err, cfg.max_result_chars)
                )
            if msg.text:
                parts.append(msg.text)
            body = "\n\n".join(p for p in parts if p).strip()
            out.append({"role": "user", "content": body or "(empty message)"})

    if cfg.merge_roles:
        merged: List[Dict[str, str]] = []
        for m in out:
            if merged and merged[-1]["role"] == m["role"] and m["role"] != "system":
                merged[-1]["content"] += "\n\n" + m["content"]
            else:
                merged.append(dict(m))
        out = merged

    if not any(m["role"] in ("user", "assistant") for m in out):
        out.append({"role": "user", "content": "(empty message)"})

    return out


def build_upstream_payload(
    req: CanonRequest, cfg: Config, extra_system: List[str], allow_tools: bool
) -> Dict[str, Any]:
    effective = req
    if not allow_tools:
        effective = CanonRequest(
            model=req.model,
            messages=req.messages,
            system=req.system,
            tools=[],
            tool_choice="none",
            max_tokens=req.max_tokens,
            temperature=req.temperature,
            top_p=req.top_p,
            stop=req.stop,
            stream=req.stream,
            protocol=req.protocol,
        )

    payload: Dict[str, Any] = {
        "model": cfg.resolve_model(req.model),
        "messages": build_upstream_messages(effective, cfg, extra_system),
        "max_tokens": max(1, min(int(req.max_tokens or 4096), 32768)),
    }
    if req.temperature is not None:
        try:
            payload["temperature"] = float(req.temperature)
        except (TypeError, ValueError):
            pass
    if req.top_p is not None:
        try:
            payload["top_p"] = float(req.top_p)
        except (TypeError, ValueError):
            pass

    stops: List[str] = list(req.stop)
    if allow_tools and req.tools and req.tool_choice != "none" and cfg.use_stop and not cfg.parallel:
        stops.append(CALL_CLOSE)
    # Upstreams cap stop sequences; keep it small and unique.
    deduped: List[str] = []
    for s in stops:
        if s and s not in deduped:
            deduped.append(s)
    if deduped:
        payload["stop"] = deduped[:4]
    return payload


# --- generated header: build_single_file.py strips these blocks ---
__all__ = [
    "LoopState",
    "analyze_history",
    "BUDGET_MESSAGE",
    "filter_calls_for_loops",
    "UpstreamError",
    "_RETRY_STATUS",
    "_upstream_url",
    "_do_request",
    "_request_with_retries",
    "upstream_complete",
    "iter_sse",
    "upstream_stream",
    "extract_completion_text",
    "_blocks_to_text",
    "anthropic_to_canon",
    "openai_to_canon",
    "build_upstream_messages",
    "build_upstream_payload",
]
# --- end generated header ---
