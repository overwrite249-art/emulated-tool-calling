# --- generated header: build_single_file.py strips these blocks ---
from __future__ import annotations
from ._prelude import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .protocol import *  # noqa: F401,F403
from .wire import *  # noqa: F401,F403
from .engine import *  # noqa: F401,F403
from .server import *  # noqa: F401,F403
from .selftest_a import *  # noqa: F401,F403
# --- end generated header ---


def _selftest_part2(r: "_Runner") -> int:  # noqa: C901
    # ---------------------------------------------------------------- streaming parser
    r.section("6. Streaming parser - chunk boundary fuzzing")

    def stream_split(text: str, sizes: List[int]) -> Tuple[str, List[ToolCall]]:
        p = StreamToolParser(DEMO_BY_NAME)
        out: List[str] = []
        i = 0
        k = 0
        while i < len(text):
            n = sizes[k % len(sizes)]
            k += 1
            out.extend(p.feed(text[i : i + n]))
            i += n
        tail, calls = p.finish()
        out.extend(tail)
        return "".join(out), calls

    sample = 'Reading now.\n<tool_call>\n{"name": "Read", "arguments": {"file_path": "/a.txt"}}\n</tool_call>'
    bad = 0
    leaked = 0
    for size in range(1, 40):
        text, calls = stream_split(sample, [size])
        if not (len(calls) == 1 and calls[0].args.get("file_path") == "/a.txt"):
            bad += 1
        if "tool_call" in text or "{" in text:
            leaked += 1
    r.eq("fixed-size splits 1..39 all parse", bad, 0)
    r.eq("no tag/JSON leaked into text", leaked, 0)

    bad = 0
    leaked = 0
    for seed in range(300):
        random.seed(seed)
        sizes = [random.randint(1, 11) for _ in range(24)]
        text, calls = stream_split(sample, sizes)
        if not (len(calls) == 1 and calls[0].args.get("file_path") == "/a.txt"):
            bad += 1
        if "tool_call" in text or "{" in text:
            leaked += 1
    r.eq("300 random split patterns parse", bad, 0)
    r.eq("300 random splits leak nothing", leaked, 0)

    text, calls = stream_split(sample, [7])
    r.eq("streamed visible text", text.strip(), "Reading now.")

    # truncated by stop sequence, streamed
    trunc = 'ok\n<tool_call>\n{"name":"Bash","arguments":{"command":"ls -la"}}'
    bad = 0
    for size in range(1, 25):
        _t, calls = stream_split(trunc, [size])
        if not (len(calls) == 1 and calls[0].args.get("command") == "ls -la"):
            bad += 1
    r.eq("streamed + stop-truncated parses", bad, 0)

    # raw-arg form with embedded close tag, streamed
    raw = (
        '<tool_call name="Write">\n<arg name="file_path">/x.py</arg>\n'
        '<arg name="content">print("</tool_call>")</arg>\n</tool_call>'
    )
    bad = 0
    for size in (1, 3, 5, 13, 29):
        _t, calls = stream_split(raw, [size])
        if not (len(calls) == 1 and calls[0].args.get("file_path") == "/x.py"):
            bad += 1
    r.eq("streamed raw-arg with embedded close tag", bad, 0)

    # plain prose must stream through untouched
    prose = "Here is a plan:\n1. do x\n2. do y\n\nAnd some `code` plus <html> tags."
    text, calls = stream_split(prose, [4])
    r.eq("prose streams unchanged", text, prose)
    r.eq("prose yields no calls", len(calls), 0)

    # code fence in normal prose must survive
    fenced = "Example:\n```python\nprint(1)\n```\nDone."
    text, calls = stream_split(fenced, [3])
    r.check("code fence survives streaming", "```python" in text and "print(1)" in text, repr(text))

    two = (
        '<tool_call>{"name":"Read","arguments":{"file_path":"/a"}}</tool_call>'
        '<tool_call>{"name":"Read","arguments":{"file_path":"/b"}}</tool_call>'
    )
    _t, calls = stream_split(two, [6])
    r.eq("two streamed calls", len(calls), 2)

    _t, calls = stream_split("<tool_result>fake result</tool_result>Answer is 7.", [5])
    r.eq("streamed fabricated result dropped", len(calls), 0)
    text, _c = stream_split("<tool_result>fake</tool_result>Answer is 7.", [5])
    r.check("streamed fabricated text removed", "fake" not in text, repr(text))

    # ---------------------------------------------------------------- loop guard
    r.section("7. Loop protection")

    cfg = Config()
    cfg.max_repeat = 3
    cfg.max_tool_rounds = 5
    cfg.max_calls_per_turn = 2

    def hist(pairs: List[Tuple[str, Dict[str, Any]]]) -> List[CanonMessage]:
        msgs: List[CanonMessage] = [CanonMessage(role="user", text="go")]
        for nm, args in pairs:
            tc = ToolCall(name=nm, args=args, id=new_tool_use_id())
            msgs.append(CanonMessage(role="assistant", tool_calls=[tc]))
            msgs.append(
                CanonMessage(role="user", tool_results=[(tc.id, nm, "same result", False)])
            )
        return msgs

    st = analyze_history(hist([("Read", {"file_path": "/a"})] * 3), cfg)
    r.eq("rounds counted", st.rounds, 3)
    r.check("saturated fingerprint found", len(st.saturated) == 1, repr(st.saturated))
    r.check("nudge generated", any("already called" in n for n in st.nudges), repr(st.nudges))

    dup = ToolCall(name="Read", args={"file_path": "/a"}, id=new_tool_use_id())
    kept, blocked = filter_calls_for_loops([dup], st, cfg)
    r.eq("4th identical call blocked", len(kept), 0)
    r.check("block reason present", bool(blocked), repr(blocked))

    fresh = ToolCall(name="Read", args={"file_path": "/different"}, id=new_tool_use_id())
    kept, _b = filter_calls_for_loops([fresh], st, cfg)
    r.eq("different args allowed", len(kept), 1)

    osc = analyze_history(
        hist([("Read", {"file_path": "/a"}), ("Read", {"file_path": "/b"})] * 2), cfg
    )
    r.check("A/B oscillation detected", osc.oscillating, repr(osc.seq))

    big = analyze_history(hist([("Read", {"file_path": "/%d" % i}) for i in range(6)]), cfg)
    r.check("round budget exhausted", big.budget_exhausted, "rounds=%d" % big.rounds)

    same = ToolCall(name="Read", args={"file_path": "/z"}, id=new_tool_use_id())
    kept, blocked = filter_calls_for_loops([same, same, same], LoopState(), cfg)
    r.eq("same-turn duplicates collapsed", len(kept), 1)

    many = [ToolCall(name="Read", args={"file_path": "/%d" % i}) for i in range(5)]
    kept, blocked = filter_calls_for_loops(many, LoopState(), cfg)
    r.eq("per-turn cap enforced", len(kept), 2)
    r.check("cap produced a reason", bool(blocked), repr(blocked))

    stale = analyze_history(hist([("Read", {"file_path": "/%d" % i}) for i in range(3)]), cfg)
    r.check(
        "identical results nudge",
        any("byte-for-byte identical" in n for n in stale.nudges),
        repr(stale.nudges),
    )

    # ---------------------------------------------------------------- end-to-end
    r.section("8. End-to-end over HTTP (mock upstream)")

    mock = _MockUpstream()
    mock.start()

    # Reset the shared config IN PLACE instead of rebinding the global name:
    # rebinding only updates this module's binding, so any other holder of a
    # reference to the original object would silently keep the old settings.
    saved = Config()
    saved.__dict__.update(CFG.__dict__)
    CFG.__dict__.update(Config().__dict__)
    CFG.upstream_base = "http://127.0.0.1:%d" % mock.port
    CFG.upstream_path = "/chat/completions"
    CFG.upstream_key = "test"
    CFG.log_level = "error"
    CFG.max_repeat = 3
    CFG.max_tool_rounds = 6

    proxy = Server(("127.0.0.1", 0), Handler)
    proxy.daemon_threads = True
    pport = proxy.server_address[1]
    threading.Thread(target=proxy.serve_forever, daemon=True).start()

    try:
        # --- Anthropic non-streaming, tool call
        mock.script('I will read it.\n<tool_call>{"name":"Read","arguments":{"file_path":"/a"}}</tool_call>')
        status, body = _http(
            pport,
            "/v1/messages",
            {
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 1024,
                "system": "You are a coding agent.",
                "messages": [{"role": "user", "content": "read /a"}],
                "tools": _anthropic_tools(),
            },
        )
        data = json.loads(body)
        r.eq("anthropic status", status, 200)
        r.eq("anthropic stop_reason", data.get("stop_reason"), "tool_use")
        blocks = data.get("content") or []
        tu = [b for b in blocks if b.get("type") == "tool_use"]
        tx = [b for b in blocks if b.get("type") == "text"]
        r.eq("anthropic one tool_use block", len(tu), 1)
        r.eq("anthropic tool name", tu[0]["name"] if tu else None, "Read")
        r.eq("anthropic tool input", tu[0]["input"] if tu else None, {"file_path": "/a"})
        r.check("anthropic tool_use id shape", tu and tu[0]["id"].startswith("toolu_"), repr(tu))
        r.check("anthropic text block kept", len(tx) == 1 and "read it" in tx[0]["text"], repr(tx))
        r.check(
            "anthropic usage present",
            isinstance(data.get("usage", {}).get("input_tokens"), int),
            repr(data.get("usage")),
        )

        sysp = mock.system_prompt()
        r.check("tool schemas injected into prompt", "### Read" in sysp and "### Bash" in sysp, sysp[:200])
        r.check("original system preserved", "You are a coding agent." in sysp, sysp[:200])
        r.check("anti-hallucination rule present", "NEVER write a `<tool_result>`" in sysp, "missing")
        r.check("no native tools sent upstream", "tools" not in mock.last_request(), repr(list(mock.last_request())))
        r.check("stop sequence follows configuration", (CALL_CLOSE in (mock.last_request().get("stop") or [])) == CFG.use_stop, repr(mock.last_request().get("stop")))

        # --- Anthropic streaming
        mock.script('Sure.\n<tool_call>{"name":"Bash","arguments":{"command":"ls"}}</tool_call>')
        status, body = _http(
            pport,
            "/v1/messages",
            {
                "model": "claude-sonnet-4-5-20250929",
                "max_tokens": 512,
                "messages": [{"role": "user", "content": "list files"}],
                "tools": _anthropic_tools(),
                "stream": True,
            },
        )
        evs = _parse_sse(body)
        names = [e[0] for e in evs]
        r.eq("stream starts with message_start", names[0] if names else None, "message_start")
        r.eq("stream ends with message_stop", names[-1] if names else None, "message_stop")
        r.check("has message_delta", "message_delta" in names, repr(names))
        starts = [e[1] for e in evs if e[0] == "content_block_start"]
        stops = [e[1] for e in evs if e[0] == "content_block_stop"]
        r.eq("block starts == block stops", len(starts), len(stops))
        tool_starts = [s for s in starts if s["content_block"]["type"] == "tool_use"]
        r.eq("one streamed tool_use block", len(tool_starts), 1)
        r.eq("streamed tool name", tool_starts[0]["content_block"]["name"] if tool_starts else None, "Bash")
        deltas = [e[1] for e in evs if e[0] == "content_block_delta"]
        partial = "".join(
            d["delta"]["partial_json"] for d in deltas if d["delta"]["type"] == "input_json_delta"
        )
        r.eq("input_json_delta reassembles", json.loads(partial) if partial else None, {"command": "ls"})
        txt = "".join(d["delta"]["text"] for d in deltas if d["delta"]["type"] == "text_delta")
        r.check("streamed text clean", "tool_call" not in txt and "Sure." in txt, repr(txt))
        md = [e[1] for e in evs if e[0] == "message_delta"]
        r.eq("streamed stop_reason", md[0]["delta"]["stop_reason"] if md else None, "tool_use")
        idxs = [s["index"] for s in starts]
        r.eq("block indices monotonic", idxs, sorted(set(idxs)))

        # --- Anthropic streaming, pure text
        mock.script("The answer is 42, no tools needed.")
        status, body = _http(
            pport,
            "/v1/messages",
            {
                "model": "claude-3-5-haiku-20241022",
                "max_tokens": 256,
                "messages": [{"role": "user", "content": "hi"}],
                "tools": _anthropic_tools(),
                "stream": True,
            },
        )
        evs = _parse_sse(body)
        deltas = [e[1] for e in evs if e[0] == "content_block_delta"]
        txt = "".join(d["delta"].get("text", "") for d in deltas)
        r.eq("text-only stream content", txt, "The answer is 42, no tools needed.")
        md = [e[1] for e in evs if e[0] == "message_delta"]
        r.eq("text-only stop_reason", md[0]["delta"]["stop_reason"] if md else None, "end_turn")
        r.eq("haiku routed to small model", mock.last_request().get("model"), CFG.model_small)

        # --- OpenAI non-streaming
        mock.script('<tool_call>{"name":"Grep","arguments":{"pattern":"TODO","mode":"content"}}</tool_call>')
        status, body = _http(
            pport,
            "/v1/chat/completions",
            {
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "find TODOs"}],
                "tools": _openai_tools(),
            },
        )
        data = json.loads(body)
        r.eq("openai status", status, 200)
        choice = (data.get("choices") or [{}])[0]
        r.eq("openai finish_reason", choice.get("finish_reason"), "tool_calls")
        tcs = choice.get("message", {}).get("tool_calls") or []
        r.eq("openai one tool call", len(tcs), 1)
        r.eq("openai tool name", tcs[0]["function"]["name"] if tcs else None, "Grep")
        r.check("openai arguments is a string", isinstance(tcs[0]["function"]["arguments"], str), "not str")
        r.eq(
            "openai arguments parse",
            json.loads(tcs[0]["function"]["arguments"]) if tcs else None,
            {"pattern": "TODO", "mode": "content"},
        )
        r.check("openai call id shape", tcs and tcs[0]["id"].startswith("call_"), repr(tcs))
        r.check("openai usage", isinstance(data.get("usage", {}).get("total_tokens"), int), repr(data.get("usage")))

        # --- OpenAI streaming
        mock.script('Looking.\n<tool_call>{"name":"Read","arguments":{"file_path":"/b"}}</tool_call>')
        status, body = _http(
            pport,
            "/v1/chat/completions",
            {
                "model": "gpt-4o",
                "messages": [{"role": "user", "content": "read b"}],
                "tools": _openai_tools(),
                "stream": True,
                "stream_options": {"include_usage": True},
            },
        )
        evs = _parse_sse(body)
        r.eq("openai stream terminated with [DONE]", evs[-1][0] if evs else None, "[DONE]")
        chunks = [e[1] for e in evs if e[0] != "[DONE]"]
        r.check("all chunks typed", all(c.get("object") == "chat.completion.chunk" for c in chunks), "bad object")
        acc_name = ""
        acc_args = ""
        acc_text = ""
        finishes = []
        for c in chunks:
            for ch in c.get("choices") or []:
                d = ch.get("delta") or {}
                acc_text += d.get("content") or ""
                for tc in d.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    acc_name += fn.get("name") or ""
                    acc_args += fn.get("arguments") or ""
                if ch.get("finish_reason"):
                    finishes.append(ch["finish_reason"])
        r.eq("openai streamed tool name", acc_name, "Read")
        r.eq("openai streamed args", json.loads(acc_args) if acc_args else None, {"file_path": "/b"})
        r.check("openai streamed text clean", "tool_call" not in acc_text, repr(acc_text))
        r.eq("openai finish_reason once", finishes, ["tool_calls"])
        usage_chunks = [c for c in chunks if c.get("usage")]
        r.eq("usage chunk emitted", len(usage_chunks), 1)

        return _selftest_part3(r, mock, pport, saved, proxy)
    except Exception:
        CFG.__dict__.update(saved.__dict__)
        proxy.shutdown()
        mock.stop()
        raise


def run_selftest() -> int:  # noqa: C901 - a test suite is allowed to be long
    random.seed(1337)
    r = _Runner()

    # ---------------------------------------------------------------- parser
    r.section("1. Parser - happy paths")

    t, c = extract_tool_calls(
        '<tool_call>\n{"name": "Read", "arguments": {"file_path": "/a.txt"}}\n</tool_call>',
        DEMO_BY_NAME,
    )
    r.check("clean JSON call", len(c) == 1 and c[0].name == "Read", repr(c))
    r.eq("clean JSON args", c[0].args if c else None, {"file_path": "/a.txt"})
    r.eq("no leftover text", t, "")

    t, c = extract_tool_calls(
        'Let me look.\n<tool_call>{"name":"Read","arguments":{"file_path":"/a"}}</tool_call>',
        DEMO_BY_NAME,
    )
    r.eq("prose preserved", t, "Let me look.")
    r.check("prose + call", len(c) == 1, repr(c))

    t, c = extract_tool_calls("Just a normal answer, no tools.", DEMO_BY_NAME)
    r.check("plain text -> no calls", not c and t == "Just a normal answer, no tools.", repr((t, c)))

    t, c = extract_tool_calls(
        '<tool_call>{"name":"Read","arguments":{"file_path":"/a"}}',  # stop seq ate close tag
        DEMO_BY_NAME,
    )
    r.check("missing close tag (stop sequence)", len(c) == 1 and c[0].args["file_path"] == "/a", repr(c))

    t, c = extract_tool_calls(
        '<tool_call>{"name":"Bash","arguments":{}}</tool_call>', DEMO_BY_NAME
    )
    r.eq("empty arguments object", c[0].args if c else None, {})

    r.section("2. Parser - malformed output the model actually produces")

    cases = [
        (
            "markdown fenced json",
            '```json\n<tool_call>\n{"name":"Read","arguments":{"file_path":"/a"}}\n</tool_call>\n```',
        ),
        (
            "fence inside tag",
            '<tool_call>\n```json\n{"name":"Read","arguments":{"file_path":"/a"}}\n```\n</tool_call>',
        ),
        ("trailing comma", '<tool_call>{"name":"Read","arguments":{"file_path":"/a",}}</tool_call>'),
        ("single quotes", "<tool_call>{'name':'Read','arguments':{'file_path':'/a'}}</tool_call>"),
        (
            "python literals",
            '<tool_call>{"name":"Read","arguments":{"file_path":"/a","limit":None}}</tool_call>',
        ),
        ("truncated json", '<tool_call>{"name":"Read","arguments":{"file_path":"/a"'),
        ("hyphen tag", '<tool-call>{"name":"Read","arguments":{"file_path":"/a"}}</tool-call>'),
        (
            "function_call tag",
            '<function_call>{"name":"Read","arguments":{"file_path":"/a"}}</function_call>',
        ),
        ("name attr + json args", '<tool_call name="Read">{"file_path":"/a"}</tool_call>'),
        ("tool_name alias", '<tool_call>{"tool_name":"Read","args":{"file_path":"/a"}}</tool_call>'),
        (
            "nested function form",
            '<tool_call>{"function":{"name":"Read","arguments":"{\\"file_path\\":\\"/a\\"}"}}</tool_call>',
        ),
        (
            "stringified arguments",
            '<tool_call>{"name":"Read","arguments":"{\\"file_path\\":\\"/a\\"}"}</tool_call>',
        ),
        ("xml arg form", '<tool_call name="Read">\n<arg name="file_path">/a</arg>\n</tool_call>'),
        (
            "antml parameter form",
            '<invoke name="Read">\n<parameter name="file_path">/a</parameter>\n</invoke>',
        ),
        ("invoke tag", '<invoke name="Read">\n<arg name="file_path">/a</arg>\n</invoke>'),
        ("lowercase mismatch", '<tool_call>{"name":"read","arguments":{"file_path":"/a"}}</tool_call>'),
        ("UPPER tag", '<TOOL_CALL>{"name":"Read","arguments":{"file_path":"/a"}}</TOOL_CALL>'),
        (
            "whitespace close tag",
            '<tool_call>{"name":"Read","arguments":{"file_path":"/a"}}</tool_call >',
        ),
    ]
    for label, payload in cases:
        _t, cc = extract_tool_calls(payload, DEMO_BY_NAME)
        ok = len(cc) == 1 and cc[0].name == "Read" and cc[0].args.get("file_path") == "/a"
        r.check(label, ok, repr(cc))

    _t, cc = extract_tool_calls(
        '<tool_call>{"name":"Read","arguments":{"file_path":"/a\nb"}}</tool_call>', DEMO_BY_NAME
    )
    r.check("raw newline inside JSON string", len(cc) == 1 and "\n" in cc[0].args["file_path"], repr(cc))

    r.section("3. Parser - raw content and escaping hazards")

    code = 'def f():\n    return "</tool_call> is literal"\n'
    payload = (
        '<tool_call name="Write">\n<arg name="file_path">/x.py</arg>\n'
        '<arg name="content">\n' + code + "</arg>\n</tool_call>"
    )
    _t, cc = extract_tool_calls(payload, DEMO_BY_NAME)
    r.check(
        "raw arg containing </tool_call>",
        len(cc) == 1 and cc[0].args.get("content", "").strip().endswith('is literal"'),
        repr(cc),
    )
    r.eq("raw arg sibling value", cc[0].args.get("file_path") if cc else None, "/x.py")

    payload = (
        '<tool_call name="Bash">\n<arg name="command">echo "hi" && ls | grep x</arg>\n'
        '<arg name="timeout">30</arg>\n<arg name="background">false</arg>\n</tool_call>'
    )
    _t, cc = extract_tool_calls(payload, DEMO_BY_NAME)
    r.eq("raw arg number coercion", cc[0].args.get("timeout") if cc else None, 30)
    r.eq("raw arg bool coercion", cc[0].args.get("background") if cc else None, False)
    r.eq(
        "raw arg shell string intact",
        cc[0].args.get("command") if cc else None,
        'echo "hi" && ls | grep x',
    )

    _t, cc = extract_tool_calls(
        '<tool_call>{"name":"Read","arguments":{"file_path":"/a","limit":"25"}}</tool_call>',
        DEMO_BY_NAME,
    )
    r.eq("schema coercion string->int", cc[0].args.get("limit") if cc else None, 25)

    r.section("4. Parser - hallucinated results and multi-call")

    payload = (
        '<tool_call>{"name":"Read","arguments":{"file_path":"/a"}}</tool_call>\n'
        "<tool_result>hello world this is fake</tool_result>\n"
        "Based on the file, the answer is 42."
    )
    t, cc = extract_tool_calls(payload, DEMO_BY_NAME)
    r.check("fabricated tool_result removed", "fake" not in t, repr(t))
    r.check("call still parsed", len(cc) == 1, repr(cc))

    t, _cc = extract_tool_calls("Working...\n<tool_result>partial and cut off", DEMO_BY_NAME)
    r.check("orphan tool_result removed", "partial" not in t, repr(t))

    payload = (
        '<tool_call>{"name":"Read","arguments":{"file_path":"/a"}}</tool_call>\n'
        '<tool_call>{"name":"Read","arguments":{"file_path":"/b"}}</tool_call>'
    )
    _t, cc = extract_tool_calls(payload, DEMO_BY_NAME)
    r.eq("two calls parsed", len(cc), 2)
    r.check("distinct fingerprints", cc[0].fp() != cc[1].fp(), "same fp")

    _t, cc = extract_tool_calls(
        '{"name":"Read","arguments":{"file_path":"/a"}}', DEMO_BY_NAME, salvage=True
    )
    r.check("bare JSON salvaged", len(cc) == 1, repr(cc))

    t, cc = extract_tool_calls(
        '{"name":"NotATool","arguments":{"x":1}}', DEMO_BY_NAME, salvage=True
    )
    r.check("unknown bare JSON not salvaged", not cc, repr(cc))

    t, cc = extract_tool_calls(
        'Here is JSON I am discussing: {"name":"Read"} - note it has no arguments key.',
        DEMO_BY_NAME,
        salvage=True,
    )
    r.check("prose about JSON not misparsed", not cc, repr(cc))

    r.section("5. Validation")

    r.eq("valid args", validate_args({"file_path": "/a"}, DEMO_BY_NAME["Read"].schema), [])
    r.check(
        "missing required detected",
        validate_args({}, DEMO_BY_NAME["Read"].schema) != [],
        "expected a problem",
    )
    r.check(
        "wrong type detected",
        validate_args({"file_path": 5}, DEMO_BY_NAME["Read"].schema) != [],
        "expected a problem",
    )
    r.check(
        "bad enum detected",
        validate_args({"pattern": "x", "mode": "nope"}, DEMO_BY_NAME["Grep"].schema) != [],
        "expected a problem",
    )
    r.eq(
        "good enum accepted",
        validate_args({"pattern": "x", "mode": "content"}, DEMO_BY_NAME["Grep"].schema),
        [],
    )
    r.check(
        "bool is not integer",
        validate_args({"file_path": "/a", "limit": True}, DEMO_BY_NAME["Read"].schema) != [],
        "expected a problem",
    )
    return _selftest_part2(r)


# --- generated header: build_single_file.py strips these blocks ---
__all__ = [
    "_selftest_part2",
    "run_selftest",
]
# --- end generated header ---
