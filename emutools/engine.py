# --- generated header: build_single_file.py strips these blocks ---
from __future__ import annotations
from ._prelude import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .protocol import *  # noqa: F401,F403
from .wire import *  # noqa: F401,F403
# --- end generated header ---


# ======================================================================================
# Turn orchestration
# ======================================================================================


@dataclass
class TurnResult:
    text: str = ""
    calls: List[ToolCall] = field(default_factory=list)
    finish: str = "stop"  # stop | tool_calls | length
    usage: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    attempts: int = 1


EMPTY_FALLBACK = "(The model returned an empty response.)"


def _tools_by_name(req: CanonRequest) -> Dict[str, ToolDef]:
    return {t.name: t for t in req.tools}


def _estimate_input_tokens(payload: Dict[str, Any]) -> int:
    total = 0
    for m in payload.get("messages") or []:
        total += estimate_tokens(safe_str(m.get("content")))
    return total


def _prepare(req: CanonRequest, cfg: Config) -> Tuple[LoopState, List[str], bool]:
    st = analyze_history(req.messages, cfg)
    extra = list(st.nudges)
    allow_tools = not st.budget_exhausted
    if st.budget_exhausted:
        extra.append(BUDGET_MESSAGE)
        log_warn(
            "tool budget exhausted after %d rounds; forcing a final answer" % st.rounds
        )
    return st, extra, allow_tools


def run_turn(req: CanonRequest, cfg: Config) -> TurnResult:
    """Non-streaming: one client turn, with repair/loop retries."""
    st, extra, allow_tools = _prepare(req, cfg)
    tools_by_name = _tools_by_name(req)
    notes: List[str] = []
    result = TurnResult()
    max_attempts = 3 if cfg.loop_retry else 1

    for attempt in range(1, max_attempts + 1):
        payload = build_upstream_payload(req, cfg, extra, allow_tools)
        data = upstream_complete(cfg, payload)
        content, finish_reason, usage = extract_completion_text(data)
        text, calls = extract_tool_calls(content, tools_by_name, cfg.salvage_bare_json)

        if not usage:
            usage = {
                "prompt_tokens": _estimate_input_tokens(payload),
                "completion_tokens": estimate_tokens(content),
            }

        result = TurnResult(text=text, calls=calls, usage=usage, attempts=attempt)

        if not allow_tools and calls:
            notes.append(
                "Tool-call budget reached (%d rounds); ignored %d further tool call(s)."
                % (st.rounds, len(calls))
            )
            calls = []
            result.calls = []

        kept, blocked = filter_calls_for_loops(calls, st, cfg)

        # Retry once when loop protection nuked every call the model wanted.
        if blocked and not kept and attempt < max_attempts and allow_tools:
            log_warn("loop guard blocked all calls; retrying with escalation")
            extra = list(extra) + [
                "CRITICAL: "
                + " ".join(blocked)
                + " Do not emit that call again. Either use a genuinely different tool or "
                "arguments, or stop calling tools and give your final answer now."
            ]
            continue

        # Validate arguments; one repair round-trip if the model got them wrong.
        problems: List[str] = []
        for tc in kept:
            tdef = tools_by_name.get(tc.name)
            if tdef is None:
                problems.append(
                    "Tool `%s` does not exist. Available tools: %s."
                    % (tc.name, ", ".join(sorted(tools_by_name)) or "(none)")
                )
                continue
            issues = validate_args(tc.args, tdef.schema)
            if issues:
                problems.append(
                    "Call to `%s` is invalid: %s. Emit the call again, corrected."
                    % (tc.name, "; ".join(issues))
                )
        if problems and attempt < max_attempts:
            log_warn("invalid tool args, repairing: %s" % problems[0][:160])
            extra = list(extra) + ["Your previous tool call was rejected. " + " ".join(problems)]
            continue
        if problems:
            notes.extend(problems)
            kept = [tc for tc in kept if tc.name in tools_by_name]

        # tool_choice=required but the model answered in prose.
        if (
            allow_tools
            and req.tools
            and req.tool_choice not in ("auto", "none")
            and not kept
            and attempt < max_attempts
        ):
            log_warn("tool_choice=%s but no call produced; retrying" % req.tool_choice)
            extra = list(extra) + [
                "You did not emit a tool call. This turn REQUIRES one. Output only a "
                "<tool_call> block now, nothing else."
            ]
            continue

        # Loop guard explanation must be applied BEFORE the empty-response fallback,
        # otherwise a fully-blocked turn looks like an empty upstream reply.
        if blocked:
            notes.extend(blocked)
            if not kept and not text.strip():
                text = (
                    "I stopped because I was about to repeat a tool call I have already "
                    "made with identical arguments. " + " ".join(blocked)
                )

        # Empty response guard - clients crash on empty content.
        if not text.strip() and not kept:
            if attempt < max_attempts:
                log_warn("empty upstream response; retrying once")
                extra = list(extra) + [
                    "Your previous reply was empty. Produce a substantive reply now."
                ]
                continue
            text = EMPTY_FALLBACK

        result.text = text
        result.calls = kept
        result.notes = notes
        result.finish = "tool_calls" if kept else (
            "length" if finish_reason in ("length", "max_tokens") else "stop"
        )
        return result

    result.notes = notes
    if not result.text.strip() and not result.calls:
        result.text = EMPTY_FALLBACK
    result.finish = "tool_calls" if result.calls else "stop"
    return result


def run_turn_stream(req: CanonRequest, cfg: Config) -> Iterator[Tuple[str, Any]]:
    """Streaming: yields ('text', str) | ('call', ToolCall) | ('usage', dict) | ('finish', str).

    Loop protection is applied at the moment a call completes, before it reaches the
    client, so a blocked call is converted into an explanatory text delta instead.
    """
    st, extra, allow_tools = _prepare(req, cfg)
    tools_by_name = _tools_by_name(req)
    payload = build_upstream_payload(req, cfg, extra, allow_tools)

    parser = StreamToolParser(tools_by_name, cfg.salvage_bare_json)
    emitted_calls: List[ToolCall] = []
    seen_this_turn: Dict[str, int] = {}
    usage: Dict[str, Any] = {}
    finish_reason = "stop"
    any_text = False
    raw_len = 0

    def consider(tc: ToolCall) -> Iterator[Tuple[str, Any]]:
        nonlocal any_text
        fp = tc.fp()
        if not allow_tools:
            yield (
                "text",
                "\n\n[loop guard] Tool budget of %d calls is exhausted, so I stopped "
                "calling tools." % cfg.max_tool_rounds,
            )
            any_text = True
            return
        if tc.name not in tools_by_name:
            yield (
                "text",
                "\n\n[error] The model tried to call an unknown tool `%s`." % tc.name,
            )
            any_text = True
            return
        if seen_this_turn.get(fp, 0) >= 1:
            return
        if st.counts.get(fp, 0) >= cfg.max_repeat:
            yield (
                "text",
                "\n\n[loop guard] Skipping a repeated `%s` call - identical arguments "
                "were already used %d times." % (tc.name, st.counts.get(fp, 0)),
            )
            any_text = True
            return
        if len(emitted_calls) >= cfg.max_calls_per_turn:
            return
        seen_this_turn[fp] = 1
        emitted_calls.append(tc)
        yield ("call", tc)

    for event in upstream_stream(cfg, payload):
        if "usage" in event:
            usage = event["usage"]
            continue
        if "finish" in event:
            finish_reason = event["finish"] or finish_reason
            continue
        if "reasoning" in event:
            continue  # never parse reasoning traces as tool calls
        chunk = event.get("text")
        if not chunk:
            continue
        raw_len += len(chunk)
        before = len(parser.calls)
        pieces = parser.feed(chunk)
        for piece in pieces:
            if piece:
                any_text = True
                yield ("text", piece)
        for tc in parser.calls[before:]:
            for out in consider(tc):
                yield out

    before = len(parser.calls)
    tail_pieces, _all_calls = parser.finish()
    for piece in tail_pieces:
        if piece:
            any_text = True
            yield ("text", piece)
    for tc in parser.calls[before:]:
        for out in consider(tc):
            yield out

    if not any_text and not emitted_calls:
        yield ("text", EMPTY_FALLBACK)

    if not usage:
        usage = {
            "prompt_tokens": _estimate_input_tokens(payload),
            "completion_tokens": estimate_tokens("x" * raw_len),
        }
    yield ("usage", usage)
    yield (
        "finish",
        "tool_calls"
        if emitted_calls
        else ("length" if finish_reason in ("length", "max_tokens") else "stop"),
    )


# ======================================================================================
# Canonical -> Anthropic responses
# ======================================================================================

_ANTHROPIC_STOP = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}


def _usage_anthropic(usage: Dict[str, Any]) -> Dict[str, int]:
    return {
        "input_tokens": int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("completion_tokens") or usage.get("output_tokens") or 0),
    }


def anthropic_response(req: CanonRequest, res: TurnResult) -> Dict[str, Any]:
    content: List[Dict[str, Any]] = []
    if res.text.strip():
        content.append({"type": "text", "text": res.text})
    for tc in res.calls:
        content.append(
            {
                "type": "tool_use",
                "id": tc.id or new_tool_use_id(),
                "name": tc.name,
                "input": tc.args if isinstance(tc.args, dict) else {},
            }
        )
    if not content:
        content.append({"type": "text", "text": EMPTY_FALLBACK})
    return {
        "id": new_message_id("msg"),
        "type": "message",
        "role": "assistant",
        "model": req.model or "emulated",
        "content": content,
        "stop_reason": _ANTHROPIC_STOP.get(res.finish, "end_turn"),
        "stop_sequence": None,
        "usage": _usage_anthropic(res.usage),
    }


def sse(event: str, data: Dict[str, Any]) -> bytes:
    return ("event: %s\ndata: %s\n\n" % (event, json.dumps(data, ensure_ascii=False))).encode("utf-8")


def anthropic_stream_bytes(req: CanonRequest, cfg: Config) -> Iterator[bytes]:
    msg_id = new_message_id("msg")
    model = req.model or "emulated"
    yield sse(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [],
                "stop_reason": None,
                "stop_sequence": None,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
        },
    )

    index = 0
    text_open = False
    usage: Dict[str, Any] = {}
    finish = "stop"
    out_chars = 0

    try:
        for kind, value in run_turn_stream(req, cfg):
            if kind == "text":
                if not text_open:
                    yield sse(
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": index,
                            "content_block": {"type": "text", "text": ""},
                        },
                    )
                    text_open = True
                out_chars += len(value)
                yield sse(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {"type": "text_delta", "text": value},
                    },
                )
            elif kind == "call":
                if text_open:
                    yield sse("content_block_stop", {"type": "content_block_stop", "index": index})
                    text_open = False
                    index += 1
                tc: ToolCall = value
                yield sse(
                    "content_block_start",
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {
                            "type": "tool_use",
                            "id": tc.id or new_tool_use_id(),
                            "name": tc.name,
                            "input": {},
                        },
                    },
                )
                blob = json.dumps(tc.args if isinstance(tc.args, dict) else {}, ensure_ascii=False)
                out_chars += len(blob)
                for i in range(0, len(blob), 256):
                    yield sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {"type": "input_json_delta", "partial_json": blob[i : i + 256]},
                        },
                    )
                yield sse("content_block_stop", {"type": "content_block_stop", "index": index})
                index += 1
            elif kind == "usage":
                usage = value
            elif kind == "finish":
                finish = value
    except UpstreamError as exc:
        log_error("stream failed: %s" % exc.message)
        if not text_open:
            yield sse(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                },
            )
            text_open = True
        yield sse(
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "text_delta", "text": "\n\n[upstream error] " + exc.message},
            },
        )

    if text_open:
        yield sse("content_block_stop", {"type": "content_block_stop", "index": index})

    u = _usage_anthropic(usage)
    if not u["output_tokens"]:
        u["output_tokens"] = estimate_tokens("x" * out_chars)
    yield sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": _ANTHROPIC_STOP.get(finish, "end_turn"), "stop_sequence": None},
            "usage": {"output_tokens": u["output_tokens"]},
        },
    )
    yield sse("message_stop", {"type": "message_stop"})


# ======================================================================================
# Canonical -> OpenAI responses
# ======================================================================================


def _usage_openai(usage: Dict[str, Any]) -> Dict[str, int]:
    p = int(usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
    c = int(usage.get("completion_tokens") or usage.get("output_tokens") or 0)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c}


def openai_response(req: CanonRequest, res: TurnResult) -> Dict[str, Any]:
    message: Dict[str, Any] = {"role": "assistant", "content": res.text or None}
    if res.calls:
        message["tool_calls"] = [
            {
                "id": new_openai_call_id(),
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(
                        tc.args if isinstance(tc.args, dict) else {}, ensure_ascii=False
                    ),
                },
            }
            for tc in res.calls
        ]
    return {
        "id": "chatcmpl-" + _rand_id(24),
        "object": "chat.completion",
        "created": int(time.time()),
        "model": req.model or "emulated",
        "choices": [
            {
                "index": 0,
                "message": message,
                "logprobs": None,
                "finish_reason": res.finish,
            }
        ],
        "usage": _usage_openai(res.usage),
    }


def openai_stream_bytes(req: CanonRequest, cfg: Config, include_usage: bool) -> Iterator[bytes]:
    cid = "chatcmpl-" + _rand_id(24)
    created = int(time.time())
    model = req.model or "emulated"

    def chunk(delta: Dict[str, Any], finish: Optional[str] = None) -> bytes:
        obj = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "logprobs": None, "finish_reason": finish}],
        }
        return ("data: %s\n\n" % json.dumps(obj, ensure_ascii=False)).encode("utf-8")

    yield chunk({"role": "assistant", "content": ""})

    tool_index = 0
    usage: Dict[str, Any] = {}
    finish = "stop"
    out_chars = 0

    try:
        for kind, value in run_turn_stream(req, cfg):
            if kind == "text":
                out_chars += len(value)
                yield chunk({"content": value})
            elif kind == "call":
                tc: ToolCall = value
                yield chunk(
                    {
                        "tool_calls": [
                            {
                                "index": tool_index,
                                "id": new_openai_call_id(),
                                "type": "function",
                                "function": {"name": tc.name, "arguments": ""},
                            }
                        ]
                    }
                )
                blob = json.dumps(tc.args if isinstance(tc.args, dict) else {}, ensure_ascii=False)
                out_chars += len(blob)
                for i in range(0, len(blob), 256):
                    yield chunk(
                        {
                            "tool_calls": [
                                {
                                    "index": tool_index,
                                    "function": {"arguments": blob[i : i + 256]},
                                }
                            ]
                        }
                    )
                tool_index += 1
            elif kind == "usage":
                usage = value
            elif kind == "finish":
                finish = value
    except UpstreamError as exc:
        log_error("stream failed: %s" % exc.message)
        yield chunk({"content": "\n\n[upstream error] " + exc.message})

    yield chunk({}, finish)

    if include_usage:
        u = _usage_openai(usage)
        if not u["completion_tokens"]:
            u["completion_tokens"] = estimate_tokens("x" * out_chars)
            u["total_tokens"] = u["prompt_tokens"] + u["completion_tokens"]
        obj = {
            "id": cid,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [],
            "usage": u,
        }
        yield ("data: %s\n\n" % json.dumps(obj, ensure_ascii=False)).encode("utf-8")

    yield b"data: [DONE]\n\n"


# ======================================================================================
# HTTP server
# ======================================================================================

ADVERTISED_MODELS = [
    "claude-sonnet-4-5-20250929",
    "claude-opus-4-1-20250805",
    "claude-3-5-haiku-20241022",
    "gpt-4o",
    "gpt-4o-mini",
]


# --- generated header: build_single_file.py strips these blocks ---
__all__ = [
    "TurnResult",
    "EMPTY_FALLBACK",
    "_tools_by_name",
    "_estimate_input_tokens",
    "_prepare",
    "run_turn",
    "run_turn_stream",
    "_ANTHROPIC_STOP",
    "_usage_anthropic",
    "anthropic_response",
    "sse",
    "anthropic_stream_bytes",
    "_usage_openai",
    "openai_response",
    "openai_stream_bytes",
    "ADVERTISED_MODELS",
]
# --- end generated header ---
