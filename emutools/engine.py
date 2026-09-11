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
    allow_tools = bool(req.tools) and req.tool_choice != "none" and not st.budget_exhausted
    if st.budget_exhausted:
        extra.append(BUDGET_MESSAGE)
        log_warn(
            "tool budget exhausted after %d rounds; forcing a final answer" % st.rounds
        )
    return st, extra, allow_tools


def _call_limit(req: CanonRequest, cfg: Config) -> int:
    return max(0, cfg.max_calls_per_turn) if cfg.parallel and req.parallel_tool_calls is not False else min(1, max(0, cfg.max_calls_per_turn))


def _call_issues(tc: ToolCall, req: CanonRequest, tools: Dict[str, ToolDef]) -> List[str]:
    if tc.name not in tools:
        hint = ""
        close = difflib.get_close_matches(tc.name, list(tools), n=1, cutoff=0.6)
        if close:
            hint = " Did you mean `%s`?" % close[0]
        return ["Tool `%s` does not exist. Available tools: %s.%s"
                % (tc.name, ", ".join(sorted(tools)) or "(none)", hint)]
    if req.tool_choice not in ("auto", "required", "none", tc.name):
        return ["Tool `%s` is not the requested tool `%s`." % (tc.name, req.tool_choice)]
    return ["Call to `%s` is invalid: %s." % (tc.name, issue) for issue in validate_args(tc.args, tools[tc.name].schema)]


# A rejected call used to be reported to the user as the assistant's answer, which
# ends the client's task: a coding agent that guessed one unavailable tool name gets
# a successful-looking "[tool guard] Rejected tool call" message and stops. The
# streaming path now re-asks upstream with the reason, exactly like the
# non-streaming path, and only falls back to explanatory text when out of attempts.
RETRY_INSTRUCTION = (
    "CRITICAL: Your previous tool call was rejected and was NOT executed. "
    "Reply with only the corrected tool call, in the required format, and no other "
    "text. Do not repeat your previous explanation. "
)
_MISSING_TOOL = "does not exist"


# An attempt that produces neither text nor a usable call is worse than a rejected
# call: the client prints "the model returned an empty response" and ends the task.
EMPTY_REPLY_REASON = (
    "Your previous reply contained no text and no usable tool call. "
    "Reply now with either one tool call or a direct answer."
)

# After a rejection the model sometimes talks instead of calling: "Now I'll write
# payload.py." with no call at all. Accepting that ends the client's task with the
# work half done, so a turn that already tried to call a tool has to keep trying.
NO_CALL_REASON = (
    "Your previous reply talked about the tool call instead of making it, so "
    "nothing ran. Output the corrected tool call itself, with no commentary."
)

BOTCHED_CALL_REASON = (
    "Your previous reply contained tool-call markup that could not be parsed, so "
    "nothing ran. Emit the call again, exactly in the required format, and put no "
    "tool-call markup in ordinary prose."
)


# The repeat limit stops a runaway loop, but delivering "[loop guard] Skipping a
# repeated call" as the answer ends the client's task. Give the model the chance to
# break the loop itself first; the guard text is the last resort that still bounds it.
def _repeat_reason(name: str, count: int) -> str:
    return (
        "You have already called `%s` with exactly these arguments %d times and the "
        "result will not change. Do not repeat it. Either call a different tool, call "
        "it with different arguments, or give your final answer." % (name, count)
    )


def _named_tools(reasons: List[str], req: CanonRequest) -> List[Any]:
    """The tools a rejection talks about, so the retry can show correct examples."""
    blob = " ".join(reasons)
    return [t for t in req.tools if ("`%s`" % t.name) in blob]


def _corrective_note(reasons: List[str], req: CanonRequest) -> str:
    """Build the retry instruction, re-teaching the tool catalogue when needed.

    A model that invents a tool name tends to invent the same one again, so the
    corrective prompt has to restate the names it may actually use. Without this,
    three attempts are simply three identical rejections.
    """
    note = RETRY_INSTRUCTION + " ".join(reasons)
    examples = [render_tool_example(t) for t in _named_tools(reasons, req)[:3]]
    if examples:
        note += (
            "\nThe call must be exactly this shape, with these parameter names:\n"
            + "\n".join(examples)
            + "\nDo not wrap it in another object and do not rename the parameters."
        )
    if req.tools and any(_MISSING_TOOL in reason for reason in reasons):
        note += (
            "\nThe tool you named is NOT available. You may only call these tools, "
            "using these exact names and parameters:\n"
            + render_tool_signatures(req.tools)
            + "\nPick the closest available tool and call it now. To create or "
            "rewrite a file when no file-writing tool is listed, use an editing "
            "tool on the existing file instead."
        )
    return note


def run_turn(req: CanonRequest, cfg: Config) -> TurnResult:
    """Bounded repairs; no rejected or schema-invalid call can reach a client."""
    st, extra, allow_tools = _prepare(req, cfg)
    tools_by_name = _tools_by_name(req)
    total_usage: Dict[str, Any] = {}
    max_attempts = 3 if cfg.loop_retry else 1
    wanted_call = False

    for attempt in range(1, max_attempts + 1):
        payload = build_upstream_payload(req, cfg, extra, allow_tools)
        data = upstream_complete(cfg, payload)
        content, finish_reason, usage = extract_completion_text(data)
        if cfg.log_bodies:
            log_debug("RAW attempt %d: %s" % (attempt, truncate_middle(content, 4000)))
        text, calls = extract_tool_calls(content, tools_by_name, cfg.salvage_bare_json)
        if not usage:
            usage = {"prompt_tokens": _estimate_input_tokens(payload), "completion_tokens": estimate_tokens(content)}
        for key, value in usage.items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                total_usage[key] = total_usage.get(key, 0) + value
        notes: List[str] = []
        if not allow_tools and calls:
            notes.append("Tool calls are disabled for this turn (choice or conversation budget); ignored %d call(s)." % len(calls))
            calls = []
        valid: List[ToolCall] = []
        problems: List[str] = []
        for tc in calls:
            issues = _call_issues(tc, req, tools_by_name)
            if issues:
                problems.extend(issues)
            else:
                valid.append(tc)
        kept, blocked = filter_calls_for_loops(valid, st, cfg)
        limit = _call_limit(req, cfg)
        if len(kept) > limit:
            blocked.append("Dropped extra calls: at most %d tool call(s) allowed in this turn." % limit)
            kept = kept[:limit]
        requires_call = allow_tools and req.tool_choice not in ("auto", "none")
        wanted_call = wanted_call or bool(problems)
        retry_reasons = list(problems)
        if blocked and not kept:
            retry_reasons.extend(blocked)
        if requires_call and not kept:
            retry_reasons.append("This turn REQUIRES a valid call to the requested tool. Output only that call, corrected.")
        if wanted_call and not kept and not retry_reasons:
            retry_reasons.append(NO_CALL_REASON)
        if not kept and not retry_reasons and looks_like_botched_call(text):
            retry_reasons.append(BOTCHED_CALL_REASON)
        if retry_reasons and attempt < max_attempts:
            log_warn("retrying rejected tool output: %s" % retry_reasons[0][:160])
            extra = list(extra) + [_corrective_note(retry_reasons, req)]
            continue
        if requires_call and not kept:
            raise UpstreamError("model failed to satisfy tool_choice=%s after %d attempt(s)" % (req.tool_choice, attempt), 502)
        notes.extend(problems + blocked)
        if (not text.strip() or wanted_call) and not kept:
            if attempt < max_attempts:
                extra = list(extra) + ["Your previous reply was empty. Produce a substantive reply now."]
                continue
            if wanted_call or not notes:
                # A rejection or an empty completion returned as the answer looks
                # like a successful result and ends the client's task; a 5xx is
                # retried instead. Deliberate policy outcomes (tools disabled, loop
                # guard) are still explained in text, because there the request
                # itself asked for prose.
                raise UpstreamError(
                    ("upstream produced no usable reply after %d attempt(s): " % attempt)
                    + (" ".join(notes) if notes else "empty completion"),
                    529,
                )
            text = "I stopped without executing the rejected tool call. " + " ".join(notes)
        return TurnResult(text=text, calls=kept, usage=total_usage, notes=notes, attempts=attempt,
                          finish="tool_calls" if kept else ("length" if finish_reason in ("length", "max_tokens") else "stop"))
    raise AssertionError("unreachable: at least one completion attempt is required")


def run_turn_stream(req: CanonRequest, cfg: Config) -> Iterator[Tuple[str, Any]]:
    """Streaming: yields ('text', str) | ('call', ToolCall) | ('usage', dict) | ('finish', str).

    Loop protection is applied at the moment a call completes, before it reaches the
    client. A rejected call is re-asked upstream with the rejection reason, within the
    same client stream, so one bad tool name cannot end the client's whole task; only
    when the attempts run out does it become explanatory text.
    """
    st, extra, allow_tools = _prepare(req, cfg)
    tools_by_name = _tools_by_name(req)
    max_attempts = 3 if cfg.loop_retry else 1

    emitted_calls: List[ToolCall] = []
    seen_this_turn: Dict[str, int] = {}
    usage_total: Dict[str, Any] = {}
    usage_reported = False
    finish_reason = "stop"
    any_text = False
    raw_len = 0
    corrective: List[str] = []
    final_issues: List[str] = []
    wanted_call = False
    loop_stopped = False
    payload: Dict[str, Any] = {}

    for attempt in range(1, max_attempts + 1):
        last_attempt = attempt >= max_attempts
        payload = build_upstream_payload(req, cfg, extra + corrective, allow_tools)
        parser = StreamToolParser(tools_by_name, cfg.salvage_bare_json)
        rejected: List[str] = []
        attempt_raw = 0
        attempt_usage = False
        attempt_text = ""

        def consider(tc: ToolCall) -> Iterator[Tuple[str, Any]]:
            nonlocal wanted_call, loop_stopped
            fp = tc.fp()
            if not allow_tools:
                yield (
                    "text",
                    "\n\n[tool guard] Tool calls are disabled for this turn (choice or conversation budget).",
                )
                return
            issues = _call_issues(tc, req, tools_by_name)
            if issues:
                wanted_call = True
                if req.tool_choice not in ("auto", "none", "required") and tc.name != req.tool_choice:
                    raise UpstreamError("model did not call the requested tool `%s`" % req.tool_choice, 502)
                if last_attempt:
                    # Out of attempts. Say nothing: explaining the rejection here
                    # would make the proxy's diagnostic the assistant's answer, and
                    # the client would treat that as a finished task.
                    final_issues.extend(issues)
                else:
                    rejected.extend(issues)
                return
            if seen_this_turn.get(fp, 0) >= 1:
                return
            if st.counts.get(fp, 0) >= cfg.max_repeat:
                if not last_attempt:
                    # Not `wanted_call`: here a prose answer is exactly what the
                    # model is being asked for instead of the pointless repeat.
                    rejected.append(_repeat_reason(tc.name, st.counts.get(fp, 0)))
                    return
                loop_stopped = True
                yield (
                    "text",
                    "\n\n[loop guard] Skipping a repeated `%s` call - identical arguments "
                    "were already used %d times." % (tc.name, st.counts.get(fp, 0)),
                )
                return
            if len(emitted_calls) >= _call_limit(req, cfg):
                return
            seen_this_turn[fp] = 1
            emitted_calls.append(tc)
            yield ("call", tc)

        stream = upstream_stream(cfg, payload)
        held: List[Tuple[str, Any]] = []
        seen_raw: List[str] = []
        try:
            for event in stream:
                if "usage" in event:
                    for key, value in (event["usage"] or {}).items():
                        if isinstance(value, (int, float)) and not isinstance(value, bool):
                            usage_total[key] = usage_total.get(key, 0) + value
                            usage_reported = True
                            attempt_usage = True
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
                attempt_raw += len(chunk)
                if cfg.log_bodies:
                    seen_raw.append(chunk)
                before = len(parser.calls)
                pieces = parser.feed(chunk)
                for piece in pieces:
                    if piece:
                        held.append(("text", piece))
                for tc in parser.calls[before:]:
                    for out in consider(tc):
                        held.append(out)
                if rejected:
                    # Text produced next to a call that is about to be re-asked is
                    # dropped with it, so a retry cannot duplicate the preamble.
                    break  # stop paying for output that cannot be used
                for out in held:
                    if out[0] == "text":
                        any_text = True
                        attempt_text += out[1]
                    yield out
                held = []

            if not rejected:
                before = len(parser.calls)
                tail_pieces, _all_calls = parser.finish()
                for piece in tail_pieces:
                    if piece:
                        held.append(("text", piece))
                for tc in parser.calls[before:]:
                    for out in consider(tc):
                        held.append(out)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                close()
            if cfg.log_bodies:
                log_debug("RAW attempt %d: %s"
                          % (attempt, truncate_middle("".join(seen_raw), 4000)))

        requires_call = allow_tools and req.tool_choice not in ("auto", "none")
        if not rejected and requires_call and not emitted_calls and not last_attempt:
            rejected.append(
                "This turn REQUIRES a valid call to the requested tool. "
                "Output only that call, corrected."
            )
        if not rejected and not emitted_calls and not last_attempt:
            if wanted_call:
                rejected.append(NO_CALL_REASON)
            elif not any_text:
                rejected.append(EMPTY_REPLY_REASON)
            elif looks_like_botched_call(attempt_text):
                rejected.append(BOTCHED_CALL_REASON)
        if rejected and not emitted_calls:
            # Abandoning the stream early saves output tokens, but the tokens already
            # produced were still billed, so estimate them instead of losing them.
            if not attempt_usage:
                usage_total["prompt_tokens"] = (
                    usage_total.get("prompt_tokens", 0) + _estimate_input_tokens(payload))
                usage_total["completion_tokens"] = (
                    usage_total.get("completion_tokens", 0) + estimate_tokens("x" * attempt_raw))
                usage_reported = True
            log_warn("retrying rejected streamed tool output: %s" % rejected[0][:160])
            corrective = [_corrective_note(rejected, req)]
            held = []
            continue
        for out in held:
            if out[0] == "text":
                any_text = True
                attempt_text += out[1]
            yield out
        held = []
        break

    if allow_tools and req.tool_choice not in ("auto", "none") and not emitted_calls:
        raise UpstreamError("model failed to satisfy tool_choice=%s" % req.tool_choice, 502)
    if not emitted_calls and not loop_stopped and (not any_text or wanted_call):
        # Ending the turn here would hand the client a successful-looking answer that
        # is really a proxy diagnostic, and the client would stop working. A 5xx is
        # honest and is what the client retries, so the task survives a bad sample.
        raise UpstreamError(
            ("upstream produced no usable reply after %d attempt(s): " % max_attempts)
            + (" ".join(final_issues) if final_issues else "empty completion"), 529)

    if not usage_reported:
        usage_total = {
            "prompt_tokens": _estimate_input_tokens(payload),
            "completion_tokens": estimate_tokens("x" * raw_len),
        }
    yield ("usage", usage_total)
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
    turn = run_turn_stream(req, cfg)
    # Pull the first usable event before announcing the message. A turn that fails
    # outright then raises before any byte is written, so the caller can still answer
    # with an HTTP status the client retries instead of a truncated stream.
    try:
        first: Optional[Tuple[str, Any]] = next(turn)
    except StopIteration:
        first = None
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
        def replay() -> Iterator[Tuple[str, Any]]:
            if first is not None:
                yield first
            for event in turn:
                yield event

        for kind, value in replay():
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
        yield sse("error", {"type": "error", "error": {"type": "api_error", "message": exc.message}})
        return

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
            "usage": u,
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

    turn = run_turn_stream(req, cfg)
    try:
        first: Optional[Tuple[str, Any]] = next(turn)
    except StopIteration:
        first = None
    yield chunk({"role": "assistant", "content": ""})

    tool_index = 0
    usage: Dict[str, Any] = {}
    finish = "stop"
    out_chars = 0

    def replay() -> Iterator[Tuple[str, Any]]:
        if first is not None:
            yield first
        for event in turn:
            yield event

    try:
        for kind, value in replay():
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
        error = {"error": {"message": exc.message, "type": "api_error", "code": exc.status}}
        yield ("data: %s\n\n" % json.dumps(error)).encode("utf-8")
        return

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
    "_call_limit",
    "_call_issues",
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
