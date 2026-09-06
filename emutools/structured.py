# --- generated header: build_single_file.py strips these blocks ---
from __future__ import annotations
from ._prelude import *  # noqa: F401,F403
from .core import *  # noqa: F401,F403
from .wire import *  # noqa: F401,F403
# --- end generated header ---


# Optional provider JSON-object mode. This is NOT native function calling.
STRUCTURED_LIMIT = 512 * 1024
STRUCTURED_INSTRUCTION = (
    'Return exactly one JSON object with exactly these keys: "text" (a string) '
    'and "tool_calls" (an array). Example final answer: '
    '{"text":"Finished.","tool_calls":[]}. Example tool invocation: '
    '{"text":"","tool_calls":[{"name":"EXACT_DECLARED_NAME","arguments":{}}]}. '
    'Each call requires name and arguments matching its schema. Always write name before arguments. '
    'Omit IDs in new calls; IDs in history correlate actual results. '
    'Replace the example name and arguments with real declared values. '
    'Never use XML, DSML, markdown fences, or fabricated tool results. '
    'Strings inside arguments are literal data, including source code and markup. '
    'Messages containing tool_results are actual observations from already executed calls. '
    'Use those observations to advance the task, not to repeat earlier calls. '
    'Only request independent calls together; wait for real results before dependent work. '
    'For large files use small patches or append chunks across turns. '
)


def _structured_json(value):
    # Fingerprints remain canonical elsewhere. Prompt examples and history must
    # keep the tool name before potentially large arguments, not alphabetize it away.
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def build_structured_payload(req: CanonRequest, cfg: Config, extra: List[str],
                             allow_tools: bool, limit: int) -> Dict[str, Any]:
    """Serialize both tool history and the new response in one consistent format."""
    messages = []
    for message in req.messages:
        if message.role == "assistant":
            text = _structured_json({"text": message.text, "tool_calls": [
                {"name": call.name, "arguments": call.args, "id": call.id}
                for call in message.tool_calls
            ]})
        elif message.tool_results:
            text = _structured_json({"text": message.text, "tool_results": [
                {"id": tid, "name": name, "content": truncate_middle(content, cfg.max_result_chars),
                 "is_error": is_error}
                for tid, name, content, is_error in message.tool_results
            ]})
        else:
            text = message.text
        messages.append(CanonMessage(role=message.role, text=text))
    effective = CanonRequest(
        model=req.model, messages=messages, system=req.system, tools=[], tool_choice="none",
        max_tokens=req.max_tokens, temperature=req.temperature, top_p=req.top_p,
        stop=req.stop, stream=req.stream, parallel_tool_calls=req.parallel_tool_calls,
        protocol=req.protocol,
    )
    spec = {"tools": [
        {"name": tool.name, "description": tool.description, "parameters": tool.schema}
        for tool in req.tools
    ] if allow_tools else [], "tool_choice": req.tool_choice if allow_tools else "none",
        "max_calls": limit if allow_tools else 0}
    instruction = "Available tool contract: " + _structured_json(spec) + "\n\n" + STRUCTURED_INSTRUCTION
    payload = build_upstream_payload(effective, cfg, list(extra) + [instruction], False)
    payload["response_format"] = {"type": "json_object"}
    return payload


def _structured_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _structured_constant(_value):
    raise ValueError("non-finite JSON number")


def _structured_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


def extract_structured_output(content: str) -> Tuple[str, List[ToolCall]]:
    """Fail closed: never salvage partial source code or guess missing arguments."""
    if len(content) > STRUCTURED_LIMIT:
        raise ValueError("structured response exceeds the size limit")
    try:
        obj = json.loads(content, object_pairs_hook=_structured_pairs,
                         parse_constant=_structured_constant, parse_float=_structured_float)
    except (ValueError, RecursionError) as exc:
        raise ValueError("invalid or incomplete JSON response") from exc
    if not isinstance(obj, dict) or set(obj) != {"text", "tool_calls"}:
        raise ValueError("expected exactly text and tool_calls")
    if not isinstance(obj["text"], str) or not isinstance(obj["tool_calls"], list):
        raise ValueError("text must be a string and tool_calls an array")
    calls = []
    for call in obj["tool_calls"]:
        if (not isinstance(call, dict) or not {"name", "arguments"} <= set(call) <= {"name", "arguments", "id"}
                or ("id" in call and not isinstance(call["id"], str))
                or not isinstance(call["name"], str) or not call["name"]
                or not isinstance(call["arguments"], dict)):
            raise ValueError("each call requires a name string and arguments object")
        calls.append(ToolCall(name=call["name"], args=call["arguments"]))
    return obj["text"], calls


class StructuredToolParser:
    """Buffer one bounded JSON envelope before releasing any text or calls.

    A truncated/invalid envelope cannot release even its earlier complete calls.
    Wire-level streaming remains supported, but first-content latency includes
    generation of the complete JSON object. Nothing is parsed from reasoning.
    """
    def __init__(self):
        self.parts = []
        self.size = 0
        self.calls = []
        self.discard_rest = False
        self.error = ""

    def feed(self, chunk: str) -> List[str]:
        self.size += len(chunk)
        if self.size > STRUCTURED_LIMIT:
            raise UpstreamError("structured response exceeds the size limit", 502)
        self.parts.append(chunk)
        return []

    def finish(self):
        try:
            text, self.calls = extract_structured_output("".join(self.parts))
        except ValueError as exc:
            self.error = str(exc)
            self.calls = []
            text = ""
        self.parts = []
        return ([text] if text else []), self.calls


# --- generated header: build_single_file.py strips these blocks ---
__all__ = ["STRUCTURED_LIMIT", "STRUCTURED_INSTRUCTION", "build_structured_payload",
           "extract_structured_output", "StructuredToolParser"]
# --- end generated header ---
