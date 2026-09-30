"""Regressions for bugs found in the 2026-09-30 code review.

Run: python3 -m unittest discover -s tests -v
"""
import json
import unittest
from unittest.mock import MagicMock

from emutools.core import Config, ToolDef
from emutools.protocol import StreamToolParser, extract_tool_calls, repair_args, validate_args
from emutools.server import Handler, RequestError, validate_request
from emutools.wire import anthropic_to_canon, openai_to_canon

BASH = ToolDef("Bash", schema={"type": "object", "required": ["command"],
                               "properties": {"command": {"type": "string"}, "n": {}}})
BY_NAME = {"Bash": BASH}


def streamed(text, width):
    parser = StreamToolParser(BY_NAME)
    out = []
    for i in range(0, len(text), width):
        out.extend(parser.feed(text[i:i + width]))
    tail, calls = parser.finish()
    return "".join(out + tail).strip(), [(c.name, c.args) for c in calls]


class ReviewFixes(unittest.TestCase):
    def test_single_pipe_dsml_is_parsed_while_streaming(self):
        text = ('<\uff5cDSML\uff5cfunction_calls><\uff5cDSML\uff5cinvoke name="Bash">'
                '<\uff5cDSML\uff5cparameter name="command">pwd</\uff5cDSML\uff5cparameter>'
                '</\uff5cDSML\uff5cinvoke></\uff5cDSML\uff5cfunction_calls>')
        for width in (1, 3, 7, len(text)):
            self.assertEqual(streamed(text, width), ("", [("Bash", {"command": "pwd"})]), width)

    def test_fenced_bare_call_after_prose_leaves_no_fence(self):
        text = 'text\n```json\n{"name":"Bash","arguments":{"command":"ls"}}\n```'
        visible, calls = extract_tool_calls(text, BY_NAME)
        self.assertEqual(visible, "text")
        self.assertEqual([c.args for c in calls], [{"command": "ls"}])

    def test_non_finite_arguments_are_rejected(self):
        _, calls = extract_tool_calls(
            '<tool_call>{"name":"Bash","arguments":{"command":"ls","n":NaN}}</tool_call>', BY_NAME)
        self.assertTrue(validate_args(calls[0].args, BASH.schema))

    def test_additional_properties_schema_keeps_extra_keys(self):
        schema = {"type": "object", "properties": {"name": {"type": "string"}},
                  "additionalProperties": {"type": "string"}}
        self.assertEqual(repair_args({"name": "a", "FOO": "bar"}, schema)[0], {"name": "a", "FOO": "bar"})

    def test_string_tool_choice_any_means_required(self):
        msgs = [{"role": "user", "content": "hi"}]
        a = anthropic_to_canon({"messages": msgs, "tools": [{"name": "Bash"}], "tool_choice": "any"}, Config())
        o = openai_to_canon({"messages": msgs, "tools": [{"type": "function", "function": {"name": "Bash"}}],
                             "tool_choice": "any"}, Config())
        self.assertEqual((a.tool_choice, o.tool_choice), ("required", "required"))

    def test_openai_explicit_nulls_are_accepted(self):
        body = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "max_tokens": None,
                "stream": None, "parallel_tool_calls": None, "tool_choice": None}
        validate_request(body, "openai")
        with self.assertRaises(RequestError):
            validate_request({"messages": [{"role": "user", "content": "x"}], "max_tokens": "5"}, "openai")

    def test_legacy_function_call_history_is_kept(self):
        req = openai_to_canon({"messages": [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": None,
             "function_call": {"name": "Bash", "arguments": "{\"command\": \"ls\"}"}},
            {"role": "function", "name": "Bash", "content": "a.txt"},
        ]}, Config())
        self.assertEqual([(c.name, c.args) for c in req.messages[1].tool_calls], [("Bash", {"command": "ls"})])
        self.assertEqual(req.messages[2].tool_results[0][1], "Bash")

    def test_model_tier_uses_whole_tokens(self):
        cfg = Config(model_big="big", model_small="small", model_map_raw="")
        self.assertEqual(cfg.resolve_model("gemini-2.5-pro"), "big")
        self.assertEqual(cfg.resolve_model("gpt-4o-mini"), "small")
        self.assertEqual(cfg.resolve_model("claude-3-5-haiku-20241022"), "small")

    def test_stream_exception_after_headers_is_not_a_second_response(self):
        handler = Handler.__new__(Handler)
        handler._start_stream = MagicMock(return_value=True)
        written = []
        handler._write_chunk = lambda data: written.append(data) or True
        handler._end_chunks = MagicMock()
        handler.close_connection = False

        def pieces():
            yield b"data: 1\n\n"
            raise ValueError("boom")

        handler._stream_out("openai", pieces())
        self.assertEqual(written, [b"data: 1\n\n"])
        self.assertTrue(handler.close_connection)
        handler._end_chunks.assert_not_called()


if __name__ == "__main__":
    unittest.main()
