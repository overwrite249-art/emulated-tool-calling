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
    # Bare vendor wrappers still require an explicitly named call in their body.
    "｜｜dsml｜｜",
    "||dsml||",
    "｜dsml｜",
    "|dsml|",
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


_NESTED_CLOSE_RE = re.compile(r"</tool_call\s*>", re.IGNORECASE)
STREAM_SENTINELS.append("</tool_call")


def _anonymous_call_wrapper(tag: str, attrs: Dict[str, str], tail: str) -> bool:
    # A JSON body is an actual call, never a container. Only a child opener
    # immediately inside an anonymous canonical tag is safe to unwrap.
    return tag.lower() == "tool_call" and not attrs and bool(_OPEN_RE.match(tail.lstrip()))


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
        parsed, _ = loads_tolerant(candidate)
        if isinstance(parsed, dict):
            nm = next((parsed[k] for k in ("name", "tool", "tool_name", "function_name")
                       if isinstance(parsed.get(k), str)), "")
            if nm in tools_by_name and any(k in parsed for k in ("arguments", "input", "parameters", "args")):
                call = parse_call_body(candidate, {}, tools_by_name)
                if call:
                    return "", [call]

    calls: List[ToolCall] = []
    out_parts: List[str] = []
    pos = 0
    wrapper_depth = 0

    while True:
        m = _OPEN_RE.search(text, pos)
        result = re.search(r"<" + _VENDOR + r"tool_result\b[^>]*>", text[pos:], re.IGNORECASE)
        closing = _NESTED_CLOSE_RE.search(text, pos) if wrapper_depth else None
        boundary = min(m.start() if m else len(text), closing.start() if closing else len(text))
        if result and pos + result.start() < boundary:
            # Everything after a fabricated result depends on a tool that never ran.
            out_parts.append(text[pos:pos + result.start()])
            break
        if closing and (not m or closing.start() < m.start()):
            out_parts.append(text[pos:closing.start()])
            pos = closing.end()
            wrapper_depth -= 1
            continue
        if not m:
            out_parts.append(text[pos:])
            break
        tag = m.group(1)
        attrs = _parse_attrs(m.group(2))
        if (wrapper_depth < 16 and not m.group(0).rstrip().endswith("/>")
                and _anonymous_call_wrapper(tag, attrs, text[m.end():])):
            out_parts.append(text[pos:m.start()])
            pos = m.end()
            wrapper_depth += 1
            continue
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

    visible = _WRAPPER_RE.sub("", "".join(out_parts))
    visible = strip_hallucinated_results(visible)
    if calls:
        visible = _clean_trailing_fence(visible)

    # Salvage: a bare JSON call object with no tags at all.
    if not calls and salvage:
        candidate = strip_fences(visible)
        if candidate.startswith("{") and len(candidate) < 20000:
            parsed, _ = loads_tolerant(candidate)
            if isinstance(parsed, dict):
                nm = ""
                for key in ("name", "tool", "tool_name", "function_name"):
                    if isinstance(parsed.get(key), str):
                        nm = parsed[key]
                        break
                has_args = any(
                    k in parsed for k in ("arguments", "input", "parameters", "args")
                )
                if nm and has_args and nm in tools_by_name:
                    call = parse_call_body(candidate, {}, tools_by_name)
                    if call:
                        calls.append(call)
                        visible = ""

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
        self.saw_any_call = False
        self.discard_rest = False
        self.wrapper_depth = 0

    @staticmethod
    def _holdback_len(buf: str) -> int:
        """How many trailing chars must be withheld as a possible sentinel.

        Two cases must be covered, and both can straddle an SSE chunk boundary:
          1. a partial sentinel            -> '<too'
          2. a complete sentinel whose attribute list is still open, which can be
             far longer than the sentinel  -> '<tool_call name="Wri'
        """
        n = len(buf)
        lt = buf.rfind("<")
        if lt >= 0 and ">" not in buf[lt:] and (n - lt) <= 8192:
            tail = buf[lt:].lower()
            for sent in STREAM_SENTINELS:
                if sent.startswith(tail) or tail.startswith(sent):
                    return n - lt
        m = _FENCE_TAIL_RE.search(buf)
        if m and m.end() == n:
            return n - m.start()
        return 0

    def feed(self, delta: str) -> List[str]:
        """Consume a chunk; return visible text pieces to forward to the client."""
        if not delta or self.discard_rest:
            return []
        out: List[str] = []
        self.buf += delta
        # Do not leak a bare JSON call as text before salvaging it at EOF.
        if (self.salvage and not self.text_emitted and not self.calls and not self.in_call
                and strip_fences(self.buf).lstrip().startswith("{") and len(self.buf) < 20000):
            return []

        while True:
            if self.in_call:
                if (self.wrapper_depth < 16
                        and _anonymous_call_wrapper(self.call_tag, self.call_attrs, self.buf)):
                    self.in_call = False
                    self.call_buf = ""
                    self.wrapper_depth += 1
                    continue
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
                continue

            m = _OPEN_RE.search(self.buf)
            result = re.search(r"<" + _VENDOR + r"tool_result\b[^>]*>", self.buf, re.IGNORECASE)
            closing = _NESTED_CLOSE_RE.search(self.buf) if self.wrapper_depth else None
            boundary = min(m.start() if m else len(self.buf), closing.start() if closing else len(self.buf))
            if result and result.start() < boundary:
                head = _WRAPPER_RE.sub("", self.buf[:result.start()])
                if head:
                    out.append(head)
                    self.text_emitted += head
                self.buf = ""
                self.discard_rest = True
                break
            if closing and (not m or closing.start() < m.start()):
                head = _WRAPPER_RE.sub("", self.buf[:closing.start()])
                if head:
                    out.append(head)
                    self.text_emitted += head
                self.buf = self.buf[closing.end():]
                self.wrapper_depth -= 1
                continue
            if m:
                head = _WRAPPER_RE.sub("", self.buf[: m.start()])
                if self.saw_any_call is False and not self.calls:
                    head = _clean_trailing_fence(head)
                head = strip_hallucinated_results(head)
                if head:
                    out.append(head)
                    self.text_emitted += head
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

            hold = self._holdback_len(self.buf)
            emit = self.buf[: len(self.buf) - hold] if hold else self.buf
            self.buf = self.buf[len(self.buf) - hold :] if hold else ""
            if emit:
                emit = strip_hallucinated_results(_WRAPPER_RE.sub("", emit))
                if emit:
                    out.append(emit)
                    self.text_emitted += emit
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
            if self.salvage and not self.text_emitted and not self.calls:
                visible, salvaged = extract_tool_calls(self.buf, self.tools_by_name, salvage=True)
                if salvaged:
                    self.calls.extend(salvaged)
                    self.buf = ""
                    if visible:
                        out.append(visible)
                        self.text_emitted += visible
                    return out, self.calls
            tail = strip_hallucinated_results(_WRAPPER_RE.sub("", self.buf))
            if self.calls:
                tail = _clean_trailing_fence(tail)
            if tail.strip():
                out.append(tail)
                self.text_emitted += tail
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
    "_build_call",
    "parse_call_body",
    "strip_hallucinated_results",
    "_clean_trailing_fence",
    "extract_tool_calls",
    "StreamToolParser",
]
# --- end generated header ---
