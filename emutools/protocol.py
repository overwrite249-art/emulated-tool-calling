# --- generated header: build_single_file.py strips these blocks ---
from __future__ import annotations
from ._prelude import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
# --- end generated header ---


_PROTOCOL_HEADER = """\
# Tool calling protocol

You have access to tools. There is no native tool-calling channel here: you invoke a
tool by writing a plain-text block into your reply, and the runtime executes it for you.

Do NOT use your own built-in tool-call markup, function-call channel, or any special
sentinel tokens. Only the exact `<tool_call>` block described below is read by the
runtime; any other tool-call syntax is treated as plain text and discarded.

## Available tools

{tools_block}

## How to invoke a tool

Emit exactly this, and nothing after it:

<tool_call>
{{"name": "TOOL_NAME", "arguments": {{"arg": "value"}}}}
</tool_call>

Hard rules:

1. The block body MUST be a single JSON object with exactly two keys: "name" and "arguments".
   "arguments" is always an object, even when empty: {{"name": "Ping", "arguments": {{}}}}
2. STOP generating immediately after `</tool_call>`. Write nothing after it.
3. NEVER write a `<tool_result>` block yourself. NEVER invent, guess, predict or
   describe what a tool returned. The runtime executes the tool and sends you the real
   result in the next message. Text you invent is a hallucination and will be discarded.
4. Use only the tool names listed above, spelled exactly. Do not invent tools.
5. Supply every required parameter, with the declared JSON types (a number is `3`,
   not `"3"`; a boolean is `true`, not `"true"`).
6. If you do not need a tool, just answer normally in prose with no block at all.
7. Never place a tool call inside a markdown code fence.

## Raw form for awkward strings

If an argument contains source code, newlines, backslashes or quotes that are painful to
JSON-escape, use the raw form instead - no escaping is needed inside it:

<tool_call name="TOOL_NAME">
<arg name="file_path">/tmp/demo.py</arg>
<arg name="content">
print("hello \\ \"world\"")
</arg>
</tool_call>

## Avoiding loops

- Before calling a tool, check whether an earlier `<tool_result>` in this conversation
  already answers the question. If it does, reuse it instead of calling again.
- Never repeat a call you have already made with identical arguments.
- If a tool keeps failing, change your approach or explain the problem to the user.
  Do not retry the same call over and over.
- Prefer the smallest number of calls that gets the job done, then answer.
"""

_ONE_CALL_RULE = (
    "\n## One call at a time\n\n"
    "Emit at most ONE `<tool_call>` block per reply. Wait for its result before deciding\n"
    "what to do next.\n"
)

_PARALLEL_RULE = (
    "\n## Multiple calls\n\n"
    "You may emit several `<tool_call>` blocks back to back when the calls are genuinely\n"
    "independent. Never emit two calls where the second depends on the first's result.\n"
)


def _schema_summary(schema: Dict[str, Any]) -> str:
    """Compact, readable rendering of a JSON schema's top-level params."""
    if not isinstance(schema, dict):
        return "(no parameters)"
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return "(no parameters)"
    required = schema.get("required") or []
    if not isinstance(required, list):
        required = []
    lines = []
    for pname, pschema in props.items():
        if not isinstance(pschema, dict):
            pschema = {}
        ptype = pschema.get("type", "any")
        if isinstance(ptype, list):
            ptype = "|".join(str(t) for t in ptype)
        bits = [str(ptype)]
        if pname in required:
            bits.append("required")
        else:
            bits.append("optional")
        enum = pschema.get("enum")
        if isinstance(enum, list) and enum:
            preview = ", ".join(json.dumps(e, ensure_ascii=False) for e in enum[:8])
            if len(enum) > 8:
                preview += ", ..."
            bits.append("one of: " + preview)
        desc = pschema.get("description") or ""
        desc = re.sub(r"\s+", " ", str(desc)).strip()
        if len(desc) > 320:
            desc = desc[:317] + "..."
        line = "    - %s (%s)" % (pname, "; ".join(bits))
        if desc:
            line += ": " + desc
        lines.append(line)
    return "\n".join(lines)


def render_tools_block(tools: List[ToolDef]) -> str:
    chunks = []
    for t in tools:
        desc = re.sub(r"\n{3,}", "\n\n", (t.description or "").strip())
        if len(desc) > 4000:
            desc = desc[:3997] + "..."
        piece = ["### %s" % t.name]
        if desc:
            piece.append(desc)
        piece.append("Parameters:")
        piece.append(_schema_summary(t.schema))
        try:
            piece.append(
                "JSON schema: " + json.dumps(t.schema, ensure_ascii=False, separators=(",", ":"))
            )
        except (TypeError, ValueError):
            pass
        chunks.append("\n".join(piece))
    return "\n\n".join(chunks)


def render_tool_signatures(tools: List[ToolDef]) -> str:
    """One compact line per tool: `- Edit(file_path, old_string, new_string)`.

    Used when a model keeps calling a tool the client never registered. Repeating
    the exact available names, with their parameters, is what actually moves a weak
    model off a wrong name; a bare "does not exist" does not.
    """
    lines = []
    for tool in tools:
        schema = tool.schema or {}
        properties = schema.get("properties") or {}
        required = [k for k in (schema.get("required") or []) if k in properties]
        optional = [k for k in properties if k not in required]
        names = required + ["%s?" % k for k in optional]
        lines.append("- %s(%s)" % (tool.name, ", ".join(names)))
    return "\n".join(lines)


def build_tool_prompt(tools: List[ToolDef], parallel: bool) -> str:
    body = _PROTOCOL_HEADER.format(tools_block=render_tools_block(tools))
    body += _PARALLEL_RULE if parallel else _ONE_CALL_RULE
    return body


def render_tool_call_text(tc: ToolCall) -> str:
    payload = {"name": tc.name, "arguments": tc.args if isinstance(tc.args, dict) else {}}
    return CALL_OPEN + "\n" + json.dumps(payload, ensure_ascii=False) + "\n" + CALL_CLOSE


def render_tool_result_text(name: str, content: str, is_error: bool, limit: int) -> str:
    body = truncate_middle(content or "", limit)
    attrs = ' name="%s"' % (name or "tool")
    if is_error:
        attrs += ' status="error"'
    return "%s%s\n%s\n%s" % (RESULT_OPEN[:-1], attrs + ">", body, RESULT_CLOSE)


# ======================================================================================
# Tolerant JSON
# ======================================================================================

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*\n(.*?)\n?\s*```\s*$", re.DOTALL)


def strip_fences(text: str) -> str:
    s = text.strip()
    for _ in range(3):
        m = _FENCE_RE.match(s)
        if not m:
            break
        s = m.group(1).strip()
    return s


def _walk_strings(s: str):
    """Yield (index, char, in_string) with correct escape handling."""
    in_str = False
    esc = False
    for i, ch in enumerate(s):
        yield i, ch, in_str
        if esc:
            esc = False
            continue
        if ch == "\\" and in_str:
            esc = True
            continue
        if ch == '"':
            in_str = not in_str


def _escape_control_chars_in_strings(s: str) -> str:
    out = []
    for _, ch, in_str in _walk_strings(s):
        if in_str and ch == "\n":
            out.append("\\n")
        elif in_str and ch == "\r":
            out.append("\\r")
        elif in_str and ch == "\t":
            out.append("\\t")
        else:
            out.append(ch)
    return "".join(out)


def _strip_trailing_commas(s: str) -> str:
    out = []
    pending: List[str] = []
    for _, ch, in_str in _walk_strings(s):
        if in_str:
            if pending:
                out.extend(pending)
                pending = []
            out.append(ch)
            continue
        if ch == ",":
            pending.append(ch)
            continue
        if pending:
            if ch in "}]":
                pending = []  # drop the trailing comma(s)
            else:
                out.extend(pending)
                pending = []
        out.append(ch)
    out.extend(pending)
    return "".join(out)


_PY_LITERALS = (("True", "true"), ("False", "false"), ("None", "null"))


def _fix_python_literals(s: str) -> str:
    spans = []
    for py, js in _PY_LITERALS:
        for m in re.finditer(r"\b%s\b" % py, s):
            spans.append((m.start(), m.end(), js))
    if not spans:
        return s
    instr = {}
    for i, _ch, in_str in _walk_strings(s):
        instr[i] = in_str
    spans.sort(reverse=True)
    chars = list(s)
    for start, end, js in spans:
        if instr.get(start, False):
            continue
        chars[start:end] = list(js)
    return "".join(chars)


def _balance_braces(s: str) -> str:
    """Append the closing brackets a truncated object/array is missing."""
    stack = []
    for _, ch, in_str in _walk_strings(s):
        if in_str:
            continue
        if ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack and ((ch == "}" and stack[-1] == "{") or (ch == "]" and stack[-1] == "[")):
                stack.pop()
    # unterminated string?
    in_str_final = False
    for _, _ch, in_str in _walk_strings(s + " "):
        in_str_final = in_str
    # Never invent the rest of a string: it may be a shell command or file body.
    # Closing structural braces is safe only after the last value is complete.
    if in_str_final:
        return s
    tail = ""
    while stack:
        tail += "}" if stack.pop() == "{" else "]"
    return s + tail


def _extract_first_object(s: str) -> Optional[str]:
    start = -1
    depth = 0
    for i, ch, in_str in _walk_strings(s):
        if in_str:
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    return s[start : i + 1]
    if start >= 0:
        return s[start:]
    return None


def loads_tolerant(text: str) -> Tuple[Optional[Any], bool]:
    """Parse JSON, repairing common LLM mistakes.

    Returns (value, repaired). value is None when unrecoverable.
    """
    if text is None:
        return None, False
    s = strip_fences(str(text))
    if not s.strip():
        return None, False

    try:
        return json.loads(s), False
    except ValueError:
        pass

    candidates = []
    obj = _extract_first_object(s)
    if obj:
        candidates.append(obj)
    candidates.append(s)

    for cand in candidates:
        for transform in (
            lambda x: x,
            _strip_trailing_commas,
            lambda x: _fix_python_literals(_strip_trailing_commas(x)),
            lambda x: _escape_control_chars_in_strings(_fix_python_literals(_strip_trailing_commas(x))),
            lambda x: _balance_braces(
                _escape_control_chars_in_strings(_fix_python_literals(_strip_trailing_commas(x)))
            ),
        ):
            try:
                fixed = transform(cand)
            except Exception:  # noqa: BLE001 - repair must never explode
                continue
            try:
                return json.loads(fixed), True
            except ValueError:
                continue

    # Last resort: single-quoted pseudo-JSON.
    try:
        import ast

        val = ast.literal_eval(s if not obj else obj)
        if isinstance(val, (dict, list)):
            return json.loads(json.dumps(val, default=str)), True
    except Exception:  # noqa: BLE001
        pass
    return None, False


# ======================================================================================
# Tool-call extraction
# ======================================================================================

OPEN_TAG_NAMES = [
    "tool_call",
    "tool-call",
    "toolcall",
    "function_call",
    "function-call",
    "tool_use",
    "antml:invoke",
    "invoke",
]

_TAGS_ALT = "|".join(re.escape(t) for t in OPEN_TAG_NAMES)
# Recognize vendor sentinels at syntax boundaries, never rewrite argument data.
_VENDOR = r"(?:(?:\uff5c{1,2}|\|{1,2})\s*DSML\s*(?:\uff5c{1,2}|\|{1,2})\s*)?"
_OPEN_RE = re.compile(r"<" + _VENDOR + r"(" + _TAGS_ALT + r")(\s[^>]*?)?\s*/?>", re.IGNORECASE)
_ATTR_RE = re.compile(r"([A-Za-z_:][-\w:.]*)\s*=\s*(\"([^\"]*)\"|'([^']*)')")
_ARG_RE = re.compile(
    r"<" + _VENDOR + r"(arg|parameter|param)(\s[^>]*?)?\s*>(.*?)</" + _VENDOR + r"\1\s*>", re.IGNORECASE | re.DOTALL
)
_RESULT_BLOCK_RE = re.compile(
    r"<tool_result\b[^>]*>.*?</tool_result\s*>", re.IGNORECASE | re.DOTALL
)
_ORPHAN_RESULT_RE = re.compile(r"<tool_result\b[^>]*>.*\Z", re.IGNORECASE | re.DOTALL)

# --- Vendor dialect normalisation -------------------------------------------------
# Some models emit their OWN native tool-call markup into the plain text channel
# instead of the protocol we asked for. DeepSeek v4 (verified live against
# api.deepseek.com) uses fullwidth U+FF5C sentinels:
#
#   <｜｜DSML｜｜tool_calls>
#   <｜｜DSML｜｜invoke name="Read">
#   <｜｜DSML｜｜parameter name="file_path" string="true">/etc/hosts</｜｜DSML｜｜parameter>
#   </｜｜DSML｜｜invoke>
#   </｜｜DSML｜｜tool_calls>
#
# Without this the sentinels leak to the client as visible garbage and the call is
# lost. Normalising here means the rest of the parser only sees canonical tags.
# The trailing lookahead matters for streaming: without it, a buffer that ends mid
# marker ('<｜｜DSML｜') would match with a single closing pipe and strand the second
# one, corrupting the tag once the rest of the chunk arrives.
_DSML_RE = re.compile(
    r"(</?)\s*(?:\uff5c{1,2}|\|{1,2})\s*DSML\s*(?:\uff5c{1,2}|\|{1,2})\s*(?=[A-Za-z_])",
    re.IGNORECASE,
)
# Plural wrapper around one or more <invoke> blocks - carries no data itself.
_WRAPPER_RE = re.compile(
    r"</?" + _VENDOR + r"(?:tool_calls|tool-calls|toolcalls|function_calls|antml:function_calls)\s*>",
    re.IGNORECASE,
)

# --- Vendor sentinel names --------------------------------------------------------
# Observed live from DeepSeek: the model wraps the requested <tool_call> protocol in
# its own channel markers. They arrive in several shapes for the same name, because
# the underscore is sometimes U+2581 ('tool▁calls▁begin'), sometimes a plain space
# ('<｜｜DSML｜｜ calls>'), and the fullwidth pipe count varies. These names never
# carry data, so the only correct thing to do with them in the *text* channel is
# delete them. Nothing here runs on tool-argument data: argument bodies are cut out
# of the text before this is applied, so a file whose contents legitimately include
# '｜tool▁calls▁begin｜' still round-trips byte for byte.
_SENTINEL_SEP = r"[\u2581_\-\s]*"


def _sentinel_pattern(name: str) -> str:
    return _SENTINEL_SEP.join(re.escape(part) for part in name.split("_"))


_WRAPPER_NAMES = (
    "calls",
    "tool_calls",
    "tool_calls_begin",
    "tool_calls_end",
    "tool_call_begin",
    "tool_call_end",
    "tool_sep",
    "tool_outputs_begin",
    "tool_outputs_end",
    "tool_output_begin",
    "tool_output_end",
    "function_calls",
    "antml:function_calls",
    "dsml",
)
_PIPES = r"(?:[\uff5c|]{1,2}\s*)?"
# A wrapper written as a tag: '<｜｜DSML｜｜ calls>', '<｜tool▁calls▁begin｜>', '</calls>'.
# Observed live with trailing words too - '<calls in parallel>' - so anything that is
# not a '>' is allowed after the name, exactly as in a real tag's attribute list.
_VENDOR_TAG_RE = re.compile(
    r"</?\s*" + _PIPES + r"(?:" + "|".join(_sentinel_pattern(n) for n in _WRAPPER_NAMES)
    + r")(?:\s+[^<>]{0,120}?)?\s*" + _PIPES + r"/?>",
    re.IGNORECASE,
)
# The same wrapper written bare, with no angle brackets: '｜tool▁calls▁begin｜'.
_BARE_SENTINEL_NAMES = tuple(n for n in _WRAPPER_NAMES if n != "calls")
# Consecutive sentinels share a pipe run ('...begin｜｜tool▁call▁begin｜'), so match a
# whole run of names in one go instead of letting the first match eat the pipe that
# opens the next one and strand its name as visible text.
_BARE_SENTINEL_RE = re.compile(
    r"(?:[\uff5c|]{1,2}\s*(?:"
    + "|".join(_sentinel_pattern(n) for n in _BARE_SENTINEL_NAMES)
    + r")\s*)+[\uff5c|]{1,2}",
    re.IGNORECASE,
)


_TAG_NORMALIZED = tuple(sorted({
    re.sub(r"[^a-z0-9:]", "", name.lower())
    for name in tuple(OPEN_TAG_NAMES) + _WRAPPER_NAMES + ("tool_result",)
}))


_BARE_NORMALIZED = tuple(
    re.sub(r"[^a-z0-9:]", "", name.lower()) for name in _BARE_SENTINEL_NAMES
)


def _maybe_bare_sentinel(tail: str) -> bool:
    """True when a pipe-led tail could still grow into a bare vendor sentinel."""
    if not tail or tail[0] not in ("\uff5c", "|"):
        return False
    core = tail.lstrip("\uff5c|")
    if "\uff5c" in core or "|" in core:
        return False  # already closed: it is either a full sentinel or ordinary text
    key = re.sub(r"[^a-z0-9:]", "", core.lower())
    if len(key) != len(re.sub(r"[\u2581_\-\s]", "", core)):
        return False  # contains characters no sentinel name can hold
    if core and not key:
        return False  # separators only: a pipe followed by a newline is not a name
    return any(name.startswith(key) for name in _BARE_NORMALIZED)


def strip_vendor_markup(text: str) -> str:
    """Delete vendor channel markers from text that is about to be shown to a user.

    Applied only to the visible-text channel. `_WRAPPER_RE` alone was not enough:
    it recognized `<tool_calls>` but not the fullwidth/U+2581/space spellings that
    DeepSeek actually emits, so markers leaked into every assistant message.
    """
    if not text:
        return text
    if "DSML" in text or "dsml" in text:
        text = _DSML_RE.sub(r"\1", text)
    if "<" in text:
        text = _VENDOR_TAG_RE.sub("", text)
    if "\uff5c" in text or "|" in text:
        text = _BARE_SENTINEL_RE.sub("", text)
    return text


_ENDS_WITH_SENTINEL_RE = re.compile(
    r"(?:" + _BARE_SENTINEL_RE.pattern + r"|" + _VENDOR_TAG_RE.pattern + r")$",
    re.IGNORECASE,
)


def split_vendor_markup(text: str) -> Tuple[str, str, bool]:
    """Stream-safe strip: returns (clean text, held tail, tail is vendor residue).

    A trailing pipe run is ambiguous: it can close the sentinel that just ended or
    open the next one, and consecutive sentinels share their pipes. Stripping the
    whole buffer unconditionally therefore eats the pipe that a later chunk needs
    and leaks the following name as visible text. So: if a complete sentinel ends
    exactly at the buffer end, strip everything; otherwise hold back the trailing
    fragment that could still grow into one.

    The third value distinguishes "this pipe is left over from a sentinel" from
    "this pipe is ordinary text", so a Markdown table is never rewritten.
    """
    if not text:
        return text, "", False
    if _ENDS_WITH_SENTINEL_RE.search(text):
        # Keep one pipe: the next chunk may attach the name of the next sentinel to
        # it. A pipe left over at the true end of the message is dropped on flush.
        if text[-1] in ("\uff5c", "|"):
            return strip_vendor_markup(text), text[-1], True
        return strip_vendor_markup(text), "", False
    bar = max(text.rfind("\uff5c"), text.rfind("|"))
    while bar > 0 and text[bar - 1] in ("\uff5c", "|"):
        bar -= 1  # hold the whole pipe run, not just its last character
    if bar >= 0 and (len(text) - bar) <= 48 and _maybe_bare_sentinel(text[bar:]):
        # Backing up over the run can steal the closing pipe of the sentinel that
        # just ended, leaving it unclosed and visible. Close it with a synthetic
        # pipe, strip, then drop the synthetic pipe if it survived.
        pipe = text[bar]
        head = strip_vendor_markup(text[:bar] + pipe)
        if head.endswith(pipe):
            head = head[:-1]
        return head, text[bar:], False
    return strip_vendor_markup(text), "", False


def normalize_dialects(text: str) -> str:
    """Rewrite vendor-specific tool-call markup into the canonical tag form."""
    if not text:
        return text
    if "DSML" in text or "dsml" in text:
        text = _DSML_RE.sub(r"\1", text)
    if "calls" in text or "CALLS" in text:
        text = _WRAPPER_RE.sub("", text)
    return text


# Text fragments that could be the beginning of a sentinel; held back while streaming.
# Closing forms are included so a split '</｜｜DSM' never leaks half a sentinel.
STREAM_SENTINELS = (
    ["<" + t for t in OPEN_TAG_NAMES]
    + ["</" + t for t in OPEN_TAG_NAMES]
    + ["<tool_result", "<tool_calls", "</tool_calls", "<tool-calls", "</tool-calls", "<function_calls", "</function_calls", "<antml:function_calls", "</antml:function_calls"]
    + [
        "<tool_result",
        "<tool_calls",
        "</tool_calls",
        "<\uff5c\uff5cdsml\uff5c\uff5c",
        "</\uff5c\uff5cdsml\uff5c\uff5c",
        "<||dsml||",
        "</||dsml||",
    ]
)
_MAX_SENTINEL = max(len(s) for s in STREAM_SENTINELS)
_FENCE_TAIL_RE = re.compile(r"(?:^|\n)`{1,3}[a-zA-Z0-9_+-]*[ \t]*\n?$")



# Markup that can only be an attempted tool call. Both shapes were seen live and both
# ended the client's task: an argument element with no call tag around it, and a call
# tag whose attribute list is never closed, so no parser can find its body.
_ARG_ELEMENT_RE = re.compile(
    r"<(?:arg|parameter|param)\s+name\s*=", re.IGNORECASE)
_UNCLOSED_CALL_TAG_RE = re.compile(
    r"<(?:" + _TAGS_ALT + r")\s[^>]{0,400}\Z", re.IGNORECASE)
# Any orphan closing tag that reads like a call terminator, however the model spelled
# it. Live, a turn ended with a bare '</_call>' as the assistant's answer.
_ANY_CLOSE_RE = re.compile(r"</\s*([\w:.\-\u2581\uff5c|]{1,40})\s*>")
_CALL_CLOSE_SUFFIXES = (
    "call", "calls", "invoke", "arg", "args", "parameter", "param", "function",
    "tooluse", "toolcalls",
)


def looks_like_botched_call(text: str) -> bool:
    """True when visible text contains tool-call markup but no call was parsed."""
    if not text:
        return False
    if _ARG_ELEMENT_RE.search(text) or _UNCLOSED_CALL_TAG_RE.search(text):
        return True
    for match in _ANY_CLOSE_RE.finditer(text):
        key = re.sub(r"[^a-z]", "", match.group(1).lower())
        if key.endswith(_CALL_CLOSE_SUFFIXES):
            return True
    return False


def _parse_attrs(raw: Optional[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not raw:
        return out
    for m in _ATTR_RE.finditer(raw):
        key = m.group(1).lower()
        val = m.group(3) if m.group(3) is not None else (m.group(4) or "")
        out[key] = val
    return out


def _find_close(text: str, tag: str, start: int) -> Tuple[int, int]:
    """Find syntax, not a closing tag embedded in JSON or raw argument data."""
    close_re = re.compile(r"</\s*" + _VENDOR + re.escape(tag) + r"\s*>", re.IGNORECASE)
    body = strip_fences(text[start:]).lstrip()
    is_json = body.startswith(("{", "["))
    for match in close_re.finditer(text, start):
        segment = text[start:match.start()]
        if is_json:
            quote = ""
            escaped = False
            for ch in segment:
                if escaped:
                    escaped = False
                elif quote and ch == "\\":
                    escaped = True
                elif quote and ch == quote:
                    quote = ""
                elif not quote and ch in ("\"", "'"):
                    quote = ch
            if quote:
                continue
        else:
            opens = len(re.findall(r"<" + _VENDOR + r"(?:arg|parameter|param)\b", segment, re.IGNORECASE))
            closes = len(re.findall(r"</" + _VENDOR + r"(?:arg|parameter|param)\s*>", segment, re.IGNORECASE))
            if opens > closes:
                continue
        return match.start(), match.end()
    return -1, -1


def _coerce_scalar(value: str, ptype: Any) -> Any:
    types = ptype if isinstance(ptype, list) else [ptype]
    types = [str(t).lower() for t in types if t]
    text = value
    if "string" in types and len(types) == 1:
        return text
    stripped = text.strip()
    if "boolean" in types:
        if stripped.lower() in ("true", "yes", "1"):
            return True
        if stripped.lower() in ("false", "no", "0"):
            return False
    if "integer" in types:
        try:
            return int(stripped)
        except ValueError:
            pass
    if "number" in types:
        try:
            f = float(stripped)
            return int(f) if f.is_integer() and "integer" in types else f
        except ValueError:
            pass
    if "array" in types or "object" in types:
        parsed, _ = loads_tolerant(stripped)
        if isinstance(parsed, (list, dict)):
            return parsed
    if "null" in types and stripped.lower() in ("null", "none", ""):
        return None
    if not types or types == ["any"]:
        parsed, _ = loads_tolerant(stripped)
        if isinstance(parsed, (list, dict, bool, int, float)):
            return parsed
    return text


def coerce_args(args: Dict[str, Any], schema: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Nudge string-ified values into the types the schema declares."""
    if not isinstance(args, dict):
        return {}
    if not isinstance(schema, dict):
        return args
    props = schema.get("properties")
    if not isinstance(props, dict):
        return args
    out = dict(args)
    for key, val in list(out.items()):
        pschema = props.get(key)
        if not isinstance(pschema, dict):
            continue
        ptype = pschema.get("type")
        if ptype is None:
            continue
        if isinstance(val, str):
            out[key] = _coerce_scalar(val, ptype)
        elif isinstance(val, (int, float)) and not isinstance(val, bool):
            types = ptype if isinstance(ptype, list) else [ptype]
            if "string" in [str(t).lower() for t in types]:
                out[key] = str(val)
    return out


def validate_args(args: Dict[str, Any], schema: Optional[Dict[str, Any]]) -> List[str]:
    """Validate the common JSON Schema subset, recursively, without dependencies.

    Supports local $ref, combinators, types, required/properties/items, enum/const,
    additionalProperties, and common bounds. Unsupported keywords are ignored;
    this is not a replacement for a complete JSON Schema implementation.
    """
    root = schema if isinstance(schema, dict) else {}

    def check(value: Any, spec: Any, path: str, depth: int) -> List[str]:
        if depth > 32:
            return [path + " exceeds schema validation depth"]
        if spec is False:
            return [path + " is not allowed"]
        if not isinstance(spec, dict):
            return []
        errors: List[str] = []
        ref = spec.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            target: Any = root
            for part in ref[2:].split("/"):
                target = target.get(part.replace("~1", "/").replace("~0", "~")) if isinstance(target, dict) else None
            if target is not None:
                errors.extend(check(value, target, path, depth + 1))
        for keyword in ("allOf", "anyOf", "oneOf"):
            options = spec.get(keyword)
            if isinstance(options, list) and options:
                matches = sum(not check(value, opt, path, depth + 1) for opt in options)
                if ((keyword == "allOf" and matches != len(options)) or
                    (keyword == "anyOf" and matches == 0) or
                    (keyword == "oneOf" and matches != 1)):
                    errors.append(path + " does not satisfy " + keyword)
        if "not" in spec and not check(value, spec["not"], path, depth + 1):
            errors.append(path + " matches a forbidden schema")
        ptype = spec.get("type")
        types = ptype if isinstance(ptype, list) else ([ptype] if ptype else [])
        predicates = {
            "null": value is None,
            "boolean": isinstance(value, bool),
            "string": isinstance(value, str),
            "number": isinstance(value, (int, float)) and not isinstance(value, bool) and (not isinstance(value, float) or math.isfinite(value)),
            "integer": isinstance(value, (int, float)) and not isinstance(value, bool) and (not isinstance(value, float) or math.isfinite(value)) and int(value) == value,
            "array": isinstance(value, list),
            "object": isinstance(value, dict),
        }
        if types and not any(predicates.get(t, True) for t in types):
            return errors + [path + " must be " + "|".join(map(str, types))]
        def same(a: Any, b: Any) -> bool:
            if isinstance(a, bool) != isinstance(b, bool):
                return False
            return a == b
        if isinstance(spec.get("enum"), list) and not any(same(value, v) for v in spec["enum"]):
            errors.append(path + " is not one of the allowed values")
        if "const" in spec and not same(value, spec["const"]):
            errors.append(path + " does not match const")
        if isinstance(value, dict):
            required = spec.get("required", [])
            if isinstance(required, list):
                errors.extend(path + " missing required parameter %r" % k for k in required if isinstance(k, str) and k not in value)
            props = spec.get("properties", {})
            props = props if isinstance(props, dict) else {}
            patterns = spec.get("patternProperties", {})
            patterns = patterns if isinstance(patterns, dict) else {}
            for key, item in value.items():
                matched = key in props
                if matched:
                    errors.extend(check(item, props[key], path + "." + key, depth + 1))
                for pattern, sub in patterns.items():
                    try:
                        if re.search(pattern, key):
                            matched = True
                            errors.extend(check(item, sub, path + "." + key, depth + 1))
                    except re.error:
                        pass
                if not matched:
                    errors.extend(check(item, spec.get("additionalProperties", True), path + "." + key, depth + 1))
        if isinstance(value, list):
            for i, item in enumerate(value):
                errors.extend(check(item, spec.get("items", {}), "%s[%d]" % (path, i), depth + 1))
            for keyword, fails in (("minItems", lambda n: len(value) < n), ("maxItems", lambda n: len(value) > n)):
                if isinstance(spec.get(keyword), (int, float)) and fails(spec[keyword]):
                    errors.append(path + " violates " + keyword)
            if spec.get("uniqueItems") and len({canon_json(v) for v in value}) != len(value):
                errors.append(path + " must contain unique items")
        if isinstance(value, str):
            for keyword, fails in (("minLength", lambda n: len(value) < n), ("maxLength", lambda n: len(value) > n)):
                if isinstance(spec.get(keyword), (int, float)) and fails(spec[keyword]):
                    errors.append(path + " violates " + keyword)
            if isinstance(spec.get("pattern"), str):
                try:
                    if not re.search(spec["pattern"], value):
                        errors.append(path + " does not match pattern")
                except re.error:
                    pass
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            for keyword, fails in (("minimum", lambda n: value < n), ("maximum", lambda n: value > n),
                                   ("exclusiveMinimum", lambda n: value <= n), ("exclusiveMaximum", lambda n: value >= n)):
                bound = spec.get(keyword)
                if isinstance(bound, (int, float)) and not isinstance(bound, bool) and fails(bound):
                    errors.append(path + " violates " + keyword)
        return errors[:20]

    if not isinstance(args, dict):
        return ["arguments must be an object"]
    return check(args, root, "arguments", 0)


# Keys a model uses when it writes a call envelope. A weak model often writes the
# envelope twice - '{"name":"Read","arguments":{"name":"Read","arguments":{...}}}' -
# and observed live from DeepSeek it repeats the same mistake on every retry, so
# rejecting it means the client's task simply stops. The inner object is
# unambiguous, so unwrap it instead.
_ENVELOPE_NAME_KEYS = ("name", "tool", "tool_name", "function_name")
_ENVELOPE_ARG_KEYS = ("arguments", "input", "parameters", "args")


def unwrap_call_envelope(args: Dict[str, Any], schema: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Strip repeated call envelopes from an argument object.

    Only unwraps when the outer object is *nothing but* an envelope and the tool's
    own schema does not declare those names, so a tool that really takes an `input`
    or `name` parameter is never rewritten.
    """
    properties = (schema or {}).get("properties") or {}
    for _ in range(3):
        if not isinstance(args, dict) or not args:
            return args
        if any(key in properties for key in args):
            return args
        if set(args) - set(_ENVELOPE_NAME_KEYS) - set(_ENVELOPE_ARG_KEYS):
            return args
        inner = None
        for key in _ENVELOPE_ARG_KEYS:
            value = args.get(key)
            if isinstance(value, dict):
                inner = value
                break
            if isinstance(value, str) and value.strip().startswith("{"):
                parsed, _repaired = loads_tolerant(value)
                if isinstance(parsed, dict):
                    inner = parsed
                    break
        if inner is None:
            return args
        args = inner
    return args


# Parameter names a weak model reaches for instead of the schema's own. Observed
# live: `cmd` for Bash, `path` for Read, `old_str` for Edit. Rejecting these costs
# a whole retry round for a call whose intent is unambiguous, so rename instead.
_ARG_SYNONYMS = {
    "cmd": ("command",),
    "command_line": ("command",),
    "shell": ("command",),
    "script": ("command",),
    "path": ("file_path", "filename", "notebook_path", "target_file"),
    "filepath": ("file_path",),
    "file": ("file_path",),
    "filename": ("file_path",),
    "file_name": ("file_path",),
    "full_path": ("file_path",),
    "absolute_path": ("file_path",),
    "old_str": ("old_string",),
    "new_str": ("new_string",),
    "old": ("old_string",),
    "new": ("new_string",),
    "old_text": ("old_string",),
    "new_text": ("new_string",),
    "search": ("old_string", "pattern"),
    "replace": ("new_string", "replacement"),
    "text": ("content", "prompt", "command"),
    "contents": ("content",),
    "body": ("content",),
    "data": ("content",),
    "query": ("pattern", "prompt"),
    "regex": ("pattern",),
    "dir": ("path", "directory"),
    "url": ("uri",),
}


def _rename_target(key: str, props: Dict[str, Any], taken: Iterable[str]) -> Optional[str]:
    """Find the schema property a stray argument name was probably meant to be."""
    used = set(taken)
    free = [name for name in props if name not in used]
    if not free:
        return None
    lowered = {name.lower(): name for name in free}
    plain = {re.sub(r"[^a-z0-9]", "", name.lower()): name for name in free}
    candidates = [key] + list(_ARG_SYNONYMS.get(key.lower(), ()))
    for candidate in candidates:
        low = candidate.lower()
        if low in lowered:
            return lowered[low]
        flat = re.sub(r"[^a-z0-9]", "", low)
        if flat in plain:
            return plain[flat]
    close = difflib.get_close_matches(key.lower(), list(lowered), n=1, cutoff=0.82)
    if close:
        return lowered[close[0]]
    return None


def repair_args(
    args: Dict[str, Any], schema: Optional[Dict[str, Any]]
) -> Tuple[Dict[str, Any], bool]:
    """Make a nearly-right argument object satisfy the tool's schema.

    A tool-less model gets the *shape* of a call right far more often than the exact
    parameter names, and every rejection costs a retry round that usually repeats the
    same mistake. Four unambiguous repairs are applied, in order:

    1. repeated call envelopes are unwrapped (see `unwrap_call_envelope`);
    2. a stray name is renamed to the schema property it clearly meant
       (`cmd` -> `command`), never overwriting a value that is already there;
    3. if exactly one required property is still missing and exactly one unknown
       name is left over, that value is moved onto it;
    4. names the schema does not declare are dropped rather than failing the call,
       which is what a real tool API does with extra keys.

    Returns the repaired object and whether anything changed.
    """
    if not isinstance(args, dict):
        return ({} if args is None else {"value": args}), args is not None
    if not isinstance(schema, dict):
        return args, False
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return args, False
    out = unwrap_call_envelope(args, schema)
    changed = out is not args
    if not isinstance(out, dict):
        return args, False
    out = dict(out)

    strays = [key for key in out if key not in props]
    for key in list(strays):
        target = _rename_target(key, props, out)
        if target is None:
            continue
        out[target] = out.pop(key)
        strays.remove(key)
        changed = True

    required = [name for name in (schema.get("required") or []) if isinstance(name, str)]
    missing = [name for name in required if name not in out]
    if len(missing) == 1 and len(strays) == 1:
        out[missing[0]] = out.pop(strays[0])
        strays = []
        changed = True

    extra = schema.get("additionalProperties")
    if strays and extra is not True:
        for key in strays:
            out.pop(key, None)
        changed = True
    return out, changed


def render_tool_example(tool: ToolDef) -> str:
    """One correct call for `tool`, to show a model what it got wrong."""
    schema = tool.schema if isinstance(tool.schema, dict) else {}
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    required = [n for n in (schema.get("required") or []) if isinstance(n, str) and n in props]
    names = required or list(props)[:2]
    example: Dict[str, Any] = {}
    for name in names:
        spec = props.get(name) if isinstance(props.get(name), dict) else {}
        kind = spec.get("type")
        kind = (kind[0] if isinstance(kind, list) and kind else kind) or "string"
        enum = spec.get("enum")
        if isinstance(enum, list) and enum:
            example[name] = enum[0]
        elif kind in ("integer", "number"):
            example[name] = 1
        elif kind == "boolean":
            example[name] = True
        elif kind == "array":
            example[name] = ["..."]
        elif kind == "object":
            example[name] = {}
        else:
            example[name] = "..."
    return json.dumps({"name": tool.name, "arguments": example}, ensure_ascii=False)



# A text-only model very often writes a sentence and *then* a bare JSON call, with
# no wrapper at all:
#
#     Now I'll fill in payload.py.
#
#     {"name": "Edit", "arguments": {"file_path": "...", ...}}
#
# Observed live from deepseek-flash in the middle of an otherwise successful job.
# Leaking that as prose loses the call and ends the client's task, so a bare call
# object is recognized wherever it starts a line.
_BARE_CALL_START_RE = re.compile(r"(?m)^[ \t]*\{")
_RESULT_OPEN_RE = re.compile(r"<" + _VENDOR + r"tool_result\b[^>]*>", re.IGNORECASE)
# '```json' on the line above a bare call belongs to the call, not to the prose.
_FENCE_OPEN_TAIL_RE = re.compile(r"(?:^|\n)[ \t]*`{3}[a-zA-Z0-9_+-]*[ \t]*\n[ \t]*$")


def _fence_backoff(text: str, start: int) -> int:
    """Move `start` back over a code fence that opens the bare call."""
    match = _FENCE_OPEN_TAIL_RE.search(text[:start])
    if not match:
        return start
    return match.start() + 1 if text[match.start()] == "\n" else match.start()
_BARE_CALL_KEYS = _ENVELOPE_NAME_KEYS + ("function",)
# '{"type": "function", "function": {...}}' is the OpenAI shape, which a model that
# has seen that schema writes even when asked for the text protocol.
_BARE_CALL_HINT_KEYS = _BARE_CALL_KEYS + ("type",)
_BARE_CALL_PREFIXES = tuple('{"%s"' % key for key in _BARE_CALL_HINT_KEYS)


def _maybe_bare_call(text: str, extra: Tuple[str, ...] = ()) -> bool:
    """True when `text` could still grow into a bare JSON call envelope."""
    core = re.sub(r"\s+", "", text[:48])
    if not core.startswith("{"):
        return False
    return any(p.startswith(core) or core.startswith(p)
               for p in _BARE_CALL_PREFIXES + extra)


def argument_key_prefixes(tools_by_name: Dict[str, ToolDef]) -> Tuple[str, ...]:
    """`{"<key>"` for every parameter name any registered tool declares.

    A model sometimes writes only the argument object and leaves the name out
    entirely - `{"command": "...", "description": "..."}` - so the first key of a
    call can be a parameter name rather than `name`.
    """
    keys = set()
    for tool in tools_by_name.values():
        schema = tool.schema if isinstance(tool.schema, dict) else {}
        props = schema.get("properties")
        if isinstance(props, dict):
            keys.update(k for k in props if isinstance(k, str))
    return tuple('{"%s"' % key for key in sorted(keys))


def args_only_tool(
    args: Dict[str, Any], tools_by_name: Dict[str, ToolDef]
) -> Optional[str]:
    """The one tool whose schema exactly fits a nameless argument object.

    Only unambiguous fits count: every key must be a parameter of that tool, every
    required parameter must be present, and no other tool may fit as well.
    """
    if not isinstance(args, dict) or not args:
        return None
    keys = set(args)
    hits = []
    for tool in tools_by_name.values():
        schema = tool.schema if isinstance(tool.schema, dict) else {}
        props = schema.get("properties")
        required = {n for n in (schema.get("required") or []) if isinstance(n, str)}
        if not isinstance(props, dict) or not props or not required:
            continue
        if keys <= set(props) and required <= keys:
            hits.append(tool.name)
    return hits[0] if len(hits) == 1 else None


def json_object_end(text: str, start: int) -> int:
    """Index just past the balanced JSON object at `start`, or -1 if still open."""
    if start >= len(text) or text[start] != "{":
        return -1
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
    return -1


# The whole object has to look like nothing but a call envelope. That keeps a JSON
# sample inside an explanation - `{"name": "svc", "port": 80}` - as prose, while
# still catching a call to a tool the client never registered: the model really is
# calling, so it must reach the rejection-and-retry path instead of leaking as the
# assistant's answer. Live, an unrecognized bare `{"name": "Write", ...}` became the
# final answer and the job stopped with the work unfinished.
_ENVELOPE_ONLY_KEYS = frozenset(
    _ENVELOPE_NAME_KEYS + _ENVELOPE_ARG_KEYS + ("function", "type", "id", "index")
)


def _bare_call_envelope(text: str, tools_by_name: Dict[str, ToolDef]) -> Optional[ToolCall]:
    """Parse `text` as a bare call envelope, registered tool or not."""
    parsed, _repaired = loads_tolerant(text)
    if not isinstance(parsed, dict):
        return None
    inner_fn = parsed.get("function") if isinstance(parsed.get("function"), dict) else {}
    name = ""
    for key in _BARE_CALL_KEYS:
        value = inner_fn.get("name") if key == "function" else parsed.get(key)
        if isinstance(value, str) and value:
            name = value
            break
    if not name:
        implied = args_only_tool(parsed, tools_by_name)
        if implied:
            return _build_call(implied, parsed, text, True, tools_by_name)
        return None
    if set(parsed) - _ENVELOPE_ONLY_KEYS:
        return None
    args = None
    for source in (parsed, inner_fn):
        for key in _ENVELOPE_ARG_KEYS:
            if key in source:
                args = source[key]
                break
        if args is not None:
            break
    if isinstance(args, str):
        args, _repaired = loads_tolerant(args)
    if not isinstance(args, dict):
        return None
    return parse_call_body(text, {}, tools_by_name)


def bare_call_starts(
    text: str, at_line_start: bool = True, extra: Tuple[str, ...] = ()
) -> Iterator[int]:
    """Indices of `{` that begin a line and could open a bare call envelope.

    `at_line_start` says whether index 0 of `text` really is the start of a line.
    A streaming buffer keeps only the unemitted tail, so `^` alone would treat the
    middle of a sentence - or a nested object - as a line start.
    """
    for match in _BARE_CALL_START_RE.finditer(text):
        start = match.end() - 1
        if start == 0 and not at_line_start:
            continue
        if _maybe_bare_call(text[start:], extra):
            yield start


def find_bare_call(
    text: str,
    tools_by_name: Dict[str, ToolDef],
    at_line_start: bool = True,
    extra: Optional[Tuple[str, ...]] = None,
) -> Optional[Tuple[int, int, ToolCall]]:
    """Find the first line-leading bare JSON call. Returns (start, end, call)."""
    if extra is None:
        extra = argument_key_prefixes(tools_by_name)
    for start in bare_call_starts(text, at_line_start, extra):
        end = json_object_end(text, start)
        if end < 0:
            continue
        call = _bare_call_envelope(text[start:end], tools_by_name)
        if call is not None:
            return start, end, call
    return None


def _build_call(
    name: str,
    args: Any,
    raw: str,
    repaired: bool,
    tools_by_name: Dict[str, ToolDef],
) -> Optional[ToolCall]:
    if not name:
        return None
    resolved = name
    if resolved not in tools_by_name:
        lowered = {k.lower(): k for k in tools_by_name}
        if resolved.lower() in lowered:
            resolved = lowered[resolved.lower()]
        else:
            stripped = re.sub(r"[^A-Za-z0-9_]", "", resolved).lower()
            alt = {re.sub(r"[^A-Za-z0-9_]", "", k).lower(): k for k in tools_by_name}
            if stripped in alt:
                resolved = alt[stripped]
    if not isinstance(args, dict):
        args = {} if args is None else {"value": args}
    tdef = tools_by_name.get(resolved)
    if tdef is not None:
        args, fixed = repair_args(args, tdef.schema)
        repaired = repaired or fixed
        args = coerce_args(args, tdef.schema)
    return ToolCall(name=resolved, args=args, id=new_tool_use_id(), raw=raw, repaired=repaired)


def parse_call_body(
    body: str,
    attrs: Dict[str, str],
    tools_by_name: Dict[str, ToolDef],
) -> Optional[ToolCall]:
    """Turn the inside of a <tool_call> block into a ToolCall."""
    raw = body
    body = strip_fences(body)
    name = attrs.get("name") or attrs.get("tool") or attrs.get("function") or ""

    # Raw <arg name="...">...</arg> form.
    arg_matches = [] if body.lstrip().startswith(("{", "[")) else list(_ARG_RE.finditer(body))
    if arg_matches:
        args: Dict[str, Any] = {}
        for m in arg_matches:
            a_attrs = _parse_attrs(m.group(2))
            key = a_attrs.get("name") or a_attrs.get("key")
            if not key:
                continue
            val = m.group(3)
            if val.startswith("\n"):
                val = val[1:]
            if val.endswith("\n"):
                val = val[:-1]
            args[key] = val
        if not name:
            leading = body[: arg_matches[0].start()].strip()
            parsed, _ = loads_tolerant(leading)
            if isinstance(parsed, dict):
                name = safe_str(parsed.get("name"))
            if not name and leading and "\n" not in leading and len(leading) < 80:
                name = leading.strip().strip("\"'")
        if name:
            return _build_call(name, args, raw, True, tools_by_name)

    parsed, repaired = loads_tolerant(body)
    if isinstance(parsed, dict):
        pname = ""
        for key in ("name", "tool", "tool_name", "function", "function_name", "recipient_name"):
            if isinstance(parsed.get(key), str) and parsed.get(key):
                pname = parsed[key]
                break
        if not pname and isinstance(parsed.get("function"), dict):
            pname = safe_str(parsed["function"].get("name"))
        pargs: Any = None
        for key in ("arguments", "input", "parameters", "args", "parameter_values"):
            if key in parsed:
                pargs = parsed[key]
                break
        if pargs is None and isinstance(parsed.get("function"), dict):
            pargs = parsed["function"].get("arguments")
        if isinstance(pargs, str):
            reparsed, rep2 = loads_tolerant(pargs)
            if isinstance(reparsed, dict):
                pargs = reparsed
                repaired = repaired or rep2
            else:
                pargs = {"value": pargs}
        if pargs is None:
            if name or pname:
                leftovers = {
                    k: v
                    for k, v in parsed.items()
                    if k
                    not in (
                        "name",
                        "tool",
                        "tool_name",
                        "function",
                        "function_name",
                        "recipient_name",
                        "type",
                        "id",
                    )
                }
                pargs = leftovers
            else:
                pargs = {}
        final_name = pname or name
        if final_name:
            return _build_call(final_name, pargs, raw, repaired, tools_by_name)

    if name and not body.strip():
        return _build_call(name, {}, raw, True, tools_by_name)
    return None


def strip_hallucinated_results(text: str) -> str:
    """Delete any <tool_result> the model wrote itself - it is always fabricated."""
    cleaned = _RESULT_BLOCK_RE.sub("", text)
    cleaned = _ORPHAN_RESULT_RE.sub("", cleaned)
    return cleaned


def _clean_trailing_fence(text: str) -> str:
    return re.sub(r"(?:^|\n)`{3}[a-zA-Z0-9_+-]*[ \t]*\n?\s*$", "\n", text)


def extract_tool_calls(
    text: str,
    tools_by_name: Dict[str, ToolDef],
    salvage: bool = True,
) -> Tuple[str, List[ToolCall]]:
    """Split model output into (visible text, tool calls)."""
    if not text:
        return "", []
    # A bare call must be recognized before scanning its JSON string values.
    candidate = strip_fences(text)
    if salvage and candidate.startswith("{") and len(candidate) < 20000:
        call = _bare_call_envelope(candidate, tools_by_name)
        if call:
            return "", [call]

    calls: List[ToolCall] = []
    out_parts: List[str] = []
    pos = 0

    while True:
        m = _OPEN_RE.search(text, pos)
        result = re.search(r"<" + _VENDOR + r"tool_result\b[^>]*>", text[pos:], re.IGNORECASE)
        if result and (not m or pos + result.start() < m.start()):
            # Everything after a fabricated result depends on a tool that never ran.
            out_parts.append(text[pos:pos + result.start()])
            break
        if not m:
            out_parts.append(text[pos:])
            break
        tag = m.group(1)
        attrs = _parse_attrs(m.group(2))
        # Self-closing tag with everything in attributes.
        if m.group(0).rstrip().endswith("/>"):
            out_parts.append(text[pos : m.start()])
            call = parse_call_body("", attrs, tools_by_name)
            if call:
                calls.append(call)
            pos = m.end()
            continue

        c_start, c_end = _find_close(text, tag, m.end())
        if c_start < 0:
            # Truncated by a stop sequence or max_tokens: parse what we have.
            out_parts.append(text[pos : m.start()])
            call = parse_call_body(text[m.end() :], attrs, tools_by_name)
            if call:
                calls.append(call)
            pos = len(text)
            break

        out_parts.append(text[pos : m.start()])
        call = parse_call_body(text[m.end() : c_start], attrs, tools_by_name)
        if call:
            calls.append(call)
        pos = c_end

    visible = strip_vendor_markup("".join(out_parts))
    visible = strip_hallucinated_results(visible)
    if calls:
        visible = _clean_trailing_fence(visible)

    # Salvage: a bare JSON call object with no tags at all.
    if not calls and salvage:
        candidate = strip_fences(visible)
        if candidate.startswith("{") and len(candidate) < 20000:
            call = _bare_call_envelope(candidate, tools_by_name)
            if call:
                calls.append(call)
                visible = ""

    # Salvage: prose, and *then* a bare JSON call object on its own line.
    if not calls and salvage and len(visible) < 200000:
        kept: List[str] = []
        rest = visible
        while True:
            found = find_bare_call(rest, tools_by_name)
            if not found:
                break
            start, end, call = found
            kept.append(rest[:start])
            calls.append(call)
            rest = rest[end:]
        if calls:
            visible = _clean_trailing_fence("".join(kept)) + rest

    return visible.strip(), calls


# ======================================================================================
# Streaming parser
# ======================================================================================


class StreamToolParser:
    """Incremental splitter: text deltas out, complete ToolCalls out.

    Holds back any tail that might be the start of a sentinel so a tag split across
    SSE chunk boundaries is never leaked to the client as visible text.
    """

    def __init__(self, tools_by_name: Dict[str, ToolDef], salvage: bool = True) -> None:
        self.tools_by_name = tools_by_name
        self.salvage = salvage
        self.buf = ""
        self.in_call = False
        self.call_tag = ""
        self.call_attrs: Dict[str, str] = {}
        self.call_buf = ""
        self.calls: List[ToolCall] = []
        self.text_emitted = ""
        # True when the buffer ends in a pipe kept only because a sentinel
        # closed there; it is vendor residue, not content, if nothing follows.
        self.sentinel_tail = False
        self.saw_any_call = False
        self.discard_rest = False
        # Whether index 0 of `buf` is the start of a line. Emitted text is gone from
        # the buffer, so this is the only way to tell a line-leading '{' from one in
        # the middle of a sentence or nested inside another object.
        self.at_line_start = True
        self.arg_prefixes = argument_key_prefixes(tools_by_name)

    @staticmethod
    def _holdback_len(buf: str) -> int:
        """How many trailing chars must be withheld as a possible sentinel.

        Two cases must be covered, and both can straddle an SSE chunk boundary:
          1. a partial sentinel            -> '<too'
          2. a complete sentinel whose attribute list is still open, which can be
             far longer than the sentinel  -> '<tool_call name="Wri'
        """
        return (StreamToolParser._tag_holdback_len(buf)
                or StreamToolParser._bare_holdback_len(buf))

    @staticmethod
    def _tag_holdback_len(buf: str) -> int:
        """Holdback for an unterminated '<...' tag only."""
        n = len(buf)
        lt = buf.rfind("<")
        if lt < 0 or ">" in buf[lt:] or (n - lt) > 8192:
            return 0
        tail = buf[lt:].lower()
        for sent in STREAM_SENTINELS:
            if sent.startswith(tail) or tail.startswith(sent):
                return n - lt
        # A pipe-led vendor tag ('<｜tool▁calls▁begin｜>') matches no literal
        # sentinel, so compare on the name with separators and pipes removed.
        core = tail[1:]
        if core.startswith("/"):
            core = core[1:]
        core = core.lstrip("\uff5c| \t")
        key = re.sub(r"[^a-z0-9:]", "", core)
        if len(key) == len(re.sub(r"[\u2581_\-\s\uff5c|]", "", core)):
            if any(name.startswith(key) for name in _TAG_NORMALIZED):
                return n - lt
        return 0

    @staticmethod
    def _bare_holdback_len(buf: str) -> int:
        """Holdback for a bracket-less vendor sentinel or an open code fence."""
        n = len(buf)
        # A bare sentinel has no angle brackets ('｜tool▁calls▁begin｜'), so the '<'
        # rule above never sees it. Hold a short pipe-led tail until it can be
        # classified; worst case it is flushed by the next chunk or by finish().
        bar = max(buf.rfind("\uff5c"), buf.rfind("|"))
        if bar >= 0 and (n - bar) <= 48 and _maybe_bare_sentinel(buf[bar:]):
            return n - bar
        m = _FENCE_TAIL_RE.search(buf)
        if m and m.end() == n:
            return n - m.start()
        return 0

    def _emit(self, out: List[str], text: str) -> None:
        """Forward visible text and remember whether a line is left open."""
        if not text:
            return
        out.append(text)
        self.text_emitted += text
        self.at_line_start = text.endswith(("\n", "\r"))

    def _json_holdback_len(self, buf: str) -> int:
        """Holdback for an unfinished bare JSON call object.

        A call written as plain JSON after a sentence has no closing sentinel, so
        the only safe stop point is its matching brace. Text that cannot still grow
        into a call envelope is released immediately, and a closed object is handled
        by the main loop, so this only ever withholds an open candidate.
        """
        if len(buf) > 200000:
            return 0
        stop = len(buf)
        for pattern in (_OPEN_RE, _RESULT_OPEN_RE):
            hit = pattern.search(buf)
            if hit:
                stop = min(stop, hit.start())
        buf = buf[:stop]
        for start in bare_call_starts(buf, self.at_line_start, self.arg_prefixes):
            if json_object_end(buf, start) < 0:
                return len(buf) - _fence_backoff(buf, start)
        return 0

    def feed(self, delta: str) -> List[str]:
        """Consume a chunk; return visible text pieces to forward to the client."""
        if not delta or self.discard_rest:
            return []
        out: List[str] = []
        self.buf += delta
        # A pipe kept from a closed sentinel is residue as soon as what follows
        # cannot continue a sentinel name; dropping it here covers every branch
        # below, including the one that splits on an opening tool-call tag.
        if self.sentinel_tail and self.buf[:1] in ("\uff5c", "|"):
            if len(self.buf) > 1 and not _maybe_bare_sentinel(self.buf):
                self.buf = self.buf[1:]
                self.sentinel_tail = False
        # Do not leak a bare JSON call as text before salvaging it at EOF.
        if (self.salvage and not self.text_emitted and not self.calls and not self.in_call
                and strip_fences(self.buf).lstrip().startswith("{") and len(self.buf) < 20000):
            return []

        while True:
            if self.in_call:
                c_start, c_end = _find_close(self.buf, self.call_tag, 0)
                if c_start < 0:
                    self.call_buf = self.buf
                    break
                body = self.buf[:c_start]
                call = parse_call_body(body, self.call_attrs, self.tools_by_name)
                if call:
                    self.calls.append(call)
                    self.saw_any_call = True
                self.in_call = False
                self.call_buf = ""
                self.buf = self.buf[c_end:]
                self.at_line_start = True
                continue

            m = _OPEN_RE.search(self.buf)
            result = re.search(r"<" + _VENDOR + r"tool_result\b[^>]*>", self.buf, re.IGNORECASE)

            # A wrapped call, or a hallucinated result, always wins over a bare JSON
            # object: the object may simply be this call's own argument block.
            limit = len(self.buf)
            if m:
                limit = min(limit, m.start())
            if result:
                limit = min(limit, result.start())
            if self.salvage and limit and limit < 200000:
                found = find_bare_call(self.buf[:limit], self.tools_by_name,
                                       self.at_line_start, self.arg_prefixes)
                if found:
                    start, end, call = found
                    head = strip_vendor_markup(self.buf[:start])
                    if not self.saw_any_call and not self.calls:
                        head = _clean_trailing_fence(head)
                    self._emit(out, strip_hallucinated_results(head))
                    self.calls.append(call)
                    self.saw_any_call = True
                    self.buf = self.buf[end:]
                    self.at_line_start = True
                    continue
            if result and (not m or result.start() < m.start()):
                self._emit(out, strip_vendor_markup(self.buf[:result.start()]))
                self.buf = ""
                self.discard_rest = True
                break
            if m:
                head = strip_vendor_markup(self.buf[: m.start()])
                if self.saw_any_call is False and not self.calls:
                    head = _clean_trailing_fence(head)
                self._emit(out, strip_hallucinated_results(head))
                self.at_line_start = True
                self.call_tag = m.group(1)
                self.call_attrs = _parse_attrs(m.group(2))
                if m.group(0).rstrip().endswith("/>"):
                    call = parse_call_body("", self.call_attrs, self.tools_by_name)
                    if call:
                        self.calls.append(call)
                        self.saw_any_call = True
                    self.buf = self.buf[m.end() :]
                    continue
                self.in_call = True
                self.buf = self.buf[m.end() :]
                continue

            # An unterminated '<' may still grow into a real tag, so hold it
            # untouched. Once no tag is pending, delete complete vendor markers
            # *before* choosing the hold point: a bare sentinel's closing pipe also
            # looks like the start of the next sentinel, and holding it back would
            # emit the opening half and leak the marker in two pieces.
            hold = self._tag_holdback_len(self.buf)
            if not hold:
                head, pending, residue = split_vendor_markup(self.buf)
                self.sentinel_tail = residue
                self.buf = head + pending
                hold = len(pending) or self._bare_holdback_len(head)
            if self.salvage:
                hold = max(hold, self._json_holdback_len(self.buf))
            emit = self.buf[: len(self.buf) - hold] if hold else self.buf
            self.buf = self.buf[len(self.buf) - hold :] if hold else ""
            if emit:
                self._emit(out, strip_hallucinated_results(strip_vendor_markup(emit)))
            break

        return out

    def finish(self) -> Tuple[List[str], List[ToolCall]]:
        """Flush. Handles truncation by stop-sequence (missing close tag)."""
        out: List[str] = []
        if self.in_call:
            body = self.buf
            call = parse_call_body(body, self.call_attrs, self.tools_by_name)
            if call:
                self.calls.append(call)
                self.saw_any_call = True
            self.buf = ""
            self.in_call = False
        elif self.buf:
            if self.salvage:
                visible, salvaged = extract_tool_calls(self.buf, self.tools_by_name, salvage=True)
                if salvaged:
                    self.calls.extend(salvaged)
                    self.saw_any_call = True
                    self.buf = ""
                    self._emit(out, visible)
                    return out, self.calls
            if self.sentinel_tail and self.buf.strip("\uff5c| \t\r\n") == "":
                self.buf = ""
            tail = strip_hallucinated_results(strip_vendor_markup(self.buf))
            if self.calls:
                tail = _clean_trailing_fence(tail)
            if tail.strip():
                self._emit(out, tail)
            self.buf = ""

        if not self.calls and self.salvage:
            whole = self.text_emitted.strip()
            cand = strip_fences(whole)
            if cand.startswith("{"):
                _txt, salvaged = extract_tool_calls(whole, self.tools_by_name, salvage=True)
                if salvaged:
                    self.calls.extend(salvaged)
                    return out, self.calls
        return out, self.calls


# --- generated header: build_single_file.py strips these blocks ---
__all__ = [
    "_PROTOCOL_HEADER",
    "_ONE_CALL_RULE",
    "_PARALLEL_RULE",
    "_schema_summary",
    "render_tools_block",
    "render_tool_signatures",
    "build_tool_prompt",
    "render_tool_call_text",
    "render_tool_result_text",
    "_FENCE_RE",
    "strip_fences",
    "_walk_strings",
    "_escape_control_chars_in_strings",
    "_strip_trailing_commas",
    "_PY_LITERALS",
    "_fix_python_literals",
    "_balance_braces",
    "_extract_first_object",
    "loads_tolerant",
    "OPEN_TAG_NAMES",
    "_TAGS_ALT",
    "_OPEN_RE",
    "_VENDOR",
    "_ATTR_RE",
    "_ARG_RE",
    "_RESULT_BLOCK_RE",
    "_ORPHAN_RESULT_RE",
    "_DSML_RE",
    "_SENTINEL_SEP",
    "_sentinel_pattern",
    "_WRAPPER_NAMES",
    "_PIPES",
    "_VENDOR_TAG_RE",
    "_BARE_SENTINEL_NAMES",
    "_BARE_SENTINEL_RE",
    "_TAG_NORMALIZED",
    "_BARE_NORMALIZED",
    "_maybe_bare_sentinel",
    "strip_vendor_markup",
    "looks_like_botched_call",
    "_ENDS_WITH_SENTINEL_RE",
    "split_vendor_markup",
    "_WRAPPER_RE",
    "normalize_dialects",
    "STREAM_SENTINELS",
    "_MAX_SENTINEL",
    "_FENCE_TAIL_RE",
    "_parse_attrs",
    "_find_close",
    "_coerce_scalar",
    "coerce_args",
    "validate_args",
    "_ENVELOPE_NAME_KEYS",
    "_ENVELOPE_ARG_KEYS",
    "json_object_end",
    "find_bare_call",
    "bare_call_starts",
    "argument_key_prefixes",
    "args_only_tool",
    "unwrap_call_envelope",
    "repair_args",
    "render_tool_example",
    "_build_call",
    "parse_call_body",
    "strip_hallucinated_results",
    "_clean_trailing_fence",
    "extract_tool_calls",
    "StreamToolParser",
]
# --- end generated header ---
