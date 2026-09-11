#!/usr/bin/env python3


# -*- coding: utf-8 -*-


"""emutools.py - Emulated tool-calling proxy.

Speaks BOTH wire protocols that coding agents use:

  * Anthropic Messages API   POST /v1/messages            (Claude Code CLI)
                             POST /v1/messages/count_tokens
  * OpenAI Chat Completions  POST /v1/chat/completions     (opencode, aider, cline, ...)
                             GET  /v1/models

...and translates them onto ANY OpenAI-compatible upstream (DeepSeek by default)
WITHOUT using the upstream's native tool-calling support.

Tool calls are *emulated*: tool schemas are rendered into the prompt, the model
emits a text block, and this proxy parses that block back into real, native
`tool_use` / `tool_calls` structures so the client never knows the difference.

Zero dependencies. Python 3.9+. Single file.

    export EMU_UPSTREAM_API_KEY=sk-...
    python3 emutools.py

    # Claude Code
    ANTHROPIC_BASE_URL=http://127.0.0.1:8787 ANTHROPIC_AUTH_TOKEN=dummy claude

    # opencode / any OpenAI client
    OPENAI_BASE_URL=http://127.0.0.1:8787/v1 OPENAI_API_KEY=dummy opencode

    # run the full test suite (no network needed)
    python3 emutools.py --selftest"""


from __future__ import annotations

import argparse
import codecs
import difflib
import math
import http.client
import hashlib
import json
import os
import random
import re
import socket
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Iterator, List, Optional, Tuple

__version__ = "1.0.0"


# ======================================================================================
# Config
# ======================================================================================


def _env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return default if v is None else v


def _env_int(name: str, default: int) -> int:
    try:
        raw = os.environ.get(name)
        if raw is None or raw.strip() == "":
            return default
        return int(raw)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        raw = os.environ.get(name)
        if raw is None or raw.strip() == "":
            return default
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "y")


@dataclass
class Config:
    host: str = field(default_factory=lambda: _env("EMU_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: _env_int("EMU_PORT", 8787))

    upstream_base: str = field(
        default_factory=lambda: _env("EMU_UPSTREAM_BASE_URL", "https://api.deepseek.com").rstrip("/")
    )
    upstream_key: str = field(
        default_factory=lambda: _env("EMU_UPSTREAM_API_KEY", _env("DEEPSEEK_API_KEY", ""))
    )
    upstream_path: str = field(default_factory=lambda: _env("EMU_UPSTREAM_PATH", "/chat/completions"))

    # Model routing. Clients send e.g. "claude-sonnet-4-5"; we map onto upstream ids.
    model_big: str = field(default_factory=lambda: _env("EMU_MODEL_BIG", "deepseek-v4-pro"))
    model_small: str = field(default_factory=lambda: _env("EMU_MODEL_SMALL", "deepseek-flash"))
    model_map_raw: str = field(default_factory=lambda: _env("EMU_MODEL_MAP", ""))

    # Loop / runaway protection
    max_tool_rounds: int = field(default_factory=lambda: _env_int("EMU_MAX_TOOL_ROUNDS", 25))
    max_repeat: int = field(default_factory=lambda: _env_int("EMU_MAX_REPEAT", 3))
    max_calls_per_turn: int = field(default_factory=lambda: _env_int("EMU_MAX_CALLS_PER_TURN", 4))
    loop_retry: bool = field(default_factory=lambda: _env_bool("EMU_LOOP_RETRY", True))

    # Emulation behaviour
    parallel: bool = field(default_factory=lambda: _env_bool("EMU_PARALLEL", False))
    use_stop: bool = field(default_factory=lambda: _env_bool("EMU_USE_STOP", False))
    merge_roles: bool = field(default_factory=lambda: _env_bool("EMU_MERGE_ROLES", True))
    salvage_bare_json: bool = field(default_factory=lambda: _env_bool("EMU_SALVAGE", True))
    max_result_chars: int = field(default_factory=lambda: _env_int("EMU_MAX_RESULT_CHARS", 24000))

    # Inbound HTTP resource limits (the server is intended for loopback use).
    max_request_bytes: int = field(default_factory=lambda: _env_int("EMU_MAX_REQUEST_BYTES", 16 * 1024 * 1024))
    client_timeout: float = field(default_factory=lambda: _env_float("EMU_CLIENT_TIMEOUT", 30.0))

    # Upstream transport
    timeout: float = field(default_factory=lambda: _env_float("EMU_TIMEOUT", 300.0))
    connect_retries: int = field(default_factory=lambda: _env_int("EMU_MAX_RETRIES", 3))

    log_level: str = field(default_factory=lambda: _env("EMU_LOG", "info").lower())
    log_bodies: bool = field(default_factory=lambda: _env_bool("EMU_LOG_BODIES", False))

    def model_map(self) -> Dict[str, str]:
        if not self.model_map_raw.strip():
            return {}
        raw = self.model_map_raw.strip()
        if not raw.startswith("{"):
            mapping: Dict[str, str] = {}
            for item in raw.split(","):
                key, sep, value = item.partition("=")
                if not sep or not key.strip() or not value.strip():
                    log_warn("EMU_MODEL_MAP must be JSON or comma-separated from=to pairs; ignoring")
                    return {}
                mapping[key.strip()] = value.strip()
            return mapping
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                return {k: v for k, v in data.items() if k and isinstance(v, str) and v}
        except (ValueError, TypeError):
            log_warn("EMU_MODEL_MAP is not valid JSON; ignoring")
        return {}

    def resolve_model(self, requested: str) -> str:
        requested = (requested or "").strip()
        mapping = self.model_map()
        if requested in mapping:
            return mapping[requested]
        low = requested.lower()
        for key, val in mapping.items():
            if key.lower() in low:
                return val
        # Already an upstream id? pass through untouched.
        if low.startswith("deepseek") or low.startswith("qwen") or low.startswith("glm"):
            return requested
        # Small/fast tier used by Claude Code for titles & cheap classification.
        if any(tok in low for tok in ("haiku", "mini", "small", "flash", "fast", "lite")):
            return self.model_small
        return self.model_big


CFG = Config()

_LEVELS = {"debug": 10, "info": 20, "warn": 30, "error": 40, "silent": 100}
_LOG_LOCK = threading.Lock()


def _log(level: str, msg: str) -> None:
    if _LEVELS.get(level, 20) < _LEVELS.get(CFG.log_level, 20):
        return
    line = "%s [%s] %s" % (time.strftime("%H:%M:%S"), level.upper()[:4], msg)
    with _LOG_LOCK:
        sys.stderr.write(line + "\n")
        sys.stderr.flush()


def log_debug(m: str) -> None:
    _log("debug", m)


def log_info(m: str) -> None:
    _log("info", m)


def log_warn(m: str) -> None:
    _log("warn", m)


def log_error(m: str) -> None:
    _log("error", m)


# ======================================================================================
# Small utilities
# ======================================================================================

_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"


def _rand_id(n: int = 24) -> str:
    return "".join(random.choice(_ID_ALPHABET) for _ in range(n))


def new_tool_use_id() -> str:
    """Anthropic-shaped tool_use id."""
    return "toolu_" + _rand_id(24)


def new_openai_call_id() -> str:
    return "call_" + _rand_id(24)


def new_message_id(prefix: str = "msg") -> str:
    return prefix + "_" + _rand_id(24)


def canon_json(obj: Any) -> str:
    try:
        return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        return repr(obj)


def fingerprint(name: str, args: Any) -> str:
    raw = (name or "") + "\x00" + canon_json(args)
    return hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:16]


def estimate_tokens(text: str) -> int:
    """Cheap but stable token estimate (~4 chars/token, min 1 per non-empty)."""
    if not text:
        return 0
    return max(1, int(len(text) / 3.7))


def truncate_middle(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head - 40
    if tail < 0:
        tail = 0
    omitted = len(text) - head - tail
    return text[:head] + ("\n... [%d characters omitted] ...\n" % omitted) + (text[-tail:] if tail else "")


def safe_str(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (dict, list)):
        try:
            return json.dumps(v, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(v)
    return str(v)


# ======================================================================================
# Canonical types
# ======================================================================================


@dataclass
class ToolDef:
    name: str
    description: str = ""
    schema: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolCall:
    name: str
    args: Dict[str, Any]
    id: str = ""
    raw: str = ""
    repaired: bool = False

    def fp(self) -> str:
        return fingerprint(self.name, self.args)


@dataclass
class CanonMessage:
    """Protocol-neutral message."""

    role: str  # system | user | assistant
    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    # tool results attached to a user turn: (tool_use_id, name, content, is_error)
    tool_results: List[Tuple[str, str, str, bool]] = field(default_factory=list)


@dataclass
class CanonRequest:
    model: str
    messages: List[CanonMessage]
    system: str = ""
    tools: List[ToolDef] = field(default_factory=list)
    tool_choice: str = "auto"  # auto | required | none | <tool name>
    max_tokens: int = 4096
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    stop: List[str] = field(default_factory=list)
    stream: bool = False
    parallel_tool_calls: Optional[bool] = None
    protocol: str = "anthropic"  # anthropic | openai


# ======================================================================================
# Prompt construction (this is what replaces native tool calling)
# ======================================================================================

CALL_OPEN = "<tool_call>"
CALL_CLOSE = "</tool_call>"
RESULT_OPEN = "<tool_result>"
RESULT_CLOSE = "</tool_result>"


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
    except (OSError, http.client.HTTPException) as exc:
        raise UpstreamError("upstream response interrupted: %s" % exc, 502) from exc
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
    """Decode UTF-8 incrementally and dispatch complete SSE events, not lines.

    HTTPResponse.read(n) waits for n bytes; read1(n) returns currently available
    bytes, so a short first token is not held until a kilobyte or EOF arrives.
    """
    decoder = codecs.getincrementaldecoder("utf-8-sig")("strict")
    read = getattr(resp, "read1", None) or resp.read
    buf = ""
    fields: List[str] = []
    eof = False
    while not eof:
        raw = read(4096)
        eof = not raw
        try:
            buf += decoder.decode(raw, final=eof)
        except UnicodeError as exc:
            raise UpstreamError("upstream SSE is not valid UTF-8", 502) from exc
        while True:
            # Hold a trailing CR until the next read: it may be half of CRLF.
            newline = re.search(r"\r\n|\r(?=.)|\n", buf, re.DOTALL)
            if newline:
                line, buf = buf[:newline.start()], buf[newline.end():]
            elif eof and buf:
                line, buf = buf.rstrip("\r"), ""
            elif eof and fields:
                line = ""  # accept a final event lacking its blank terminator
            else:
                break
            if line == "":
                if not fields:
                    continue
                data = "\n".join(fields)
                fields = []
                if data.strip() == "[DONE]":
                    return
                if not data.strip():
                    continue
                try:
                    obj = json.loads(data)
                except ValueError as exc:
                    raise UpstreamError("upstream sent invalid JSON in an SSE event", 502) from exc
                if not isinstance(obj, dict):
                    raise UpstreamError("upstream SSE payload must be an object", 502)
                yield obj
            elif not line.startswith(":"):
                name, _, value = line.partition(":")
                if name == "data":
                    fields.append(value[1:] if value.startswith(" ") else value)


def upstream_stream(cfg: Config, payload: Dict[str, Any]) -> Iterator[Dict[str, Any]]:
    """Yield {'text':..} / {'reasoning':..} / {'usage':..} / {'finish':..} events."""
    payload = dict(payload)
    payload["stream"] = True
    payload["stream_options"] = {"include_usage": True}
    resp = _request_with_retries(cfg, payload, stream=True)
    saw_finish = False
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
                saw_finish = True
                yield {"finish": fr}
        if not saw_finish:
            raise UpstreamError("upstream stream ended before finish_reason", 502)
    except (OSError, http.client.HTTPException) as exc:
        raise UpstreamError("upstream stream interrupted: %s" % exc, 502) from exc
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
        raise UpstreamError("upstream response has no choices", 502)
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
        elif btype == "tool_result":
            out.append(_blocks_to_text(block.get("content")))
        elif btype == "input_audio" or btype == "audio":
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
        parallel_tool_calls=(not tc["disable_parallel_tool_use"]
                             if isinstance(tc, dict) and "disable_parallel_tool_use" in tc else None),
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
        parallel_tool_calls=body.get("parallel_tool_calls"),
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
        system_chunks.append(build_tool_prompt(req.tools, cfg.parallel and req.parallel_tool_calls is not False))
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
        if req.tools and req.tool_choice == "none":
            extra_system = list(extra_system) + ["Tools are disabled for this turn. Answer directly without tool calls."]
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

    def _stream_out(self, protocol: str, pieces: Any) -> None:
        """Stream a generator, but let it fail with a real status before the headers.

        The generators hold back their first event until the turn is known to be
        usable, so a turn that cannot be salvaged reaches the client as a retryable
        HTTP error rather than as a truncated 200 stream.
        """
        source = iter(pieces)
        try:
            first = next(source)
        except StopIteration:
            first = None
        except UpstreamError as exc:
            log_error("stream refused: %s" % exc.message)
            self._error(protocol, exc.status or 502, exc.message, "api_error")
            return
        if not self._start_stream():
            return
        if first is not None and not self._write_chunk(first):
            return
        for piece in source:
            if not self._write_chunk(piece):
                return
        self._end_chunks()

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
            self._stream_out("anthropic", anthropic_stream_bytes(req, self.cfg))
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
            self._stream_out("openai", openai_stream_bytes(req, self.cfg, include_usage))
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

        # Streaming loop guard: the model gets told the repeat is pointless and is
        # given the chance to break the loop itself before the guard ends the turn.
        mock.script(repeat_call, "I already have that; here is the answer.")
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
        txt = "".join(e[1]["delta"].get("text", "")
                      for e in evs if e[0] == "content_block_delta")
        r.check("streaming repeat is re-asked, not answered with the guard",
                "here is the answer" in txt and "loop guard" not in txt, repr(txt))

        # Three repeats in a row is a real runaway loop, and the guard still stops it.
        mock.script(repeat_call, repeat_call, repeat_call)
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

        # Three empty samples in a row are re-asked, then reported as a retryable
        # error. Answering "(empty response)" with a 200 instead would look to a
        # coding client like a finished task.
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
        r.eq("empty upstream -> retryable status", status, 529)
        r.check("empty upstream -> error body", data.get("type") == "error", body[:200])

        mock.script("", "", "recovered after empty samples")
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
        r.eq("empty upstream retried -> 200", status, 200)
        r.check(
            "empty upstream retried -> real text",
            "recovered after empty samples" in json.dumps(data.get("content") or []),
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
        r.section("14. Real captured output (deepseek-flash, live API)")

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

    # A bare call to a tool the client never registered is still a call. Leaving it
    # as text made it the assistant's answer and ended the client's task; it has to
    # reach the validator so the model is told which tools exist.
    t, cc = extract_tool_calls(
        '{"name":"NotATool","arguments":{"x":1}}', DEMO_BY_NAME, salvage=True
    )
    r.check("unknown bare JSON is recognized as a call",
            len(cc) == 1 and cc[0].name == "NotATool" and not t, repr((t, cc)))

    t, cc = extract_tool_calls(
        "I'll do that now.\n\n{\"name\":\"Read\",\"arguments\":{\"file_path\":\"/a\"}}",
        DEMO_BY_NAME, salvage=True
    )
    r.check("bare JSON after prose salvaged",
            len(cc) == 1 and t == "I'll do that now.", repr((t, cc)))

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


# ======================================================================================
# Entry point
# ======================================================================================


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="emutools",
        description="Emulated tool-calling proxy for Claude Code and OpenAI-compatible clients.",
    )
    parser.add_argument("--host", default=None, help="bind host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="bind port (default 8787)")
    parser.add_argument("--upstream", default=None, help="upstream base URL")
    parser.add_argument("--api-key", default=None, help="upstream API key")
    parser.add_argument("--model-big", default=None, help="model for the main tier")
    parser.add_argument("--model-small", default=None, help="model for the fast tier")
    parser.add_argument("--max-tool-rounds", type=int, default=None)
    parser.add_argument("--max-repeat", type=int, default=None)
    parser.add_argument("--parallel", action="store_true", help="allow multiple calls per reply")
    parser.add_argument("--log", default=None, choices=["debug", "info", "warn", "error", "silent"])
    parser.add_argument("--log-bodies", action="store_true")
    parser.add_argument("--selftest", action="store_true", help="run the offline test suite")
    parser.add_argument("--version", action="version", version="emutools " + __version__)
    args = parser.parse_args(argv)

    if args.selftest:
        CFG.log_level = args.log or "error"
        return run_selftest()

    if args.host:
        CFG.host = args.host
    if args.port:
        CFG.port = args.port
    if args.upstream:
        CFG.upstream_base = args.upstream.rstrip("/")
    if args.api_key:
        CFG.upstream_key = args.api_key
    if args.model_big:
        CFG.model_big = args.model_big
    if args.model_small:
        CFG.model_small = args.model_small
    if args.max_tool_rounds is not None:
        CFG.max_tool_rounds = args.max_tool_rounds
    if args.max_repeat is not None:
        CFG.max_repeat = args.max_repeat
    if args.parallel:
        CFG.parallel = True
    if args.log:
        CFG.log_level = args.log
    if args.log_bodies:
        CFG.log_bodies = True

    serve(CFG)
    return 0


if __name__ == "__main__":
    sys.exit(main())
